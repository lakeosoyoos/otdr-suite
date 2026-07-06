"""
trc_parser.py — Parse EXFO multi-wavelength .trc files (two-wavelength build).

A .trc file is an "AppReg Format Ex" container holding multiple wavelengths
worth of trace data in one file. The number of wavelengths is auto-detected
from the number of RawSamples blocks. Structure:

  Outer wrapper   : 36-byte header + 1 tiny zlib chunk (version marker)
  Nested "AppReg Format Ex" container begins partway in
  Inside          : 36-byte header + N size-prefixed zlib chunks (~32 KB each)
  Decompressed    : hierarchical key-value stream decoded by exfo_proprietary_decoder

Each wavelength appears as its own set of:
  - RawSamples block (uint16 LE, scaled by 1024)
  - SpansLoss, SpansLength
  - Events (5 per wavelength: launch connector, launch section, near connector,
           main section, end connector + return section + end reflection)
  - NominalWavelength / ExactWavelength / CalibratedPulseWidth / etc.
"""
from __future__ import annotations
import os, sys, struct, zlib
import numpy as np

# Import the bundled decoder that ships beside this file.  The runner already
# puts this folder on sys.path, so a plain import resolves to
# secretsauce/exfo_proprietary_decoder.py.  (Do NOT sys.path.insert a dev-only
# decoder path here — that folder is absent on a tech machine, so the import
# raised ModuleNotFoundError and broke every .trc report in packaged installs.)
from exfo_proprietary_decoder import decode_all_fields


# Event types we care about
EVENT_FIELDS = {'Position', 'Length', 'Type', 'Loss', 'Reflectance',
                'PeakReflectionToRbs'}
# Other per-wavelength metadata
META_FIELDS = {'NominalWavelength', 'ExactWavelength', 'CalibratedPulseWidth',
               'NominalPulseWidth', 'SamplingPeriod', 'ScaleFactor',
               'NumberOfAverages', 'NumberOfPhases', 'InternalFiberLength',
               'InjectionLevel', 'RmsNoise', 'SaturationLevel',
               'SpansLoss', 'SpansLength', 'TotalOrl'}


_MAX_DECOMPRESSED_BYTES = 256 * 1024 * 1024  # 256 MB cap (real traces: a few MB)


def _zlib_decompress_capped(chunk, max_bytes=_MAX_DECOMPRESSED_BYTES):
    """zlib.decompress with a hard OUTPUT ceiling.  A malicious/corrupt block can
    inflate ~1000x (DEFLATE) to multiple GB and OOM the process.  Raises
    zlib.error past the cap; the caller already skips a raised decompress."""
    d = zlib.decompressobj()
    out = d.decompress(chunk, max_bytes)
    if d.unconsumed_tail:
        raise zlib.error("decompressed output exceeds %d-byte cap (possible zlib bomb)"
                         % max_bytes)
    out += d.flush()
    if not d.eof:                # truncated/incomplete stream: raise like the old
        raise zlib.error("incomplete or truncated zlib stream")  # zlib.decompress did,
    return out                   # so callers resync instead of taking partial bytes


def _decompress_trc(path: str) -> bytes:
    """Return the fully decompressed field stream from a .trc file."""
    with open(path, 'rb') as f:
        raw = f.read()
    # Second "AppReg Format Ex" header is the real container
    inner = raw.find(b'AppReg Format Ex', 1)
    if inner < 0:
        # Maybe the file IS a plain embedded block
        inner = 0
    buf = raw[inner:]
    chunks = []
    pos = 36
    while pos < len(buf) - 4:
        sz = struct.unpack_from('<I', buf, pos)[0]
        if sz < 2 or sz > len(buf) - pos - 4:
            break
        try:
            chunks.append(_zlib_decompress_capped(buf[pos + 4:pos + 4 + sz]))
            pos += 4 + sz
        except zlib.error:
            pos += 1
    return b''.join(chunks)


def _group_events(fields: list, n_wavelengths: int) -> list[list[dict]]:
    """Walk fields in offset order, build per-event dicts, then split into
    N wavelength groups based on NominalWavelength markers.

    Returns N lists of event dicts.
    """
    by_off = sorted(fields, key=lambda f: f['offset'])

    # Cut points: take the FIRST offset of each distinct consecutive wavelength
    # value. NominalWavelength appears 2–3 times per wavelength depending on file
    # format, so using a fixed stride is unreliable. Unique-by-value is robust.
    wl_markers = [f for f in by_off
                  if f['name'] == 'NominalWavelength'
                  and f['value'] is not None
                  and not isinstance(f['value'], tuple)]
    cuts = []
    prev_val = None
    for m in wl_markers:
        if m['value'] != prev_val:
            cuts.append(m['offset'])
            prev_val = m['value']
        if len(cuts) >= n_wavelengths:
            break
    if len(cuts) < n_wavelengths:
        return [[] for _ in range(n_wavelengths)]
    cuts = cuts[:n_wavelengths]

    groups = [[] for _ in range(n_wavelengths)]
    current_event = None
    cur_wl_idx = 0

    for f in by_off:
        while cur_wl_idx < n_wavelengths - 1 and f['offset'] > cuts[cur_wl_idx]:
            cur_wl_idx += 1

        if f['name'] == 'Position' and f['value'] is not None and not isinstance(f['value'], tuple):
            if current_event is not None and ('Type' in current_event or 'Loss' in current_event):
                _commit_event(current_event, cuts, groups)
            current_event = {'Position': f['value'], '_offset': f['offset']}
        elif current_event is not None and f['name'] in EVENT_FIELDS:
            v = f['value']
            if v is None or isinstance(v, tuple):
                continue
            current_event[f['name']] = v

    if current_event is not None:
        _commit_event(current_event, cuts, groups)

    return groups


def _commit_event(ev: dict, cuts: list, groups: list) -> None:
    off = ev.pop('_offset')
    idx = 0
    for i, c in enumerate(cuts):
        if off < c:
            idx = i
            break
    else:
        idx = len(cuts) - 1
    groups[idx].append(ev)


def _ordered_by_name(fields: list, name: str) -> list[dict]:
    """All fields with a given name, in stream order, with non-None scalar values."""
    return sorted(
        [f for f in fields if f['name'] == name
         and f['value'] is not None
         and not isinstance(f['value'], tuple)],
        key=lambda f: f['offset'],
    )


def _raw_samples_blocks(fields: list, stream: bytes) -> list[np.ndarray]:
    """Extract the N RawSamples arrays in stream order."""
    out = []
    for f in sorted([f for f in fields if f['name'] == 'RawSamples'],
                    key=lambda f: f['offset']):
        val_off = f['offset'] + len(b'RawSamples\x00')
        size = f['data_size']
        arr = np.frombuffer(stream[val_off:val_off + size], dtype='<u2').astype(np.float64)
        out.append(arr)
    return out


def _estimate_alpha(samples: np.ndarray, sampling_period_s: float,
                    ior: float = 1.4682, exclude_ends_frac: float = 0.1) -> float:
    """Estimate fiber attenuation (dB/km) from raw samples.
    RawSamples convention: loss_dB = 64 - raw/1024.
    Distance per sample: c * sampling_period / (2 * IOR).
    """
    if samples.size < 100:
        return float('nan')
    loss_db = 64.0 - samples / 1024.0
    c = 2.998e8
    dz_m = c * sampling_period_s / (2.0 * ior)
    z_m = np.arange(samples.size) * dz_m
    n = samples.size
    a = int(n * exclude_ends_frac)
    b = int(n * (1.0 - exclude_ends_frac))
    if b - a < 50:
        return float('nan')
    slope, _ = np.polyfit(z_m[a:b], loss_db[a:b], 1)
    return float(slope * 1000.0)  # dB/km (positive = attenuation)


def parse_trc_file(path: str) -> dict:
    """Parse a multi-wavelength .trc file. Number of wavelengths is auto-detected
    from the count of RawSamples blocks in the stream.

    Returns:
      {
        'filename': str,
        'filesize': int,
        'timestamp': int or None,
        'n_wavelengths': int,
        'wavelengths': [  (one entry per wavelength)
            {
              'wavelength_nm': int,
              'length_m': float,
              'span_loss_db': float,
              'sampling_period_s': float,
              'pulse_width_s': float,
              'n_averages': int,
              'samples': np.ndarray,
              'alpha_db_per_km': float,
              'events': [ {position_m, length_m, type, loss_db, refl_db}, ... ],
            }, ...
        ]
      }
    """
    stream = _decompress_trc(path)
    fields = decode_all_fields(stream)

    samples_list = _raw_samples_blocks(fields, stream)
    n_wl = len(samples_list)
    if n_wl == 0:
        raise ValueError(f"No RawSamples blocks found in {path}")

    span_loss_list   = [f['value'] for f in _ordered_by_name(fields, 'SpansLoss')]
    span_length_list = [f['value'] for f in _ordered_by_name(fields, 'SpansLength')]
    # These fields repeat K times per wavelength (2x for 3-wavelength files, 3x
    # for 2-wavelength files). Collapse contiguous runs to get 1 value per λ.
    wl_nom_raw   = [f['value'] * 1e9 for f in _ordered_by_name(fields, 'NominalWavelength')]
    wl_exact_raw = [f['value'] * 1e9 for f in _ordered_by_name(fields, 'ExactWavelength')]
    sample_period_raw = [f['value'] for f in _ordered_by_name(fields, 'SamplingPeriod')]
    pulse_width_raw   = [f['value'] for f in _ordered_by_name(fields, 'CalibratedPulseWidth')]
    n_avg_raw         = [f['value'] for f in _ordered_by_name(fields, 'NumberOfAverages')]

    def first_n(xs, n): return (xs[:n] + [None] * n)[:n]

    def unique_in_order(xs):
        out = []
        for x in xs:
            if not out or out[-1] != x:
                out.append(x)
        return out

    def chunk_last(xs, n):
        """Split xs into n equal-ish chunks; return the last value of each chunk."""
        if not xs or n == 0:
            return [None] * n
        size = len(xs) // n
        if size == 0:
            return first_n(xs, n)
        return [xs[min((i + 1) * size - 1, len(xs) - 1)] for i in range(n)]

    wl_nom_n   = first_n(unique_in_order(wl_nom_raw),   n_wl)
    wl_exact_n = first_n(unique_in_order(wl_exact_raw), n_wl)
    sp_n       = first_n(sample_period_raw, n_wl) if len(sample_period_raw) == n_wl \
                 else chunk_last(sample_period_raw, n_wl)
    pw_n       = chunk_last(pulse_width_raw, n_wl)
    avg_n      = chunk_last(n_avg_raw, n_wl)

    event_groups = _group_events(fields, n_wl)

    try:
        ts = int(os.path.getmtime(path))
    except OSError:
        ts = None

    wavelengths = []
    for i in range(n_wl):
        wl_nm = int(round(wl_nom_n[i])) if wl_nom_n[i] is not None else None
        events = []
        for ev in event_groups[i]:
            events.append({
                'position_m':    ev.get('Position'),
                'length_m':      ev.get('Length'),
                'type':          ev.get('Type'),
                'loss_db':       ev.get('Loss'),
                'refl_db':       ev.get('Reflectance'),
            })
        alpha = _estimate_alpha(samples_list[i], sp_n[i]) if sp_n[i] else float('nan')
        wavelengths.append({
            'wavelength_nm':      wl_nm,
            'exact_wavelength_nm': wl_exact_n[i],
            'length_m':           span_length_list[i] if i < len(span_length_list) else None,
            'span_loss_db':       span_loss_list[i] if i < len(span_loss_list) else None,
            'sampling_period_s':  sp_n[i],
            'pulse_width_s':      pw_n[i],
            'n_averages':         avg_n[i],
            'samples':            samples_list[i],
            'alpha_db_per_km':    alpha,
            'events':             events,
        })

    return {
        'filename':     os.path.basename(path),
        'filepath':     path,
        'filesize':     os.path.getsize(path),
        'timestamp':    ts,
        'n_wavelengths': n_wl,
        'wavelengths':  wavelengths,
    }


if __name__ == '__main__':
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else \
        "/Users/robertcolbert/Desktop/newbeta/TEST0001_155016251310.trc"
    out = parse_trc_file(path)
    print(f"\n{out['filename']}  ({out['filesize']:,} bytes)\n")
    for wl in out['wavelengths']:
        print(f"  Wavelength: {wl['wavelength_nm']} nm "
              f"(exact {wl['exact_wavelength_nm']:.2f})")
        print(f"    length: {wl['length_m']:.2f} m    "
              f"span_loss: {wl['span_loss_db']:.4f} dB    "
              f"alpha: {wl['alpha_db_per_km']:.4f} dB/km")
        print(f"    pulse: {wl['pulse_width_s']*1e9:.0f} ns    "
              f"sampling: {wl['sampling_period_s']*1e12:.1f} ps    "
              f"averages: {wl['n_averages']}")
        print(f"    events: {len(wl['events'])}")
        for j, ev in enumerate(wl['events'], 1):
            def _fmt(v, spec):
                return '---' if v is None else format(v, spec)
            print(f"       #{j}: pos={_fmt(ev['position_m'],'+8.3f')}m  "
                  f"len={_fmt(ev['length_m'],'8.3f')}m  "
                  f"type={ev['type']}  "
                  f"loss={_fmt(ev['loss_db'],'+.4f'):>10}  "
                  f"refl={_fmt(ev['refl_db'],'+.2f'):>8}")
        print()

"""
bdr_reader.py — Parse EXFO FastReporter bidirectional report files (.bdr).

WHY THIS EXISTS
---------------
A `.sor` is ONE direction of ONE fiber.  A `.bdr` is FastReporter's saved
bidirectional analysis: BOTH directions of one fiber in a single file, and
it carries strictly more than the two .sor files would:

  * both full RawSamples traces (the proprietary trace FR itself fits on),
  * both per-direction event lists with complete LSA cursor quintets,
  * FR's own MERGED bidirectional table (MeanPosition / MeanLoss / MeanLength),
  * the silent-side records FR synthesized by transplanting the detecting
    direction's geometry (see project-fr-silent-window) — values we otherwise
    have to reconstruct.

So a crew that hands us .bdr instead of .sor is handing us a SUPERSET.  This
module unpacks one .bdr into the two per-direction dicts the splice engine
already knows how to consume, so the whole pipeline downstream — gates,
grid, Excel, Zach's PDF, the Viewer — runs unchanged.

CONTAINER FORMAT
----------------
A .bdr is the same "AppReg Format Ex" container the .sor proprietary block
uses, just nested one level deeper.  The SECOND container starts at byte 55,
its chunk stream at 55+36 = 91, then `[4B LE size][zlib chunk]` repeating to
EOF.  Concatenate the inflated chunks and the result is one flat field
stream with the familiar descriptor layout:

    [self_offset 4B] [type_code 4B] [data_size 4B] [next_ref 4B] Name\0 [value]

Type codes: 1 = uint32, 2 = binary array, 3 = float64, 4 = UTF-16LE string.

STREAM LAYOUT (verified on 24 ORPVL fibers and 12 SEANOR fibers)
----------------------------------------------------------------
    fiber header      Identifier / Cable / LocationA / LocationB / Date
    RawSamples  #1    TraceAB, followed by its acquisition settings
    RawSamples  #2    TraceBA, followed by its acquisition settings
    directories       Event0..EventN name tables + per-direction META
                      (UserNameA/B, ModelName, SerialNumber)
    record blocks     merged bidi table (MeanPosition records), then the
                      A event list, then the B event list, then one
                      two-record block per merged row (the A-frame and
                      B-frame view of that row)

Record blocks are separated by POSITION RESETS.  Every list runs ascending
from 0 in its OWN frame, so a .bdr event list needs no reframing — it is
already what a .sor of that direction would carry.

DIRECTION BINDING — the one thing not to guess
----------------------------------------------
Binding an event list to the right trace matters more than anything else
here: get it backwards and every silent-side measurement is fitted against
the wrong glass (that exact mistake cost a week on SEANOR — see
project-fr-silent-window's "mis-framing invents phantom silent events").

The binding is not inferred from order.  Each direction's first event record
carries a `CurveLevel` equal to that trace's own `InjectionLevel` — an
independently stored float64 that has matched to <1e-6 on 36/36 fibers
across two unrelated spans.  We match on that and cross-check that both
lists terminate within 50 m of each other (they must: it is one span
measured from both ends).

VALIDATION
----------
Feeding each list's own stored cursors and its bound trace back through
`measure_fr_exact_loss` reproduces FR's own stored Loss on 259 of 278 ORPVL
events at max error 0.000000 mdB (median 0.000000).  The 19 residuals are
all A-direction, median ~3 mdB, and do not affect reported values: the
engine prefers the stored full-precision Loss whenever positions align, the
same as it does for .sor.

Public API
----------
    parse_bdr(filepath)        -> {'a': dict, 'b': dict, 'merged': [...]}
    parse_bdr_side(fp, side)   -> dict  ('a' or 'b')
    is_bdr(filename)           -> bool
"""
from __future__ import annotations

import os
import struct
import zlib
from typing import Any, Optional

import numpy as np

# The .bdr's outer wrapper: second container at byte 55, chunk stream 36
# bytes into it.  Both constants are structural, not heuristic — they hold on
# every .bdr seen from FR3 (SEANOR 2026-06, ORPVL 2026-09).
_CHUNK_STREAM_OFFSET = 55 + 36

# Hard ceiling on the inflated stream.  A corrupt or hostile chunk can
# inflate ~1000x under DEFLATE; the hub parses uploads in-process, so an
# unbounded decompress is an OOM away from taking down every page.  Real
# files inflate to a few hundred KB per fiber.
_MAX_INFLATED_BYTES = 256 * 1024 * 1024

# Per-event record fields we keep.  Same set the .sor proprietary-block
# parser keeps, so `exfo_events` from a .bdr is shape-identical to
# `exfo_events` from a .sor and every downstream consumer is none the wiser.
_RECORD_FIELDS = (
    'Length', 'Loss', 'Type', 'Status', 'CurveLevel', 'Reflectance',
    'PeakReflectionToRbs', 'LocalNoise', 'SplitterRatio',
    'InvalidAttenuation',
    'SubCursorAPosition', 'CursorAPosition',
    'CursorBPosition', 'SubCursorBPosition',
)

# Bellcore group index fallback.  Only used when the file carries no `Ior`
# (never seen in practice — it is stored per direction).
_IOR_FALLBACK = 1.468325

# Bellcore KeyEvents distance constant, kept bit-identical to
# sor_reader324802a so a .bdr event's `time_of_travel` round-trips through
# the same formula the .sor path uses.
_TOT_C = 0.02998

# How far the two directions' measured end-of-fibre may disagree before the
# file is rejected as not-one-span.  Relative, because the disagreement is a
# property of length, not a fixed metre count (see parse_bdr).
_SPAN_END_TOL_FRACTION = 0.005     # 0.5% of span
_SPAN_END_TOL_MIN_M = 100.0        # absolute floor for short spans


# ═══════════════════════════════════════════════════════════════════════
#  Container / field decoding
# ═══════════════════════════════════════════════════════════════════════

def is_bdr(filename: str) -> bool:
    return str(filename).lower().endswith('.bdr')


def _inflate(path: str) -> bytes:
    """Concatenate the .bdr's zlib chunk stream into one flat field stream."""
    with open(path, 'rb') as fh:
        data = fh.read()
    off = _CHUNK_STREAM_OFFSET
    out, total = [], 0
    while off + 4 <= len(data):
        size = struct.unpack_from('<I', data, off)[0]
        off += 4
        if size == 0 or off + size > len(data):
            break
        try:
            chunk = zlib.decompress(data[off:off + size])
        except zlib.error:
            # A tail chunk that is not zlib ends the stream; everything
            # already inflated stays usable.  Partial reads are normal at
            # EOF and are not an error condition.
            break
        total += len(chunk)
        if total > _MAX_INFLATED_BYTES:
            raise ValueError(f"{os.path.basename(path)}: inflated stream "
                             f"exceeds {_MAX_INFLATED_BYTES} bytes")
        out.append(chunk)
        off += size
    return b''.join(out)


def _decode_fields(stream: bytes) -> list[dict]:
    """Walk the flat stream and return every named field, in stream order.

    Values are decoded for the four type codes that carry them; containers
    (type 0) come back with value None and are kept because their NAMES are
    the structure (FiberAB, TraceBA, EventAB, ...).
    """
    fields = []
    p, n = 0, len(stream)
    while p < n - 1:
        end = stream.find(b'\x00', p)
        if end < 0:
            break
        ln = end - p
        if 2 <= ln < 100 and p >= 16:
            try:
                name = stream[p:end].decode('ascii')
            except UnicodeDecodeError:
                name = None
            if name and name.isprintable() and name[0].isalpha():
                tc = struct.unpack_from('<I', stream, p - 12)[0]
                dsz = struct.unpack_from('<I', stream, p - 8)[0]
                voff = end + 1
                val: Any = None
                if tc == 3 and dsz == 8 and voff + 8 <= n:
                    val = struct.unpack_from('<d', stream, voff)[0]
                elif tc == 1 and dsz == 4 and voff + 4 <= n:
                    val = struct.unpack_from('<I', stream, voff)[0]
                elif tc == 4 and 0 < dsz <= 1024 and voff + dsz <= n:
                    val = (stream[voff:voff + dsz]
                           .decode('utf-16-le', errors='replace')
                           .split('\x00')[0])
                fields.append({'offset': p, 'name': name, 'type_code': tc,
                               'data_size': dsz, 'value': val})
        p = end + 1
    return fields


def _raw_traces(stream: bytes) -> list[np.ndarray]:
    """Every RawSamples blob, in stream order (AB first, then BA)."""
    out, off = [], 0
    needle = b'RawSamples\x00'
    while True:
        i = stream.find(needle, off)
        if i < 0:
            break
        if i >= 16:
            tc = struct.unpack_from('<I', stream, i - 12)[0]
            dsz = struct.unpack_from('<I', stream, i - 8)[0]
            voff = i + len(needle)
            if tc == 2 and dsz >= 4 and voff + dsz <= len(stream):
                out.append(np.frombuffer(stream, dtype='<u2',
                                         count=dsz // 2, offset=voff).copy())
        off = i + 1
    return out


def _scalars(fields: list[dict], name: str) -> list:
    return [f['value'] for f in fields
            if f['name'] == name and f['value'] is not None]


def _first(fields: list[dict], name: str, default=None):
    v = _scalars(fields, name)
    return v[0] if v else default


def _nth(fields: list[dict], name: str, i: int, default=None):
    v = _scalars(fields, name)
    return v[i] if len(v) > i else default


# ═══════════════════════════════════════════════════════════════════════
#  Record blocks
# ═══════════════════════════════════════════════════════════════════════

def _record_blocks(fields: list[dict]) -> list[list[dict]]:
    """Group the stream's event/section records and split on position resets.

    A record starts at each `Position` (own-direction lists and the paired
    per-frame views) or `MeanPosition` (FR's merged bidi table) field and
    collects the record fields that follow it.  `setdefault` matters: a
    record's fields appear once each, and a nested sub-object can repeat a
    name before the next Position — first write wins, which is the record's
    own value.
    """
    recs: list[dict] = []
    cur: Optional[dict] = None
    for f in fields:
        name, val = f['name'], f['value']
        if name in ('Position', 'MeanPosition'):
            if cur is not None:
                recs.append(cur)
            cur = {'Position': val, '_off': f['offset'],
                   '_merged': name == 'MeanPosition'}
        elif cur is not None:
            if name in _RECORD_FIELDS:
                cur.setdefault(name, val)
            elif name in ('MeanLength', 'MeanLoss'):
                cur.setdefault(name[4:], val)
    if cur is not None:
        recs.append(cur)

    blocks: list[list[dict]] = []
    block: list[dict] = []
    prev = None
    for r in recs:
        pos = r.get('Position')
        if prev is not None and isinstance(pos, float) and pos < prev - 500.0:
            blocks.append(block)
            block = []
        if isinstance(pos, float):
            prev = pos
        block.append(r)
    if block:
        blocks.append(block)
    return blocks


def _tag_sections(records: list[dict]) -> list[dict]:
    """A record is an EVENT iff the truck wrote a CurveLevel for it; the
    interleaved records without one are fiber SECTIONS (the span between two
    events).  Same rule the .sor proprietary parser uses."""
    for r in records:
        r['_is_section'] = 'CurveLevel' not in r
    return records


def _own_lists(blocks: list[list[dict]],
               injections: list[float]) -> list[list[dict]]:
    """Bind each direction's event list to its trace via CurveLevel.

    See DIRECTION BINDING in the module docstring: the first event's
    CurveLevel IS that trace's InjectionLevel.  Matching on the value rather
    than on block order is what keeps a file with extra record blocks (the
    3- and 5-record oddballs, and the reflectance-only tail) from being
    mistaken for an event list.
    """
    out = []
    for lvl in injections[:2]:
        for b in blocks:
            if not b or b[0].get('_merged') or len(b) < 3:
                continue
            if any(b is o for o in out):
                continue
            head = b[0].get('CurveLevel')
            start = b[0].get('Position')
            last = b[-1].get('Position')
            if (head is not None and abs(head - lvl) < 1e-6
                    and isinstance(start, float) and start < 1.0
                    and isinstance(last, float) and last > 1000.0):
                out.append(b)
                break
    return out


def _exact_pitch(records: list[dict], sampling_period: Optional[float],
                 ior: float) -> Optional[float]:
    """The exact sample pitch in metres.

    Marker Lengths are integer sample multiples, so the population pins the
    pitch far tighter than the IOR-derived estimate — which drifts ~0.3
    permil, i.e. whole samples at 50+ km, and whole samples move a fitted
    loss by millidecibels.
    """
    if not sampling_period or sampling_period <= 0:
        return None
    seed = 299_792_458.0 * float(sampling_period) / 2.0 / ior
    cands = []
    for r in records:
        L = r.get('Length')
        if isinstance(L, float) and 50.0 < L < 3000.0:
            n = round(L / seed)
            if n >= 10:
                cands.append(L / n)
    if len(cands) < 3:
        return seed
    cands.sort()
    return float(cands[len(cands) // 2])


# ═══════════════════════════════════════════════════════════════════════
#  Event normalization  (.bdr records -> the engine's event dicts)
# ═══════════════════════════════════════════════════════════════════════

def _is_finite(x) -> bool:
    return isinstance(x, float) and x == x and abs(x) != float('inf')


def _build_events(records: list[dict], ior: float) -> list[dict]:
    """Turn one direction's record list into the engine's event dicts.

    Field-for-field the same shape `_parse_key_events` produces from a
    Bellcore KeyEvents block, so nothing downstream needs a .bdr branch.
    """
    events = []
    # Section records carry the attenuation of the span that STARTS at their
    # own position, so the section preceding event i is the one recorded at
    # event i-1.  Stash it as we walk, and hand each event the slope of the
    # fiber leading INTO it — the convention parse_otdr_json uses
    # (PreviousFiberSection) and the engine reads.
    pending_slope = 0.0
    number = 0
    for r in records:
        if r.get('_is_section') or 'CurveLevel' not in r:
            L, ln = r.get('Loss'), r.get('Length')
            if _is_finite(L) and _is_finite(ln) and ln > 0:
                pending_slope = L / ln * 1000.0
            continue

        pos_m = r.get('Position')
        if not isinstance(pos_m, float):
            continue
        number += 1

        status = r.get('Status')
        # FR marks the end-of-fibre record with the high bit of Status
        # (128 / 132 observed; the launch connector is 64 / 72).  Proven on
        # the SEANOR set, where KeyEvents independently flags the same
        # records is_end.
        is_end = bool(int(status) & 0x80) if isinstance(status, (int, float)) else False
        refl = r.get('Reflectance')
        # A reflective event is one FR measured a reflectance for.  NaN is
        # how it records "non-reflective", so finiteness is the test — not
        # presence of the key.
        is_reflective = _is_finite(refl)

        loss = r.get('Loss')
        type_str = (('1' if is_reflective else '0')
                    + ('E' if is_end else 'F') + '9999LS')

        def _tot(metres):
            if not isinstance(metres, float):
                return 0
            return int(round(metres * ior / _TOT_C))

        events.append({
            'number':         number,
            'time_of_travel': _tot(pos_m),
            'dist_km':        round(pos_m / 1000.0, 4),
            'splice_loss':    float(loss) if _is_finite(loss) else 0.0,
            'reflection':     float(refl) if is_reflective else 0.0,
            'slope':          pending_slope,
            'type':           type_str,
            'is_reflective':  is_reflective,
            'is_end':         is_end,
            # FR's own LSA cursors, in the same time-of-travel units the .sor
            # KeyEvents markers use.  These ARE the fit windows FastReporter
            # used; carrying them means the engine's FR-exact path needs no
            # window prediction on a .bdr at all.
            'tot_end_prev':   _tot(r.get('SubCursorAPosition')),
            'tot_start_curr': _tot(r.get('CursorAPosition')),
            'tot_end_curr':   _tot(r.get('CursorBPosition')),
            'tot_start_next': _tot(r.get('SubCursorBPosition')),
            'tot_peak_curr':  _tot(r.get('CursorAPosition')),
            # Provenance: this loss came from FR's float64 record, not from a
            # 1 mdB-quantized KeyEvents int16.  Downstream tie-breaks care.
            'loss_full_precision': _is_finite(loss),
            '_bdr_record': r,
        })
        pending_slope = 0.0
    return events


def _date_epoch(date_str: Optional[str]) -> int:
    """'2026-09-08T22:40:00' -> unix epoch.  0 when absent or unparseable —
    the acquisition audit renders 0 as blank rather than inventing a date."""
    if not date_str:
        return 0
    import calendar
    import datetime as _dt
    for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M', '%Y-%m-%d'):
        try:
            return calendar.timegm(_dt.datetime.strptime(date_str, fmt).timetuple())
        except ValueError:
            continue
    return 0


# ═══════════════════════════════════════════════════════════════════════
#  Public API
# ═══════════════════════════════════════════════════════════════════════

def parse_bdr(filepath: str) -> dict:
    """Unpack one .bdr into both directions.

    Returns {'a': <dict>, 'b': <dict>, 'merged': [records]} where each
    direction dict matches what `sor_reader324802a.parse_sor_full(trim=False)`
    returns for a .sor of that direction.  `merged` is FR's own bidirectional
    table, carried through untouched for comparison work — the engine does
    not read it and must not: the whole point of the report is OUR
    bidirectional arithmetic, not FR's.

    Raises ValueError when the file does not hold two traces and two
    bindable event lists — a .bdr that cannot supply both directions is not
    something to half-load, because a half-loaded fiber reads downstream as
    a dead direction.
    """
    stream = _inflate(filepath)
    if not stream:
        raise ValueError(f"{os.path.basename(filepath)}: no readable "
                         f"chunk stream (not a FastReporter .bdr?)")

    fields = _decode_fields(stream)
    traces = _raw_traces(stream)
    if len(traces) < 2:
        raise ValueError(f"{os.path.basename(filepath)}: found "
                         f"{len(traces)} trace(s), need 2 (A and B)")

    injections = [v for v in _scalars(fields, 'InjectionLevel')][:2]
    if len(injections) < 2:
        raise ValueError(f"{os.path.basename(filepath)}: only "
                         f"{len(injections)} InjectionLevel(s) — cannot bind "
                         f"event lists to traces")

    blocks = _record_blocks(fields)
    for b in blocks:
        _tag_sections(b)
    own = _own_lists(blocks, injections)
    if len(own) != 2:
        raise ValueError(f"{os.path.basename(filepath)}: matched {len(own)} "
                         f"event list(s) to traces, need 2")

    # Sanity: the two lists must describe ONE span measured from both ends.
    # They never agree exactly — each direction measures the other's end
    # connector through its own glass and its own IOR — and the disagreement
    # scales with length: 1 m on a 55 km ORPVL span, 51-61 m on a 110 km
    # SEANOR span (0.05%).  So the tolerance is RELATIVE, with an absolute
    # floor for short spans.  A genuinely mis-paired file differs by
    # kilometres, not by a fraction of a percent.
    end_a = own[0][-1].get('Position') or 0.0
    end_b = own[1][-1].get('Position') or 0.0
    tol = max(_SPAN_END_TOL_MIN_M,
              _SPAN_END_TOL_FRACTION * (end_a + end_b) / 2.0)
    if abs(end_a - end_b) > tol:
        raise ValueError(
            f"{os.path.basename(filepath)}: the two event lists end "
            f"{abs(end_a - end_b):.0f} m apart ({end_a:.0f} / {end_b:.0f}, "
            f"tolerance {tol:.0f} m) — they cannot be two directions of "
            f"one span")

    merged = [r for b in blocks for r in b if r.get('_merged')]

    # Shared fiber identity — one fiber, so these are not per-direction.
    identifier = _first(fields, 'Identifier', '') or ''
    cable = _first(fields, 'Cable', '') or ''
    loc_a = _first(fields, 'LocationA', '') or ''
    loc_b = _first(fields, 'LocationB', '') or ''

    sides = {}
    for i, key in enumerate(('a', 'b')):
        recs = own[i]
        raw = traces[i]
        # Per-direction scalars appear once per trace, in trace order.
        ior = _nth(fields, 'Ior', i) or _IOR_FALLBACK
        sampling = _nth(fields, 'SamplingPeriod', i)
        res_m = _exact_pitch(recs, sampling, ior)
        wl_m = _nth(fields, 'Wavelength', i)
        pulse_s = _nth(fields, 'Pulse', i)
        acq_range_m = _nth(fields, 'Range', i) or 0.0

        trace = 64.0 - raw.astype(np.float64) / 1024.0
        events = _build_events(recs, ior)

        # acq_range in the Bellcore sense: time-of-travel of the acquisition
        # window, which is what the engine's sample-spacing arithmetic
        # expects.  Derived from the file's own range so a 80 km / 140 km
        # acquisition is described correctly.
        acq_range_tot = int(round(acq_range_m * ior / _TOT_C))

        result = {
            'filename':  os.path.basename(filepath),
            'filepath':  filepath,
            # Identity.  LocationA/LocationB are stored once, oriented A->B;
            # the B direction is shot the other way, so its originating and
            # terminating locations are the swap — exactly what a real B .sor
            # carries in GenParams, which is what the direction detector and
            # the span-name reader read.
            'gen_cable_id': cable,
            'gen_fiber_id': identifier,
            'gen_loc_a':    loc_a if key == 'a' else loc_b,
            'gen_loc_b':    loc_b if key == 'a' else loc_a,

            'trace':       trace,
            'num_points':  len(trace),
            'full_points': len(trace),
            'start_index': 0,
            'end_index':   len(trace) - 1,
            'min_db':  float(trace.min()),
            'max_db':  float(trace.max()),
            'mean_db': float(trace.mean()),

            'wavelength': (wl_m * 1e9) if wl_m else None,
            'acq_range':  acq_range_tot,
            'events':     events,
            'date_time':  _date_epoch(_nth(fields, 'Date', i + 1)),
            'duration_sec': _nth(fields, 'Duration', i),

            'otdr_model':  _nth(fields, 'ModelName', i, '') or '',
            'otdr_serial': _nth(fields, 'SerialNumber', i, '') or '',
            'sup_params': {
                'sup_supplier':     'EXFO',
                'sup_mainframe_id': _nth(fields, 'ModelName', i, '') or '',
                'sup_mainframe_sn': _nth(fields, 'SerialNumber', i, '') or '',
                'sup_module_id':    _nth(fields, 'ModelName', i, '') or '',
                'sup_module_sn':    _nth(fields, 'SerialNumber', i, '') or '',
                'sup_software_rev': _nth(fields, 'InstrumentVersion', i, '') or '',
                'sup_other':        '',
                'otdr_model':       _nth(fields, 'ModelName', i, '') or '',
                'otdr_serial':      _nth(fields, 'SerialNumber', i, '') or '',
                'pulse_ns':         (pulse_s * 1e9) if pulse_s else None,
            },

            'backscatter_db': _nth(fields, 'Rbs', i),
            'fxd_pulse_ns':   (pulse_s * 1e9) if pulse_s else None,
            'ior':            ior,
            # GenParams glass designation.  A .bdr stores no fiber-type
            # field; leave it blank rather than assume G.652 — the Test
            # Settings table exists to VERIFY the two directions, and an
            # invented value there is worse than an empty cell.
            'fiber_type':     '',

            'test_settings': {
                k: v for k, v in (
                    ('Ior', ior),
                    ('Rbs', _nth(fields, 'Rbs', i)),
                    ('HelixFactor', _nth(fields, 'HelixFactor', i)),
                    ('SpliceLossThreshold', _nth(fields, 'SpliceLossThreshold', i)),
                    ('SplitterDetectionThreshold',
                     _nth(fields, 'SplitterDetectionThreshold', i)),
                    ('ReflectanceThreshold', _nth(fields, 'ReflectanceThreshold', i)),
                    ('EndOfFiberThreshold', _nth(fields, 'EndOfFiberThreshold', i)),
                    ('SplitterDetection', _nth(fields, 'SplitterDetection', i)),
                    ('FiberCode', _nth(fields, 'FiberCode', i)),
                ) if v is not None
            },

            'exfo_calibration': {
                k: v for k, v in (
                    ('SamplingPeriod', sampling),
                    ('InjectionLevel', injections[i]),
                    ('ScaleFactor', _nth(fields, 'ScaleFactor', i)),
                    ('SaturationLevel', _nth(fields, 'SaturationLevel', i)),
                    ('RmsNoise', _nth(fields, 'RmsNoise', i)),
                    ('NominalPulseWidth', _nth(fields, 'NominalPulseWidth', i)),
                    ('CalibratedPulseWidth', _nth(fields, 'CalibratedPulseWidth', i)),
                    ('NumberOfAverages', _nth(fields, 'NumberOfAverages', i)),
                    ('SpansLoss', _nth(fields, 'SpansLoss', i)),
                    ('SpansLength', _nth(fields, 'SpansLength', i)),
                ) if v is not None
            },
            'exfo_events':          _tag_sections(list(recs)),
            'exfo_raw':             raw,
            'exfo_res_m':           res_m,
            'exfo_spans_loss':      _nth(fields, 'SpansLoss', i),
            'exfo_spans_length':    _nth(fields, 'SpansLength', i),
            'exfo_total_orl':       _nth(fields, 'TotalOrl', i),
            'exfo_sampling_period': sampling,
            'exfo_wavelength_nm':   ((_nth(fields, 'ExactWavelength', i) or 0) * 1e9)
                                    or ((wl_m * 1e9) if wl_m else None),
            'exfo_injection_level': injections[i],
            'exfo_saturation_level': _nth(fields, 'SaturationLevel', i),

            # A .bdr event list is stored in the SAME frame as its trace:
            # sample 0 is Position 0, verified by fitting every event at
            # Position/pitch and reproducing FR's own losses exactly.  So
            # there is no declared-span-start shift to undo.
            'user_offset_km': 0.0,

            # .bdr-only extras.
            '_bdr_side':     key.upper(),
            '_bdr_merged':   merged,
            '_bdr_path':     filepath,
        }
        sides[key] = result

    return {'a': sides['a'], 'b': sides['b'], 'merged': merged}


def parse_bdr_side(filepath: str, side: str) -> dict:
    """One direction of a .bdr.  `side` is 'a' or 'b'."""
    s = str(side).lower()
    if s not in ('a', 'b'):
        raise ValueError(f"side must be 'a' or 'b', got {side!r}")
    return parse_bdr(filepath)[s]

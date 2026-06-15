#!/usr/bin/env python3
"""
EXFO Proprietary Block Decoder
================================
Fully decodes the ExfoNewProprietaryBlock from SR-4731 (.sor) OTDR files.

Structure:
  Header (36 bytes):
    "AppReg Format Ex  \0\0" + 16 bytes metadata

  N size-prefixed zlib chunks:
    [4 bytes LE: compressed_size] [zlib data]

  Decompressed stream is a hierarchical key-value tree:
    [4B self_offset] [4B type_code] [4B size] [4B next_ref] FieldName\0 [value]

  Type codes:
    1 = uint32 (4 bytes)
    2 = binary array (size bytes follow)
    3 = float64 (8 bytes)
    4+ = string / container

Key discoveries:
  - RawSamples: uint16 LE array, same sample count as DataPts
  - Relationship: std_dB = 64.0 - prop_raw / 1024.0
  - ScaleFactor = 1024 (vs DataPts scale of 1000)
  - Rich event data: Position, Loss, CurveLevel, Reflectance,
    PeakReflectionToRbs, cursor positions per event
  - Section attenuation between events
  - Hardware calibration: pulse width, bandwidth, APD gain, temperature,
    wavelength, Fresnel correction, noise measurements
"""

import struct
import zlib
import sys
import os
import numpy as np


# ─── Block directory parser ───

def parse_block_directory(data):
    off = 0
    name_end = data.index(b'\x00', off) + 1
    off = name_end + 6
    num_blocks = struct.unpack_from('<H', data, off)[0]
    off += 2
    block_list = []
    for _ in range(num_blocks):
        ne = data.index(b'\x00', off) + 1
        nm = data[off:ne - 1].decode('latin-1')
        bv = struct.unpack_from('<H', data, ne)[0]
        bs = struct.unpack_from('<I', data, ne + 2)[0]
        block_list.append((nm, bv, bs))
        off = ne + 6
    seen = set()
    blocks = {}
    search_from = name_end + 2 + 4
    for nm, bv, bs in block_list:
        if nm in seen:
            continue
        seen.add(nm)
        ndl = nm.encode('latin-1') + b'\x00'
        i = data.find(ndl, search_from)
        if i >= 0:
            blocks[nm] = {'offset': i, 'size': bs, 'ver': bv, 'body': i + len(ndl)}
            search_from = i + bs
    return blocks


# ─── Decompress proprietary block ───

def decompress_proprietary(data, blocks):
    """Extract and decompress the ExfoNewProprietaryBlock."""
    blk_name = None
    for name in blocks:
        if 'ExfoNewProprietaryBlock' in name:
            blk_name = name
            break
    if blk_name is None:
        return None

    blk = blocks[blk_name]
    raw = data[blk['body']:blk['offset'] + blk['size']]

    header = raw[:36]
    chunks = []
    pos = 36
    while pos < len(raw) - 4:
        sz = struct.unpack_from('<I', raw, pos)[0]
        if sz < 2 or sz > len(raw) - pos - 4:
            break
        chunk = raw[pos + 4:pos + 4 + sz]
        if len(chunk) >= 2 and chunk[0] == 0x78:
            try:
                dec = zlib.decompress(chunk)
                chunks.append(dec)
                pos += 4 + sz
                continue
            except zlib.error:
                pass
        pos += 1

    return b''.join(chunks)


# ─── Field decoder ───

def decode_all_fields(stream):
    """
    Scan the decompressed stream and extract all named fields with typed values.

    Returns a list of (offset, name, type_code, value) tuples.
    """
    fields = []
    p = 0
    while p < len(stream) - 1:
        end = stream.find(b'\x00', p)
        if end < 0:
            break
        if end - p >= 2 and end - p < 100:
            try:
                s = stream[p:end].decode('ascii')
                if s.isprintable() and s[0].isalpha():
                    type_code = 0
                    data_size = 0
                    if p >= 16:
                        # Descriptor: [self_off(4B)] [type(4B)] [size(4B)] [next_ref(4B)] Name\0
                        type_code = struct.unpack_from('<I', stream, p - 12)[0]
                        data_size = struct.unpack_from('<I', stream, p - 8)[0]

                    val_off = end + 1
                    value = None

                    if type_code == 3 and data_size == 8:  # float64
                        if val_off + 8 <= len(stream):
                            value = struct.unpack_from('<d', stream, val_off)[0]
                    elif type_code == 1 and data_size == 4:  # uint32
                        if val_off + 4 <= len(stream):
                            value = struct.unpack_from('<I', stream, val_off)[0]
                    elif type_code == 2:  # binary array
                        value = ('binary', data_size, val_off)

                    fields.append({
                        'offset': p,
                        'name': s,
                        'type_code': type_code,
                        'data_size': data_size,
                        'value': value,
                    })
            except (UnicodeDecodeError, ValueError):
                pass
        p = end + 1

    return fields


def extract_trace(stream):
    """Extract the RawSamples trace data from the decompressed stream."""
    idx = stream.find(b'RawSamples\x00')
    if idx < 0:
        return None, None

    # Read descriptor: [self_off(4B)] [type(4B)] [data_size(4B)] [next_ref(4B)] Name\0
    data_size = struct.unpack_from('<I', stream, idx - 8)[0]
    val_off = idx + len(b'RawSamples\x00')
    num_samples = data_size // 2

    raw = np.frombuffer(stream[val_off:val_off + data_size], dtype='<u2')

    # Convert to dB using ScaleFactor (1024) and inversion
    # prop_dB = raw / 1024.0  (signal power, higher = stronger)
    # loss_dB = 64.0 - raw / 1024.0  (matches DataPts convention)
    return raw, num_samples


def extract_calibration(fields):
    """Extract calibration/hardware parameters."""
    cal_names = {
        'SamplingPeriod', 'DisplayRange', 'InjectionLevel', 'ScaleFactor',
        'SaturationLevel', 'RangeStart', 'RangeEnd', 'BaseClockPeriod',
        'NominalPulseWidth', 'CalibratedPulseWidth', 'PulseRiseTime',
        'PulseFallTime', 'Bandwidth', 'TypicalApdGain', 'TypicalAnalogGain',
        'NominalWavelength', 'ExactWavelength', 'NumberOfAverages',
        'InternalModuleReflection', 'FresnelCorrection', 'SaturationLevelLinear',
        'InternalFiberLength', 'RmsNoise', 'ModuleTemperature', 'ApdTemperature',
        'NormalizationExponent', 'TimeToOutputConnector',
        'UnfilteredRawDataRmsNoise', 'SpansLoss', 'SpansLength', 'TotalOrl',
        'HighResolution', 'NumberOfPhases',
    }
    cal = {}
    for f in fields:
        if f['name'] in cal_names and f['value'] is not None:
            if not isinstance(f['value'], tuple):
                cal[f['name']] = f['value']
    return cal


def extract_events(fields):
    """
    Extract event records from the field list.

    Events are identified by having a Type field. Each event has:
    Position, Length, Type, Status, Loss, CurveLevel, LocalNoise,
    Reflectance, PeakReflectionToRbs, and cursor positions.
    """
    # Find EventTable marker
    et_offset = None
    for f in fields:
        if f['name'] == 'EventTable':
            et_offset = f['offset']
            break
    if et_offset is None:
        return []

    # Find all events (Type fields after EventTable that look like event types)
    event_fields = ['Position', 'Length', 'Type', 'Status', 'Loss',
                    'CurveLevel', 'LocalNoise', 'Reflectance',
                    'PeakReflectionToRbs', 'SubCursorAPosition',
                    'CursorAPosition', 'CursorBPosition', 'SubCursorBPosition',
                    'SplitterRatio', 'EventChanged']

    # Group fields into event blocks
    # An event block starts with a Position and contains a Type
    post_et = [f for f in fields if f['offset'] > et_offset and f['offset'] < et_offset + 80000]

    events = []
    current_event = {}
    last_type_offset = 0

    for f in post_et:
        if f['name'] == 'Position' and f['value'] is not None:
            if current_event and 'Type' in current_event:
                events.append(current_event)
            elif current_event and 'Loss' in current_event and 'Type' not in current_event:
                events.append(current_event)  # section
            current_event = {'Position': f['value'], '_offset': f['offset']}
        elif f['name'] in event_fields and f['value'] is not None:
            if not isinstance(f['value'], tuple):
                current_event[f['name']] = f['value']

    if current_event:
        events.append(current_event)

    return events


# ─── Main decoder ───

def decode_sor(filepath):
    """Fully decode a .sor file including the EXFO proprietary block."""
    with open(filepath, 'rb') as f:
        data = f.read()

    blocks = parse_block_directory(data)
    stream = decompress_proprietary(data, blocks)
    if stream is None:
        return None

    fields = decode_all_fields(stream)
    trace_raw, num_samples = extract_trace(stream)
    calibration = extract_calibration(fields)
    events = extract_events(fields)

    return {
        'filename': os.path.basename(filepath),
        'blocks': list(blocks.keys()),
        'stream_size': len(stream),
        'num_fields': len(fields),
        'trace_samples': num_samples,
        'trace_raw': trace_raw,
        'calibration': calibration,
        'events': events,
        'all_fields': fields,
    }


# ─── CLI ───

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python exfo_proprietary_decoder.py <file.sor>")
        sys.exit(1)

    result = decode_sor(sys.argv[1])
    if result is None:
        print("No ExfoNewProprietaryBlock found.")
        sys.exit(1)

    print(f"\n{'='*70}")
    print(f"File: {result['filename']}")
    print(f"Stream: {result['stream_size']} bytes, {result['num_fields']} fields")
    print(f"Trace: {result['trace_samples']} samples")
    print(f"{'='*70}")

    print(f"\n--- Calibration ---")
    for k, v in sorted(result['calibration'].items()):
        if isinstance(v, float):
            print(f"  {k:40s} = {v:.10g}")
        else:
            print(f"  {k:40s} = {v}")

    print(f"\n--- Events ({len(result['events'])}) ---")
    for i, evt in enumerate(result['events']):
        has_type = 'Type' in evt
        label = f"Event {i+1}" if has_type else f"Section {i+1}"
        print(f"\n  {label}:")
        for k, v in evt.items():
            if k.startswith('_'):
                continue
            if isinstance(v, float):
                if abs(v) < 1e10:
                    print(f"    {k:30s} = {v:.6f}")
            else:
                print(f"    {k:30s} = {v}")

    if result['trace_raw'] is not None:
        tr = result['trace_raw']
        scale = result['calibration'].get('ScaleFactor', 1024)
        print(f"\n--- Trace (proprietary) ---")
        print(f"  Samples: {len(tr)}")
        print(f"  Raw range: {tr.min()} – {tr.max()}")
        print(f"  Power dB range: {tr.min()/scale:.3f} – {tr.max()/scale:.3f}")
        print(f"  Loss dB range: {64.0 - tr.max()/scale:.3f} – {64.0 - tr.min()/scale:.3f}")

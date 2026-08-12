#!/usr/bin/env python3
"""Build the `shortspan_A/` SYNTHETIC short-span fixture from real SOR bytes.

WHY A SYNTHETIC FIXTURE
    The short-span defect (every break/bend/damage/reflectance rule dies when a
    km blanket sized for 60-120 km spans is subtracted from a 2 km span) needs a
    SHORT span that actually contains a break.  The real short-span folder we
    have — Defuniak Springs Tie Panels, DNN1<->DNN2, 144 fibers each direction —
    has NO broken fiber: all 288 traces run the full 2.0415 km, confirmed
    against the raw samples (far-end Fresnel present on 288/288, A+B path sum
    2L on all 144 fiber numbers, real backscatter on every leg).  So the
    positive control has to be constructed, and it is constructed here in the
    open rather than asserted.

PROVENANCE
    Source bytes: Defuniak Springs Tie Panels / Aside / DNN1DNN2000{1..6}.sor
    (real EXFO acquisitions, 5 ns pulse, 1550 nm).  Everything except the two
    edits below is untouched real data — header, GenParams, FxdParams, the
    full DataPts trace.

THE TWO EDITS (both length-preserving; no block size or map offset changes)

  1. EVERY file — event #2 retyped '1F9999LS' -> '0F9999LS'.
     Reason: the real Defuniak trace is an UNTRIMMED shot — a 1.0047 km launch
     reel, then the 30.9 m panel-to-panel cable, then a 1.0053 km receive reel.
     `_normalize_untrimmed_events` re-references such a file to the launch
     connector, which is correct for that file but leaves a 0.03 km span that
     exercises the normalizer rather than the detection gates under test.
     Making event #2 non-reflective makes the file read as ALREADY TRIMMED
     (the untrimmed detector requires the first two events to both be
     reflective), so distances arrive at the detection gates unshifted and the
     fixture is a plain 2.0415 km span:

         0.0000 km  1F  launch connector (origin)
         1.0049 km  0F  splice
         1.0361 km  1F  reflective connector       <- the "connector"
         2.0415 km  1E  end of fiber

  2. FIBERS 5 AND 6 ONLY — broken at the 1.0361 km connector:
       * event #3 retyped '1F9999LS' -> '1E9999LS' (it becomes end-of-fiber)
       * the KeyEvents event COUNT is lowered 4 -> 3, which drops the old
         2.0415 km end event without moving a single byte (the trailing
         record simply stops being read; the block keeps its size)
       * DataPts samples past 1.0361 km are overwritten with the file's own
         measured post-EOF noise level, so trace-reading gates (ladder
         refutation, spike confirmation) see genuinely dead glass rather than
         a live trace contradicting the event table.

    That is exactly the boss's reported case: "short shots not detecting
    broken fiber at connector".  4 fibers run the full 2.0415 km, 2 die at
    1.0361 km — 1.0054 km short of the span.

WHAT IT PROVES
    Shipped code needs a fiber to die more than UNI_BREAK_PREMATURE_KM = 3.0 km
    short of the span.  On a 2.0415 km span `span - 3.0` is NEGATIVE, so the
    break test can never be satisfied and fibers 5/6 are silently missed.  With
    the span-scaled constant the premature blanket becomes
    min(3.0, 0.25 * 2.0415) = 0.5104 km and the two broken fibers are found.

REGENERATE
    python3 desktop/tests/fixtures/make_shortspan.py <path to Defuniak Aside>
"""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, 'shortspan_A')

N_FIBERS = 6
BROKEN_FIBERS = (5, 6)          # die at the 1.0361 km connector
BREAK_EVENT_INDEX = 2           # 0-based index of the 1.0361 km event
EVENT_RECORD_BYTES = 44         # SR-4731 KeyEvents record, fixed width
TYPE_FIELD_OFFSET = 14          # bytes into the record where the 8-char type sits


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, os.path.join(ROOT, 'splicereport'))
import sor_reader324802a as SR  # noqa: E402  (the engine's own parser)

_blocks = SR._parse_block_directory   # reuse the shipped map, never a copy


def _ior(data, blocks):
    return SR._read_ior(data)


def build_one(src_path, dst_path, break_it):
    data = bytearray(open(src_path, 'rb').read())
    blocks = _blocks(bytes(data))
    ke = blocks['KeyEvents']['body']
    n_events = struct.unpack_from('<H', data, ke)[0]
    assert n_events >= 4, f"{src_path}: expected >=4 events, got {n_events}"
    rec0 = ke + 2

    def rec(i):
        return rec0 + i * EVENT_RECORD_BYTES

    def ev_type(i):
        off = rec(i) + TYPE_FIELD_OFFSET
        return data[off:off + 8].split(b'\x00')[0].decode('latin-1')

    def set_type(i, new):
        off = rec(i) + TYPE_FIELD_OFFSET
        assert len(new) == 8, new
        data[off:off + 8] = new.encode('latin-1')

    def tot(i):
        return struct.unpack_from('<I', data, rec(i) + 2)[0]

    # Sanity: the real Defuniak event shape we documented above.
    assert ev_type(0).startswith('1F'), ev_type(0)
    assert ev_type(1).startswith('1F'), ev_type(1)
    assert ev_type(2).startswith('1F'), ev_type(2)
    assert ev_type(3).startswith('1E'), ev_type(3)

    # Edit 1 — make the file read as already-trimmed.
    set_type(1, '0F9999LS')

    if break_it:
        # Edit 2 — the fiber dies at the 1.0361 km connector.
        set_type(BREAK_EVENT_INDEX, '1E9999LS')
        struct.pack_into('<H', data, ke, 3)          # event count 4 -> 3

        # Blank the trace past the break to the file's own post-EOF noise.
        ior = _ior(bytes(data), blocks)
        break_km = tot(BREAK_EVENT_INDEX) * 0.02998 / ior / 1000.0
        dp = blocks['DataPts']['body']
        total_pts = struct.unpack_from('<I', data, dp)[0]
        pts_trace = struct.unpack_from('<I', data, dp + 6)[0]
        scale = struct.unpack_from('<H', data, dp + 10)[0]
        start = dp + 12
        if pts_trace > 500_000 or pts_trace < 10 or scale == 0:
            pts_trace, start = total_pts, dp + 4
        end_tot = tot(3)                              # old 2.0415 km end event
        end_km = end_tot * 0.02998 / ior / 1000.0
        # Sample index is linear in distance across the acquisition.
        idx_break = int(pts_trace * (break_km / end_km) * (end_km / end_km))
        idx_break = int(round(pts_trace * break_km / end_km))
        # Noise level = median of the real samples that already sit past EOF.
        tail = [struct.unpack_from('<H', data, start + 2 * i)[0]
                for i in range(min(pts_trace - 1, idx_break + 40), pts_trace)]
        noise = sorted(tail)[len(tail) // 2] if tail else 0
        for i in range(idx_break + 1, pts_trace):
            struct.pack_into('<H', data, start + 2 * i, noise)

    with open(dst_path, 'wb') as fh:
        fh.write(bytes(data))


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        print("ERROR: pass the path to the real Defuniak 'Aside' folder.")
        return 2
    src_dir = sys.argv[1]
    os.makedirs(OUT_DIR, exist_ok=True)
    for n in range(1, N_FIBERS + 1):
        src = os.path.join(src_dir, f"DNN1DNN2{n:04d}.sor")
        dst = os.path.join(OUT_DIR, f"DNN1DNN2{n:04d}.sor")
        build_one(src, dst, break_it=(n in BROKEN_FIBERS))
        print(f"  wrote {os.path.basename(dst)}"
              + ("   [BROKEN @1.0361 km]" if n in BROKEN_FIBERS else ""))
    return 0


if __name__ == '__main__':
    sys.exit(main())

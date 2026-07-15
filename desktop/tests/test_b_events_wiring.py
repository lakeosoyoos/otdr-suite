"""scan_b_events wiring (Pass 2a') — the B-panel pass that was never called.

splicereportmatchexfo.scan_b_events — the pass that takes B-direction
event-table entries with no A-side twin, grey-measures the A side at the
mirrored position, averages, and flags — existed since the Splice Report was
created but was never invoked by either pipeline replica.  Consequence: 7 of
the boss's 9 reported misses on Platteville-Cheyenne were B-panel-only events
the engine never looked at (fiber 175 .169@S12, 887 .173@S9, 553 .213@S20,
744 .566@S17, 837 .185@S11, 265 .34@S21, 1008 .224@S17).

These tests lock:
  * both call sites (engine main() + run_splicereport.py replica) — the same
    source-lock pattern as test_runner_passes_fibers_b_to_refine;
  * the acceptance behavior for the 7 boss items via synthetic data using the
    PLACHE-measured stored-B losses and A-grey values;
  * the live re-measure gate in the b_only fallback (suppresses stored claims
    the trace doesn't support at >= LOCAL_STEP_CONFIRM_RATIO, passes real
    losses) — including end-to-end on a byte-patched copy of a fixture SOR;
  * the _raw_events stash surviving into the analysis passes (the gate's
    time-of-travel position recovery needs it; the old pop predated the gate
    and silently degraded it to measuring ~1 launch-length upstream on
    untrimmed spans).

Engine runs in a clean subprocess (single sor_reader copy — the 3-engine
isolation rule).
"""
import struct
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"
SPLICE_A_DIR = REPO_ROOT / "desktop" / "tests" / "fixtures" / "splice_A"
SPLICE_B_DIR = REPO_ROOT / "desktop" / "tests" / "fixtures" / "splice_B"


def _run(body):
    body = textwrap.dedent(body)
    header = ("import sys\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import splicereportmatchexfo as E\n")
    p = subprocess.run([sys.executable, "-c", header + body],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


# ── Source locks: BOTH pipeline replicas must run the pass, in order ──────

def test_engine_main_wires_scan_b_events():
    """Engine main() must call scan_b_events after analyze_all +
    scan_a_standalone_events (dedup contract) and BEFORE the ghost /
    merged-reflective / b-side scans (they consume the accumulated dict)."""
    eng = (SPLICEREPORT_DIR / "splicereportmatchexfo.py").read_text(encoding="utf-8")
    call = "b_events = scan_b_events("
    assert call in eng, "engine main() no longer calls scan_b_events"
    assert "{**results, **a_standalone}, span_km" in eng, \
        "scan_b_events must dedup against pass1 + a_standalone"
    assert eng.index(call) > eng.index("a_standalone = scan_a_standalone_events("), \
        "scan_b_events must run AFTER scan_a_standalone_events"
    assert eng.index(call) < eng.index("ghost_refl = scan_bidir_ghost_reflections("), \
        "scan_b_events must run BEFORE scan_bidir_ghost_reflections"
    # Its results must flow into every later accumulated dict + final merge.
    assert eng.count("**a_standalone, **b_events") >= 4, \
        "b_events missing from the accumulated dicts / final merge"


def test_runner_replica_wires_scan_b_events():
    """run_splicereport.py's replicated SOR path must wire scan_b_events
    identically — missing the runner is exactly how a fix runs on the dev
    box and silently does nothing in production."""
    run = (SPLICEREPORT_DIR / "run_splicereport.py").read_text(encoding="utf-8")
    call = "b_ev = E.scan_b_events(fa, fb, splices, threshold,"
    assert call in run, "runner replica no longer calls scan_b_events"
    assert "{**results, **a_st}, span_km)" in run, \
        "runner scan_b_events must dedup against pass1 + a_standalone"
    assert run.index(call) > run.index("a_st = E.scan_a_standalone_events("), \
        "runner: scan_b_events must run AFTER scan_a_standalone_events"
    assert run.index(call) < run.index("ghost = E.scan_bidir_ghost_reflections("), \
        "runner: scan_b_events must run BEFORE scan_bidir_ghost_reflections"
    assert run.count("**a_st, **b_ev") >= 4, \
        "runner: b_ev missing from the accumulated dicts / final merge"


def test_raw_event_stash_survives_for_the_gate():
    """The re-measure gate (_local_step_from_event) recovers a normalized
    event's RAW trace-frame position by tot-matching against _raw_events.
    The SOR paths used to pop the stash before analyze_all (the pop predates
    the gate), silently degrading the gate to measuring at the normalized km
    — ~1 launch-length upstream on untrimmed spans, where flat glass reads
    ~0 and REAL >= 0.25 dB claims get suppressed.  Only the JSON trace-path
    restore (r['events'] = r.pop('_raw_events')) may remain."""
    eng = (SPLICEREPORT_DIR / "splicereportmatchexfo.py").read_text(encoding="utf-8")
    run = (SPLICEREPORT_DIR / "run_splicereport.py").read_text(encoding="utf-8")
    assert eng.count("pop('_raw_events'") == 1, \
        "engine: SOR path must not pop _raw_events before the scans"
    assert "r['events'] = r.pop('_raw_events')" in eng, \
        "engine: JSON trace-path restore should still exist"
    assert "pop('_raw_events'" not in run, \
        "runner: must not pop _raw_events before the scans"


# ── Acceptance: the 7 boss-reported B-panel items (synthetic) ─────────────
# Stored B losses and A-grey values are the ones measured on the real
# Platteville-Cheyenne files; expected bidir values are the boss's numbers.

def test_acceptance_seven_bpanel_items():
    _run("""
        SPAN = 107.4
        # fiber: (column_km, stored_b_loss, a_grey, boss_expected_bidir)
        CASES = {
            175:  (57.941,  0.324,  0.0135, 0.169),
            887:  (43.665,  0.326,  0.0192, 0.173),
            553:  (100.484, 0.425, -0.0016, 0.213),
            744:  (84.785,  1.131, -0.0024, 0.566),
            837:  (52.730,  0.366,  0.0133, 0.185),
            265:  (106.195, 0.691, -0.0065, 0.342),
            1008: (84.785,  0.422,  0.0283, 0.224),
        }
        col_kms = sorted({v[0] for v in CASES.values()})
        splices = [{'position_km': km, 'column_kind': 'splice', 'count': 24}
                   for km in col_kms]
        fibers_a, fibers_b, greys = {}, {}, {}
        for fnum, (km, b_loss, a_grey, _exp) in CASES.items():
            fibers_a[fnum] = {'_fnum': fnum, 'events': [
                {'dist_km': SPAN, 'splice_loss': 0.0, 'is_end': True, 'type': '1E'}]}
            fibers_b[fnum] = {'_fnum': fnum, 'events': [
                {'dist_km': SPAN - km, 'splice_loss': b_loss,
                 'is_end': False, 'type': '0F'},
                {'dist_km': SPAN, 'splice_loss': 0.0, 'is_end': True, 'type': '1E'}]}
            greys[fnum] = a_grey
        # The A side has no stored event at the mirror -> the pass must
        # grey-measure it.  Patch the measurement with the PLACHE values.
        E._grey_loss = lambda fd, km: greys[fd['_fnum']] if fd else None
        res = E.scan_b_events(fibers_a, fibers_b, splices,
                              E.REBURN_THRESHOLD, {}, SPAN)
        for fnum, (km, b_loss, a_grey, exp) in CASES.items():
            si = col_kms.index(km)
            cell = res.get((fnum, si))
            assert cell is not None, f'missing cell for fiber {fnum} @ {km}'
            got = cell['bidir_loss']
            assert abs(got - exp) <= 0.02, (fnum, got, exp)
            # engine rounds the average to 4 decimals
            assert abs(got - (a_grey + b_loss) / 2.0) <= 5.1e-5, (fnum, got)
            assert cell['event_source'] == 'bidir_grey_a', cell
            assert cell['is_flagged'] and not cell['is_b_only'], cell
        assert len(res) == len(CASES), res.keys()
        print('OK')
    """)


def test_scan_b_events_dedups_against_existing():
    """The insertion-point contract: a cell already claimed by pass 1 /
    a_standalone must NOT be overwritten or duplicated."""
    _run("""
        SPAN = 107.4
        splices = [{'position_km': 57.941, 'column_kind': 'splice', 'count': 24}]
        fibers_a = {175: {'events': [
            {'dist_km': SPAN, 'splice_loss': 0.0, 'is_end': True, 'type': '1E'}]}}
        fibers_b = {175: {'events': [
            {'dist_km': SPAN - 57.941, 'splice_loss': 0.324,
             'is_end': False, 'type': '0F'},
            {'dist_km': SPAN, 'splice_loss': 0.0, 'is_end': True, 'type': '1E'}]}}
        E._grey_loss = lambda fd, km: 0.0135 if fd else None
        existing = {(175, 0): {'label': 'claimed by pass 1'}}
        res = E.scan_b_events(fibers_a, fibers_b, splices,
                              E.REBURN_THRESHOLD, existing, SPAN)
        assert res == {}, res
        print('OK')
    """)


# ── The live re-measure gate in the b_only fallback ───────────────────────

def test_b_only_fallback_consults_the_gate():
    """No A trace at all -> the fallback flags a stored B claim >= 0.25 ONLY
    when _local_step_confirms says the trace supports it."""
    _run("""
        SPAN = 107.4
        splices = [{'position_km': 47.05, 'column_kind': 'splice', 'count': 24}]
        fibers_b = {24: {'events': [
            {'dist_km': SPAN - 47.05, 'splice_loss': 0.500,
             'is_end': False, 'type': '0F'},
            {'dist_km': SPAN, 'splice_loss': 0.0, 'is_end': True, 'type': '1E'}]}}
        # fiber 24 has NO A-side record -> a_grey is None -> b_only fallback.
        # Gate reads a step the claim doesn't support (0.05 < 0.35 * 0.5):
        E._local_step_from_event = lambda fd, e, **kw: 0.05
        res = E.scan_b_events({}, fibers_b, splices, E.REBURN_THRESHOLD, {}, SPAN)
        assert res == {}, ('gate should suppress the unsupported claim', res)
        # Same claim, but the trace supports it (0.30 >= 0.175):
        E._local_step_from_event = lambda fd, e, **kw: 0.30
        res = E.scan_b_events({}, fibers_b, splices, E.REBURN_THRESHOLD, {}, SPAN)
        cell = res.get((24, 0))
        assert cell is not None and cell['is_b_only'], res
        assert cell['label'] == '24 .500 (B)', cell
        print('OK')
    """)


def test_gate_live_on_byte_patched_fixture_sor(tmp_path):
    """End-to-end on real SOR data: copy a fixture B file, byte-patch a
    ~flat-glass event's stored loss (+0.064 @ 21.498 km raw) to a 0.500 dB
    claim, and run the ACTUAL wired pass.  The re-measure gate must DROP the
    phantom (tight local read ~0.07 = 14% of claim), and the same fiber's
    REAL 0.259 dB event must still confirm.  Bypassing the gate must produce
    the '.500 (B)' cell — proving the pipeline otherwise flags it and the
    suppression is exactly the gate."""
    import shutil
    b_dir = tmp_path / "B"
    b_dir.mkdir()
    src = SPLICE_B_DIR / "MILELM0024_1550.sor"
    dst = b_dir / src.name
    shutil.copy(src, dst)

    # Byte-patch: KeyEvents record = evnum(u16) tot(u32) slope(i16)
    # splice(i16) refl(i32) type(8s) 5x marker(u32) pad(2) = 44 bytes;
    # splice at +8, int16 LE, 0.001 dB units.  Find the record by its
    # raw-frame distance.  The name string appears twice (directory entry,
    # then block header — sor_reader locates blocks by this search too);
    # the block body follows the LAST occurrence.
    data = bytearray(dst.read_bytes())
    idx = data.rfind(b"KeyEvents\x00")
    assert idx > 0, "KeyEvents block not found"
    body = idx + len(b"KeyEvents\x00")
    num = struct.unpack_from("<H", data, body)[0]
    ior = 1.47  # this fixture's group index (matches _read_ior)
    rec = body + 2
    patched = False
    for _ in range(num):
        tot = struct.unpack_from("<I", data, rec + 2)[0]
        dist_km = (tot * 0.02998 / ior) / 1000.0
        old = struct.unpack_from("<h", data, rec + 8)[0]
        # Select by distance AND the known stored loss (+0.064 dB) so an
        # IOR drift can never patch the wrong record.
        if abs(dist_km - 21.498) < 0.15 and old == 64:
            struct.pack_into("<h", data, rec + 8, 500)
            patched = True
            break
        rec += 44
    assert patched, "no +0.064 dB event near 21.498 km to patch"
    dst.write_bytes(bytes(data))

    _run(f"""
        import numpy as np
        fa, fb = E.load_all({str(SPLICE_A_DIR)!r}, {str(b_dir)!r})
        for r in list(fa.values()) + list(fb.values()):
            r['_raw_events'] = r['events']
            r['_trace_offset_km'] = E._untrimmed_launch_offset_km(r['events'])
            r['events'] = E._normalize_untrimmed_events(r['events'])
        cand = E.discover_splices(fa)
        real, phantom = E.refine_closure_centers(fa, cand, return_phantoms=True,
                                                 fibers_b=fb)
        splices = sorted(list(real) + list(phantom),
                         key=lambda sp: sp.get('position_km_refined',
                                               sp['position_km']))
        ends = sorted([e['dist_km'] for r in fa.values()
                       for e in r['events'] if e['is_end']])
        span_km = round(float(np.median(ends[int(len(ends) * 0.75):])), 2)
        # No A record for fiber 24 -> its patched B event takes the b_only
        # fallback, where the re-measure gate lives.
        fa_no24 = {{k: v for k, v in fa.items() if k != 24}}
        res = E.scan_b_events(fa_no24, fb, splices, E.REBURN_THRESHOLD, {{}},
                              span_km)
        assert not any(k[0] == 24 for k in res), (
            'gate failed to drop the byte-patched phantom', res)
        # The patched claim itself must be gate-rejected...
        rb = fb[24]
        ev = min((e for e in rb['events'] if not e['is_end']),
                 key=lambda e: abs(e['dist_km'] - 20.491))
        assert abs(ev['splice_loss'] - 0.500) < 1e-9, ev
        assert not E._local_step_confirms(rb, ev), 'phantom claim confirmed?!'
        # ...while the same fiber's REAL 0.259 dB event still confirms
        # (locks the _raw_events tot-matching position recovery too).
        ev2 = min((e for e in rb['events'] if not e['is_end']),
                  key=lambda e: abs(e['dist_km'] - 55.820))
        assert abs(ev2['splice_loss'] - 0.259) < 1e-9, ev2
        assert E._local_step_confirms(rb, ev2), 'real 0.259 dB event suppressed'
        # Bypassing the gate must surface the phantom -> the suppression
        # above is the gate, not an upstream filter.
        E.LOCAL_STEP_CONFIRM_RATIO = 0.0
        res = E.scan_b_events(fa_no24, fb, splices, E.REBURN_THRESHOLD, {{}},
                              span_km)
        cells = {{k: v['label'] for k, v in res.items() if k[0] == 24}}
        assert any(lbl == '24 .500 (B)' for lbl in cells.values()), cells
        print('OK')
    """)

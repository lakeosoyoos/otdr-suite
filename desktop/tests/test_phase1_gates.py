"""Phase-1 trace-confirmation gates — regression tests.

The survey (2026-07-23) found six ungated stored-table consumers that could
ship a phantom cell on the firmware's word alone.  This locks the wiring:

  * both B-FILL emitters call _local_step_confirms (the shipped a_only/
    b_only gate's exact contract);
  * both BROKE emitters refute a stored mid-span is_end when the raw trace
    shows LIVE backscatter continuing past it;
  * both BREAK-vs-REF sites ask the raw samples first, stored-list fallback
    only on None.

Primitive calibration (LAMBEY 432 + SEANOR 110 km, 2026-07-23): live
backscatter fits at rms 0.005-0.007 dB at EVERY distance out to >70 km
(162/162 True); noise past a real break reads 1.25-1.9 dB (116/117 False,
1 None, 0 True).  Only a confident True refutes — every site fails open.
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SPLICE_DIR = os.path.join(ROOT, 'splicereport')
sys.path.insert(0, SPLICE_DIR)

import splicereportmatchexfo as E  # noqa: E402

SP = 5e-08                                   # sampling period → ~5.1 m/sample


def _fiber_with_trace(kind='live', n=3000, eof_km=None):
    """Synthetic record: 'live' = clean rising backscatter line the whole
    way; 'dead_after' = live up to eof_km then noise scatter."""
    m0 = SP * (299792458.0 / 1.468) / 2.0
    rng = np.random.RandomState(7)
    x = np.arange(n)
    tr = 5.0 + 0.002 * x + rng.normal(0, 0.004, n)      # live line, tiny noise
    if kind == 'dead_after' and eof_km is not None:
        i = int(eof_km * 1000.0 / m0)
        tr[i:] = 30.0 + rng.normal(0, 1.6, n - i)       # noise scatter
    return {'trace': tr, 'exfo_sampling_period': SP, 'events': []}


def _ev(km, loss=0.0, typ='0F', end=False):
    return {'dist_km': km, 'splice_loss': loss, 'type': typ,
            'is_end': end, 'is_reflective': typ.startswith('1F'),
            'reflection': -30.0, 'time_of_travel': 0}


def test_alive_on_live_glass():
    r = _fiber_with_trace('live')
    assert E._raw_backscatter_alive(r, _ev(5.0)) is True


def test_dead_past_real_termination():
    r = _fiber_with_trace('dead_after', eof_km=4.0)
    assert E._raw_backscatter_alive(r, _ev(4.0, end=True)) is False


def test_alive_before_the_break():
    r = _fiber_with_trace('dead_after', eof_km=8.0)
    assert E._raw_backscatter_alive(r, _ev(3.0)) is True


def test_unmeasurable_is_none():
    assert E._raw_backscatter_alive({'events': []}, _ev(5.0)) is None   # no trace
    assert E._raw_backscatter_alive(None, _ev(5.0)) is None
    r = _fiber_with_trace('live')
    assert E._raw_backscatter_alive(r, _ev(20.0)) is None               # off record


# ── Wiring: scan_b_past_breaks B-fill must call the re-measure gate ─────

def test_bfill_scan_gated_by_local_step(monkeypatch):
    splices = [{'position_km': 5.5, 'position_km_refined': 5.5}]
    fibers_a = {1: {'events': [_ev(2.0, typ='1E', end=True)]}}       # A broke @2
    fibers_b = {1: {'events': [_ev(4.5, loss=0.30),                  # A-frame 5.5
                               _ev(10.0, typ='1E', end=True)]}}

    monkeypatch.setattr(E, '_local_step_confirms', lambda r, e: False)
    out = E.scan_b_past_breaks(fibers_a, fibers_b, splices,
                               threshold=0.160, existing_results={},
                               total_span_a=10.0)
    assert out == {}                          # refuted stored loss must not fill

    monkeypatch.setattr(E, '_local_step_confirms', lambda r, e: True)
    out = E.scan_b_past_breaks(fibers_a, fibers_b, splices,
                               threshold=0.160, existing_results={},
                               total_span_a=10.0)
    assert any(v.get('is_bfill') for v in out.values())


# ── Wiring: B-side broke refuted only by a full-ladder True ───────────

def test_b_side_broke_refuted_only_by_ladder(monkeypatch):
    splices = [{'position_km': 5.0, 'position_km_refined': 5.0}]
    fibers_a = {1: {'events': [_ev(10.0, typ='1E', end=True)]}}      # A healthy
    fibers_b = {1: {'events': [_ev(4.0, typ='1E', end=True)]}}       # B dies @4
    for f in range(2, 6):                     # healthy B population → span 10
        fibers_b[f] = {'events': [_ev(10.0, typ='1E', end=True)]}

    for verdict, expect_broke in ((True, False), (False, True)):
        monkeypatch.setattr(E, '_broke_refuted_by_ladder',
                            lambda r, e, span, _v=verdict: _v)
        out = E.scan_b_side_breaks(fibers_a, fibers_b, splices,
                                   existing_results={}, total_span_a=10.0)
        got = any(v.get('is_broke') for v in out.values())
        assert got is expect_broke, 'refuted=%r -> broke=%r' % (verdict, got)


# ── Ladder semantics: the HOWLAN adjudication lesson ─────────────────

def test_ladder_refutes_only_healthy_to_span_end():
    # Phantom termination on healthy glass: alive at every rung → refuted.
    r = _fiber_with_trace('live', n=3000)                 # ~15 km record
    end = _ev(4.0, typ='1E', end=True)
    assert E._broke_refuted_by_ladder(r, end, span_km=14.0) is True

    # HOWLAN class: live for ~2 km past the stored EOF, then dead — a real
    # break with an early-marked EOF.  The far rungs fail → NOT refuted.
    r2 = _fiber_with_trace('dead_after', n=3000, eof_km=6.0)
    end2 = _ev(4.0, typ='1E', end=True)
    assert E._broke_refuted_by_ladder(r2, end2, span_km=14.0) is False


def test_continuation_ladder_break_vs_ref():
    # Live-for-700m-then-dead must read NOT-continuing (BREAK, not REF).
    r = _fiber_with_trace('dead_after', n=3000, eof_km=5.0)
    lad = E._raw_alive_ladder(r, _ev(4.0, typ='1F'), (0.0, 1.5, 3.0))
    assert any(v is False for v in lad)
    # Genuinely continuing glass reads all-True.
    r2 = _fiber_with_trace('live', n=3000)
    lad2 = E._raw_alive_ladder(r2, _ev(4.0, typ='1F'), (0.0, 1.5, 3.0))
    assert all(v is True for v in lad2)


# ── Source locks: the six call sites stay wired ─────────────────────────

def test_source_locks_phase1_wiring():
    src = open(os.path.join(SPLICE_DIR, 'splicereportmatchexfo.py'),
               encoding='utf-8').read()
    assert src.count('_local_step_confirms(rb, b_evt)') == 1   # analyze_all B-fill
    assert src.count('_local_step_confirms(rb, e)') >= 2       # scan gate + b_only sibling
    assert src.count('_broke_refuted_by_ladder(r, _end_ev, total_span_a)') == 1
    assert src.count('_broke_refuted_by_ladder(rb, end[0], total_span_b)') == 1
    assert src.count("_raw_alive_ladder(r, ea, (0.0, 1.5, 3.0))") == 1
    assert src.count("_raw_alive_ladder(ra, e, (0.0, 1.5, 3.0))") == 1

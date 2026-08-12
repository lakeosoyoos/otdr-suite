"""Short-span (connector-to-connector) detection — regression tests.

THE DEFECT
    Every km constant in splicereportmatchexfo.py was sized for the 60-120 km
    spans the tool grew up on.  Subtracted from a SHORT span they go negative
    and silently disable the rule they guard:

        threshold = span_km - UNI_BREAK_PREMATURE_KM   # 2.04 - 3.0 = -0.96
        if eof_km >= threshold: continue               # ... skips every fiber

    Reported by the boss as "short shots not detecting broken fiber at
    connector (this is in splice report feature)", on a Defuniak Springs
    tie-panel shot: a 30.9 m panel-to-panel cable between a 1.0047 km launch
    reel and a 1.0053 km receive reel.  Robert, 2026-08-12: "A connector to
    connector span needs to be something that we can analyze and look for
    breaks or bends or damage or reflectance."

THE FIX
    `span_scaled_km(const_km, span_km)` — the absolute constant becomes a
    CEILING, capped at SHORT_SPAN_FRAC (0.25) of the span.  Same idiom the
    file already used in MIDSPAN_DEAD_SPAN_FRAC / uni_front_dead_km, now the
    single shared helper.

THE HARD CONSTRAINT
    Long spans must be bit-identical.  A constant K is untouched for every
    span >= K / 0.25 — for the 3.0 km blankets that is 12 km, well below every
    span this tool has shipped a report for.  test_long_span_* below lock that
    down per-constant; the branch's cell-for-cell ripple over ELMNEW/NEWELM,
    SANDUR/DURSAN and Eugene (spans 59.9-101.6 km) came back at 0 differing
    cells across 67,216 cells.
"""
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SPLICE_DIR = os.path.join(ROOT, 'splicereport')
sys.path.insert(0, SPLICE_DIR)

import splicereportmatchexfo as E  # noqa: E402

FIXTURE_SHORT_A = os.path.join(HERE, 'fixtures', 'shortspan_A')

SHORT_SPAN = 2.0415        # the Defuniak trace length
LONG_SPAN = 62.0           # a representative real span


def _ev(km, loss=0.0, typ='0F9999LS', end=False, refl=False):
    return {'dist_km': km, 'splice_loss': loss, 'type': typ,
            'is_end': end, 'is_reflective': refl, 'reflection': 0.0,
            'time_of_travel': km * 1000}


def _fiber(events):
    return {'events': events, 'gen_loc_a': 'DNN1', 'gen_loc_b': 'DNN2',
            'gen_cable_id': 'DNN'}


def _span_fiber(eof_km, extra=()):
    evs = [_ev(0.0, typ='1F9999LS', refl=True)]
    evs += list(extra)
    evs.append(_ev(eof_km, typ='1E9999LS', end=True, refl=True))
    return _fiber(evs)


# ── the helper itself ───────────────────────────────────────────────────

def test_span_scaled_km_caps_on_short_spans():
    assert E.span_scaled_km(3.0, 2.0415) == pytest.approx(0.510375)
    assert E.span_scaled_km(0.3, 2.0415) == pytest.approx(0.3)   # already small


def test_span_scaled_km_is_identity_on_long_spans():
    for const in (0.2, 0.3, 1.0, 2.0, 3.0, 6.0):
        for span in (24.0, 59.88, 62.0, 74.74, 100.94, 101.62):
            assert E.span_scaled_km(const, span) == const, (const, span)


def test_span_scaled_km_degrades_to_the_constant_without_a_span():
    # A missing / zero / nonsense length must keep shipped behaviour, never 0.
    for bad in (0, 0.0, None, -5.0, '', 'x'):
        assert E.span_scaled_km(3.0, bad) == 3.0


def test_span_scaled_km_window_can_never_invert():
    # [d, len-d] with d <= 0.25*len is always at least half the fiber wide, so
    # no caller needs a max(0, ...) clamp.  This is the property that makes the
    # whole fix safe.
    for span in (0.031, 0.5, 2.0415, 4.0, 11.9, 12.0, 62.0):
        d = E.span_scaled_km(3.0, span)
        assert d <= 0.25 * span or d == 3.0
        assert (span - d) - d >= 0.5 * span - 1e-9


def test_short_span_frac_is_the_single_shared_constant():
    assert E.MIDSPAN_DEAD_SPAN_FRAC == E.SHORT_SPAN_FRAC
    assert E.UNI_FRONT_DEAD_SPAN_FRAC == E.SHORT_SPAN_FRAC


# ── BREAK: the reported defect ──────────────────────────────────────────

def test_break_is_found_on_a_short_span():
    """Pre-fix: `span - 3.0` is negative, so no fiber can ever be a break."""
    fibers = {1: _span_fiber(1.0361), 2: _span_fiber(1.0361)}
    fibers.update({f: _span_fiber(SHORT_SPAN) for f in range(3, 7)})
    breaks = E.uni_find_breaks(fibers, [], SHORT_SPAN)
    assert sorted(b['fiber'] for b in breaks) == [1, 2]
    assert all(b['position_km'] == pytest.approx(1.0361) for b in breaks)


def test_break_floor_scales_so_a_31m_cable_can_break():
    """UNI_BREAK_MIN_KM (0.3 km) is 10x the whole Defuniak panel cable."""
    span = 0.0312
    fibers = {1: _span_fiber(0.0151)}
    fibers.update({f: _span_fiber(span) for f in range(2, 6)})
    assert [b['fiber'] for b in E.uni_find_breaks(fibers, [], span)] == [1]


def test_healthy_short_span_reports_no_break():
    """NEGATIVE control.  The real Defuniak set has no broken fiber; a fix
    that invents one is worse than the bug it replaces."""
    fibers = {f: _span_fiber(SHORT_SPAN) for f in range(1, 13)}
    assert E.uni_find_breaks(fibers, [], SHORT_SPAN) == []


def test_long_span_break_detection_is_unchanged():
    fibers = {1: _span_fiber(40.0), 2: _span_fiber(59.5)}
    fibers.update({f: _span_fiber(LONG_SPAN) for f in range(3, 7)})
    breaks = E.uni_find_breaks(fibers, [], LONG_SPAN)
    # 40.0 is > 3 km short (a break); 59.5 is inside the 3 km end region.
    assert [b['fiber'] for b in breaks] == [1]


# ── BEND / DAMAGE ───────────────────────────────────────────────────────

def test_tail_guard_scales_on_short_spans():
    # At 2.04 km the 0.5 km end region is still under the 25% cap, so it is
    # unchanged — the guard only shrinks once the cable is short enough for
    # 0.5 km to matter.  On the real 30.9 m Defuniak panel cable it does.
    assert E.uni_tail_guard_km(SHORT_SPAN) == pytest.approx(0.5)
    assert E.uni_tail_guard_km(0.0312) == pytest.approx(0.0078)
    assert E.uni_tail_guard_km(0.0312, broken=True) == pytest.approx(0.0078)


def test_long_span_tail_guard_is_unchanged():
    assert E.uni_tail_guard_km(LONG_SPAN) == E.UNI_END_REGION_KM
    assert E.uni_tail_guard_km(LONG_SPAN, broken=True) == E.UNI_PREBREAK_GUARD_KM


def test_off_splice_bend_survives_on_the_31m_panel_cable():
    """UNI_END_REGION_KM (0.5 km) is 16x the 30.9 m Defuniak panel cable, so
    `eof - 0.5` was negative and every event was excluded as 'end region'."""
    span = 0.0312
    fibers = {1: _span_fiber(span, extra=[_ev(0.0180, loss=0.25)])}
    evs = E.uni_find_off_splice_events(fibers, [], launch_box_present=True,
                                       span_km=span)
    assert [(e['fiber'], e['position_km']) for e in evs] == [(1, 0.0180)]


def test_long_span_end_region_exclusion_is_unchanged():
    """An event 0.3 km before EOF on a long span stays excluded."""
    fibers = {1: _span_fiber(LONG_SPAN, extra=[_ev(LONG_SPAN - 0.3, loss=0.25)])}
    assert E.uni_find_off_splice_events(fibers, [], launch_box_present=True,
                                        span_km=LONG_SPAN) == []


# ── bidirectional gates ─────────────────────────────────────────────────

def test_trace_continues_past_works_on_a_short_span():
    """min_distance_to_eof_km=3.0 made every reflective event on a short span
    read as a BREAK, because nothing can be 3 km clear of a 2 km EOF."""
    evs = [_ev(0.0, typ='1F9999LS', refl=True),
           _ev(0.50, typ='1F9999LS', refl=True),
           _ev(1.0361, typ='1F9999LS', refl=True),
           _ev(SHORT_SPAN, typ='1E9999LS', end=True, refl=True)]
    # EOF is 1.5415 km past the 0.50 km event.  Pre-fix that had to clear a
    # flat 3.0 km, which a 2.04 km fiber can never do -> called a BREAK.
    assert E._trace_continues_past(evs, 0.50, SHORT_SPAN) is True


def test_trace_continues_past_long_span_unchanged():
    evs = [_ev(0.0, typ='1F9999LS', refl=True),
           _ev(30.0),
           _ev(LONG_SPAN, typ='1E9999LS', end=True, refl=True)]
    assert E._trace_continues_past(evs, 30.0, LONG_SPAN) is False   # 32 km EOF, event past? no
    evs2 = [_ev(0.0, typ='1F9999LS', refl=True),
            _ev(30.0), _ev(45.0),
            _ev(LONG_SPAN, typ='1E9999LS', end=True, refl=True)]
    assert E._trace_continues_past(evs2, 30.0, LONG_SPAN) is True
    # 1 km before EOF on a long span still does NOT "continue".
    assert E._trace_continues_past(evs2, LONG_SPAN - 1.0, LONG_SPAN) is False


def test_field_gainer_can_fire_on_a_short_span():
    lo = E.FIELD_GAINER_MIN_DB
    mid = (lo + E.FIELD_GAINER_MAX_DB) / 2.0
    # 1.0361 km is past the scaled launch guard (0.51) and before the scaled
    # end guard (2.0415-0.51 = 1.53).
    assert E._is_field_gainer(1.0361, SHORT_SPAN, mid) is True
    # Pre-fix both guards were 3.0 km and the rule could never fire.
    assert E._is_field_gainer(0.2, SHORT_SPAN, mid) is False      # in launch zone
    assert E._is_field_gainer(1.9, SHORT_SPAN, mid) is False      # in end zone


def test_field_gainer_long_span_unchanged():
    mid = (E.FIELD_GAINER_MIN_DB + E.FIELD_GAINER_MAX_DB) / 2.0
    assert E._is_field_gainer(2.9, LONG_SPAN, mid) is False        # < 3 km launch
    assert E._is_field_gainer(3.1, LONG_SPAN, mid) is True
    assert E._is_field_gainer(LONG_SPAN - 2.9, LONG_SPAN, mid) is False
    assert E._is_field_gainer(LONG_SPAN - 3.1, LONG_SPAN, mid) is True


# ── distributed loss stays silent rather than wrong ─────────────────────

def test_distributed_loss_needs_real_length_not_a_scaled_window():
    """DIST_MIN_RUN_KM / DIST_WINDOW_KM are measurement geometry, not
    exclusion blankets.  Scaling them too made the engine report a
    +1.649 dB/km 'distributed loss' over 67 fibers of provably healthy
    0.19 dB/km Defuniak glass.  They must stay absolute."""
    src = open(os.path.join(SPLICE_DIR, 'splicereportmatchexfo.py'),
               encoding='utf-8').read()
    assert 'while a + DIST_WINDOW_KM <= hi:' in src
    assert 'if sb - sa >= DIST_MIN_RUN_KM:' in src
    # ...while the guards DO scale.
    assert 'lo = span_scaled_km(DIST_LAUNCH_GUARD_KM, eof_km)' in src


# ── end-to-end on the synthetic short-span fixture ──────────────────────

@pytest.mark.skipif(not os.path.isdir(FIXTURE_SHORT_A),
                    reason="shortspan_A fixture not present")
def test_uni_report_finds_the_connector_break_end_to_end(tmp_path):
    """Full pipeline on fixtures/shortspan_A (see fixtures/make_shortspan.py
    for exactly how those bytes were built): 4 fibers run the full 2.0415 km,
    fibers 5 and 6 die at the 1.0361 km connector.  Pre-fix the manifest
    reported n_breaks 0."""
    import json
    out = tmp_path / "shortspan.xlsx"
    proc = subprocess.run(
        [sys.executable, os.path.join(SPLICE_DIR, 'run_splicereport.py'),
         '--uni', '--dir-a', FIXTURE_SHORT_A, '--out', str(out)],
        capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, proc.stderr[-2000:]
    manifest = None
    for line in proc.stdout.splitlines():
        if line.startswith('{'):
            manifest = json.loads(line)
    assert manifest is not None, proc.stdout[-2000:]
    uni = manifest['uni']
    assert uni['span_km'] == pytest.approx(2.04, abs=0.01)
    assert uni['n_breaks'] == 2, f"expected fibers 5+6 broken, got {uni['n_breaks']}"
    # break_columns in the manifest are rounded display km.
    assert uni['break_columns'] == [pytest.approx(1.03, abs=0.01)]
    # ...and the flagged-event rows name the two broken fibers.
    broken = sorted({c['fiber'] for c in uni['cells'] if c.get('kind') == 'break'})
    assert broken == [5, 6], f"break cells on the wrong fibers: {broken}"
    # The 4 healthy fibers must NOT be called broken.
    assert all(c['fiber'] in (5, 6)
               for c in uni['cells'] if c.get('kind') == 'break')

"""iOLM-export handling for the AWS / IIG MT.1085 profile.

Span 29 (Ingomar-Musselshell, 432 fibers, shot as iOLM and exported to .sor)
showed three things the engine had never met:

  * A BROKEN fiber's break is written as a reflective '1F' event carrying the
    whole loss, with NO 'E' end-of-fiber marker.  Healthy fibers carry one.
    With no marker the engine read the trace as endless and printed nothing
    for the two fibers NCT's own review marks BROKEN (F289 at the panel,
    F380 904 m out).
  * The crews shot straight from the panel, so the panel connector IS the
    0 km event and there is no launch reel.  The connector gates look for the
    second reflective event after the port and found nothing to grade on
    F97 (0.480 / 0.589) and F108 (0.503 / 0.755), both over the 0.50 contract.
  * The iOLM picks its own acquisition time per fiber, so DURATION_MISMATCH
    landed on nearly every ILA cell and buried the real findings.

Three engine switches handle those.  Every one ships inert -- OFF, or the
shipped behaviour -- and only the IIG profile turns them on, so Default,
Lumen and Zayo render exactly as before.  These tests pin the arithmetic of
each switch on synthetic records, that OFF really is off, and the wiring.
"""
from __future__ import annotations

import importlib
import sys

from conftest import (
    run_splicereport, FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR, SPLICEREPORT_DIR,
)

sys.path.insert(0, str(SPLICEREPORT_DIR))
import splicereportmatchexfo as E  # noqa: E402

IIG = "AWS / IIG MT.1085"


def _hub():
    return importlib.import_module('app')


def _ev(km, loss, typ='0F9999LS', end=False, refl=0.0):
    return dict(type=typ, dist_km=km, splice_loss=loss, reflection=refl,
                is_end=end, is_reflective=typ.startswith(('1', '2')),
                time_of_travel=(0 if km == 0.0 else int(km * 1000)))


def _healthy(span_km=72.3):
    """A panel-to-panel fiber the way the iOLM exports it: port event with the
    panel connector's loss, mid-span splices, an 'E' end at the far panel."""
    return {'events': [_ev(0.0, 0.30, '1F9999LS'), _ev(8.35, 0.05),
                       _ev(46.1, 0.07), _ev(span_km, 0.42, '1E9999LS', end=True),
                       _ev(span_km + 1.0, None, '1F9999LS')],
            'exfo_spans_length': span_km * 1000.0, '_source': 'sor'}


def _run_fallback(fa, fb, on=True):
    old = E.IOLM_END_FALLBACK
    try:
        E.IOLM_END_FALLBACK = 1 if on else 0
        if E.IOLM_END_FALLBACK:
            E._synthesize_missing_ends(fa, fb)
    finally:
        E.IOLM_END_FALLBACK = old


# ── 1. End-of-fiber fallback ─────────────────────────────────────────────
def test_break_mid_span_without_marker_gets_its_end():
    fa = {n: _healthy() for n in range(1, 6)}
    fb = {n: _healthy() for n in range(1, 6)}
    # F380 shape: port event, then the break at 0.904 km with 12 dB, no 'E'.
    fa[9] = {'events': [_ev(0.0, -0.20, '1F9999LS'), _ev(0.904, 12.145, '1F9999LS', refl=-50.1)],
             'exfo_spans_length': 903.5, '_source': 'sor'}
    fb[9] = _healthy(70.7)                                # B sees it end early too
    _run_fallback(fa, fb)
    assert fa[9]['events'][-1]['is_end'] is True
    assert fa[9]['events'][0]['is_end'] is False
    assert fa[9].get('_iolm_synth_end') is True
    assert not fa[9].get('_iolm_break_at_launch')         # not at the port
    for n in range(1, 6):                                 # healthy untouched
        assert [e['is_end'] for e in fa[n]['events']].count(True) == 1


def test_break_at_the_port_is_a_break_not_a_dead_shot_when_other_side_is_short():
    fa = {n: _healthy() for n in range(1, 6)}
    fb = {n: _healthy() for n in range(1, 6)}
    # F289 shape: ONE event at the port carrying 8.5 dB and -39 dB, no 'E'.
    fa[9] = {'events': [_ev(0.0, 8.517, '1F9999LS', refl=-39.2)],
             'exfo_spans_length': 0.0, '_source': 'sor'}
    fb[9] = _healthy(71.64)                               # B ends 0.66 km short
    _run_fallback(fa, fb)
    assert fa[9]['events'][0]['is_end'] is True
    assert fa[9].get('_iolm_break_at_launch') is True
    assert E._is_dead_acquisition(fa[9]) is False         # a break, not a re-shoot


def test_end_at_port_with_a_full_other_side_stays_a_dead_shot():
    fa = {n: _healthy() for n in range(1, 6)}
    fb = {n: _healthy() for n in range(1, 6)}
    fa[9] = {'events': [_ev(0.0, 8.517, '1F9999LS', refl=-39.2)],
             'exfo_spans_length': 0.0, '_source': 'sor'}
    fb[9] = _healthy()                                    # B sees the whole span
    _run_fallback(fa, fb)
    assert fa[9]['events'][0]['is_end'] is True
    assert not fa[9].get('_iolm_break_at_launch')
    assert E._is_dead_acquisition(fa[9]) is True


def test_unmarked_but_full_length_record_is_left_alone():
    fa = {n: _healthy() for n in range(1, 6)}
    fb = {n: _healthy() for n in range(1, 6)}
    r = _healthy()
    for e in r['events']:
        e['is_end'] = False                               # marker lost, glass fine
    fa[9] = r
    fb[9] = _healthy()
    _run_fallback(fa, fb)
    assert not any(e['is_end'] for e in fa[9]['events'])
    assert not fa[9].get('_iolm_synth_end')


def test_fallback_off_changes_nothing():
    fa = {n: _healthy() for n in range(1, 6)}
    fb = {n: _healthy() for n in range(1, 6)}
    fa[9] = {'events': [_ev(0.0, -0.20, '1F9999LS'), _ev(0.904, 12.145, '1F9999LS')],
             'exfo_spans_length': 903.5, '_source': 'sor'}
    fb[9] = _healthy(70.7)
    _run_fallback(fa, fb, on=False)
    assert not any(e['is_end'] for e in fa[9]['events'])
    assert E.IOLM_END_FALLBACK == 0 and E.PANEL_CONN_DIRECT == 0 and E.FQA_DURATION_TAG == 1, \
        "the three switches must ship inert"


# ── 2. Direct-panel connector pairing ────────────────────────────────────
def test_direct_panel_events_are_the_port_event_and_the_far_end_event():
    a = _healthy(); b = _healthy()
    near = E._direct_panel_conn_event(a)
    far = E._direct_panel_far_event(b)
    assert near is a['events'][0] and near['splice_loss'] == 0.30
    assert far is b['events'][3] and far['is_end'] and far['splice_loss'] == 0.42
    assert near.get('_direct_panel') and far.get('_direct_panel')


def test_direct_panel_refuses_a_break_or_a_lossless_port():
    a = {'events': [_ev(0.0, 8.5, '1F9999LS', end=True)]}     # break at the port
    assert E._direct_panel_conn_event(a) is None
    a2 = {'events': [_ev(0.0, None, '1F9999LS')]}             # port with no loss
    assert E._direct_panel_conn_event(a2) is None
    b = _healthy(); b['_iolm_synth_end'] = True               # synthesized end = a break
    assert E._direct_panel_far_event(b) is None


# ── 3. The OFF invariant on the fixture, and the wiring ──────────────────
def test_default_fixture_run_is_unchanged_by_the_switches(tmp_path):
    """The switches ship inert, so a default run and a run that sends the
    shipped values explicitly must produce the same flagged set."""
    o1 = tmp_path / "d.xlsx"; o2 = tmp_path / "e.xlsx"
    rc1, m1, e1 = run_splicereport(FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR, o1)
    rc2, m2, e2 = run_splicereport(FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR, o2,
                                   overrides={"IOLM_END_FALLBACK": 0, "PANEL_CONN_DIRECT": 0,
                                              "FQA_DURATION_TAG": 1})
    assert rc1 == 0 and rc2 == 0 and m1["ok"] and m2["ok"], (e1[-600:], e2[-600:])
    assert m1["n_flagged"] == m2["n_flagged"]
    assert [(c['fiber'], c['splice'], c['loss'], c['category']) for c in m1["cells"]] == \
           [(c['fiber'], c['splice'], c['loss'], c['category']) for c in m2["cells"]]


def test_switches_on_do_not_break_a_reel_shot_span(tmp_path):
    """The fixtures were shot with launch reels and carry end markers; with
    every IIG switch on they must still run and flag the same cells (the
    fallback finds nothing to synthesize, the direct pairing never triggers)."""
    o1 = tmp_path / "d.xlsx"; o2 = tmp_path / "i.xlsx"
    rc1, m1, _ = run_splicereport(FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR, o1)
    rc2, m2, e2 = run_splicereport(FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR, o2,
                                   overrides={"IOLM_END_FALLBACK": 1, "PANEL_CONN_DIRECT": 1,
                                              "FQA_DURATION_TAG": 0})
    assert rc1 == 0 and rc2 == 0 and m1["ok"] and m2["ok"], e2[-800:]
    assert [(c['fiber'], c['splice'], c['loss'], c['category']) for c in m1["cells"]] == \
           [(c['fiber'], c['splice'], c['loss'], c['category']) for c in m2["cells"]]


def test_iig_profile_turns_the_switches_on_and_the_average_connector_gate():
    hub = _hub()
    ex = hub._engine_extras_from_profile(IIG)
    assert ex["IOLM_END_FALLBACK"] == 1 and ex["PANEL_CONN_DIRECT"] == 1
    assert ex["FQA_DURATION_TAG"] == 0
    conn = hub._conn_settings_from_profile(IIG)
    assert conn["LAUNCH_CONN_AVG_MIN_DB"] == 0.50
    assert conn["LAUNCH_CONN_UNI_MIN_DB"] == 0.0
    assert conn["LAUNCH_CONN_LOSS_MIN_DB"] == 0.62


def test_other_profiles_leave_the_switches_alone():
    hub = _hub()
    for prof in hub.CUSTOMER_PROFILES:
        if prof == IIG:
            continue
        ex = hub._engine_extras_from_profile(prof)
        assert not any(k in ex for k in ("IOLM_END_FALLBACK", "PANEL_CONN_DIRECT",
                                         "FQA_DURATION_TAG")), prof
        assert hub._conn_settings_from_profile(prof)["LAUNCH_CONN_AVG_MIN_DB"] == 0.0, prof

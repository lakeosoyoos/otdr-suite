"""Rung hardening — layer 2 (densest cluster) + layer 3 (helix trend).

Hazards these guard against (both found by the gate-tightening research):
  * WINNIL 80.29 ribbon 39: a bimodal member window let the plain median
    land between two event groups — layer 2 publishes nothing instead.
  * Seattle S6 ribbon 5: three bent fibers outvoted the splice events and
    put the rung ON the bend (0 m from it) — layer 2 requires the densest
    CLUSTER to reach the fiber minimum, and layer 3 rejects a rung that
    jumps off its ribbon's own smooth helix trend while sibling ribbons
    stay on theirs.
  * HOWLAN splice 9: when MANY ribbons disagree with the global center
    the same way, the global center is what's off — layer 3 centers each
    residual on the splice's cross-ribbon median so those rungs survive.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, 'splicereport'))

import splicereportmatchexfo as E  # noqa: E402


# ── layer 2: _hardened_rung ──────────────────────────────────────────────

def test_clean_unimodal_ribbon_publishes_median():
    members = {f: [24.220 + f * 0.001] for f in (1, 2, 3, 4, 5)}
    r = E._hardened_rung(members)
    assert r is not None and abs(r - 24.223) < 0.005


def test_bimodal_split_publishes_nothing():
    # WINNIL-class: 3 fibers at one position, 3 at another 300 m away —
    # neither cluster reaches the fiber minimum, no median-between-humps
    members = {1: [80.10], 2: [80.11], 3: [80.10],
               4: [80.40], 5: [80.41], 6: [80.42]}
    assert E._hardened_rung(members) is None


def test_bend_cluster_loses_to_splice_cluster():
    # Seattle-S6-class: 3 bent fibers at +130 m vs 5 splice events at the
    # closure — the splice cluster has more distinct fibers and wins
    members = {53: [24.35], 58: [24.352], 60: [24.351],
               49: [24.22], 50: [24.221], 51: [24.219], 52: [24.222], 54: [24.220]}
    r = E._hardened_rung(members)
    assert r is not None and abs(r - 24.220) < 0.01


def test_one_fiber_doublet_does_not_qualify_cluster():
    # 4 events but only 2 distinct fibers in the tight cluster
    members = {1: [10.10, 10.101], 2: [10.102], 9: [10.60]}
    assert E._hardened_rung(members) is None


def test_too_few_members_returns_none():
    assert E._hardened_rung({1: [10.1], 2: [10.1]}) is None
    assert E._hardened_rung({}) is None


# ── layer 3: _reject_offtrend_rungs ──────────────────────────────────────

def _mk_splices(refs, rungs):
    """refs: list of global centers; rungs: {rib: {si: pos}}"""
    splices = []
    for si, ref in enumerate(refs):
        rp, src = {}, {}
        for rib, per in rungs.items():
            if si in per:
                rp[rib] = per[si]
                src[rib] = 'own'
        splices.append({'position_km': ref, 'position_km_refined': ref,
                        'ribbon_positions': rp, '_ribbon_rung_src': src})
    return splices


def test_single_ribbon_jump_is_rejected():
    refs = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0]
    # ribbon 5 rides a smooth helix trend except a +0.13 jump at splice 2;
    # ribbons 6 and 7 stay on trend so the splice median pins the truth
    trend = {si: refs[si] + 0.001 * si for si in range(6)}
    r5 = dict(trend); r5[2] = refs[2] + 0.131
    rungs = {5: r5, 6: dict(trend), 7: dict(trend)}
    splices = _mk_splices(refs, rungs)
    E._reject_offtrend_rungs(splices)
    assert splices[2]['_ribbon_rung_src'][5] == 'trend_reject'
    assert splices[2]['ribbon_positions'][5] == refs[2]
    # siblings untouched
    assert splices[2]['_ribbon_rung_src'][6] == 'own'
    assert all(splices[si]['_ribbon_rung_src'][5] == 'own' for si in (0, 1, 3, 4, 5))


def test_shared_shift_survives_global_center_error():
    # HOWLAN-splice-9-class: EVERY ribbon reads −0.10 at splice 3 — the
    # global center is what's wrong; no rung may be rejected there
    refs = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0]
    rungs = {}
    for rib in (1, 2, 3, 4):
        per = {si: refs[si] for si in range(6)}
        per[3] = refs[3] - 0.10
        rungs[rib] = per
    splices = _mk_splices(refs, rungs)
    E._reject_offtrend_rungs(splices)
    for rib in (1, 2, 3, 4):
        assert splices[3]['_ribbon_rung_src'][rib] == 'own'


def test_ribbon1_rejection_restores_display():
    refs = [10.0, 20.0, 30.0, 40.0, 50.0]
    trend = {si: refs[si] for si in range(5)}
    r1 = dict(trend); r1[1] = refs[1] + 0.15
    rungs = {1: r1, 2: dict(trend), 3: dict(trend)}
    splices = _mk_splices(refs, rungs)
    splices[1]['_display_pre_r1'] = (20.01, 20.013, 7)
    splices[1]['position_km_display'] = 20.15
    E._reject_offtrend_rungs(splices)
    assert splices[1]['_ribbon_rung_src'][1] == 'trend_reject'
    assert splices[1]['position_km_display'] == 20.01
    assert splices[1]['position_km_display_fiber'] == 7


def test_too_few_points_skips_trend_test():
    refs = [10.0, 20.0, 30.0]
    rungs = {5: {0: 10.0, 1: 20.13, 2: 30.0}}   # 3 points < minimum
    splices = _mk_splices(refs, rungs)
    E._reject_offtrend_rungs(splices)
    assert splices[1]['_ribbon_rung_src'][5] == 'own'


# ── wiring locks ─────────────────────────────────────────────────────────

def test_constants_present():
    assert E.RIBBON_RUNG_CLUSTER_KM == 0.050
    assert E.RIBBON_TREND_MAX_RESID_M == 75.0
    assert E.RIBBON_TREND_MIN_POINTS == 4


def test_hardening_wired_into_refine():
    src = open(os.path.join(ROOT, 'splicereport', 'splicereportmatchexfo.py'),
               encoding='utf-8').read()
    body = src.split('def refine_closure_centers', 1)[1]
    assert '_hardened_rung(' in body
    assert '_reject_offtrend_rungs(out)' in body

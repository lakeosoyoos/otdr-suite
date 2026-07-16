"""Asymmetry veto (joint signature) + parsimony rule — regression tests.

Ground truth (Platteville–Cheyenne, boss's read): 18 cells painted
"bend at splice" all carried the fiber-joint signature — A 0.02–0.05 dB
vs B 0.14–0.25 dB at the same physical spot, a single event within
splice-attribution range of a known closure.  A bend is the same glass
seen from both directions and must read A ≈ B (Seattle's confirmed deep
bend clusters read ratios 1.0–1.9), so a strongly asymmetric pair is a
JOINT — the fiber's own splice — and must not be classified bend.

Scope guards under test:
  * closure proximity: veto only within BEND_SPLICE_FOLD_KM of a real
    splice column (PLACHE F607 @5.83 km, 1.26 km mid-span, keeps its flag)
  * splice-only anchors: phantom bend/damage columns must not anchor the
    attribution (veto_splice_kms excludes them)
  * parsimony: a second distinct event near the closure disables the veto
    (the splice is accounted for, the candidate may be a real bend)
  * twin exclusion: a paired opposite-direction table entry for the same
    feature is not a "second event" (A/B positions disagree 100+ m)
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SPLICE_DIR = os.path.join(ROOT, 'splicereport')
sys.path.insert(0, SPLICE_DIR)

import splicereportmatchexfo as E  # noqa: E402


def _ev(km, loss=0.05, refl=False, end=False):
    return {'dist_km': km, 'splice_loss': loss,
            'is_reflective': refl, 'is_end': end}


# ── _asym_joint_signature ────────────────────────────────────────────────

def test_plache_pair_is_joint():
    # F24: A 0.039 / B 0.238 — the canonical boss-disputed cell
    assert E._asym_joint_signature(0.039, 0.238)


def test_weakest_plache_pair_is_joint():
    # F331: A 0.048 / B 0.135 — ratio 2.81, the tightest of the 18
    assert E._asym_joint_signature(0.048, 0.135)


def test_symmetric_seattle_bend_is_not_joint():
    # Seattle F146 @98.4: A 0.128 / B 0.121 — confirmed real bend
    assert not E._asym_joint_signature(0.128, 0.121)


def test_moderately_asymmetric_bend_survives():
    # Seattle F361: A 0.090 / B 0.174 — ratio 1.93, stays a bend
    # (flag_consensus_bends' own docstring names this fiber)
    assert not E._asym_joint_signature(0.090, 0.174)


def test_gainer_is_definitive_joint():
    assert E._asym_joint_signature(-0.02, 0.20)


def test_noise_floor_blocks_small_pairs():
    # big ratio but hi side below BEND_ASYM_VETO_MIN_DB → no veto
    assert not E._asym_joint_signature(0.01, 0.05)


def test_missing_side_never_fires():
    assert not E._asym_joint_signature(None, 0.25)
    assert not E._asym_joint_signature(0.03, None)


# ── _single_event_near_closure ───────────────────────────────────────────

def test_single_event_true():
    evs = [_ev(10.13), _ev(2.0), _ev(50.0)]
    assert E._single_event_near_closure(evs, 10.0, 10.13)


def test_second_event_blocks():
    evs = [_ev(10.13), _ev(10.02)]   # splice already accounted for
    assert not E._single_event_near_closure(evs, 10.0, 10.13)


def test_twin_is_not_second_event():
    # paired A/B positions disagree ~110 m for the same feature
    evs = [_ev(10.02)]
    assert E._single_event_near_closure(evs, 10.0, 10.13,
                                        twin_pos_km=10.02)


def test_reflective_and_end_ignored():
    evs = [_ev(10.13), _ev(10.05, refl=True), _ev(10.20, end=True)]
    assert E._single_event_near_closure(evs, 10.0, 10.13)


def test_no_events_list_disables_veto():
    assert not E._single_event_near_closure(None, 10.0, 10.13)


# ── _is_bend_event with the veto ─────────────────────────────────────────
# closure_kms=None skips the per-fiber length model, so the legacy
# offset-gate fallback would return True — any False below is the veto.

def _bend(pos, a, b, evs, veto_kms, twin=None):
    return E._is_bend_event(pos, 10.0, (a + b) / 2.0,
                            fiber_events=evs, a_loss=a, b_loss=b,
                            closure_kms=None, fiber_data=None,
                            twin_pos_km=twin, veto_splice_kms=veto_kms)


# NOTE: the per-fiber anchor picks the event nearest the closure center,
# so a lone candidate would self-anchor and exit at the offset gate before
# the veto (pre-existing behavior).  Anchor each case on the candidate's
# opposite-direction twin at 9.95 (excluded from parsimony via twin_pos_km)
# so the candidate sits 180 m off its anchor and reaches the veto.

def test_veto_flips_asymmetric_single_event_near_splice():
    assert _bend(10.13, 0.039, 0.238, [_ev(9.95)], [10.0],
                 twin=9.95) is False


def test_symmetric_event_stays_bend():
    # identical geometry, symmetric pair → veto must not fire
    assert _bend(10.13, 0.110, 0.120, [_ev(9.95)], [10.0],
                 twin=9.95) is True


def test_midspan_asymmetric_event_stays_bend():
    # PLACHE F607-class: no closure within BEND_SPLICE_FOLD_KM of the
    # event — identical to the veto case except the splice list
    assert _bend(10.13, 0.039, 0.238, [_ev(9.95)], [5.0],
                 twin=9.95) is True


def test_second_event_keeps_bend():
    evs = [_ev(10.13), _ev(10.01)]
    assert _bend(10.13, 0.039, 0.238, evs, [10.0]) is True


def test_twin_pos_does_not_block_veto():
    evs = [_ev(10.02)]
    assert _bend(10.13, 0.039, 0.238, evs, [10.0], twin=10.02) is False


def test_loss_below_bend_threshold_unchanged():
    # sub-threshold events were never bends; veto must not resurrect them
    assert E._is_bend_event(10.13, 10.0, 0.05,
                            fiber_events=[_ev(10.13)],
                            a_loss=0.02, b_loss=0.08,
                            veto_splice_kms=[10.0]) is False


# ── source-locks: veto wired at every producing path ─────────────────────

def _engine_src():
    with open(os.path.join(SPLICE_DIR, 'splicereportmatchexfo.py'),
              encoding='utf-8') as f:
        return f.read()


def test_constants_present_with_shipped_defaults():
    assert E.BEND_ASYM_VETO_RATIO == 2.5
    assert E.BEND_ASYM_VETO_MIN_DB == 0.120
    assert E.PARSIMONY_WINDOW_KM == 0.350
    assert E.PARSIMONY_DISTINCT_KM == 0.030


def test_callsites_pass_splice_only_anchors():
    # analyze bidir + grey-B + scan_b paired + scan_b grey-A = 4 sites
    src = _engine_src()
    assert src.count('veto_splice_kms=veto_splice_kms') >= 4


def test_consensus_pass_has_veto():
    src = _engine_src()
    body = src.split('def flag_consensus_bends', 1)[1]
    body = body.split('\ndef ', 1)[0]
    assert '_asym_joint_signature' in body
    assert '_single_event_near_closure' in body
    assert 'BEND_SPLICE_FOLD_KM' in body


def test_splice_only_anchor_lists_built_at_both_pipelines():
    src = _engine_src()
    assert src.count("sp.get('column_kind', 'splice') == 'splice'") >= 2

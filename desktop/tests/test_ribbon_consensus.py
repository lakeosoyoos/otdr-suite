"""Per-ribbon closure consensus (two-level helix model).

Helix offset is set by a fiber's radial position in the cable: fibers in the
SAME ribbon (RIBBON_SIZE=12) share nearly identical lay, so per-splice event
positions cluster tightly WITHIN a ribbon while the large far-end spreads
(100s of metres on deep HOWLAN splices) are BETWEEN ribbons.  The consensus
layer publishes sp['ribbon_positions'] = {1-based ribbon: km} from
refine_closure_centers — the median of each ribbon's member-fiber event
positions inside the neighbor-aware gather window, with a small-sample guard
(>= RIBBON_CONSENSUS_MIN_FIBERS distinct member fibers, else the ribbon
inherits the GLOBAL refined center) — and _closure_km_for_fiber prefers it,
so every bend-offset reference / off-splice attribution anchors to the
fiber's own ribbon ladder instead of the pooled global center.

These tests lock:
  * the consensus refinement itself (two ribbon populations offset ±100 m
    are recovered; a 3-fiber ribbon and a single-fiber event pile both
    inherit the global center; a 4-fiber ribbon contributes);
  * consumption (_closure_km_for_fiber preference order; _bend_reference_km;
    scan_a_standalone_events anchoring its per-fiber primary-splice search
    at the ribbon position — the fold that stops helix-drifted splice events
    from being flagged as off-splice bends/refs);
  * e2e no-regression on the 24-fiber (2-ribbon) fixture span: consensus is
    computed, and stripping it changes NOTHING on a clean low-helix span;
  * source-locks on the wiring.

Engine runs in a clean subprocess (single sor_reader copy — the 3-engine
isolation rule), same pattern as test_b_events_wiring.
"""
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


# Shared synthetic-span builder: one closure at ~10 km, EOF at 20 km.
#   ribbon 1 (fibers 1-12)  : events at 9.900 km  (tight ±5 m)   → own rung
#   ribbon 2 (fibers 13-24) : events at 10.100 km (±15 m)        → own rung
#   ribbon 3 (fibers 25-27) : events at 10.300 km — only 3 member
#                             fibers → must inherit the global center
#   ribbon 4 (fiber 37 only): FIVE events at ~10.152 km — one fiber's
#                             event pile must not fake a consensus
#   ribbon 5 (fibers 49-52) : events at 10.220 km — exactly 4 member
#                             fibers → contributes (guard boundary)
_SYNTH = """
def ev(km, loss=0.05, typ='0F9999', end=False):
    return {'dist_km': km, 'splice_loss': loss, 'type': typ, 'is_end': end}

fibers = {}
j12 = [(-5 + i) / 1000.0 for i in range(11)] + [0.005]      # ±5 m
j15 = [(-15 + 3 * i) / 1000.0 for i in range(11)] + [0.015]  # ±15 m
for i, f in enumerate(range(1, 13)):
    fibers[f] = {'events': [ev(9.900 + j12[i]), ev(20.0, typ='1E9999', end=True)]}
for i, f in enumerate(range(13, 25)):
    fibers[f] = {'events': [ev(10.100 + j15[i]), ev(20.0, typ='1E9999', end=True)]}
for i, f in enumerate(range(25, 28)):
    fibers[f] = {'events': [ev(10.300 + (i - 1) / 1000.0), ev(20.0, typ='1E9999', end=True)]}
fibers[37] = {'events': [ev(10.148 + i / 500.0) for i in range(5)]
                        + [ev(20.0, typ='1E9999', end=True)]}
for i, f in enumerate(range(49, 53)):
    fibers[f] = {'events': [ev(10.220 + (i - 1) / 1000.0), ev(20.0, typ='1E9999', end=True)]}

cand = E.discover_splices(fibers)
assert len(cand) == 1, f"expected 1 closure candidate, got {len(cand)}"
out = E.refine_closure_centers(fibers, cand, validate=False)
assert len(out) == 1
sp = out[0]
refined = sp['position_km_refined']
rp = sp.get('ribbon_positions')
"""


def test_per_ribbon_consensus_recovers_offset_populations():
    """Two 12-fiber ribbon populations offset ±100 m around the closure →
    per-ribbon centers recovered to a few metres, independent of where the
    global mode lands."""
    _run(_SYNTH + """
assert rp is not None, "refine_closure_centers no longer publishes ribbon_positions"
assert set(rp) == {1, 2, 3, 4, 5}, f"one entry per ribbon present in the span: {sorted(rp)}"
assert abs(rp[1] - 9.900) < 0.010, f"ribbon 1 center not recovered: {rp[1]}"
assert abs(rp[2] - 10.100) < 0.020, f"ribbon 2 center not recovered: {rp[2]}"
# the global refined center sits on the densest population (ribbon 1 here);
# the consensus separates the ribbons regardless
assert abs(rp[2] - rp[1]) > 0.150, "ribbon separation lost"
print('OK')
""")


def test_small_ribbons_inherit_global_center():
    """<4 member fibers (3-fiber ribbon, or ONE fiber with five events) →
    the ribbon inherits the global refined center; exactly 4 member fibers →
    contributes its own value."""
    _run(_SYNTH + """
# 3 distinct member fibers → too small: inherits global, NOT its own 10.300
assert rp[3] == refined, f"3-fiber ribbon must inherit global: {rp[3]} vs {refined}"
assert abs(rp[3] - 10.300) > 0.200
# one fiber's five-event pile → 1 member fiber: inherits global
assert rp[4] == refined, f"single-fiber event pile must inherit global: {rp[4]}"
# exactly RIBBON_CONSENSUS_MIN_FIBERS (4) member fibers → contributes
assert E.RIBBON_CONSENSUS_MIN_FIBERS == 4
assert abs(rp[5] - 10.220) < 0.010, f"4-fiber ribbon must contribute: {rp[5]}"
print('OK')
""")


def test_closure_km_for_fiber_prefers_consensus():
    """Anchor preference order: ribbon_positions (1-based) → legacy
    position_km_refined_by_ribbon (0-based) → global refined → position_km."""
    _run("""
sp = {'position_km': 10.0, 'position_km_refined': 10.02,
      'ribbon_positions': {1: 9.90, 2: 10.11},
      'position_km_refined_by_ribbon': {0: 9.80, 1: 10.30}}
# consensus wins over the legacy mode-based value
assert E._closure_km_for_fiber(sp, 5) == 9.90     # fiber 5 → ribbon 1
assert E._closure_km_for_fiber(sp, 13) == 10.11   # fiber 13 → ribbon 2
# ribbon absent from consensus → legacy would say 10.30 for ribbon 2... but
# consensus covers it; use ribbon 3 (in neither) → global refined
assert E._closure_km_for_fiber(sp, 25) == 10.02
# no consensus at all → legacy per-ribbon still honored (old cached dicts)
sp2 = {'position_km': 10.0, 'position_km_refined': 10.02,
       'position_km_refined_by_ribbon': {0: 9.95}}
assert E._closure_km_for_fiber(sp2, 3) == 9.95
assert E._closure_km_for_fiber(sp2, 20) == 10.02
# bare dict → position_km
assert E._closure_km_for_fiber({'position_km': 7.5}, 1) == 7.5
print('OK')
""")


def test_bend_reference_uses_ribbon_anchor():
    """_bend_reference_km searches the fiber's own events around the RIBBON
    anchor: an event 20 m from the ribbon rung is the reference even though
    it is 120 m from the global center; a fiber with no event near its rung
    falls back to the rung itself (not the global center)."""
    _run("""
sp = {'position_km': 10.0, 'position_km_refined': 10.0,
      'ribbon_positions': {1: 9.90}}
evs = [{'dist_km': 9.92, 'splice_loss': 0.05, 'type': '0F', 'is_end': False}]
assert E._bend_reference_km(sp, 5, evs) == 9.92
assert E._bend_reference_km(sp, 5, []) == 9.90
print('OK')
""")


def test_scan_a_standalone_folds_helix_drifted_splice():
    """THE consumption behavior: a reflective event sitting exactly on its
    ribbon's consensus rung — 600 m from the global center, beyond the
    ±0.5 km per-fiber search window — is the fiber's own (helix-drifted)
    splice.  With the consensus anchor it FOLDS (no cell); with the anchor
    stripped (the old global-center behavior) the same event is flagged as
    a standalone REF/BREAK 600 m off-splice."""
    _run("""
def ev(km, loss, typ='0F9999', end=False, refl=None):
    e = {'dist_km': km, 'splice_loss': loss, 'type': typ, 'is_end': end}
    if refl is not None:
        e['reflection'] = refl
        e['is_reflective'] = True
    return e

fibers = {1: {'events': [ev(10.6, 0.15, typ='1F9999', refl=-30.0),
                         ev(20.0, 0.01),          # trace continues past
                         ev(40.0, 0.0, typ='1E9999', end=True)]}}
sp = {'position_km': 10.0, 'position_km_refined': 10.0,
      'ribbon_positions': {1: 10.6}, 'column_kind': 'splice'}
res = E.scan_a_standalone_events(fibers, [sp], {}, 40.0)
assert res == {}, f"helix-drifted own-splice event must fold, got {res}"

sp_old = {'position_km': 10.0, 'position_km_refined': 10.0,
          'column_kind': 'splice'}          # no per-ribbon anchor
res_old = E.scan_a_standalone_events(fibers, [sp_old], {}, 40.0)
assert (1, 0) in res_old, f"control (global anchor) should flag: {res_old}"
assert res_old[(1, 0)]['is_flagged']
print('OK')
""")


def test_e2e_fixture_consensus_present_and_inert():
    """24-fiber (2-ribbon) fixture span e2e: every validated closure carries
    a complete consensus dict for ribbons {1, 2}, close to the global center
    (clean low-helix span) — and stripping the consensus changes NOTHING in
    Pass 1 + the standalone scan (no regression on non-helix spans)."""
    _run(f"""
import numpy as np
fa, fb = E.load_all({str(SPLICE_A_DIR)!r}, {str(SPLICE_B_DIR)!r})
assert len(fa) == 24 and len(fb) == 24
for r in list(fa.values()) + list(fb.values()):
    r['_raw_events'] = r['events']
    r['_trace_offset_km'] = E._untrimmed_launch_offset_km(r['events'])
    r['events'] = E._normalize_untrimmed_events(r['events'])
cand = E.discover_splices(fa)
real, phantom = E.refine_closure_centers(fa, cand, return_phantoms=True,
                                         fibers_b=fb)
assert real, "fixture span lost its closures"
for sp in real:
    rp = sp.get('ribbon_positions')
    assert rp is not None and set(rp) == {{1, 2}}, rp
    g = sp['position_km_refined']
    for v in rp.values():
        assert abs(v - g) < 0.35, (sp['position_km'], rp, g)

splices = sorted(list(real) + list(phantom),
                 key=lambda s: s.get('position_km_refined', s['position_km']))
ends = sorted(e['dist_km'] for r in fa.values() for e in r['events'] if e['is_end'])
span_km = float(np.median(ends[int(len(ends) * 0.75):]))

def signature(spl):
    res = E.analyze_all(fa, fb, spl, E.REBURN_THRESHOLD)
    a_st = E.scan_a_standalone_events(fa, spl, res, span_km, fibers_b=fb)
    merged = {{**res, **a_st}}
    return {{k: (v.get('label'), v.get('event_source'), v.get('is_flagged'),
                 v.get('is_bend'), round(v.get('bidir_dist') or -1, 4))
             for k, v in merged.items()}}

with_consensus = signature(splices)
import copy
stripped = copy.deepcopy(splices)
for sp in stripped:
    sp.pop('ribbon_positions', None)
    sp.pop('position_km_refined_by_ribbon', None)
without_consensus = signature(stripped)
assert with_consensus == without_consensus, (
    "consensus must be inert on the clean 2-ribbon fixture span:\\n"
    f"only-with={{ {{k: v for k, v in with_consensus.items() if without_consensus.get(k) != v}} }}\\n"
    f"only-without={{ {{k: v for k, v in without_consensus.items() if with_consensus.get(k) != v}} }}")
print('OK')
""")


# ── Source locks ─────────────────────────────────────────────────────────

def test_source_lock_consensus_published_and_consumed():
    eng = (SPLICEREPORT_DIR / "splicereportmatchexfo.py").read_text(encoding="utf-8")
    assert "sp['ribbon_positions'] = ribbon_positions" in eng, \
        "refine_closure_centers no longer publishes the per-ribbon consensus"
    assert "RIBBON_CONSENSUS_MIN_FIBERS" in eng, "small-sample guard constant gone"
    # _closure_km_for_fiber must consult the consensus BEFORE the legacy
    # mode-based per-ribbon dict
    fn = eng.split("def _closure_km_for_fiber", 1)[1].split("\ndef ", 1)[0]
    assert "ribbon_positions" in fn and "position_km_refined_by_ribbon" in fn
    assert fn.index("ribbon_positions") < fn.index("position_km_refined_by_ribbon"), \
        "_closure_km_for_fiber must prefer the consensus anchor"


def test_source_lock_standalone_scan_anchors_per_ribbon():
    eng = (SPLICEREPORT_DIR / "splicereportmatchexfo.py").read_text(encoding="utf-8")
    fn = eng.split("def scan_a_standalone_events", 1)[1].split("\ndef ", 1)[0]
    assert "_closure_km_for_fiber(best_sp, fnum)" in fn, \
        ("scan_a_standalone_events must anchor its per-fiber primary-splice "
         "search at the fiber's ribbon consensus position, not the global center")

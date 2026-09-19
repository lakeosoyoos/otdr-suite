"""analyze_all searches for a fiber's A event at THAT FIBER'S closure, not at
the raw 1 km-bin mean (ZAYO BETA 432, ORPVL <-> ZYO-OR-DES-0048, 2026-09-18).

`sp_km = sp['position_km']` is discover_splices' PRE-REFINEMENT cluster mean,
`round(float(np.mean(kms)), 2)`.  On splice 2 of that span the clusterer swept
the 20.6 km bend population in with the 21.87 km closure, so the mean landed at
21.50 while the refiner put the closure at 21.8559 and ribbon 18's own rung at
21.8610 -- a 356 m error.

Fiber 212 has two A events in the window: an isolated 21.3161 (+0.0636, which
only a handful of fibers on the cable carry) and its real 21.7717 (-0.0466).
Nearest-to-21.50 picks the isolated one, 184 m away, over the real one at
272 m.  Paired with B's +0.2042 the cell printed .134; FastReporter prints
.078.  The two lines that compute the neighbour tolerance already used the
REFINED positions, so the block disagreed with itself.

_closure_km_for_fiber is the per-ribbon consensus PR #27 already computes and
six other call sites already consume.  Engine runs in a clean subprocess.
"""
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"


def _run(body, engine_dir=SPLICEREPORT_DIR):
    body = "".join(textwrap.dedent(part) for part in body.split("\x00"))
    header = ("import sys\n"
              f"sys.path.insert(0, {str(engine_dir)!r})\n"
              "import splicereportmatchexfo as E\n")
    p = subprocess.run([sys.executable, "-c", header + body],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


# ZAYO geometry, reduced to the one closure that shows it.  Fiber 212 sits in
# ribbon 18 ((212-1)//12 + 1).  Filler fibers give the column a population.
_SETUP = """
    SPAN = 55.0322
    IMPOSTOR, IMPOSTOR_L = 21.3161, 0.0636      # isolated, only a few fibers
    REAL, REAL_L         = 21.7717, -0.0466     # this fiber's own event
    B_KM, B_L            = 21.8572, 0.2042      # B sees it here, and clearly

    def _ev(km, loss, end=False):
        return {'dist_km': km, 'splice_loss': loss, 'is_end': end,
                'type': '1E' if end else '0F', 'reflection': 0.0}

    def build(rungs=None, refined=None):
        sp = {'position_km': 21.50, 'column_kind': 'splice', 'count': 300}
        if refined is not None:
            sp['position_km_refined'] = refined
        if rungs is not None:
            sp['ribbon_positions'] = dict(rungs)
            sp['_ribbon_rung_src'] = {k: 'own' for k in rungs}
        fa, fb = {}, {}
        fa[212] = {'events': [_ev(IMPOSTOR, IMPOSTOR_L), _ev(REAL, REAL_L),
                              _ev(SPAN, 0.0, True)]}
        fb[212] = {'events': [_ev(SPAN - B_KM, B_L), _ev(SPAN, 0.0, True)]}
        for f in list(range(205, 212)) + list(range(213, 217)):
            fa[f] = {'events': [_ev(REAL, -0.03), _ev(SPAN, 0.0, True)]}
            fb[f] = {'events': [_ev(SPAN - B_KM, 0.06), _ev(SPAN, 0.0, True)]}
        return fa, fb, [sp]
"""


def test_the_ribbons_own_rung_picks_the_real_event():
    # 0.05 so the cell is recorded at all -- analyze_all only keeps what
    # clears the threshold, and the whole point is that the right answer
    # (0.0788) does NOT clear the 0.100 gate this span is graded at.
    _run(_SETUP + """\x00
    fa, fb, splices = build(rungs={18: 21.8610}, refined=21.8559)
    cell = E.analyze_all(fa, fb, splices, 0.05).get((212, 0))
    assert cell is not None, 'no cell for fiber 212'
    assert abs(cell['a_loss'] - REAL_L) < 1e-9, cell['a_loss']
    assert abs(cell['b_loss'] - B_L) < 1e-9, cell['b_loss']
    assert abs(cell['bidir_loss'] - 0.0788) < 5e-4, cell['bidir_loss']
    print('OK')
    """)


def test_the_false_flag_is_gone_at_the_gate_the_span_is_graded_at():
    """ZAYO runs the 0.100 Zayo profile.  With the raw mean the cell printed
    .134 and flagged; with the fiber's own closure it is 0.0788 and correctly
    never reaches the grid."""
    _run(_SETUP + """\x00
    fa, fb, splices = build(rungs={18: 21.8610}, refined=21.8559)
    assert E.analyze_all(fa, fb, splices, 0.10).get((212, 0)) is None
    print('OK')
    """)


def test_without_a_rung_the_refined_centre_still_beats_the_raw_mean():
    """Most spans never publish a rung for every ribbon.  The refined centre is
    the next preference and it fixes the same class of mistake."""
    _run(_SETUP + """\x00
    fa, fb, splices = build(rungs=None, refined=21.8559)
    cell = E.analyze_all(fa, fb, splices, 0.05).get((212, 0))
    assert abs(cell['a_loss'] - REAL_L) < 1e-9, cell['a_loss']
    print('OK')
    """)


def test_a_fiber_with_one_candidate_is_untouched():
    """45,574 of 45,723 (fiber, closure) pairs on the five spans have exactly
    one candidate event in the window, so the search centre cannot matter."""
    _run(_SETUP + """\x00
    fa, fb, splices = build(rungs={18: 21.8610}, refined=21.8559)
    fa[212]['events'] = [_ev(REAL, REAL_L), _ev(SPAN, 0.0, True)]   # drop the impostor
    cell = E.analyze_all(fa, fb, splices, 0.05).get((212, 0))
    assert abs(cell['a_loss'] - REAL_L) < 1e-9, cell['a_loss']
    assert abs(cell['bidir_loss'] - 0.0788) < 5e-4, cell['bidir_loss']
    print('OK')
    """)


def test_the_search_centre_is_the_per_fiber_one():
    """Pin the wiring itself: the A-event search must not read the raw mean."""
    src = (SPLICEREPORT_DIR / 'splicereportmatchexfo.py').read_text(encoding='utf-8')
    block = src.split('# ── Find A event near this splice', 1)[1].split('if ea is None:', 1)[0]
    assert '_closure_km_for_fiber(sp, fnum)' in block
    assert 'search_km_a' in block
    assert '- sp_km' not in block, 'the A-event search still reads the raw bin mean'

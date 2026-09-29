"""A 2500 ns route: no bends made out of a closure's own readings.

A 94 km route, two 132-fiber cables, shot at 2500 ns (a 255 m pulse in the
fiber) with a launch reel of about 1 km at each end and no receive reel.
Suite printed 16 and 70 flagged cells where FastReporter flags 0 and 2, and
FastReporter's table has no row of its own at any of them.  Both defects are a
position radius tighter than the pulse can resolve.

1. A closure dropped as a bend zone.  That far out the A direction stores only
   the bigger events, so its losses at one closure looked like a bend (no
   gainers, median 0.102 dB).  _b_refutes_bend_verdict is there for exactly
   that case: it asks whether B's population at the closure gains.  But it
   matched B events within 75 m of the mirror, and at 2500 ns B places each
   event about 130 m from where A does, so it found 12 of 132 fibers, short of
   the 33 it needs, and never saw that 25% of them gain.  The closure became a
   bend column and every one of its 62 members was flagged, gainers and
   0.004 dB readings included.  The window is now floored at the pulse smear,
   the same floor every other position radius already has.

2. A closure's second population read as a bend.  250 to 330 m before another
   closure the A readings split into a second population as big as the
   closure's own: a second splice point the pulse cannot measure apart, one
   event per fiber in the OTDR's table.  The consensus pass and the
   standalone-A pass called those fibers' readings bends (.091 to .150).  Each
   was, fiber for fiber, the one FastReporter row there and the same reading
   the closure column already held.  A bend candidate within two smears of a
   closure, where a closure-sized population stores an event, is now that
   closure's reading (CLOSURE_SIBLING_POP_RATIO).  A few fibers beside a busy
   closure (the boss-confirmed ground-truth bends, 135 and 222 m from their
   splices at the same pulse) still print.

Fixture longpulse/: 31 fibers of the second cable, both directions, every
identifier scrubbed (file names, fiber id, locations, cable, operators,
customer, company, job, and the OTDR's supplier field) and the events, EXFO
records and traces checked unchanged.  22 of them carry the second population's A reading, 9 the
closure's own, 22 a B event within a smear of the dropped closure's mirror.

Engine tests run in a clean subprocess (3-engine sor_reader isolation).
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

from conftest import FIXTURE_DIR, REPO_ROOT

FX = FIXTURE_DIR / "longpulse"
SPLICEREPORT_DIR = REPO_ROOT / "splicereport"

# The runner's load: files, declared-span trim, Pass 0, and the run's pulse
# smear (discover_splices sets it in a real run).  CLOSURES are the cable's
# 12 closure centers from the full 132-fiber run, the 45.65 km one included.
_LOAD = """
def load():
    fa, fb = E.load_all(os.path.join(FX, 'A'), os.path.join(FX, 'B'))
    for d in (fa, fb):
        for r in d.values():
            r['events'] = E._trim_to_declared_span(r['events'])
    reels = E.reciprocal_reels(fa, fb)
    for di, d in enumerate((fa, fb)):
        reel, recv, absent, tol = reels[di]
        endmed = E._direction_end_median_km(d)
        for r in d.values():
            r['_raw_events'] = r['events']
            r['_span_side'] = 'a' if di == 0 else 'b'
            r['_launch_reel_km'] = reel
            r['_receive_reel_km'] = recv
            r['_launch_reel_absent'] = absent
            r['_launch_reel_tol_km'] = tol
            r['_trace_offset_km'] = E._trace_frame_offset_km(
                r, r['events'], reel, absent, tol)
            r['events'] = E._normalize_untrimmed_events(
                r['events'], reel, recv, absent, tol, endmed)
    E._set_run_pulse_smear(fa)
    return fa, fb


CLOSURES = [5.797, 13.3275, 21.169, 29.2298, 37.42565, 45.647, 52.7543,
            53.6618, 61.8806, 69.9414, 78.1092, 86.0425]
SPAN = 94.06


def splices():
    return [{'position_km': c, 'position_km_refined': c,
             'column_kind': 'splice'} for c in CLOSURES]
"""


def _engine(body):
    header = ("import sys, os\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import numpy as np\n"
              "import splicereportmatchexfo as E\n"
              f"FX = {str(FX)!r}\n")
    p = subprocess.run([sys.executable, "-c",
                        header + _LOAD + textwrap.dedent(body)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


def test_premise_a_long_pulse_places_b_130_m_from_a():
    """The shot: 2500 ns, a 255 m smear.  At the 45.65 km closure B's stored
    positions sit about 130 m from A's, so a 75 m window sees few of them."""
    _engine("""
        fa, fb = load()
        assert len(fa) == len(fb) == 31, (len(fa), len(fb))
        assert abs(E._RUN_PULSE_SMEAR_KM - 0.2553) < 1e-4, E._RUN_PULSE_SMEAR_KM
        a = [e['dist_km'] for r in fa.values() for e in r['events']
             if not e['is_end'] and abs(e['dist_km'] - 45.647) < 0.3]
        b = []
        for r in fb.values():
            eof = next(e['dist_km'] for e in r['events'] if e['is_end'])
            b += [eof - e['dist_km'] for e in r['events']
                  if not e['is_end'] and abs(eof - e['dist_km'] - 45.647) < 0.3]
        gap_m = (np.median(a) - np.median(b)) * 1000.0
        assert 100.0 < gap_m < 160.0, gap_m
        print('OK')
    """)


def test_b_refutes_the_bend_verdict_at_a_long_pulse():
    """THE symptom-1 regression: B's population at the closure is found once
    the window is a pulse wide, and it gains, so the closure is a splice."""
    _engine("""
        fa, fb = load()
        sp = {'position_km': 45.647, 'position_km_refined': 45.647}
        refutes, why = E._b_refutes_bend_verdict(sp, fb)
        assert refutes, why
        n = int(why.split('/')[0])
        gain = int(why.split('% gainers')[0].rsplit(' ', 1)[1])
        assert n >= E.MIN_POP_SPLICE and gain >= 10, why
        print('OK')
    """)


def test_b_refutation_window_is_unchanged_at_a_short_pulse():
    """Below about 730 ns the smear is under CLOSURE_MATCH_KM and the window
    is the 75 m it always was: the same B fibers fall short of the population."""
    _engine("""
        fa, fb = load()
        E._RUN_PULSE_SMEAR_KM = 0.051            # 500 ns
        sp = {'position_km': 45.647, 'position_km_refined': 45.647}
        assert E._b_refutes_bend_verdict(sp, fb) == (False, '')
        print('OK')
    """)


def test_a_closures_second_population_is_not_a_bend():
    """THE symptom-2 regression, through the three passes that grade the
    fibers: no bend anywhere in the second population, 250 to 330 m before the
    61.88 km closure.  The one reading there that clears the report gate is
    still printed, as a reburn in the closure column (FastReporter flags it)."""
    _engine("""
        fa, fb = load()
        sps = splices()
        res = E.analyze_all(fa, fb, sps, E.REBURN_THRESHOLD)
        a_st = E.scan_a_standalone_events(fa, sps, res, SPAN, fibers_b=fb)
        cons = E.flag_consensus_bends({**res, **a_st}, fa, fb, sps, SPAN)
        cells = {**res, **a_st, **cons}
        bends = sorted((r['fiber'], round(r['bidir_dist'], 3))
                       for r in cells.values()
                       if r.get('is_bend') and r.get('is_flagged')
                       and 61.3 < r['bidir_dist'] < 61.9)
        assert bends == [], bends
        f23 = res.get((23, CLOSURES.index(61.8806)))
        assert f23 and f23['is_flagged'] and not f23['is_bend'], f23
        assert E._format_loss(f23['bidir_loss']) == '.192', f23
        print('OK')
    """)


def test_second_population_counts():
    """The numbers behind the verdict: at least half as many fibers store the
    second population's reading as store the closure's (the full cable: 34
    against 37), and enough of them for discovery to have found a closure
    there on its own."""
    _engine("""
        fa, fb = load()
        here, there, col = E._closure_sibling_counts(fa, 61.62, CLOSURES)
        assert col == 61.8806, col
        assert here >= E.MIN_POP_SPLICE, (here, there)
        assert here >= E.CLOSURE_SIBLING_POP_RATIO * there, (here, there)
        assert E._is_closure_sibling(fa, 61.62, CLOSURES)
        # outside two smears of any closure the question does not arise,
        # nor within CLOSURE_MATCH_KM of one (that reading is at the column)
        assert E._closure_sibling_counts(fa, 57.70, CLOSURES) is None
        assert E._closure_sibling_counts(fa, 61.84, CLOSURES) is None
        # nor without a pulse
        E._RUN_PULSE_SMEAR_KM = 0.0
        assert E._closure_sibling_counts(fa, 61.62, CLOSURES) is None
        print('OK')
    """)


# A synthetic 2500 ns cable: every fiber spliced at every closure, B mirrored.
# `moved` maps fiber -> (km, A loss, B loss): that fiber's one event at the
# 61.88 closure is read at `km` instead (a single event per fiber, as on the
# field cases).
_SYNTH = """
SPAN = 94.06
CLOSURES = [5.80, 13.33, 21.17, 29.23, 37.43, 45.64, 52.75, 53.66,
            61.88, 69.94, 78.11, 86.04]


def cable(n, moved):
    fa, fb = {}, {}
    for f in range(1, n + 1):
        ea, eb = [], []
        for c in CLOSURES:
            km, a_loss, b_loss = (moved[f] if (f in moved and c == 61.88)
                                  else (c, 0.03, 0.03))
            ea.append({'dist_km': km, 'splice_loss': a_loss, 'is_end': False,
                       'is_reflective': False, 'type': '0F9999LS'})
            eb.append({'dist_km': round(SPAN - km + 0.01, 4),
                       'splice_loss': b_loss, 'is_end': False,
                       'is_reflective': False, 'type': '0F9999LS'})
        for ev in (ea, eb):
            ev.append({'dist_km': SPAN, 'splice_loss': 0.0, 'is_end': True,
                       'is_reflective': True, 'type': '1E9999LS'})
            ev.sort(key=lambda e: e['dist_km'])
        fa[f] = {'events': ea}
        fb[f] = {'events': eb}
    sps = [{'position_km': c, 'position_km_refined': c,
            'column_kind': 'splice'} for c in CLOSURES]
    return fa, fb, sps


def bend_fibers(n, moved):
    fa, fb, sps = cable(n, moved)
    new = E.flag_consensus_bends({}, fa, fb, sps, SPAN)
    return sorted(r['fiber'] for r in new.values() if r['is_bend'])
"""


def _synth(body):
    header = ("import sys\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import splicereportmatchexfo as E\n"
              "E._RUN_PULSE_SMEAR_KM = 0.2553          # 2500 ns\n")
    p = subprocess.run([sys.executable, "-c",
                        header + _SYNTH + textwrap.dedent(body)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


def test_a_few_fibers_beside_a_busy_closure_are_still_bends():
    """The ground-truth shape: 4 of 60 fibers read their one event 222 m past
    the closure, the other 56 read it at the closure.  A bend zone, flagged."""
    _synth("""
        moved = {f: (62.102, 0.10, 0.12) for f in (5, 17, 33, 48)}
        assert bend_fibers(60, moved) == [5, 17, 33, 48]
        print('OK')
    """)


def test_half_the_cable_before_the_closure_is_the_closure():
    """The field shape: 30 of 60 fibers read their one event 280 m before the
    closure.  That is the closure's second population: no bends.  The same
    population read with a 500 ns pulse is outside two smears, and the rule
    leaves it to the other gates exactly as before."""
    _synth("""
        moved = {f: (61.60, 0.12, 0.10) for f in range(1, 31)}
        assert bend_fibers(60, moved) == []
        E._RUN_PULSE_SMEAR_KM = 0.051
        assert bend_fibers(60, moved) == list(range(1, 31))
        print('OK')
    """)

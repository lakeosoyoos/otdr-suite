"""Viewer stacked mode: the mirror frame has to be MEASURED, not guessed.

Field report (the boss, screenshot of the Viewer's bidi page, fibers 1-12
loaded in A+B): "Viewer doesn't line up the two directions on x axis."  His own
event table gave the size and the sign of it -- every B row sat about a
kilometre to the RIGHT of its A partner:

    A 9.6186 -> B 10.6230   (+1.004)      A 21.3353 -> B 22.3626   (+1.027)
    A 15.3928 -> B 16.4049  (+1.012)      A 27.1528 -> B 28.1572   (+1.004)

`mirrorOriginKm` mirrors B about `far_conn_km + launch_a_km`.  Both of those
are INFERRED from the event pattern, and `_trace_launch_km` calls the first
reflective event under LAUNCH_MAX_KM a launch reel.  A fiber whose first
genuine event is a panel or a splice at about a kilometre therefore reports a
launch reel that does not exist, and the fabricated length slides the whole B
trace right by exactly its length.

GROUND TRUTH -- FastReporter 3, driven directly (2026-08-27), on
`~/Downloads/DefuniakFR/{A,B}` fiber 1.  A 2.042 km fiber with no reels at all:
its Spans by Distance dialog reads Launch 0.0000 / Span 2.0414 / Receive
0.0000, so FR mirrors B about B's own end of fiber.  Selecting the A/B pair
gives ONE event table in which both directions share the same four columns:

    Event 1 0.0000 | Event 2 1.0048 | Event 3 1.0359 | Event 4 2.0417
    A->B   ---   -0.333    0.616    ---
    B->A   ---    0.855   -0.216    ---
    Average       0.261    0.200

which is the whole answer: FR pairs A's event 2 with B's event 3, i.e. it
mirrors B about 2.0421 = B's own end.  The Viewer's reel rule mirrored about
2.0420 + 1.0049 = 3.0469 and drew every B event 1.005 km right of FR's column,
while A matched FR to 0.2 m.  Same signature the boss reported.

THE FIX, and why it is shaped this way: FR never guesses because it has a span
definition to read and we do not.  What we do have is what FR's rule amounts
to -- both directions see the SAME events -- so for a matched pair the origin M
satisfies M = a_km + b_km at every shared event.  Correct pairings all vote for
one M; wrong pairings scatter.  The densest cluster of those sums IS the frame.

The correction is applied only when it exceeds MIRROR_SNAP_KM, so a span whose
reels really are what the reel rule thinks they are does not move at all.
Measured over 12 fibers per span:

    DefuniakFR (no reels, first event @1.005)   -1004.9 m   -> corrected
    Tucu <-> Romero      (1.00 launch, 1.05 rx)     +5.1 m   -> left alone
    Tucumcari <-> Santa Rosa                       +25.5 m   -> left alone
    Winterhaven <-> Niland (no reels)               +0.0 m   -> left alone
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..'))
VIEWER = os.path.join(ROOT, 'viewer', 'viewer.html')

SRC = open(VIEWER, encoding='utf-8').read()


def _const(name):
    """Read a constant out of viewer.html so this test tracks the source.

    Re-implementing the numbers here would let the two drift apart silently,
    which is the failure this whole file exists to catch.
    """
    m = re.search(r'^const\s+%s\s*=\s*([\d.]+)\s*;' % name, SRC, re.M)
    assert m, 'viewer.html no longer defines %s' % name
    return float(m.group(1))


VOTE_TOL_KM = _const('MIRROR_VOTE_TOL_KM')
MIN_VOTES = int(_const('MIRROR_MIN_VOTES'))
SNAP_KM = _const('MIRROR_SNAP_KM')


# ── Python mirror of measuredOriginKm() / refreshMirrorFrame() ────────────
# There is no Node, jsdom or Playwright on this machine, so the JS is mirrored
# here and the wiring is asserted against the source text below.

def measured_origin_km(a_events, b_events):
    if len(a_events) < MIN_VOTES or len(b_events) < MIN_VOTES:
        return None
    sums = sorted(x + y for x in a_events for y in b_events)
    w = 2 * VOTE_TOL_KM
    best_n = lo = hi = 0
    j = 0
    for i in range(len(sums)):
        while j < len(sums) and sums[j] - sums[i] <= w:
            j += 1
        if j - i > best_n:
            best_n, lo, hi = j - i, i, j
    if best_n < MIN_VOTES:
        return None
    j = 0
    for i in range(len(sums)):
        while j < len(sums) and sums[j] - sums[i] <= w:
            j += 1
        disjoint = sums[i] > sums[hi - 1] + w or sums[j - 1] < sums[lo] - w
        if disjoint and j - i >= best_n:
            return None                      # two frames tie: refuse to pick
    win = sums[lo:hi]
    return win[len(win) // 2]


def mirror_delta_km(pairs, reel_origin_of):
    """pairs: [(a_events, b_events, b_key)] -> km to add to the reel origin."""
    deltas = []
    for a_ev, b_ev, key in pairs:
        m = measured_origin_km(a_ev, b_ev)
        if m is not None:
            deltas.append(m - reel_origin_of(key))
    if not deltas:
        return 0.0
    deltas.sort()
    d = deltas[len(deltas) // 2]
    return 0.0 if abs(d) <= SNAP_KM else d


# ── The real span FastReporter was driven on ─────────────────────────────
# Event distances read from ~/Downloads/DefuniakFR/{A,B} fiber 1 through the
# viewer's own reader.  Four events each way: OTDR port, a connector at ~1.005,
# another 31 m later, end of fiber.
DEF_A = [0.0000, 1.0049, 1.0361, 2.0415]
DEF_B = [0.0000, 1.0062, 1.0373, 2.0420]
DEF_FAR_CONN_B = 2.0420          # B carries no receive reel, so far conn == end
DEF_FABRICATED_LAUNCH_A = 1.0049  # what _trace_launch_km reports, wrongly

# FastReporter's own event-table columns for that pair, keyed by A's event
# number.  B's event n shares the column of A's event (5 - n) because FR
# mirrors B.
FR_COLUMNS = {1: 0.0000, 2: 1.0048, 3: 1.0359, 4: 2.0417}


def test_the_reel_rule_reproduces_the_reported_misalignment():
    """The bug, pinned: B a full reel-length right of where FR puts it."""
    origin = DEF_FAR_CONN_B + DEF_FABRICATED_LAUNCH_A
    errors = [(origin - km) - FR_COLUMNS[4 - i]
              for i, km in enumerate(DEF_B)]
    assert min(errors) > 1.0, 'every B event should be over a km right of FR'
    assert max(errors) < 1.01
    # ...while A, drawn untouched, is already on FR's columns.
    for i, km in enumerate(DEF_A, start=1):
        assert abs(km - FR_COLUMNS[i]) < 0.001


def test_the_measured_frame_lands_on_fastreporters_own_columns():
    """The fix, against FastReporter's numbers rather than our own."""
    reel_origin = DEF_FAR_CONN_B + DEF_FABRICATED_LAUNCH_A
    d = mirror_delta_km([(DEF_A, DEF_B, 'b')], lambda _k: reel_origin)
    assert abs(d + 1.0049) < 0.002, 'should undo the fabricated launch reel'
    origin = reel_origin + d
    worst = max(abs((origin - km) - FR_COLUMNS[4 - i])
                for i, km in enumerate(DEF_B))
    assert worst < 0.001, 'B is %.1f m off FastReporter' % (worst * 1000)


def test_a_span_whose_reels_are_real_is_left_exactly_alone():
    """Tucu <-> Romero: a 1.0044 launch reel and a 1.0453 receive reel that
    both genuinely exist.  The measured frame agrees with the reel frame to
    5 m, which is inside MIRROR_SNAP_KM, so nothing moves and every span that
    draws correctly today keeps drawing identically."""
    launch_a, far_conn_b = 1.0044, 96.1246
    reel_origin = far_conn_b + launch_a
    # Cable positions both directions see, in each one's own raw frame.
    cable = [4.1758, 10.9213, 17.6311, 25.3657, 35.9199, 41.9363]
    a_ev = [0.0, launch_a] + cable + [97.1801]
    b_ev = [0.0, 1.0095] + sorted(reel_origin - p for p in cable) + [97.1699]
    d = mirror_delta_km([(a_ev, b_ev, 'b')], lambda _k: reel_origin)
    assert d == 0.0, 'a correct reel frame must not be nudged'


def test_it_abstains_when_the_two_directions_do_not_agree():
    """No shared events means no evidence, and the reel rule stands.

    Silence is the right answer here: a short shot, a broken fiber or a
    mismatched pair would otherwise get a frame invented for it, which is the
    same class of quiet wrongness this fix exists to remove.
    """
    assert measured_origin_km([0.0, 1.0, 2.0], [0.5]) is None
    assert mirror_delta_km([([0.0, 1.0, 2.0], [0.5], 'b')], lambda _k: 3.0) == 0.0


def test_a_tie_between_two_frames_is_refused_rather_than_broken():
    """Two frames with equal support get no answer at all.

    Synthetic, deliberately: evenly spaced events make the shifted pairings
    cluster as hard as the correct one, and here 2.0 and 3.0 each collect three
    votes.  Real spans do not do this -- the window is 20 m and splice spacing
    varies by hundreds -- but the guard is what stops a coin-toss frame from
    being drawn as if it were measured.
    """
    assert measured_origin_km([0.0, 1.0, 2.0], [0.0, 1.0, 2.0, 3.0]) is None
    # An evenly spaced span still resolves when one frame genuinely leads: the
    # correct origin collects more pairings than either neighbour.
    assert measured_origin_km([0.0, 1.0, 2.0, 3.0], [0.0, 1.0, 2.0, 3.0]) == 3.0


# ── The wiring, asserted against the source ──────────────────────────────

def test_the_measured_frame_is_actually_wired_into_the_mirror():
    assert 'function reelOriginKm(t)' in SRC, 'reel rule must stay reachable'
    m = re.search(r'function mirrorOriginKm\(t\)\s*\{[^}]*\}', SRC)
    assert m and 'reelOriginKm(t) + gMirrorDelta' in m.group(0)


def test_the_frame_is_recomputed_when_the_trace_set_changes():
    """refreshMirrorFrame has to run off the draw path but on every change.

    renderChips() is the one choke point every add, remove, clear and
    visibility toggle already goes through; dispKm() runs per sample and must
    stay O(1), so the measurement cannot live there.
    """
    chips = SRC[SRC.index('function renderChips()'):][:400]
    assert 'refreshMirrorFrame();' in chips
    stack = SRC[SRC.index("getElementById('cb-stack')"):][:400]
    assert 'refreshMirrorFrame();' in stack, 'un/re-stacking changes the frame'
    disp = SRC[SRC.index('function dispKm('):][:200]
    assert 'refreshMirrorFrame' not in disp, 'must not measure inside the draw path'


def test_the_tech_is_told_when_the_frame_was_corrected():
    """A silent 1 km correction is as bad as a silent 1 km error."""
    assert 'gMirrorNote' in SRC
    readout = SRC[SRC.index('function setReadout('):][:300]
    assert 'gMirrorNote' in readout

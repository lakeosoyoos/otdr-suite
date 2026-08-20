"""A NON-REFLECTIVE launch connector, recognised by reel-length consensus.

THE BUG.  `_untrimmed_launch_offset_km` / `_normalize_untrimmed_events` decide
a trace is untrimmed by looking for the launch pattern — the OTDR port at
time-of-travel 0, then the launch reel's far connector inside
LAUNCH_FIBER_MAX — and they required that second event to be REFLECTIVE.
Usually it is: it is a mated bulkhead.  But the OTDR only tables a reflectance
when its own peak estimator fires, and WSC<->SUI fiber 242's Suisun launch
connector is tabled as a plain non-reflective `0F9999LS` @1.0197 km carrying
0.263 dB with reflectance 0.000.  So the pattern did not match, the offset came
back 0.0, the frame was never shifted, and F242's box connector landed 933 m
off the 63.9701 km ILA column, was swept into Splice 12, and printed there as
`242 .238` — the connector's own loss masquerading as a splice.  It was the
only flag on that span the field team's sheet did not carry.

Fiber 34 is the same symptom from a different cause — a `2F` SATURATED
reflective connector the reader filed as non-reflective — and is fixed
separately (see test_saturated_reflective.py).  F242's connector genuinely is
not reflective, so it needs a different rule.

WHY DROPPING THE REFLECTIVITY TEST ALONE IS NOT SAFE.  On a span the tech
already trimmed there is no launch event at all: event 0 is the launch
connector at 0.000 and event 1 is the first real SPLICE.  Measured traps, both
fixtured here:

    TUL<->BAR   BARTUL  4 fibers with a non-reflective event 1 at
                        0.0765 / 0.1734 / 0.1937 km   (fixtures/launchreel)
                TULBAR  282 fibers at ~2.09 km
    HOWLAN      HOWLAN  649 fibers at 1.65-1.90 km    (fixtures/refl)

A naive relaxation mis-frames every one of them by 77 m to 2.1 km.

THE RULE: REEL-LENGTH CONSENSUS.  The launch reel is a physical spool with one
length, shared by every fiber shot through it in that direction.  So the
POPULATION says where the connector is.  `launch_reel_consensus_km` reads the
median off the fibers whose launch connector the OTDR did table as reflective,
and returns it only when they are a majority of the direction
(LAUNCH_REEL_MIN_FRAC) and there are enough of them (LAUNCH_REEL_MIN_N).  A
lone non-reflective candidate is then accepted only if it lands on that
consensus within LAUNCH_REEL_TOL_KM.

Measured over 17 directions / ~9,900 fibers, the two populations do not
overlap: a direction shows a reflective launch connector on 99.5-100% of its
fibers, or on 0% of them.  Every trap above lives on a 0% direction, so the
consensus is None there and nothing is rescued — note this is NOT a tightness
argument: TULBAR's splice cluster is itself tight (max deviation 48 m).  It is
the reflective-consensus requirement that separates them.

Fixtures are real, unmodified acquisitions:
  launchreel/SUIWSC0242.sor       the 0F launch connector (task #110 08-18 set)
  launchreel/BARTUL0{63,72,95,159}_1550.sor   the four TUL<->BAR traps
  launchreel/BARTUL00{1..4}_1550.sor          ordinary BARTUL fibers, so the
                                              "this direction has no reel
                                              consensus" assertion is made
                                              against a population of 8 and is
                                              not a short-circuit on count
  refl/HOWLAN19{2,4}_1550.sor     two of HOWLAN's 649 traps (already committed)
  splice_A/ (24x ELMMIL), splice_B/ (24x MILELM)   real reflective-launch
                                              populations, already committed
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, 'splicereport'))

import sor_reader324802a as R                    # noqa: E402
import splicereportmatchexfo as E                # noqa: E402

FIX = os.path.join(HERE, 'fixtures')
REEL = os.path.join(FIX, 'launchreel')

F242 = os.path.join(REEL, 'SUIWSC0242.sor')
BAR_TRAPS = [os.path.join(REEL, f'BARTUL{n}_1550.sor')
             for n in ('063', '072', '095', '159')]
BAR_ALL = sorted(os.path.join(REEL, f) for f in os.listdir(REEL)
                 if f.startswith('BARTUL'))
HOW_TRAPS = [os.path.join(FIX, 'refl', f'HOWLAN{n}_1550.sor')
             for n in ('192', '194')]

# The Suisun long-shot B direction's own consensus, measured over its 1,151
# reflective launch connectors — every one of them reads exactly 1.0044 km
# (max deviation 0.0 m).  Corroborated in-repo by the two committed Suisun
# fixtures, see test_suisun_consensus_value_is_corroborated_by_committed_files.
SUI_REEL_KM = 1.0044


def _events(path):
    d = R.parse_sor_full(path, trim=False)
    assert d, path
    return d['events']


def _records(paths):
    return [R.parse_sor_full(p, trim=False) for p in paths]


# ── the raw bytes, so the tests cannot drift off the real files ───────────

def test_f242_fixture_carries_the_real_0F_connector():
    """Pin the byte pattern.  If this fixture is ever swapped for a file whose
    launch connector IS reflective, say so instead of passing hollow."""
    raw = open(F242, 'rb').read()
    assert b'2F9999LS' not in raw, 'F242 is the NON-reflective case, not the 2F one'
    ev = _events(F242)
    port, conn = ev[0], ev[1]
    assert port['type'] == '1F9999LS' and port['dist_km'] == 0.0
    assert port['time_of_travel'] == 0
    assert conn['type'] == '0F9999LS', conn['type']
    assert conn['is_reflective'] is False
    assert conn['reflection'] == 0.0
    assert conn['dist_km'] == pytest.approx(1.0197, abs=1e-4)
    assert conn['splice_loss'] == pytest.approx(0.263, abs=1e-3), (
        'the 0.263 dB that printed as `242 .238` at Splice 12'
    )


def test_tulbar_trap_fixtures_carry_the_real_near_zero_events():
    """The four TUL<->BAR fibers the rule must NOT rescue."""
    got = []
    for p in BAR_TRAPS:
        ev = _events(p)
        assert ev[0]['type'] == '1F9999LS' and ev[0]['dist_km'] == 0.0
        assert ev[1]['is_reflective'] is False and ev[1]['type'] == '0F9999LS'
        got.append(round(ev[1]['dist_km'], 4))
    assert sorted(got) == [0.0765, 0.0765, 0.1734, 0.1937], got


def test_howlan_trap_fixtures_are_non_reflective_splices():
    for p in HOW_TRAPS:
        ev = _events(p)
        assert ev[1]['is_reflective'] is False
        assert 1.6 < ev[1]['dist_km'] < 1.9, ev[1]['dist_km']


# ── the consensus itself ──────────────────────────────────────────────────

def test_consensus_reads_the_reel_off_a_real_reflective_population():
    """Two committed 24-fiber directions, each shot on its own reel."""
    assert E.launch_reel_consensus_km(
        _records(sorted(
            os.path.join(FIX, 'splice_A', f)
            for f in os.listdir(os.path.join(FIX, 'splice_A'))
            if f.endswith('.sor')))) == pytest.approx(1.0019, abs=1e-4)
    assert E.launch_reel_consensus_km(
        _records(sorted(
            os.path.join(FIX, 'splice_B', f)
            for f in os.listdir(os.path.join(FIX, 'splice_B'))
            if f.endswith('.sor')))) == pytest.approx(1.0070, abs=1e-4)


def test_a_pretrimmed_direction_has_no_consensus():
    """THE gate that rejects every trap.  Eight real BARTUL fibers — at or
    above LAUNCH_REEL_MIN_N, so this is the fraction test firing, not a
    short-circuit on population size."""
    assert len(BAR_ALL) >= E.LAUNCH_REEL_MIN_N
    assert E.launch_reel_consensus_km(_records(BAR_ALL)) is None


def test_consensus_is_none_without_a_majority():
    """One reflective launch in a crowd of trimmed fibers is not a reel."""
    recs = _records(BAR_ALL) + _records(
        [os.path.join(FIX, 'splice_A', 'ELMMIL0001_1550.sor')])
    assert E.launch_reel_consensus_km(recs) is None


def test_consensus_survives_an_unreadable_record():
    """A record that cannot be read abstains; it never takes the whole
    direction's consensus down with it."""
    recs = _records(sorted(
        os.path.join(FIX, 'splice_A', f)
        for f in os.listdir(os.path.join(FIX, 'splice_A'))
        if f.endswith('.sor')))
    recs.append({'events': [{'nonsense': 1}, {'nonsense': 2}, {'nonsense': 3}]})
    assert E.launch_reel_consensus_km(recs) == pytest.approx(1.0019, abs=1e-4)


# ── THE regression: F242 is rescued, the traps are not ────────────────────

def test_f242_is_framed_when_its_direction_has_a_reel_consensus():
    """Fails on pristine main: there the offset is 0.0 whatever you pass."""
    ev = _events(F242)
    assert E._untrimmed_launch_offset_km(ev, SUI_REEL_KM) == pytest.approx(
        1.0197, abs=1e-4), (
        'F242 s non-reflective launch connector sits 15.3 m from its '
        'direction s reel consensus and must be recognised'
    )
    norm = E._normalize_untrimmed_events(list(ev), SUI_REEL_KM)
    assert norm is not ev, 'the frame must actually be shifted'
    assert norm[0]['dist_km'] == 0.0, 'launch connector becomes the origin'
    # 5.5932 km raw -> 4.5735 km in the cable frame
    assert norm[1]['dist_km'] == pytest.approx(4.5735, abs=1e-3)


@pytest.mark.parametrize('path', BAR_TRAPS + HOW_TRAPS)
def test_traps_are_never_framed(path):
    """INVARIANCE PIN — holds on pristine main AND on this branch, and is
    written with main's one-argument call so it can run on both.

    Main leaves these alone because the event is not reflective; this branch
    leaves them alone because their direction has no reel consensus.  Same
    answer, and it must stay that way."""
    ev = _events(path)
    assert E._untrimmed_launch_offset_km(ev) == 0.0
    assert E._normalize_untrimmed_events(ev) is ev      # no-op, same object


@pytest.mark.parametrize('path', BAR_TRAPS + HOW_TRAPS)
@pytest.mark.parametrize('reel_km', [1.0019, 1.0070, SUI_REEL_KM])
def test_traps_survive_every_real_reel_length_in_the_repo(path, reel_km):
    """The traps held against each reel length actually committed here.  They
    sit 711 m to 928 m away from the nearest of them."""
    ev = _events(path)
    assert E._untrimmed_launch_offset_km(ev, reel_km) == 0.0
    assert E._normalize_untrimmed_events(list(ev), reel_km) is not None


def test_traps_stay_out_by_a_wide_margin_not_a_hair():
    """How much room the tolerance actually has.  F242 is 15.3 m from its
    reel, inside a 25 m window; the CLOSEST trap in the repo sits 711 m from
    the nearest real reel length (HOWLAN194 at 1.7182 vs MILELM's 1.0070) —
    28x the tolerance.  This is not a threshold balanced on a knife edge."""
    f242_dev = abs(_events(F242)[1]['dist_km'] - SUI_REEL_KM) * 1000.0
    assert f242_dev == pytest.approx(15.3, abs=0.5)
    assert f242_dev < E.LAUNCH_REEL_TOL_KM * 1000.0

    reels = (1.0019, 1.0070, SUI_REEL_KM)
    worst = min(min(abs(_events(p)[1]['dist_km'] - r) for r in reels) * 1000.0
                for p in BAR_TRAPS + HOW_TRAPS)
    assert worst == pytest.approx(711.2, abs=1.0), worst
    assert worst > 25 * E.LAUNCH_REEL_TOL_KM * 1000.0


def test_the_tolerance_is_what_rejects_a_trap_not_something_incidental():
    """Move the consensus onto a trap's own position and it IS accepted.

    Without this the trap tests could be passing for a reason that has
    nothing to do with the rule under test (an event-type filter, a distance
    floor), and would keep passing if the rule were removed."""
    ev = _events(BAR_TRAPS[0])          # BARTUL063, non-reflective @0.0765
    assert E._untrimmed_launch_offset_km(ev, 0.0765) == pytest.approx(
        0.0765, abs=1e-4)
    assert E._untrimmed_launch_offset_km(
        ev, 0.0765 + 2 * E.LAUNCH_REEL_TOL_KM) == 0.0


# ── nothing about the reflective path may move ────────────────────────────

@pytest.mark.parametrize('name', ['ELMMIL0001_1550.sor', 'ELMMIL0002_1550.sor',
                                  'ELMMIL0003_1550.sor', 'ELMMIL0004_1550.sor'])
def test_reflective_launch_offset_is_unchanged(name):
    """INVARIANCE PIN — holds on pristine main AND on this branch, written
    with main's one-argument call so it can run on both.

    The reflective path is the whole existing world; none of it may move."""
    ev = _events(os.path.join(FIX, 'splice_A', name))
    assert E._untrimmed_launch_offset_km(ev) == pytest.approx(1.0019, abs=1e-4)
    norm = E._normalize_untrimmed_events(list(ev))
    assert norm[0]['dist_km'] == 0.0


@pytest.mark.parametrize('name', ['ELMMIL0001_1550.sor', 'ELMMIL0002_1550.sor',
                                  'ELMMIL0003_1550.sor', 'ELMMIL0004_1550.sor'])
def test_a_consensus_never_overrides_a_reflective_launch(name):
    """A reflective launch connector is accepted on its own evidence.  The
    consensus can neither move it nor veto it — not even a wrong one."""
    ev = _events(os.path.join(FIX, 'splice_A', name))
    base = E._untrimmed_launch_offset_km(ev)
    for reel_km in (None, 1.0019, 1.0070, SUI_REEL_KM, 2.5):
        assert E._untrimmed_launch_offset_km(ev, reel_km) == base
        norm = E._normalize_untrimmed_events(list(ev), reel_km)
        assert norm[0]['dist_km'] == 0.0


def test_suisun_consensus_value_is_corroborated_by_committed_files():
    """SUI_REEL_KM is not a magic number invented for this test: the two
    Suisun fixtures already in the repo (F34's saturated connector and F491's
    plain reflective one) both read it."""
    for name in ('SUIWSC0034.sor', 'SUIWSC0491.sor'):
        ev = _events(os.path.join(FIX, 'satrefl', name))
        assert ev[1]['is_reflective'] is True
        assert ev[1]['dist_km'] == pytest.approx(SUI_REEL_KM, abs=1e-4)


def test_default_argument_is_the_old_reflective_only_behaviour():
    """INVARIANCE PIN — holds on pristine main AND on this branch.

    Called with no consensus the helpers are bit-for-bit what main did, so
    every caller not yet threaded through keeps today's answer."""
    assert E._untrimmed_launch_offset_km(_events(F242)) == 0.0
    ev = _events(F242)
    assert E._normalize_untrimmed_events(ev) is ev      # no-op, same object


# ── the constants ─────────────────────────────────────────────────────────

def test_constants_are_where_the_measurements_put_them():
    assert E.LAUNCH_REEL_TOL_KM == 0.025
    assert E.LAUNCH_REEL_MIN_FRAC == 0.50
    assert E.LAUNCH_REEL_MIN_N == 8
    # Deliberately tighter than the viewer's plot-placement window: this one
    # decides whether an event IS the connector, and a real closure was
    # measured 32.5 m past the reel on WSCSUIsh0203.
    assert E.LAUNCH_REEL_TOL_KM < 0.0325

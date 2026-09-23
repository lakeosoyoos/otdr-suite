"""A single-ribbon uni job must still find its closures.

DFW->ILA 2026-09-21, 12 traces (F301-312): UNI_MIN_POP_SPLICE is 20 fibers in
a 1 km bin, which a 12-fiber job can never reach, so uni_discover_splices
nominated ZERO closures.  Every fiber's splice at all 15 closures then fell
through to the off-splice bucket and printed as "Possible bend/damage" -- the
boss's "Uni Report is reporting splices as bends".

With no validated closures the pre-break damage pass also loses its at-splice
exclusion (it takes `splice_centers`), so every closure ahead of a break became
a "damage zone" too -- the exact failure uni_prebreak_damage's docstring warns
about.  One floor, both symptoms.

The floor now scales DOWN with the job and never up, so a full cable is
untouched.

Engine tests run in a clean subprocess (3-engine sor_reader isolation).
"""
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"


def _run(body):
    header = ("import sys\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import splicereportmatchexfo as E\n")
    p = subprocess.run([sys.executable, "-c", header + textwrap.dedent(body)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


def test_any_job_that_can_reach_twenty_is_untouched():
    """Every job with at least 20 fibers keeps the full floor.  This is the
    whole safety argument: no report that worked before can move.  24 is in
    the list on purpose — the ELMMIL fixture is 24 fibers and its columns are
    pinned in test_fr_uni_grid."""
    _run("""
        for n in (20, 21, 24, 48, 80, 432, 864, 1152):
            got = E._uni_min_pop(n)
            assert got == E.UNI_MIN_POP_SPLICE, (n, got)
        print('OK')
    """)


def test_only_a_job_that_cannot_reach_twenty_scales_down():
    """Under 20 fibers the floor becomes reachable, never below the hard
    floor, and never zero on a degenerate job."""
    _run("""
        assert E._uni_min_pop(12) == 3, E._uni_min_pop(12)
        assert E._uni_min_pop(16) == 4, E._uni_min_pop(16)
        assert E._uni_min_pop(19) == 5, E._uni_min_pop(19)
        for n in (1, 2, 4, 8):
            assert E._uni_min_pop(n) == E.UNI_MIN_POP_SPLICE_FLOOR, n
        # A job of unknown size gets the strict floor, not the loose one.
        assert E._uni_min_pop(0) == E.UNI_MIN_POP_SPLICE
        print('OK')
    """)


def test_twelve_fibers_find_their_closures():
    """Twelve synthetic fibers spliced at 3.2 / 6.9 / 10.4 km nominate three
    closures.  Under the old absolute floor of 20 they nominated none."""
    _run("""
        CLOSURES = (3.24, 6.88, 10.42)
        fibers = {}
        for f in range(1, 13):
            evs = [{'dist_km': 0.0, 'type': '1F9999LS', 'is_end': False,
                    'splice_loss': 0.0}]
            for i, km in enumerate(CLOSURES):
                evs.append({'dist_km': km + (f - 6) * 0.004,
                            'type': '0F9999LS', 'is_end': False,
                            'splice_loss': 0.05})
            evs.append({'dist_km': 14.0, 'type': '0E9999LS', 'is_end': True,
                        'splice_loss': 0.0})
            fibers[f] = {'events': evs}
        found = E.uni_discover_splices(fibers)
        kms = sorted(round(s['position_km'], 1) for s in found)
        assert kms == [3.2, 6.9, 10.4], kms
        print('OK')
    """)


def test_twelve_fibers_found_nothing_under_the_old_floor():
    """Pin the regression itself: with the floor forced back to 20, the same
    twelve fibers nominate nothing.  This is what the boss's report looked
    like."""
    _run("""
        E.UNI_MIN_POP_SPLICE_FRAC = 1.0      # defeat the small-job scaling
        E.UNI_MIN_POP_SPLICE_FLOOR = 20
        CLOSURES = (3.24, 6.88, 10.42)
        fibers = {}
        for f in range(1, 13):
            evs = [{'dist_km': 0.0, 'type': '1F9999LS', 'is_end': False,
                    'splice_loss': 0.0}]
            for km in CLOSURES:
                evs.append({'dist_km': km, 'type': '0F9999LS',
                            'is_end': False, 'splice_loss': 0.05})
            evs.append({'dist_km': 14.0, 'type': '0E9999LS', 'is_end': True,
                        'splice_loss': 0.0})
            fibers[f] = {'events': evs}
        assert E.uni_discover_splices(fibers) == []
        print('OK')
    """)

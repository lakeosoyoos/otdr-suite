"""Both cable ends get the launch-connector gate (Platteville↔Cheyenne F939).

The bidirectional connector check pairs a launch connector with the OTHER
direction's view of THAT SAME connector.  It ran on the A end only and always
wrote to `a_tags`, so which connector faults a report showed depended entirely
on which folder was loaded as the A side — and the B-dir ILA column was dead
in every report ever issued.

Platteville↔Cheyenne, 1152 fibers, proved the cost.  Loaded Cheyenne-as-A the
report flags eleven bad connectors at the Cheyenne end (F8 .85, F18 .92,
F155 .76, F176 .69, F245 .72, F246 .78, F248, F277 .83, F331 .75, F705 .65,
F707 .71) and says nothing about F939's 0.78 dB at the Platteville end.
Loaded the other way round it flags F939 and says nothing about the other
eleven.  Twelve real faults; no single run showed more than a subset.  F939's
glass: +0.783 dB from Cheyenne, +0.228 dB from Platteville — a genuine
one-way connector fault worth a reburn, invisible in the report the boss had.

Engine tests run in a clean subprocess (3-engine sor_reader isolation).
"""
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"

# Pre-dedented so it composes with a separately-dedented body; dedenting the
# concatenation would leave the body nested inside the helper's block.
_HELPERS = textwrap.dedent("""
    LAUNCH = 1.01          # launch-reel length, both directions
    SPAN   = 60.0          # cable end in each direction's own frame

    def rec(own_launch_loss, far_conn_loss, span=SPAN):
        '''One direction's record: port reflection, its OWN launch connector
        at LAUNCH, a couple of mid-span splices, the OTHER end's connector one
        reel length before EOF, then EOF.'''
        return {'events': [
            {'dist_km': 0.0, 'splice_loss': 0.0, 'is_end': False,
             'is_reflective': True, 'time_of_travel': 0, 'type': '1F9999LS'},
            {'dist_km': LAUNCH, 'splice_loss': own_launch_loss, 'is_end': False,
             'is_reflective': True, 'time_of_travel': 5000, 'type': '1F9999LS'},
            {'dist_km': 20.0, 'splice_loss': 0.05, 'is_end': False,
             'is_reflective': False, 'type': '0F9999LS'},
            {'dist_km': 40.0, 'splice_loss': 0.06, 'is_end': False,
             'is_reflective': False, 'type': '0F9999LS'},
            {'dist_km': span - LAUNCH, 'splice_loss': far_conn_loss,
             'is_end': False, 'is_reflective': True, 'type': '1F9999LS'},
            {'dist_km': span, 'splice_loss': 0.0, 'is_end': True,
             'is_reflective': True, 'type': '1E9999LS'},
        ]}

    def tags(issues, fnum):
        i = issues.get(fnum) or {}
        return list(i.get('a_tags') or []), list(i.get('b_tags') or [])
""")


def _run(body):
    header = ("import sys\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import splicereportmatchexfo as E\n")
    p = subprocess.run([sys.executable, "-c", header + _HELPERS + textwrap.dedent(body)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    out = p.stdout.strip().splitlines()
    assert out and out[-1] == "OK", p.stdout


def test_b_end_connector_fault_is_reported():
    """The F939 case: the fault is at the B end, and the B direction is the
    one that sees it.  It must land in b_tags — before this fix nothing ever
    populated that list, so the finding (and the whole B-dir ILA column) was
    silently lost."""
    _run("""
        # A end clean both ways; B end bad (B's own launch reads .78)
        A = rec(own_launch_loss=0.05, far_conn_loss=0.22)
        B = rec(own_launch_loss=0.78, far_conn_loss=0.05)
        issues = E.detect_launch_issues({1: A}, {1: B})
        a, b = tags(issues, 1)
        assert any('LAUNCH' in t for t in b), f"B-end fault not reported: {a} {b}"
        assert not any('LAUNCH 1-WAY' in t or t.endswith('LAUNCH') for t in a), a
        print('OK')
    """)


def test_a_end_connector_still_reported_unchanged():
    """Regression guard on the pre-existing behaviour: an A-end fault still
    goes to a_tags and still carries the 1-WAY side label."""
    _run("""
        A = rec(own_launch_loss=0.78, far_conn_loss=0.05)
        B = rec(own_launch_loss=0.05, far_conn_loss=0.22)
        issues = E.detect_launch_issues({1: A}, {1: B})
        a, b = tags(issues, 1)
        assert any('LAUNCH' in t for t in a), f"A-end fault lost: {a} {b}"
        assert not any('LAUNCH' in t for t in b), b
        print('OK')
    """)


def test_both_ends_bad_reports_both():
    """A span can have a bad connector at each end; one run must show both."""
    _run("""
        A = rec(own_launch_loss=0.80, far_conn_loss=0.05)
        B = rec(own_launch_loss=0.75, far_conn_loss=0.05)
        issues = E.detect_launch_issues({1: A}, {1: B})
        a, b = tags(issues, 1)
        assert any('LAUNCH' in t for t in a), a
        assert any('LAUNCH' in t for t in b), b
        print('OK')
    """)


def test_clean_span_reports_no_connector_fault():
    """Both connectors good in both directions -> no launch tag either side."""
    _run("""
        A = rec(own_launch_loss=0.05, far_conn_loss=0.06)
        B = rec(own_launch_loss=0.06, far_conn_loss=0.05)
        issues = E.detect_launch_issues({1: A}, {1: B})
        a, b = tags(issues, 1)
        assert not any('LAUNCH' in t for t in a + b), (a, b)
        print('OK')
    """)


def test_direction_swap_is_symmetric():
    """THE property the bug violated: loading the span the other way round
    must surface the SAME fault, just on the other end's column.  Feed one
    fault through both orientations and require it reported each time."""
    _run("""
        bad  = rec(own_launch_loss=0.78, far_conn_loss=0.05)
        good = rec(own_launch_loss=0.05, far_conn_loss=0.22)
        # orientation 1: bad end loaded as A
        a1, b1 = tags(E.detect_launch_issues({1: bad}, {1: good}), 1)
        # orientation 2: same span, folders swapped
        a2, b2 = tags(E.detect_launch_issues({1: good}, {1: bad}), 1)
        assert any('LAUNCH' in t for t in a1), ('orientation 1 lost it', a1, b1)
        assert any('LAUNCH' in t for t in b2), ('orientation 2 lost it', a2, b2)
        print('OK')
    """)

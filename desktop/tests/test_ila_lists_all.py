"""ILA launch-issue cells name EVERY flagged fiber — never "+N more".

The per-ribbon launch summary capped the list at six fibers and collapsed
the rest into "+N more".  A fully-bad ribbon — e.g. a badly-mated MPO
connector taking out eight or twelve fibers at once — is exactly the case
where the reviewer needs the complete list, and it was exactly the case
that got truncated (boss's report: "1009 .94 ... 1014 .97 ... +2 more").
A ribbon holds at most RIBBON_SIZE entries, so the untruncated cell is
bounded and wrap-text already handles the length.

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
    out = p.stdout.strip().splitlines()
    assert out and out[-1] == "OK", p.stdout


def test_all_twelve_fibers_listed_no_more_suffix():
    """A ribbon with 12 launch-flagged fibers lists all 12 by number."""
    _run("""
        issues = {}
        for f in range(1009, 1021):                     # ribbon 85, fibers 1009-1020
            issues[f] = {'a_tags': [], 'b_tags': [f'.9{f % 10} LAUNCH B side'],
                         'severity': 'HIGH'}
        cells, la, lb = E.build_ribbon_data({}, 1152, 12, 0, launch_issues=issues)
        ri = (1009 - 1) // 12
        text = lb[ri]['text']
        assert 'more' not in text, text
        for f in range(1009, 1021):
            assert str(f) in text, (f, text)
        print('OK')
    """)


def test_both_directions_untruncated():
    """Same guarantee for the A-dir ILA column."""
    _run("""
        issues = {}
        for f in range(1, 13):
            issues[f] = {'a_tags': [f'.8{f % 10} LAUNCH A side'], 'b_tags': [],
                         'severity': 'HIGH'}
        cells, la, lb = E.build_ribbon_data({}, 24, 12, 0, launch_issues=issues)
        text = la[0]['text']
        assert 'more' not in text, text
        assert all(str(f) in text.split() or f' {f} ' in f' {text} ' for f in range(1, 13)), text
        print('OK')
    """)

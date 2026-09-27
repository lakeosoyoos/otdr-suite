"""Show/Hide 'Connectors' covers every connector finding in the end columns.

The switch used to zero only the 1-direction connector-loss gate, so with it
off the boss's Lumen Span 7 report (7AM 9-25) still printed 27 end-column
connector entries -- launch/tailbox reflectance like '94 REFL-49.7dB'.
Robert, 2026-09-25: connectors off should only affect connectors, and never
something at a splice.  Grid cells are not touched by this filter.
"""
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"
FX = REPO_ROOT / "desktop" / "tests" / "fixtures" / "launch_noreceive"


def _run(body):
    header = ("import sys, os\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import sor_reader324802a as sr\n"
              "import splicereportmatchexfo as E\n"
              f"FX = {str(FX)!r}\n")
    p = subprocess.run([sys.executable, "-c", header + textwrap.dedent(body)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


def test_only_connector_tags_go():
    _run("""
        issues = {
            1: {'a_tags': ['REFL-49.7dB', '4.78 LAUNCH A side'], 'b_tags': ['.73 LAUNCH'],
                'refl_rules': {'A': ['x'], 'B': []}},
            2: {'a_tags': ['RESHOOT_DEAD_TRACE', 'REFL-37.6dB'],
                'b_tags': ['.212 PIGTAIL @12m', 'LAUNCH_LOSS+0.12dB'], 'refl_rules': {'A': ['x'], 'B': []}},
            3: {'a_tags': ['FILE_MISSING'], 'b_tags': [], 'refl_rules': {'A': [], 'B': []}},
        }
        import copy
        kept = E.apply_show_filter_ends(copy.deepcopy(issues))
        assert kept == issues                    # switch on: nothing moves
        E.SHOW_CATEGORIES['conn'] = False
        out = E.apply_show_filter_ends(copy.deepcopy(issues))
        assert 1 not in out                      # nothing but connectors
        assert out[2]['a_tags'] == ['RESHOOT_DEAD_TRACE']
        assert out[2]['b_tags'] == ['.212 PIGTAIL @12m']
        assert out[3]['a_tags'] == ['FILE_MISSING']
        print('OK')
    """)


def test_real_launch_connector_is_hidden():
    _run("""
        fa, fb = {}, {}
        for f in ('0229', '0183'):
            A = sr.parse_sor_full(os.path.join(FX, 'MONGRA%s_1550.sor' % f), trim=False)
            B = sr.parse_sor_full(os.path.join(FX, 'GRAMON%s_1550.sor' % f), trim=False)
            for r, s in ((A, 'a'), (B, 'b')):
                r['_source'] = 'sor'; r['_span_side'] = s
            fa[int(f)], fb[int(f)] = A, B
        issues = E.detect_launch_issues(fa, fb)
        assert any('LAUNCH' in t for t in issues[229]['a_tags'])
        E.SHOW_CATEGORIES['conn'] = False
        assert 229 not in E.apply_show_filter_ends(issues)
        print('OK')
    """)

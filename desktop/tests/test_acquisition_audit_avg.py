"""Regression: all-missing averaging must read 'Not available', not a green match.

`_averaging` returns (kind, value) tuples and (None, None) when averaging is
absent.  `consistency_check._is_null` didn't recognise an all-None tuple, so a
column where EVERY file lacks averaging rendered a green "✓ All match: (missing)"
on the boss Acquisition Parameters sheet instead of the honest "Not available".
"""
import subprocess
import sys
import textwrap

from conftest import SPLICEREPORT_DIR


def _run(body):
    header = ("import sys\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import acquisition_audit as aa\n")
    p = subprocess.run([sys.executable, "-c", header + textwrap.dedent(body)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


def test_all_none_averaging_tuple_reads_not_available():
    _run("""
        # Every file missing averaging -> all (None, None).
        v = aa.consistency_check([('A.sor', (None, None)), ('B.sor', (None, None))],
                                 display=str)
        assert v['all_missing'] is True, v
        assert v['all_match'] is False, v
        # A real averaging value in one file -> the missing one is an outlier.
        v2 = aa.consistency_check([('A.sor', ('time_sec', 15.0)), ('B.sor', (None, None))],
                                  display=str)
        assert v2['all_missing'] is False, v2
        assert any(fn == 'B.sor' and d == '(missing)' for fn, d in v2['outliers']), v2
        print('OK')
    """)

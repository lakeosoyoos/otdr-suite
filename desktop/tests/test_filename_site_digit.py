"""A site code whose digit runs into the fiber number is read from the folder.

SNARCAAH 1 East, B side, fixture = ribbon 9 (fibers 97-108) both ways plus the
re-shoot of 103 saved as ``SNA2ESNA1103.sor``: site ``SNA2E``, then ``SNA1``,
then fiber ``103``, no separator (the boss, 2026-09-24).  The other B files are
``SNA2ESNA1E`` + a 3-digit fiber.  On its own the name reads as fiber 1103, so
the re-shoot paired with nothing and the dead first shot ``SNA2ESNA1E103.sor``
was graded instead: ``103 broke B 1.0615km``.  The re-shoot passes.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

from conftest import REPO_ROOT, run_splicereport, FIXTURE_DIR

D = FIXTURE_DIR / "strayreshoot"


def _run(tmp_path):
    rc, m, stderr = run_splicereport(D / "A", D / "B", tmp_path / "sr.xlsx",
                                     "SNARCAAH 1", "SNARCAAH 2")
    assert rc == 0 and m and m.get("ok"), stderr[-1200:]
    return m


def test_the_reshoot_is_graded_as_fiber_103(tmp_path):
    m = _run(tmp_path)
    flagged = [c["label"] for c in m["cells"] if c["is_flagged"]]
    assert flagged == [], flagged


def test_the_report_names_the_file_to_rename(tmp_path):
    m = _run(tmp_path)
    assert any(w.startswith("SNA2ESNA1103.sor read as fiber #103, not #1103")
               for w in m["warnings"]), m["warnings"]


def test_only_the_off_pattern_name_is_reread():
    body = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(REPO_ROOT / 'splicereport')!r})
        import splicereportmatchexfo as E
        snae = ['SNA2ESNA1E%03d.sor' % i for i in range(1, 289)] + ['SNA2ESNA1103.sor']
        assert E._folder_pattern_fibers(snae) == {{'SNA2ESNA1103.sor': 103}}
        # a prefix ending in a digit has no letters to drop: PTL1PTL6 + 0145
        ptl = ['PTL1PTL6%04d.sor' % i for i in range(1, 145)]
        assert E._folder_pattern_fibers(ptl) == {{}}
        # no dominant pattern, nothing re-read
        mixed = ['AB1C%03d.sor' % i for i in range(1, 4)] + ['XY%03d.sor' % i for i in range(1, 5)]
        assert E._folder_pattern_fibers(mixed + ['AB11103.sor']) == {{}}
        # a genuine 4-digit fibre in a 4-digit folder is untouched
        big = ['CAB1X%04d.sor' % i for i in range(1000, 1152)]
        assert E._folder_pattern_fibers(big) == {{}}
        print('OK')
    """)
    p = subprocess.run([sys.executable, "-c", body], capture_output=True, text=True)
    assert p.returncode == 0 and p.stdout.strip().endswith("OK"), p.stdout + p.stderr

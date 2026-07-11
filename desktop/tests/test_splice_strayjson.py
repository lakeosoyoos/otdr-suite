"""Regression for issue #3 (Zach, prod): a stray .json in a splice direction
folder must not hide the .sor.

`load_all._load_dir` used to switch to JSON-only mode if the folder held ANY
.json (an EXFO export, a report, or Secret Sauce's pairs_cache.json), skipping
every .sor → that direction loaded 0 fibers and the tech saw
"Loaded A=0 B=N — both directions required" with the .sor sitting right there.
This is the splice-side twin of the viewer list_fibers fix.
"""
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT, SPLICEREPORT_DIR


def _run(body):
    header = ("import os, shutil, tempfile\n"
              f"import sys; sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import splicereportmatchexfo as E\n"
              f"FXA = {str(REPO_ROOT / 'desktop/tests/fixtures/splice_A')!r}\n"
              f"FXB = {str(REPO_ROOT / 'desktop/tests/fixtures/splice_B')!r}\n")
    p = subprocess.run([sys.executable, "-c", header + textwrap.dedent(body)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


def test_load_all_ignores_a_stray_json_in_a_direction_folder():
    _run("""
        A = sorted(f for f in os.listdir(FXA) if f.endswith('.sor'))[:6]
        B = sorted(f for f in os.listdir(FXB) if f.endswith('.sor'))[:6]
        da = tempfile.mkdtemp(); db = tempfile.mkdtemp()
        for f in A: shutil.copy(os.path.join(FXA, f), os.path.join(da, f))
        for f in B: shutil.copy(os.path.join(FXB, f), os.path.join(db, f))
        open(os.path.join(da, 'acq_summary.json'), 'w').write('{}')  # stray json in A only
        fa, fb = E.load_all(da, db)
        assert len(fa) == 6, 'A loaded %d — a stray .json hid the .sor (issue #3)' % len(fa)
        assert len(fb) == 6, 'B loaded %d' % len(fb)
        print('OK')
    """)


def test_splice_loader_uses_majority_file_type_not_any_json():
    src = (SPLICEREPORT_DIR / "splicereportmatchexfo.py").read_text(encoding="utf-8")
    assert "use_json = _n_json > 0 and _n_json >= _n_sor" in src, \
        "load_all no longer picks the file type by majority — a stray .json can hide .sor"
    assert "use_json = _dir_has_json(d)" not in src, \
        "the any-.json-wins logic is back in load_all"

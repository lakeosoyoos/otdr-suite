"""Duplicate Check keeps matplotlib's font cache between runs.

In the installed app, PyInstaller's matplotlib runtime hook points
MPLCONFIGDIR at a NEW temp folder in every process, so every Duplicate Check
run (each one is its own process) listed and parsed every font on the machine
before drawing its charts: 3 to 13 s a run.  run_secretsauce.py now points
MPLCONFIGDIR at ~/.otdrSuite/mplconfig when frozen, before matplotlib is
imported.  Each check runs in a subprocess: Secret Sauce's modules never load
into the pytest process (they ship their own sor_reader copy).
"""
import ast
import os
import subprocess
import sys

from conftest import REPO_ROOT

RUNNER = REPO_ROOT / "secretsauce" / "run_secretsauce.py"


def _import_runner(home, frozen, preset=None):
    """Import run_secretsauce as the frozen exe would, and report what
    matplotlib will use for its cache."""
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home))
    env.pop("MPLCONFIGDIR", None)
    if preset:
        env["MPLCONFIGDIR"] = str(preset)     # what pyi_rth_mplconfig leaves
    body = (
        "import sys, os\n"
        f"sys.frozen = {frozen!r}\n"
        f"sys.path.insert(0, {str(RUNNER.parent)!r})\n"
        "import run_secretsauce\n"
        "import matplotlib\n"
        "print(os.environ.get('MPLCONFIGDIR', ''))\n"
        "print(matplotlib.get_cachedir())\n")
    p = subprocess.run([sys.executable, "-c", body], capture_output=True,
                       text=True, env=env)
    assert p.returncode == 0, p.stderr
    return p.stdout.rstrip('\n').split('\n')[-2:]


def test_the_installed_app_keeps_its_font_cache(tmp_path):
    keep = tmp_path / ".otdrSuite" / "mplconfig"
    env_dir, cache_dir = _import_runner(tmp_path, True, preset=tmp_path / "per-run-temp")
    assert env_dir == str(keep)
    assert os.path.realpath(cache_dir) == os.path.realpath(keep)
    assert keep.is_dir()


def test_a_stale_lock_from_a_stopped_run_is_cleared(tmp_path):
    """A run stopped while matplotlib saved the font list leaves its lock.
    Left there, every later run waits 5 s for it and cannot save."""
    keep = tmp_path / ".otdrSuite" / "mplconfig"
    keep.mkdir(parents=True)
    stale = keep / "fontlist-v390.json.matplotlib-lock"
    fresh = keep / "other.json.matplotlib-lock"
    stale.write_text("", encoding="utf-8")
    fresh.write_text("", encoding="utf-8")
    old = stale.stat().st_mtime - 3600
    os.utime(stale, (old, old))
    _import_runner(tmp_path, True)
    assert not stale.exists()
    assert fresh.exists()            # a lock a live run may hold is left alone


def test_a_dev_run_is_left_alone(tmp_path):
    env_dir, _ = _import_runner(tmp_path, False)
    assert env_dir == ""
    assert not (tmp_path / ".otdrSuite").exists()


def test_it_runs_before_anything_can_import_matplotlib():
    """matplotlib reads MPLCONFIGDIR when it is first imported, so the call
    must come before every module-level import that could pull it in."""
    tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
    call = next(i for i, n in enumerate(tree.body)
                if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                and getattr(n.value.func, "id", "") == "_keep_font_cache")
    for n in tree.body[call:]:
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in n.names] + [getattr(n, "module", "") or ""]
            assert not any(x.split(".")[0] in ("matplotlib", "report", "report_sor")
                           for x in names), names
    for n in tree.body[:call]:
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in n.names] + [getattr(n, "module", "") or ""]
            assert not any("matplotlib" in x or x.startswith("report") for x in names), names

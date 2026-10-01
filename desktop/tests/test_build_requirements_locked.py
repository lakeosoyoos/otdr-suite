"""The Windows build installs exact, committed package versions.

requirements-desktop.txt asks for "numpy>=2.0.0" and the like, so every CI
run took the newest release of everything.  Between two builds a month apart
16 of 75 packages changed with no commit, and a numpy, scipy or openpyxl
upgrade can change report numbers.  constraints-desktop.txt pins every
package the build installs; CI and build.bat install through it.
"""
import re

from conftest import REPO_ROOT

DESKTOP = REPO_ROOT / "desktop"
REQS = DESKTOP / "requirements-desktop.txt"
LOCK = DESKTOP / "constraints-desktop.txt"
CI = REPO_ROOT / ".github" / "workflows" / "build-windows.yml"
BAT = DESKTOP / "build.bat"


def _norm(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def _lines(path):
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def _lock():
    pins = {}
    for ln in _lines(LOCK):
        m = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([0-9][^\s=<>!~;]*)", ln)
        assert m, f"not an exact pin: {ln!r}"
        pins[_norm(m.group(1))] = m.group(2)
    return pins


def test_every_requirement_has_an_exact_pin():
    pins = _lock()
    for ln in _lines(REQS):
        name = re.split(r"[<>=!~;\[ ]", ln, maxsplit=1)[0]
        assert _norm(name) in pins, f"{name} is not pinned in constraints-desktop.txt"


def test_the_libraries_behind_the_numbers_are_pinned():
    pins = _lock()
    for name in ("numpy", "scipy", "openpyxl", "pandas", "matplotlib", "reportlab",
                 "streamlit", "pyinstaller"):
        assert name in pins, name


def test_the_lock_agrees_with_the_setuptools_pin():
    assert _lock()["setuptools"] == "65.5.1"


def test_ci_and_build_bat_install_through_the_lock():
    for path in (CI, BAT):
        text = path.read_text(encoding="utf-8")
        assert "pip install -r requirements-desktop.txt -c constraints-desktop.txt" in text, path.name


def _check_lock():
    import importlib.util
    spec = importlib.util.spec_from_file_location("check_lock", DESKTOP / "check_lock.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_build_fails_on_a_package_outside_the_lock():
    C = _check_lock()
    lock = {"numpy": "2.4.6", "scipy": "1.17.1"}
    assert C.problems({"numpy": "2.4.6", "scipy": "1.17.1", "pip": "26.2.1"}, lock) == []
    assert C.problems({"numpy": "2.5.0", "scipy": "1.17.1"}, lock) == [
        "numpy is 2.5.0, the lock says 2.4.6"]
    assert C.problems({"numpy": "2.4.6", "scipy": "1.17.1", "newdep": "1.0"}, lock) == [
        "newdep 1.0 is installed but not in constraints-desktop.txt"]


def test_ci_checks_the_installed_set_against_the_lock():
    text = CI.read_text(encoding="utf-8")
    assert "python check_lock.py" in text
    assert text.index("python check_lock.py") > text.index("-c constraints-desktop.txt")
    assert set(_check_lock().read_lock()) == set(_lock())

"""The exe bundles what the app runs, and not the rest.

Two packaging rules, held in both specs (Windows and the Mac de-risk build):

  * Heavy packages are collected without their .py sources a second time,
    their test suites, or build-only files (headers, Cython and Fortran
    sources, import libraries, type stubs).  Those were over half the
    installed files and about 20 MB of the installer.
  * pkg_resources and setuptools are not bundled.  Nothing the app runs
    imports them, and bundling them ran two runtime hooks at every process
    start: the hub and every report run.  packaging and tzdata are still
    collected (streamlit uses packaging; Windows has no time-zone database).

The proof that the slimmer bundle still works is the frozen self-test in CI,
which runs every report and the hub's first page in the built exe.
"""
from conftest import REPO_ROOT

DESKTOP = REPO_ROOT / "desktop"
SPECS = [DESKTOP / "OTDRSuite.spec", DESKTOP / "OTDRSuite-mac.spec"]
CI = REPO_ROOT / ".github" / "workflows" / "build-windows.yml"


def test_heavy_packages_are_collected_slim():
    for spec in SPECS:
        text = spec.read_text(encoding="utf-8")
        assert "include_py_files=False" in text, spec.name
        assert '".tests" not in mod' in text, spec.name
        assert "collect_all(name, **_SLIM)" in text, spec.name


def test_pkg_resources_and_setuptools_are_not_bundled():
    for spec in SPECS:
        text = spec.read_text(encoding="utf-8")
        assert 'collect_submodules("pkg_resources")' not in text, spec.name
        assert 'collect_submodules("setuptools")' not in text, spec.name
        excludes = text[text.index("excludes = ["):]
        excludes = excludes[:excludes.index("]")]
        for name in ("pkg_resources", "setuptools", "_distutils_hack",
                     "pytest", "PyInstaller"):
            assert f'"{name}"' in excludes, (spec.name, name)


def test_packaging_and_tzdata_are_still_collected():
    for spec in SPECS:
        text = spec.read_text(encoding="utf-8")
        assert 'for name in ("packaging", "tzdata"):' in text, spec.name


def test_ci_runs_the_frozen_self_test_after_the_boot_test():
    text = CI.read_text(encoding="utf-8")
    assert "python frozen_selftest.py dist\\OTDRSuite\\OTDRSuite.exe" in text
    assert text.index("frozen_selftest.py") > text.index("BOOT SELF-TEST PASSED")
    assert (DESKTOP / "frozen_selftest.py").is_file()

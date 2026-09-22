"""Site-suffixed filenames must not collapse a folder onto one fiber.

A tech's upload (2026-09-22) named every trace ``0001.ILA1.1550.sor``,
``0002.ILA1.1550.sor``, ...  The parser stripped the wavelength and took the
rightmost digit run, the ILA's ``1``, so every file read as fiber 1.  The
loader kept the first and the report printed FILE_MISSING on fibers 2-12.

Two locks:
  * the parser reads the zero-padded field as the fiber (all three copies,
    see test_viewer_fiber_identity's CATALOG);
  * when some other naming scheme still collapses a folder, the Splice Report
    and the Viewer both key by each file's internal fiber id instead of
    grading one file and dropping the rest.

Engines run as subprocesses (each ships its own sor_reader copy).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

from conftest import (run_splicereport, FIXTURE_SPLICE_A_DIR,
                      FIXTURE_SPLICE_B_DIR)

VIEWER_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), 'viewer')


def _renamed_copy(src_dir, dst_dir, name_for):
    """Copy the 24 fixture traces (fibers 1-24 by internal id) under new names."""
    dst_dir.mkdir(parents=True, exist_ok=True)
    for i, src in enumerate(sorted(src_dir.glob("*.sor")), start=1):
        shutil.copy(src, dst_dir / name_for(i))
    return dst_dir


def test_site_suffixed_names_load_every_fiber(tmp_path):
    a = _renamed_copy(FIXTURE_SPLICE_A_DIR, tmp_path / "A",
                      lambda i: f"{i:04d}.ILA1.1550.sor")
    b = _renamed_copy(FIXTURE_SPLICE_B_DIR, tmp_path / "B",
                      lambda i: f"{i:04d}.ILA2.1550.sor")
    rc, m, stderr = run_splicereport(a, b, tmp_path / "rep" / "r.xlsx",
                                     "ILA1", "ILA2")
    assert rc == 0 and m and m.get("ok") is True, stderr[-1500:]
    assert m["n_fibers"] == 24, f"n_fibers={m.get('n_fibers')}"


def _collapsing_name(i):
    # Every filename parses to fiber 1; only the internal id tells them apart.
    return f"SITE1_trace_{chr(ord('A') + i - 1)}.sor"


def test_collapsed_filenames_fall_back_to_internal_ids(tmp_path):
    a = _renamed_copy(FIXTURE_SPLICE_A_DIR, tmp_path / "A", _collapsing_name)
    b = _renamed_copy(FIXTURE_SPLICE_B_DIR, tmp_path / "B", _collapsing_name)
    rc, m, stderr = run_splicereport(a, b, tmp_path / "rep" / "r.xlsx")
    assert rc == 0 and m and m.get("ok") is True, stderr[-1500:]
    assert m["n_fibers"] == 24, f"n_fibers={m.get('n_fibers')}"
    assert any("internal fiber id instead" in w
               for w in m.get("warnings") or []), m.get("warnings")


def test_viewer_lists_collapsed_folder_by_internal_id(tmp_path):
    d = _renamed_copy(FIXTURE_SPLICE_A_DIR, tmp_path / "A", _collapsing_name)
    code = ("import sys; sys.path.insert(0, %r)\n"
            "import trace_server as T\n"
            "print(T.list_fibers(%r))\n" % (VIEWER_DIR, str(d)))
    p = subprocess.run([sys.executable, '-c', code],
                       capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr[-800:]
    out = eval(p.stdout.strip().splitlines()[-1])
    assert sorted(n for n, _fn in out) == list(range(1, 25)), out
    assert dict(out)[3] == _collapsing_name(3)


def test_multi_wavelength_folder_does_not_trip_the_fallback(tmp_path):
    """Same fiber at two wavelengths collides on the filename AND the internal
    id, so the fallback must stay off and keep-first stands."""
    d = tmp_path / "A"
    d.mkdir()
    for src in sorted(FIXTURE_SPLICE_A_DIR.glob("*.sor")):
        shutil.copy(src, d / src.name)
        shutil.copy(src, d / src.name.replace("_1550", "_1625"))
    code = ("import sys; sys.path.insert(0, %r)\n"
            "import trace_server as T\n"
            "print(T.list_fibers(%r))\n" % (VIEWER_DIR, str(d)))
    p = subprocess.run([sys.executable, '-c', code],
                       capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr[-800:]
    out = eval(p.stdout.strip().splitlines()[-1])
    assert len(out) == 48 and len({n for n, _fn in out}) == 24

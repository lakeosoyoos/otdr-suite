"""Tie-panel folders must not abort the Splice Report (subprocess-isolated).

Zach's tie-panel reshoots (e.g. "ILA 1 to ILA 5/6", "Panel A|B Ports 145-288")
name their traces ``PTL1PTL60145`` / ``DNW1DNW50148``: a 1-digit ILA/panel
suffix butted straight against the 4-digit zero-padded port with NO delimiter.
``_extract_fiber_num`` took the rightmost digit run (60145 / 50148), so every
fiber parsed far past the stray-fiber ceiling, the runner dropped the entire
folder and aborted with "All A-side files had unusable (stray) fiber numbers".

These reuse the 24-fiber/dir splice fixture, renamed into the tie-panel pattern,
and drive the engine through the conftest ``run_splicereport`` subprocess runner
(the splice engine ships its own sor_reader copy — never import it in-process).
"""
from __future__ import annotations

import shutil

from conftest import (run_splicereport, SPLICEREPORT_DIR,
                      FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR)


def _tiepanel_copy(src_dir, dst_dir, a_prefix, b_prefix):
    """Copy the 24 fixture SOR files (fibers 1-24) into `dst_dir` renamed to the
    tie-panel pattern ``<a><b>0NNN.sor`` — the ILA suffix jammed onto the port."""
    dst_dir.mkdir(parents=True, exist_ok=True)
    for i, src in enumerate(sorted(src_dir.glob("*.sor")), start=1):
        shutil.copy(src, dst_dir / f"{a_prefix}{b_prefix}{i:04d}.sor")
    return dst_dir


def test_tiepanel_folder_parses_ports_and_does_not_abort(tmp_path):
    # A = ILA1→ILA6 (PTL1PTL6…), B = ILA6→ILA1 (PTL6PTL1…); ports 0001-0024.
    a = _tiepanel_copy(FIXTURE_SPLICE_A_DIR, tmp_path / "panelA", "PTL1", "PTL6")
    b = _tiepanel_copy(FIXTURE_SPLICE_B_DIR, tmp_path / "panelB", "PTL6", "PTL1")
    out = tmp_path / "rep" / "report.xlsx"
    rc, m, stderr = run_splicereport(a, b, out, "PTL ILA1", "PTL ILA6")
    assert rc == 0, f"runner exited {rc}; stderr:\n{stderr[-1500:]}"
    assert m is not None, f"no manifest emitted; stderr:\n{stderr[-1500:]}"
    # Pre-fix this aborted with the stray-fiber error; now it runs to a report.
    assert m.get("ok") is True, f"tie-panel run aborted: {m.get('error')!r}"
    # Ports 0001-0024 → fibers 1-24 (not 60001-60024).
    assert m["n_fibers"] == 24, f"ports mis-parsed: n_fibers={m.get('n_fibers')}"


def test_extract_fiber_num_guard_present():
    """Cheap revert-catcher for the parser + all-stray fixes (source-locked, so a
    later refactor that drops them is caught even if the slow e2e is skipped)."""
    eng = (SPLICEREPORT_DIR / "splicereportmatchexfo.py").read_text(encoding="utf-8")
    assert r"re.search(r'0\d{3}$', run)" in eng, "tie-panel zero-padded-port guard missing"
    runner = (SPLICEREPORT_DIR / "run_splicereport.py").read_text(encoding="utf-8")
    assert "len(_stray) == len(fa)" in runner, "all-stray parser-failure branch missing"
    assert "the filename pattern was not " in runner, "honest all-stray message missing"

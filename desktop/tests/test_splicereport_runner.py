"""Splice Report runner tests (subprocess-isolated, like Secret Sauce).

The splice engine ships its own sor_reader324802a.py, so it runs as a
subprocess (never imported in-process). These exercise it through the
conftest run_splicereport() helper against the 24-fiber/dir splice fixture
(>= MIN_POP_SPLICE), and assert the grid JSON the Splice Report page needs
to drive the Viewer.
"""
from __future__ import annotations

from pathlib import Path

from conftest import run_splicereport, FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR


def test_splicereport_happy_path(tmp_path):
    out = tmp_path / "rep" / "report.xlsx"
    rc, m, stderr = run_splicereport(FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR, out, "Elm", "Mil")
    assert rc == 0, f"runner exited {rc}; stderr:\n{stderr[-1500:]}"
    assert m is not None and m.get("ok") is True, f"manifest not ok: {m}"
    # Pipeline discovered splice columns and wrote the workbook.
    assert m["n_columns"] >= 1, "no splice/bend columns discovered"
    assert m["n_fibers"] == 24
    assert Path(m["xlsx"]).exists() and m["xlsx"].endswith(".xlsx")


def test_splicereport_grid_cells_are_navigable(tmp_path):
    """Every flagged cell must carry the fields the click-to-jump needs:
    fiber (int), km (number), category (str)."""
    out = tmp_path / "report.xlsx"
    rc, m, stderr = run_splicereport(FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR, out)
    assert rc == 0 and m and m["ok"], f"runner failed: {stderr[-1000:]}"
    cells = m["cells"]
    assert cells, "expected at least one flagged cell on this fixture"
    for c in cells:
        assert isinstance(c["fiber"], int) and c["fiber"] >= 1
        assert isinstance(c["km"], (int, float)) and c["km"] >= 0
        assert c["category"]
    # Columns carry km positions for the header + column-jump.
    for col in m["columns"]:
        assert isinstance(col["km"], (int, float))


def test_splicereport_requires_both_folders(tmp_path):
    # B missing → clean error, not a crash.
    rc, m, stderr = run_splicereport(FIXTURE_SPLICE_A_DIR, tmp_path / "does_not_exist",
                                     tmp_path / "r.xlsx")
    assert m is not None and m.get("ok") is False
    assert "folder" in m.get("error", "").lower()


def _baseline_flag_count(tmp_path):
    """n_flagged with no overrides — the 'must degrade to this' target."""
    out = tmp_path / "base.xlsx"
    rc, m, stderr = run_splicereport(FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR, out)
    assert rc == 0 and m and m.get("ok"), f"baseline run failed: {stderr[-1000:]}"
    return m["n_flagged"]


def test_splicereport_empty_dirs_yield_error_manifest_not_crash(tmp_path):
    """An existing-but-empty input folder (no SORs / wrong files — a realistic
    field mistake) must come back as an ok:false manifest, never a crash that
    leaves the hub with 'no manifest' (prod issue #3/#4 class)."""
    a = tmp_path / "A"; b = tmp_path / "B"
    a.mkdir(); b.mkdir()
    rc, m, stderr = run_splicereport(a, b, tmp_path / "r.xlsx")
    assert m is not None and m.get("ok") is False, (
        f"expected an ok:false manifest, got {m}; stderr:\n{stderr[-1000:]}")
    assert m.get("error"), "error manifest must carry a message"


def test_runner_always_emits_a_manifest_even_if_main_escapes():
    """ROBUSTNESS (prod issue #4): an engine crash must NEVER leave the hub with
    a bare 'no manifest'.  The runner must (a) wrap main() in an outer net that
    writes a manifest to the REAL stdout (sys.__stdout__) if main() escapes its
    own guard, and (b) in the in-try handler, emit the manifest BEFORE
    report_error so a reporting hiccup can't block it.  Source-level guard (the
    crash paths can't all be triggered through the subprocess)."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "splicereport"
           / "run_splicereport.py").read_text(encoding="utf-8")
    # (a) outer last-resort net around main(), writing to the true stdout
    assert "_emit_fatal" in src and "sys.__stdout__" in src, (
        "no last-resort manifest path to the real stdout")
    main_block = src[src.index("if __name__ == '__main__':"):]
    assert ("try:" in main_block and "main()" in main_block
            and "_emit_fatal" in main_block), (
        "main() must be wrapped so an escape still emits a manifest")
    # (b) the in-try except must emit the manifest BEFORE reporting
    pre = src[:src.index("def _emit_fatal")]
    handler = pre[pre.rindex("except Exception as exc:"):]
    assert handler.index("emit({'ok': False") < handler.index("report_error("), (
        "the in-try except must emit the manifest BEFORE calling report_error")


def test_splicereport_bad_overrides_degrade_to_baseline(tmp_path):
    """A malformed --overrides must NEVER abort the report — it degrades to the
    engine's baseline thresholds.  Covers (a) valid-JSON-but-not-a-dict
    ('5', '[1,2]', 'true') that has no .items(), (b) a non-numeric value that
    can't coerce ('abc'), and (c) a non-positive RIBBON_SIZE that would corrupt
    the grid divisor.  Each must come back ok:true with the baseline flag count.
    """
    base = _baseline_flag_count(tmp_path)
    bad_overrides = [
        5,                                 # valid JSON, not a dict
        [1, 2],                            # valid JSON, not a dict
        True,                              # valid JSON, not a dict
        {"REBURN_THRESHOLD": "abc"},       # right key, uncoercible value
        {"RIBBON_SIZE": 0},                # int count global must stay > 0
        {"RIBBON_SIZE": -4},               # negative count
    ]
    for ov in bad_overrides:
        out = tmp_path / "bad.xlsx"
        rc, m, stderr = run_splicereport(FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR,
                                         out, overrides=ov)
        assert rc == 0, f"runner crashed on overrides={ov!r}; stderr:\n{stderr[-1200:]}"
        assert m is not None and m.get("ok") is True, (
            f"overrides={ov!r} should degrade to baseline, got manifest: {m}"
        )
        assert m["n_flagged"] == base, (
            f"overrides={ov!r} must not change flagging "
            f"(baseline {base}, got {m['n_flagged']})"
        )


def test_splicereport_valid_override_still_applies(tmp_path):
    """Guardrail: scrubbing bad overrides must NOT swallow good ones — a real
    threshold change still takes effect (lower REBURN_THRESHOLD flags more)."""
    base = _baseline_flag_count(tmp_path)
    out = tmp_path / "low.xlsx"
    rc, m, stderr = run_splicereport(FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR,
                                     out, overrides={"REBURN_THRESHOLD": 0.01})
    assert rc == 0 and m and m.get("ok"), f"valid override failed: {stderr[-1000:]}"
    assert m["n_flagged"] > base, (
        f"a much lower reburn threshold should flag MORE than baseline "
        f"({base}); got {m['n_flagged']}"
    )

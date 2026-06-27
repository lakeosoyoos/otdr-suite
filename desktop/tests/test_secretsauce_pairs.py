"""Tests for the Secret Sauce "Stay in app" pairs-JSON runner mode.

The `--format pairs` mode emits the per-pair metrics as a JSON manifest (no
file written) so the Duplicate Check page can render the duplicate report
in-app and deep-link each pair into the Viewer.  Exercised through the same
subprocess helper as the xlsx/pdf modes (namespace isolation rule still
applies: never import the secretsauce package in this process).
"""
from __future__ import annotations

import json

from conftest import (
    run_secretsauce,
    run_streamlit,
    mixed_fixture_dir,
    single_dir_fixture,
    FIXTURE_A_DIR,
)


def test_pairs_mode_emits_pairs(tmp_path):
    """All-8-fixtures folder → ONE group (no direction split) → 28 pairs, each
    carrying the fields the in-app report + viewer deep-link need."""
    folder = mixed_fixture_dir(tmp_path)
    out_dir = tmp_path / "out"
    rc, m, stderr = run_secretsauce(folder, out_dir, "pairs")
    assert rc == 0, f"runner exited {rc}; stderr tail:\n{(stderr or '')[-800:]}"
    assert m is not None, "no JSON manifest parsed from stdout"
    assert m.get("ok") is True, f"manifest not ok: {m}"
    assert m.get("mode") == "pairs", m
    pairs = m.get("pairs")
    assert pairs, "expected pairs on this fixture"
    # One group of all 8 files = C(8,2) = 28 pairs.
    assert m["n_pairs"] == 28, m["n_pairs"]
    for p in pairs:
        assert isinstance(p["fileA"], str) and isinstance(p["fileB"], str)
        assert 0.0 <= p["p_dup"] <= 1.0
        assert isinstance(p["score"], (int, float))
        assert p["verdict"] in ("CONFIRMED duplicate", "Likely duplicate",
                                "Possible duplicate", "Unique")
        assert "viewable" in p


def test_pairs_mode_sorted_worst_first(tmp_path):
    folder = mixed_fixture_dir(tmp_path)
    out_dir = tmp_path / "out"
    rc, m, stderr = run_secretsauce(folder, out_dir, "pairs")
    assert rc == 0 and m and m["ok"], f"runner failed: {(stderr or '')[-800:]}"
    pdups = [p["p_dup"] for p in m["pairs"]]
    # Non-increasing likelihood (worst-first); ties broken by score.
    assert pdups == sorted(pdups, reverse=True), pdups


def test_pairs_single_direction_are_viewable(tmp_path):
    """One direction folder (fibers 1–4, all distinct) → every pair is
    viewable and carries two DISTINCT fiber numbers for the overlay."""
    folder = single_dir_fixture(tmp_path)
    out_dir = tmp_path / "out"
    rc, m, stderr = run_secretsauce(folder, out_dir, "pairs")
    assert rc == 0 and m and m["ok"], f"runner failed: {(stderr or '')[-800:]}"
    assert m["n_pairs"] == 6, m["n_pairs"]
    for p in m["pairs"]:
        assert p["viewable"] is True, f"expected viewable pair: {p}"
        assert isinstance(p["fiberA"], int) and isinstance(p["fiberB"], int)
        assert p["fiberA"] != p["fiberB"]


def test_pairs_mode_flags_fiber_number_collision(tmp_path):
    """All-8 folder: ELMMIL and MILELM both use fiber numbers 1–4, so in a
    flat folder the Viewer can't disambiguate by number → not viewable."""
    folder = mixed_fixture_dir(tmp_path)
    out_dir = tmp_path / "out"
    rc, m, stderr = run_secretsauce(folder, out_dir, "pairs")
    assert rc == 0 and m and m["ok"], f"runner failed: {(stderr or '')[-800:]}"
    not_viewable = [p for p in m["pairs"] if not p["viewable"]]
    assert not_viewable, "expected collisions flagged on the mixed-direction folder"
    assert all(p.get("reason") for p in not_viewable)


def test_pairs_manifest_is_single_json_line(tmp_path):
    folder = single_dir_fixture(tmp_path)
    out_dir = tmp_path / "out"
    rc, m, _stderr = run_secretsauce(folder, out_dir, "pairs")
    assert rc == 0 and isinstance(m, dict)
    line = json.dumps(m)
    assert "\n" not in line
    assert json.loads(line) == m


def test_pairs_too_small_group_clean_error(tmp_path):
    """A single SOR file can't be compared (need >=2) → clean manifest error."""
    folder = tmp_path / "solo"
    folder.mkdir()
    src = next(FIXTURE_A_DIR.glob("ELMMIL*.sor"))
    (folder / src.name).write_bytes(src.read_bytes())
    out_dir = tmp_path / "out"
    rc, m, stderr = run_secretsauce(folder, out_dir, "pairs")
    assert m is not None and m.get("ok") is False, m
    assert ">=2" in m.get("error", "") and "SOR" in m.get("error", ""), m


def test_pairs_mode_rejects_json(tmp_path):
    """Pairs view is SOR-only — a .json folder gets a clear, non-crashing error
    (Excel/PDF still handle JSON)."""
    folder = tmp_path / "jsonfolder"
    folder.mkdir()
    (folder / "a.json").write_bytes(b'{"x": 1}')
    (folder / "b.json").write_bytes(b'{"x": 2}')
    out_dir = tmp_path / "out"
    rc, m, _stderr = run_secretsauce(folder, out_dir, "pairs")
    assert m is not None and m.get("ok") is False, m
    assert ".sor" in m.get("error", "").lower(), m["error"]


# ── Regression: the pair → Viewer → "← Back" round trip (found in review) ──
def test_back_button_does_not_crash_and_returns():
    """The '← Back to Secret Check' button must set nav state in an on_click
    callback — setting it inline (after the sidebar radio is instantiated) raises
    StreamlitAPIException.  Reproduces + guards the crash found during review."""
    at = run_streamlit()
    at.run()
    at.session_state["came_from_dupcheck"] = True
    at.session_state["viewer_target"] = {"fibers": "1,2", "dir": "a"}
    at.session_state["nav_radio"] = "Viewer"
    at.run()
    btns = [b for b in at.button if "Back to Secret" in (b.label or "")]
    assert btns, "the Back-to-Duplicate-Check button was not rendered on the Viewer"
    btns[0].click().run()
    assert not at.exception, f"Back button crashed: {list(at.exception)}"
    assert at.session_state["nav_radio"] == "Secret Sauce"


def test_returning_restores_pairs_from_cache(tmp_path):
    """After a pair-click (URL nav resets session_state), returning to Duplicate
    Check must re-show the pairs list from the on-disk cache — no re-run."""
    import json
    folder = single_dir_fixture(tmp_path)
    out_dir = folder / "SecretSauce_reports"
    rc, m, stderr = run_secretsauce(folder, out_dir, "pairs")
    assert rc == 0 and m and m["ok"], f"pairs run failed: {stderr[-800:]}"
    m["_folder"] = str(folder)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "pairs_cache.json").write_text(json.dumps(m))

    at = run_streamlit()
    at.run()
    at.session_state["nav_radio"] = "Secret Sauce"
    at.session_state["ss_folder_input"] = str(folder)   # folder preserved by _handle_nav
    at.run()
    assert not at.exception, f"Duplicate Check raised on return: {list(at.exception)}"
    md = " ".join(x.value for x in at.markdown)
    assert ("nav=viewer&fibers=" in md or "Click a pair" in md), (
        "pairs report was not restored from the disk cache on return"
    )

"""OTDR settings panel ↔ splice engine wiring tests.

The OTDR Suite ports the standalone Splice Report app's pixel-perfect EXFO
threshold panel (custom HTML component) + customer-profile dropdown.  Because
the splice engine runs as a SUBPROCESS here, the panel's threshold edits cross
the process boundary as a JSON --overrides arg, and run_splicereport applies
them to the engine module globals BEFORE the pipeline runs.

These tests prove the value the panel shows is the value that reaches the
engine — closing the standalone's "load-bearing Apply" footgun at the seam
that matters for this app (the subprocess boundary):

  1. The Splice Report page renders without exception with the settings box.
  2. A stricter bidir_splice_loss override flags MORE cells than the default
     run on the same fixture (the override actually reaches the engine).
  3. splicereport_cmd emits the overrides; run_splicereport applies them.
  4. The PyInstaller specs bundle components/otdr_settings (both files).
"""
from __future__ import annotations

import re
from pathlib import Path

from conftest import (
    run_streamlit, run_splicereport,
    FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR,
    REPO_ROOT,
)

import app as hub  # the hub module — import its helpers + cmd builder


# ── 1. Page still renders with the settings box present ──────────────────
def test_splice_report_page_renders_with_settings_box(tmp_path):
    """Selecting the Splice Report page AND supplying valid A/B folders (so
    the OTDR settings panel + custom component actually render) must not
    raise.  The component returns its default (None) under AppTest — the
    panel still mounts and the profile dropdown is exercised."""
    at = run_streamlit().run()
    at.session_state["view_dir_a_input"] = str(FIXTURE_SPLICE_A_DIR)
    at.session_state["view_dir_b_input"] = str(FIXTURE_SPLICE_B_DIR)
    at.sidebar.radio[0].set_value("Splice Report").run()
    assert not at.exception, f"Splice Report page raised: {list(at.exception)}"
    # The panel seeded its session_state on first render.
    assert "otdr_settings" in at.session_state
    assert "otdr_profile" in at.session_state
    # The default profile's bidir row holds the engine-baseline fail value.
    assert at.session_state["otdr_settings"]["bidir_splice_loss"]["fail"] == 0.159


# ── 2. A changed threshold actually reaches the engine ───────────────────
def test_stricter_bidir_override_flags_more_cells(tmp_path):
    """The headline contract: drop bidir_splice_loss (REBURN_THRESHOLD) and
    MORE cells get flagged on the same fixture.  Proves the panel value
    crosses the subprocess boundary and changes engine behavior."""
    out_base = tmp_path / "base.xlsx"
    out_strict = tmp_path / "strict.xlsx"

    rc0, m0, e0 = run_splicereport(FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR, out_base)
    assert rc0 == 0 and m0 and m0.get("ok"), f"baseline failed: {e0[-1500:]}"

    rc1, m1, e1 = run_splicereport(
        FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR, out_strict,
        overrides={"REBURN_THRESHOLD": 0.02},
    )
    assert rc1 == 0 and m1 and m1.get("ok"), f"strict run failed: {e1[-1500:]}"

    print(f"\n[override check] baseline n_flagged={m0['n_flagged']} "
          f"-> strict(REBURN_THRESHOLD=0.02) n_flagged={m1['n_flagged']}")
    assert m1["n_flagged"] > m0["n_flagged"], (
        f"stricter bidir splice loss should flag MORE cells: "
        f"baseline={m0['n_flagged']} strict={m1['n_flagged']}"
    )


def test_default_override_reproduces_baseline(tmp_path):
    """Passing the Default profile's overrides (ticked rows at their
    engine-default fail values) must reproduce the no-override baseline —
    setting a global to its existing value is a no-op."""
    out_base = tmp_path / "base.xlsx"
    out_def = tmp_path / "def.xlsx"

    rc0, m0, _ = run_splicereport(FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR, out_base)
    assert m0 and m0.get("ok")

    default_settings = hub._otdr_settings_from_profile("Default (engine baseline)")
    overrides = hub._overrides_from_settings(default_settings)
    rc1, m1, _ = run_splicereport(
        FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR, out_def, overrides=overrides)
    assert m1 and m1.get("ok")
    assert m1["n_flagged"] == m0["n_flagged"], (
        "Default profile must reproduce baseline flagging "
        f"(base={m0['n_flagged']} default={m1['n_flagged']})"
    )


# ── 3. Signature / contract: cmd emits overrides; runner accepts them ────
def test_splicereport_cmd_forwards_overrides_json():
    cmd = hub.splicereport_cmd("/a", "/b", "/out.xlsx", "X", "Y",
                               overrides={"REBURN_THRESHOLD": 0.12})
    assert "--overrides" in cmd
    payload = cmd[cmd.index("--overrides") + 1]
    import json
    assert json.loads(payload) == {"REBURN_THRESHOLD": 0.12}


def test_splicereport_cmd_omits_overrides_when_empty():
    """No active overrides → no --overrides arg (keeps today's argv shape)."""
    assert "--overrides" not in hub.splicereport_cmd("/a", "/b", "/o.xlsx", "X", "Y")
    assert "--overrides" not in hub.splicereport_cmd("/a", "/b", "/o.xlsx", "X", "Y",
                                                     overrides={})


def test_runner_applies_overrides_before_pipeline():
    """The runner must apply --overrides to the engine globals BEFORE it
    derives the `threshold` local from REBURN_THRESHOLD (else a changed
    bidir splice loss wouldn't reach the bidir flag threshold)."""
    src = (Path(hub.SPLICEREPORT_DIR) / "run_splicereport.py").read_text(encoding="utf-8")
    assert "--overrides" in src
    apply_pos = src.index("setattr(E,")
    threshold_pos = src.index("threshold = args.threshold")
    assert apply_pos < threshold_pos, (
        "overrides must be applied to the engine BEFORE the threshold local "
        "is read from REBURN_THRESHOLD"
    )


def test_overrides_from_settings_maps_panel_rows_to_engine_globals():
    """A ticked custom row maps to its engine global; unticked rows fall
    back (omitted)."""
    settings = hub._otdr_settings_from_profile("Lumen")
    ov = hub._overrides_from_settings(settings)
    # Lumen ticks bidir/unidir splice, bidir connector, reflectance.
    assert ov["REBURN_THRESHOLD"] == 0.120
    assert ov["SINGLE_DIR_THRESHOLD"] == 0.200
    assert ov["BIDIR_CONNECTOR_LOSS"] == 0.400
    assert ov["LAUNCH_BAD_REFL_DB"] == -50.0
    # Unticked / visual-only rows never appear.
    assert "splitter_loss" not in ov

    # An unticked row contributes nothing even if it has a fail value.
    settings["bidir_splice_loss"]["apply"] = False
    assert "REBURN_THRESHOLD" not in hub._overrides_from_settings(settings)


# ── 4. The specs bundle the component (so it ships in the .exe / .app) ───
def test_specs_bundle_otdr_settings_component():
    for spec_name in ("OTDRSuite.spec", "OTDRSuite-mac.spec"):
        src = (Path(REPO_ROOT) / "desktop" / spec_name).read_text(encoding="utf-8")
        assert "components.otdr_settings" in src, f"{spec_name} missing hiddenimport"
        # Both component files bundled under components/otdr_settings.
        assert re.search(r'"__init__\.py"\),\s*\n?\s*"components/otdr_settings"', src) \
            or '"components/otdr_settings")' in src, \
            f"{spec_name} missing component datas"
        assert "index.html" in src and "components/otdr_settings" in src


def test_component_files_present_in_repo():
    base = Path(REPO_ROOT) / "components" / "otdr_settings"
    assert (base / "__init__.py").exists()
    assert (base / "index.html").exists()
    # The component auto-commits on edit (the standalone's bug #1 fix) so the
    # panel's shown values reach Python without a separate Apply click.
    html = (base / "index.html").read_text(encoding="utf-8")
    assert "streamlit:setComponentValue" in html

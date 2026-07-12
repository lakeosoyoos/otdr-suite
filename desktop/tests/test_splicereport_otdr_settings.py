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
    assert at.session_state["otdr_settings"]["bidir_splice_loss"]["fail"] == 0.160


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


def test_unchecking_a_row_disables_that_detection(tmp_path):
    """The boss's report: unchecking a settings row must actually TURN THE
    DETECTION OFF, not silently fall back to the engine default (which still
    fired).  End-to-end through the real settings→overrides pipeline:

      permissive unidir threshold  → many single-direction flags surface
      unchecked unidir row (Apply off) → every single-dir flag is suppressed,
        dropping back to the bidir-only baseline.

    Proves the unticked row (a) sends the disable sentinel and (b) that the
    sentinel reaches the engine and removes exactly the single-dir category.
    """
    out_base = tmp_path / "base.xlsx"
    out_perm = tmp_path / "perm.xlsx"
    out_off = tmp_path / "off.xlsx"

    rc_b, m_b, _ = run_splicereport(FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR, out_base)
    assert m_b and m_b.get("ok")

    # A permissive unidir threshold surfaces many single-direction flags.
    rc_p, m_p, _ = run_splicereport(
        FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR, out_perm,
        overrides={"SINGLE_DIR_THRESHOLD": 0.05})
    assert m_p and m_p.get("ok")
    assert m_p["n_flagged"] > m_b["n_flagged"], (
        "permissive unidir threshold should surface extra single-dir flags "
        f"(base={m_b['n_flagged']} permissive={m_p['n_flagged']})")

    # Now uncheck the Unidir. splice loss row via the real panel pipeline.
    s = hub._otdr_settings_from_profile("Default (engine baseline)")
    s["unidir_splice_loss"]["apply"] = False
    off_overrides = hub._overrides_from_settings(s)
    assert off_overrides["SINGLE_DIR_THRESHOLD"] == hub._OTDR_DISABLE_SENTINEL

    rc_o, m_o, e_o = run_splicereport(
        FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR, out_off, overrides=off_overrides)
    assert m_o and m_o.get("ok"), f"disabled-unidir run failed: {e_o[-800:]}"
    assert m_o["n_flagged"] < m_p["n_flagged"], (
        "disabling unidir must flag FEWER than the permissive run "
        f"(permissive={m_p['n_flagged']} off={m_o['n_flagged']})")
    assert m_o["n_flagged"] == m_b["n_flagged"], (
        "disabling unidir must suppress single-direction flags back to the "
        f"bidir-only baseline (base={m_b['n_flagged']} off={m_o['n_flagged']})")


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
    """A ticked custom row maps to its engine global at the tech's value; an
    unticked mapped row DISABLES that detection (sentinel threshold)."""
    settings = hub._otdr_settings_from_profile("Lumen")
    ov = hub._overrides_from_settings(settings)
    # Lumen ticks bidir/unidir splice, bidir connector, reflectance.
    assert ov["REBURN_THRESHOLD"] == 0.120
    assert ov["SINGLE_DIR_THRESHOLD"] == 0.200
    assert ov["BIDIR_CONNECTOR_LOSS"] == 0.400
    assert ov["LAUNCH_BAD_REFL_DB"] == -50.0
    # Visual-only rows (no engine global) never appear.
    assert "splitter_loss" not in ov

    # Unticking a mapped row now turns the detection OFF: its engine global is
    # sent as the disable sentinel (a threshold no reading reaches), NOT omitted
    # (which would silently revert to the engine's built-in default — the boss's
    # bug).  The sentinel must clear run_splicereport's finite/positive guard.
    settings["bidir_splice_loss"]["apply"] = False
    ov_off = hub._overrides_from_settings(settings)
    assert ov_off["REBURN_THRESHOLD"] == hub._OTDR_DISABLE_SENTINEL
    assert ov_off["REBURN_THRESHOLD"] > 0 and ov_off["REBURN_THRESHOLD"] < float("inf")


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


# ── 5. Mid-span reflectance — the two-threshold BAND reaches the engine ──
def test_midspan_reflectance_band_preset_and_reaches_engine():
    """The 'Mid-span reflectance' row is a BAND — Fail at the strong end (-50)
    and a Warning floor at the weak end (-80, the boss's TOPMIL0195 -73 dB sits
    inside it).  Every real customer preset must ship it ON at -50/-80, push
    BOTH thresholds across the subprocess boundary, and the engine must expose
    the matching globals so setattr lands on real attributes (not a silent
    no-op)."""
    # Read the engine SOURCE rather than import it in-process — importing here
    # would pull a sibling sor_reader324802a and break the 3-engine isolation
    # (the engine runs as a SUBPROCESS for exactly this reason).
    eng_src = (Path(hub.SPLICEREPORT_DIR) / "splicereportmatchexfo.py").read_text(encoding="utf-8")
    assert re.search(r"^MIDSPAN_REFL_FAIL_DB\s*=\s*-50\.0", eng_src, re.M), \
        "engine missing MIDSPAN_REFL_FAIL_DB = -50.0"
    assert re.search(r"^MIDSPAN_REFL_WARN_DB\s*=\s*-80\.0", eng_src, re.M), \
        "engine missing MIDSPAN_REFL_WARN_DB = -80.0"
    for prof in ("Default (engine baseline)", "Lumen", "Zayo"):
        s = hub._otdr_settings_from_profile(prof)
        row = s["midspan_reflectance"]
        assert row["apply"] is True, f"{prof}: mid-span reflectance must default ON"
        assert (row["fail"], row["warning"]) == (-50.0, -80.0), \
            f"{prof}: band must preset Fail -50 / Warning floor -80, got {row}"
        ov = hub._overrides_from_settings(s)
        assert ov["MIDSPAN_REFL_FAIL_DB"] == -50.0, f"{prof}: Fail must reach engine"
        assert ov["MIDSPAN_REFL_WARN_DB"] == -80.0, f"{prof}: floor must reach engine"


def test_midspan_reflectance_thresholds_edit_independently():
    """A tech edit to either threshold must flow through on its own — Fail and
    the Warning floor map to two distinct engine globals; turning the row OFF
    disables the whole band by sentinelling BOTH globals (the reporting gate is
    the Warning floor, so sentinelling it stops every mid-span reflectance
    flag)."""
    s = hub._otdr_settings_from_profile("Default (engine baseline)")
    s["midspan_reflectance"]["fail"] = -45.0      # tech tightens Fail
    s["midspan_reflectance"]["warning"] = -70.0   # tech tightens the floor
    ov = hub._overrides_from_settings(s)
    assert ov["MIDSPAN_REFL_FAIL_DB"] == -45.0
    assert ov["MIDSPAN_REFL_WARN_DB"] == -70.0

    s["midspan_reflectance"]["apply"] = False
    ov_off = hub._overrides_from_settings(s)
    assert ov_off["MIDSPAN_REFL_FAIL_DB"] == hub._OTDR_DISABLE_SENTINEL
    assert ov_off["MIDSPAN_REFL_WARN_DB"] == hub._OTDR_DISABLE_SENTINEL

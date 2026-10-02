"""The contract customer profile — thresholds, connector knobs, receipt.

Sources for every number asserted here: RFP-FOT-2025-001 (the
customer's RFP, issued 09 Jul 2026) and the Zero DB Statement of Work
(DocuSigned 06 Aug 2026), reconciled by the prime contractor on 24 Aug 2026.

Three things have to hold, and each has a way of quietly breaking:

  1. The profile's contract values reach the ENGINE GLOBALS.  A profile is
     just data; if a key stops being mapped in _OTDR_KEY_TO_ENGINE_GLOBAL the
     row still renders and grades at the baseline instead.
  2. The profile reaches the CONNECTOR panel.  Customer profiles used to
     cover only OTDR_ROWS, so the contract profile's one-sided connector rule — the change
     with real teeth (154 flags to 7 on Frenchtown) — would have rendered as
     a chosen profile and changed nothing.
  3. Turning that gate off must SURVIVE the trip.  run_splicereport rejects
     non-finite and (for some keys) non-positive overrides, so a 0.0 that
     gets skipped there would silently restore 0.65 with no error anywhere.

Plus: adding the contract profile must not move Default / customer L / customer Z.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from conftest import (
    run_splicereport, FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR, REPO_ROOT,
)

import app as hub

CONTRACT_PROFILE = "AWS / IIG MT.1085"


# ── 1. The contract thresholds reach the engine globals ──────────────────
def test_contract_thresholds_reach_engine_globals():
    """The three rows the contract actually specifies, at their governing
    values, mapped onto the globals the engine reads at run time."""
    assert CONTRACT_PROFILE in hub.CUSTOMER_PROFILES, "the contract profile must be selectable"
    ov = hub._overrides_from_settings(hub._otdr_settings_from_profile(CONTRACT_PROFILE))

    # Bidir splice loss <= 0.20 dB (RFP and SOW agree).
    assert ov["REBURN_THRESHOLD"] == 0.200
    # Bidir connector loss <= 0.50 dB — the executed SOW governs, not the
    # RFP's 0.30.  On Span 29 that is 3 failures against 152.
    assert ov["BIDIR_CONNECTOR_LOSS"] == 0.500
    # Connector reflectance <= -55 dB, SIGNED (a less negative reading fails).
    assert ov["LAUNCH_BAD_REFL_DB"] == -55.0


def test_contract_bidir_splice_is_looser_than_the_engine_baseline():
    """0.20 dB is LOOSER than the 0.160 we ship, so the contract profile flags FEWER splice
    cells than Default.  That is the contract, but it is surprising enough
    that it should fail loudly if someone 'corrects' it downward."""
    src = (Path(hub.SPLICEREPORT_DIR) / "splicereportmatchexfo.py").read_text(
        encoding="utf-8")
    base = float(re.search(r"^REBURN_THRESHOLD\s*=\s*([\d.]+)", src, re.M).group(1))
    cp = hub._otdr_settings_from_profile(CONTRACT_PROFILE)["bidir_splice_loss"]["fail"]
    assert cp > base, f"the contract profile {cp} must be looser than the baseline {base}"


def test_contract_keeps_unidir_splice_loss_on_at_the_default():
    """The contract sets no single-direction threshold.  Leaving the row ON at
    the engine default grades those cells as today; unticking it would HIDE
    events (the disable sentinel), which is not what 'unspecified' means."""
    row = hub._otdr_settings_from_profile(CONTRACT_PROFILE)["unidir_splice_loss"]
    assert row["apply"] is True
    assert row["fail"] == 0.200
    ov = hub._overrides_from_settings(hub._otdr_settings_from_profile(CONTRACT_PROFILE))
    assert ov["SINGLE_DIR_THRESHOLD"] == 0.200
    assert ov["SINGLE_DIR_THRESHOLD"] != hub._OTDR_DISABLE_SENTINEL


def test_contract_does_not_invent_gates_the_engine_cannot_grade():
    """Fiber attenuation, link ORL, OLTS, PMD and CD are all in the contract
    and none of them is an OTDR gate the engine grades.  None may appear in
    the override payload — a knob that reaches nothing is worse than no knob.
    (Average splice loss used to be on this list; it now has an engine
    global and its own sheet — see test_contract_average_splice_loss_gate.)"""
    ov = hub._overrides_from_settings(hub._otdr_settings_from_profile(CONTRACT_PROFILE))
    for key in ("span_loss", "span_length", "splitter_loss",
                "pmd", "cd", "olts"):
        assert key not in ov
    # Every emitted key must be a real engine global, not a hopeful name.
    eng = (Path(hub.SPLICEREPORT_DIR) / "splicereportmatchexfo.py").read_text(
        encoding="utf-8")
    for g in ov:
        assert re.search(r"^%s\s*=" % re.escape(g), eng, re.M), \
            f"{g} is not a module-level engine global"


# ── 2. The profile reaches the CONNECTOR panel ───────────────────────────
def test_contract_turns_the_one_sided_connector_gate_off():
    """The change with teeth.  One-sided panel offsets on this job are a
    backscatter / mode-field mismatch against the 200 um span fiber (the
    near end reads a GAINER, which neither contamination nor a bad connector
    can produce), so a one-sided reading is not a failure here."""
    # The gate is a row in OTDR Settings now; the profile still sets it.
    assert hub._overrides_from_settings(hub._otdr_settings_from_profile(CONTRACT_PROFILE))["LAUNCH_CONN_UNI_MIN_DB"] == 0.0


def test_contract_keeps_the_bidirectional_connector_gate_running():
    """Turning the one-sided gate off must not blind the connector check
    entirely — a connector BOTH directions see as bad still has to flag.
    (Mark's counts are 26->2, 154->7, 18->2: survivors, not zero.)"""
    conn = hub._conn_settings_from_profile(CONTRACT_PROFILE)
    assert conn["LAUNCH_CONN_LOSS_MIN_DB"] == 0.65
    assert conn["LAUNCH_CONN_LOSS_MIN_DB"] > 0


def test_engine_treats_zero_as_off_not_as_flag_everything():
    """A `>=` gate at 0 would flag EVERY connector.  The engine must guard it
    with an explicit `> 0`, and must not skip the whole connector block just
    because one of the three gates is zero."""
    eng = (Path(hub.SPLICEREPORT_DIR) / "splicereportmatchexfo.py").read_text(
        encoding="utf-8")
    assert re.search(
        r"_uni_fires\s*=\s*\(_both and LAUNCH_CONN_UNI_MIN_DB > 0", eng), \
        "the one-sided gate must short-circuit on > 0 before comparing"
    assert "(LAUNCH_CONN_LOSS_MIN_DB or 0) > 0" in eng and \
           "(LAUNCH_CONN_UNI_MIN_DB or 0) > 0" in eng, \
        "zeroing one connector gate must not take the others down with it"


def test_zero_survives_the_runner_override_guard():
    """run_splicereport skips non-finite overrides, and forces a few keys to
    stay positive.  LAUNCH_CONN_UNI_MIN_DB must NOT be one of those, or the
    0.0 is skipped and the gate silently returns to 0.65."""
    src = (Path(hub.SPLICEREPORT_DIR) / "run_splicereport.py").read_text(
        encoding="utf-8")
    block = src.split("_positive_float_globals", 1)[1].split("}", 1)[0]
    assert "LAUNCH_CONN_UNI_MIN_DB" not in block, \
        "a positive-only guard would silently drop the OFF value"


def test_conn_overrides_ride_the_same_channel_to_the_engine():
    """The connector knobs reach the engine through --overrides alongside the
    threshold table, and only globals the panel renders are forwarded."""
    src = (REPO_ROOT / "app.py").read_text(encoding="utf-8")
    assert "if g in _CONN_DEFAULTS" in src, \
        "connector overrides must be filtered to rendered knobs"
    conn = hub._conn_settings_from_profile(CONTRACT_PROFILE)
    assert set(conn) == set(hub._CONN_DEFAULTS), \
        "a profile may retune knobs, never invent or drop them"


def test_profile_switch_reloads_the_connector_knobs():
    """Picking a customer must reload BOTH panels, and re-mount the connector
    component (its key encodes the profile) — otherwise the iframe keeps
    showing, and re-committing, the previous customer's knobs."""
    src = (REPO_ROOT / "app.py").read_text(encoding="utf-8")
    switch = src.split("if _picked != _cur:", 1)[1][:900]
    assert "_conn_settings_from_profile(_picked)" in switch
    assert 'key=f"conn_settings_component::' in src


def test_custom_profile_keeps_the_techs_own_connector_edits():
    """'Custom' is the sentinel that preserves manual edits — it must not
    reset the connector knobs any more than it resets the threshold table."""
    src = (REPO_ROOT / "app.py").read_text(encoding="utf-8")
    switch = src.split("if _picked != _cur:", 1)[1][:900]
    conn_line = switch.index("_conn_settings_from_profile(_picked)")
    guard_line = switch.index("if 'Custom' not in _picked:")
    assert guard_line < conn_line, \
        "the connector reload must sit INSIDE the non-Custom guard"


# ── 3. Adding the contract profile must not move the profiles that shipped before it ──────
def test_existing_profiles_keep_engine_default_connector_knobs():
    """Only a profile that declares a "conn" block may differ from the
    engine defaults.  Default declares none, so its connector behavior is
    byte-identical to what it shipped with.  (customer L and customer Z declared none
    until the Sep 2026 FastReporter templates gave them connector values;
    test_fr_customer_templates pins those.)"""
    for prof in ("Default (engine baseline)",):
        assert hub._conn_settings_from_profile(prof) == hub._CONN_DEFAULTS, \
            f"{prof} connector knobs moved"


def test_unknown_conn_global_in_a_profile_is_ignored():
    """A typo in a profile's conn block must not push an unwired constant at
    the engine, nor invent a knob the panel never renders."""
    hub.CUSTOMER_PROFILES["__test__"] = {
        "apply": set(), "thresholds": {},
        "conn": {"NOT_A_REAL_GLOBAL": 1.0, "LAUNCH_CONN_UNI_MIN_DB": 0.0},
    }
    try:
        out = hub._conn_settings_from_profile("__test__")
        assert "NOT_A_REAL_GLOBAL" not in out
        assert "LAUNCH_CONN_UNI_MIN_DB" not in out   # lives in OTDR Settings now
    finally:
        hub.CUSTOMER_PROFILES.pop("__test__", None)


# ── 5. The real gesture: pick the customer from the dropdown ─────────────
def test_picking_the_contract_profile_in_the_dropdown_moves_both_panels():
    """End to end through the actual widget, because everything above tests
    helpers.  Picking the customer must move the threshold table AND the
    connector knob — and switching to another customer must take the contract profile's
    connector setting back off, or the next span silently inherits it."""
    from conftest import run_streamlit, go_tab

    def _get(at, key, default=None):
        # AppTest's session_state proxy has no .get().
        try:
            return at.session_state[key]
        except Exception:
            return default

    at = run_streamlit().run()
    at.session_state["view_dir_a_input"] = str(FIXTURE_SPLICE_A_DIR)
    at.session_state["view_dir_b_input"] = str(FIXTURE_SPLICE_B_DIR)
    go_tab(at, "Splice Report")
    assert not at.exception, list(at.exception)

    assert CONTRACT_PROFILE in at.selectbox[0].options, "the contract profile must be pickable"
    _u = lambda: (_get(at, "otdr_settings") or {})["unidir_connector_loss"]
    assert _u()["apply"] and _u()["fail"] == 0.649

    at.selectbox[0].set_value(CONTRACT_PROFILE).run()
    assert not at.exception, list(at.exception)
    assert _get(at, "otdr_profile") == CONTRACT_PROFILE
    s = _get(at, "otdr_settings") or {}
    assert s["bidir_splice_loss"]["fail"] == 0.200
    assert s["bidir_connector_loss"]["fail"] == 0.500
    assert s["reflectance"]["fail"] == -55.0
    assert _u()["apply"] is False

    # Leaving the contract profile must not leave its connector rule behind.
    at.selectbox[0].set_value("Lumen").run()
    assert not at.exception, list(at.exception)
    assert _u()["apply"] and _u()["fail"] == 0.50, \
        "the contract profile's one-sided-gate-off leaked into the next customer"

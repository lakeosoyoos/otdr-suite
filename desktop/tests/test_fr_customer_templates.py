"""Customer profiles rebuilt from the FastReporter3 .prj templates (Sep 2026).

Each template applies one threshold set to all 16 wavelengths, so a customer
is the handful of FAIL values below.  The mapping onto the engine is the one
the AWS / IIG profile established; these tests pin that every template value
lands on the global that grades it, through the same _overrides_from_settings
and _conn_settings_from_profile paths the hub uses at run time.
"""
from __future__ import annotations

import app as hub

# (profile, unidir splice, bidir splice, bidir connector, reflectance,
#  ORL floor, FR unidir connector fail)  -- FAIL values from the .prj files.
FR_TEMPLATES = [
    ("Lumen",                       0.250, 0.150, 0.500, -50.0, 30.0, 0.50),
    ("Zayo",                        0.300, 0.100, 0.500, -50.0, 30.0, 0.50),
    ("AT&T",                        0.750, 0.300, 0.500, -40.0, 30.0, 0.50),
    ("AT&T (Fusion)",               0.300, 0.300, 0.500, -40.0, 29.0, 0.75),
    ("AT&T (Rotary)",               0.300, 0.500, 0.750, -27.0, 27.0, 1.00),
    ("Blackfoot",                   0.300, 0.300, 0.500, -50.0, 30.0, 0.50),
    ("BrightSpeed",                 0.250, 0.150, 0.500, -50.0, 30.0, 0.50),
    ("Ciena (RAMAN)",               0.800, 0.800, 0.500, -33.0, 27.0, 0.80),
    ("Intermountain (FR template)", 0.200, 0.080, 0.300, -55.0, 30.0, 0.30),
    ("Meta",                        0.250, 0.250, 0.500, -50.0, 30.0, 0.50),
    ("Microsoft",                   0.250, 0.250, 0.500, -50.0, 29.0, 0.50),
]


def test_every_template_customer_is_selectable():
    for row in FR_TEMPLATES:
        assert row[0] in hub.CUSTOMER_PROFILES, row[0]


def test_template_fail_values_reach_the_engine_globals():
    for name, uni, bidi, conn, refl, orl, _ in FR_TEMPLATES:
        ov = hub._overrides_from_settings(hub._otdr_settings_from_profile(name))
        assert ov["SINGLE_DIR_THRESHOLD"] == uni, name
        assert ov["REBURN_THRESHOLD"] == bidi, name
        assert ov["BIDIR_CONNECTOR_LOSS"] == conn, name
        assert ov["LAUNCH_BAD_REFL_DB"] == refl, name
        assert ov["SPAN_ORL_MIN_DB"] == orl, name


def test_template_connector_values_reach_the_connector_knobs():
    """FR Connector Loss grades either direction alone; FR Bidir Connector
    Loss grades (A+B)/2.  Both knobs carry the template's own value."""
    for name, _, _, conn, _, _, uni_conn in FR_TEMPLATES:
        cs = hub._conn_settings_from_profile(name)
        assert hub._overrides_from_settings(hub._otdr_settings_from_profile(name))["LAUNCH_CONN_UNI_MIN_DB"] == uni_conn, name
        assert cs["LAUNCH_CONN_AVG_MIN_DB"] == conn, name


def test_templates_leave_the_iig_only_switches_alone():
    """No template carries the contract block, an engine block, the average
    splice gate or the attenuation gate: those are the IIG contract's, not a
    FastReporter template's."""
    for name, *_ in FR_TEMPLATES:
        prof = hub.CUSTOMER_PROFILES[name]
        assert not prof.get("engine"), name
        assert not prof.get("contract"), name
        ov = hub._overrides_from_settings(hub._otdr_settings_from_profile(name))
        assert ov.get("AVG_SPLICE_LOSS_DB") == 0.0, name
        assert ov.get("FIBER_ATTEN_DB_KM") == 0.0, name


def test_intermountain_template_is_not_the_contract_profile():
    """The template grades every bidir splice at 0.08 (the contract's
    per-fiber AVERAGE) and connectors at 0.30 (the RFP figure the SOW
    superseded).  Both profiles stay, and they must not drift together."""
    t = hub._overrides_from_settings(
        hub._otdr_settings_from_profile("Intermountain (FR template)"))
    c = hub._overrides_from_settings(
        hub._otdr_settings_from_profile("AWS / IIG MT.1085"))
    assert t["REBURN_THRESHOLD"] == 0.080 and c["REBURN_THRESHOLD"] == 0.200
    assert t["BIDIR_CONNECTOR_LOSS"] == 0.300 and c["BIDIR_CONNECTOR_LOSS"] == 0.500

"""cable_db: the cable construction → helix (EFL%/m) knowledge layer.

Covers the registry/lookup, EFL↔m band consistency, extensibility via
register(), GenParams auto-detection (including the empty-cable_code fallback
that is the real behavior on the HOWESPAN→LANCASTER span), and the
resolve_cable_type precedence (manual → genparams → default) with provenance.
"""

import pytest

from helixcal import cable_db


# ── Registry / lookups ──────────────────────────────────────────────────
def test_seed_entries_present():
    types = cable_db.all_types()
    assert "stranded_loose_tube" in types
    assert "central_tube" in types
    # default is a registered type
    assert cable_db.DEFAULT_CABLE_TYPE in types


def test_aen142_bands_match_note():
    slt = cable_db.get("stranded_loose_tube")
    ct = cable_db.get("central_tube")
    # AEN-142: stranded loose-tube m 0.97–0.98 ; central tube 0.99–1.00.
    assert (slt.m_low, slt.m_high) == (0.970, 0.980)
    assert (ct.m_low, ct.m_high) == (0.990, 1.000)


def test_efl_band_is_m_band_inverted():
    # EFL% = (1/m − 1)·100, so efl_low corresponds to m_high and vice versa.
    for e in cable_db.entries():
        assert abs(e.efl_low - (1.0 / e.m_high - 1.0) * 100.0) < 1e-3
        assert abs(e.efl_high - (1.0 / e.m_low - 1.0) * 100.0) < 1e-3
        # stranded loose-tube EFL lands in the ~2–3 % AEN-142 range
    slt = cable_db.get("stranded_loose_tube")
    assert 1.9 < slt.efl_low < 2.2 and 2.9 < slt.efl_high < 3.2


def test_band_for_and_bands_map_shape():
    band = cable_db.band_for("stranded_loose_tube")
    assert band[0] == 0.970 and band[1] == 0.980 and isinstance(band[2], str)
    m = cable_db.bands_map()
    assert set(m.keys()) == set(cable_db.all_types())
    assert m["central_tube"][:2] == (0.990, 1.000)
    assert cable_db.band_for("not_a_type") is None


def test_register_is_extensible():
    key = "test_microduct_xyz"
    try:
        cable_db.register(cable_db._entry(
            key, "microduct test", "micro-duct", 0.982, 0.992,
            match_tokens=("MICRODUCT", "UDUCT")))
        assert key in cable_db.all_types()
        e = cable_db.get(key)
        assert (e.m_low, e.m_high) == (0.982, 0.992)
        # match token now resolves
        k, tok = cable_db.detect_from_genparams({"cable_code": "144F MICRODUCT"})
        assert k == key and tok == "MICRODUCT"
    finally:
        cable_db._REGISTRY.pop(key, None)


def test_register_rejects_non_entry():
    with pytest.raises(TypeError):
        cable_db.register({"key": "x"})


# ── GenParams auto-detection ────────────────────────────────────────────
def test_detect_positive_tokens():
    assert cable_db.detect_from_genparams(
        {"cable_code": "144F SLT ALTOS"})[0] == "stranded_loose_tube"
    assert cable_db.detect_from_genparams(
        {"cable_code": "SMU MONOTUBE"})[0] == "central_tube"
    assert cable_db.detect_from_genparams(
        {"cable_code": "RIBBON 864F"})[0] == "ribbon_loose_tube"


def test_detect_token_boundary_guard():
    # 'CT' must not match inside 'OCTANE'; no construction token -> None.
    assert cable_db.detect_from_genparams({"cable_code": "OCTANE 24F"}) == (None, None)


def test_detect_empty_genparams_returns_none():
    # The real HOWESPAN→LANCASTER condition: cable_code empty, cable_id carries
    # no construction token.
    assert cable_db.detect_from_genparams(
        {"cable_id": "HOWLAN001", "location_a": "HOWESPAN",
         "location_b": "LANCASTER"}) == (None, None)
    assert cable_db.detect_from_genparams({}) == (None, None)
    assert cable_db.detect_from_genparams(None) == (None, None)


# ── Resolution precedence + provenance ──────────────────────────────────
def test_resolve_manual_wins():
    gp = {"cable_code": "SMU MONOTUBE"}  # would auto-detect central_tube
    r = cable_db.resolve_cable_type(explicit="stranded_loose_tube", genparams=gp)
    assert r.cable_type == "stranded_loose_tube"
    assert r.source == "manual"
    assert r.band[:2] == (0.970, 0.980)


def test_resolve_manual_unknown_is_kept_but_no_band():
    r = cable_db.resolve_cable_type(explicit="banana_cable")
    assert r.cable_type == "banana_cable"
    assert r.source == "manual"
    assert r.entry is None and r.band is None
    assert "not in the cable DB" in r.note


def test_resolve_genparams_autodetect():
    r = cable_db.resolve_cable_type(
        explicit=None, genparams={"cable_code": "144F SLT"})
    assert r.cable_type == "stranded_loose_tube"
    assert r.source == "genparams"
    assert r.matched_token == "SLT"


def test_resolve_default_fallback_on_empty_genparams():
    # The span's real path: no manual type, GenParams empty -> default.
    r = cable_db.resolve_cable_type(
        explicit=None,
        genparams={"cable_id": "HOWLAN001", "location_a": "HOWESPAN"})
    assert r.cable_type == cable_db.DEFAULT_CABLE_TYPE
    assert r.source == "default"
    assert "default" in r.note.lower()
    assert r.band is not None  # default still carries a sanity band


def test_resolve_no_genparams_at_all():
    r = cable_db.resolve_cable_type(explicit=None, genparams=None)
    assert r.source == "default"
    assert r.cable_type == cable_db.DEFAULT_CABLE_TYPE

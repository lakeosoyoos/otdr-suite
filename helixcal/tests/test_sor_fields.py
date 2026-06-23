"""sor_fields adapter: stored IOR (FxdParams body+28) and GenParams locations.

Verifies the two fields the stock parser does not surface, reusing the real
committed SOR fixtures.  These assertions pin the survey's verified facts:
stored IOR == 1.47000 at FxdParams body+28, and the GenParams last-occurrence
fix yields the cable_id + endpoint location codes.
"""

import struct

from helixcal import sor_fields


def test_stored_ior_is_147(one_sor):
    ior = sor_fields.read_stored_ior(one_sor)
    assert ior is not None, "stored IOR should be readable on the fixture"
    # Verified value on the EXFO span: 147000 -> 1.47000.
    assert abs(ior - 1.47000) < 1e-6


def test_stored_ior_matches_fxdparams_offset(one_sor):
    # Cross-check the helper against a direct FxdParams body+28 read so the
    # offset constant is pinned, not just the resulting value.
    with open(one_sor, "rb") as f:
        data = f.read()
    blocks = sor_fields._sr._parse_block_directory(data)
    raw = struct.unpack_from("<I", data, blocks["FxdParams"]["body"] + 28)[0]
    assert sor_fields.read_stored_ior(one_sor) == raw / 100000.0


def test_stored_ior_sane_band_rejects_garbage():
    # An out-of-band uint32 must come back as None, not a stray IOR.
    fake = b"\x00" * 64
    # Build a minimal blocks dict pointing FxdParams at a zero region.
    blocks = {"FxdParams": {"body": 0}}
    assert sor_fields.read_stored_ior_from_blocks(fake, blocks) is None


def test_genparams_locations(one_sor):
    gp = sor_fields.read_genparams(one_sor)
    # Fiber id is the cable_id string; A-direction fixtures are ELMMIL####.
    assert gp["cable_id"].upper().startswith("ELMMIL")
    # Endpoint location codes are populated (last-occurrence GenParams fix);
    # the directory-header occurrence would have yielded block names instead.
    assert gp["location_a"]
    assert "SUPPARAMS" not in gp["location_a"].upper()
    assert "FXDPARAMS" not in gp["location_a"].upper()


def test_read_trace_record_shape(one_sor):
    rec = sor_fields.read_trace_record(one_sor)
    assert rec is not None
    for key in ("events", "stored_ior", "derived_ior", "ior", "ior_source",
                "genparams", "eof_km", "wavelength"):
        assert key in rec
    assert rec["ior_source"] == "stored"
    assert rec["eof_km"] and rec["eof_km"] > 1.0
    # Stored and event-derived IOR agree to ~4 dp (survey cross-check).
    assert abs(rec["stored_ior"] - rec["derived_ior"]) < 1e-3

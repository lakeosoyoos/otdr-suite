"""Direction grouping for iOLM exports that name both directions alike.

The hub sorts a span's files into its two directions by the filename's
leading alpha run: SEANOR001 and NORSEA001 are two groups.  NCT's iOLM
exports (AWS / IIG MT.1085) name BOTH directions with the same cable id and
tell them apart only by a -AB / -BA token:

    MSO401-MSO402-OSP-0432F-01-0001-AB_1550.sor
    MSO401-MSO402-OSP-0432F-01-0001-BA_1550.sor

so every file keyed 'MSO' and Load span stopped with "Found only 1 direction
group".  direction_prefix now appends an explicit direction token when the
filename carries one.  Files without the token key exactly as before, which
is what the last two tests hold.
"""
from __future__ import annotations

import os
import sys

from conftest import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT))
import folder_intake as fi  # noqa: E402


def test_iolm_ab_ba_names_split_into_two_groups():
    names = [f"MSO401-MSO402-OSP-0432F-01-{n:04d}-{d}_1550.sor"
             for n in range(1, 6) for d in ("AB", "BA")]
    groups = fi.split_paths_by_direction(names)
    assert set(groups) == {"MSO-AB", "MSO-BA"}
    assert len(groups["MSO-AB"]) == 5 and len(groups["MSO-BA"]) == 5


def test_token_is_read_before_the_wavelength_suffix_and_the_extension():
    assert fi.direction_prefix("RPX401-JDN400-OSP-0432F-01-0001-AB_1625.sor") == "RPX-AB"
    assert fi.direction_prefix("RPX401-JDN400-OSP-0432F-01-0001-BA.iolm") == "RPX-BA"
    assert fi.direction_prefix("RPX401-JDN400-OSP-0432F-01-0001-BA.json") == "RPX-BA"
    assert fi.direction_prefix("cable_0007_ab_1550nm.sor") == "CABLE-AB"


def test_materialize_places_iolm_directions_in_a_and_b(tmp_path):
    src = tmp_path / "span"; src.mkdir()
    for n in range(1, 4):
        for d in ("AB", "BA"):
            (src / f"MSO401-MSO402-OSP-0432F-01-{n:04d}-{d}_1550.sor").write_bytes(b"x")
    paths = fi.find_otdr_files(str(src))
    dir_a, dir_b, info = fi.materialize_two_directions(paths, str(tmp_path / "work"))
    assert info["a_prefix"] == "MSO-AB" and info["b_prefix"] == "MSO-BA"
    assert sorted(os.listdir(dir_a)) == sorted(f for f in os.listdir(src) if "-AB_" in f)
    assert sorted(os.listdir(dir_b)) == sorted(f for f in os.listdir(src) if "-BA_" in f)


def test_site_named_files_key_exactly_as_before():
    assert fi.direction_prefix("SEANOR001_1550.sor") == "SEANOR"
    assert fi.direction_prefix("NORSEA001_1550.sor") == "NORSEA"
    assert fi.direction_prefix("HOWLAN559.sor") == "HOWLAN"
    assert fi.direction_prefix("WSC_SUI_0001.sor") == "WSC"
    assert fi.direction_prefix("ELMMIL0001.sor") == "ELMMIL"


def test_a_location_code_containing_ab_is_not_a_direction():
    # 'AB' inside a code, or not sitting as its own tail field, is not a token.
    assert fi.direction_prefix("ABILENE001_1550.sor") == "ABILENE"
    assert fi.direction_prefix("LAB-AB12-0001_1550.sor") == "LAB"
    assert fi.direction_prefix("SEANOR-AB-extra-0001.sor") == "SEANOR"

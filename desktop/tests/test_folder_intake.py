"""Regression for the one-folder / zip intake.

A single folder (or a .zip) holding BOTH directions is auto-split into A/B by
filename prefix, and reports save to Downloads (not the traces folder).  The
intake module is engine-free + stdlib-only, so it imports directly.
"""
import os
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
import folder_intake as fi  # noqa: E402

FX = REPO_ROOT / "desktop" / "tests" / "fixtures"


def _both_dirs_files():
    return (fi.find_otdr_files(str(FX / "splice_A")) +
            fi.find_otdr_files(str(FX / "splice_B")))


def test_split_by_direction_groups_by_prefix():
    groups = fi.split_paths_by_direction(_both_dirs_files())
    assert set(groups) == {"ELMMIL", "MILELM"}
    assert len(groups["ELMMIL"]) == 24 and len(groups["MILELM"]) == 24


def test_materialize_two_directions(tmp_path):
    da, db, info = fi.materialize_two_directions(_both_dirs_files(), str(tmp_path))
    assert os.path.isdir(da) and os.path.isdir(db)
    assert info["a_count"] == 24 and info["b_count"] == 24
    assert {info["a_prefix"], info["b_prefix"]} == {"ELMMIL", "MILELM"}
    assert len(fi.find_otdr_files(da)) == 24
    assert len(fi.find_otdr_files(db)) == 24


def test_materialize_requires_two_directions(tmp_path):
    one_direction = fi.find_otdr_files(str(FX / "splice_A"))   # single prefix
    with pytest.raises(ValueError):
        fi.materialize_two_directions(one_direction, str(tmp_path))


def test_extract_zip_roundtrip(tmp_path):
    zp = tmp_path / "both.zip"
    with zipfile.ZipFile(zp, "w") as z:
        for f in _both_dirs_files():
            z.write(f, os.path.basename(f))
    out = fi.extract_zip(str(zp), str(tmp_path / "unz"))
    assert len(out) == 48
    assert set(fi.split_paths_by_direction(out)) == {"ELMMIL", "MILELM"}


def test_extract_zip_skips_zip_slip(tmp_path):
    zp = tmp_path / "evil.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("../escape.sor", b"x")               # path-traversal attempt
        z.writestr("ELMMIL0001_1550.sor", b"x")
    fi.extract_zip(str(zp), str(tmp_path / "unz"))
    assert not (tmp_path / "escape.sor").exists()        # traversal blocked


def test_extract_zip_caps_oversized_member(tmp_path, monkeypatch):
    """SECURITY: a malicious/corrupt zip must not disk-fill the tech's machine —
    an oversized decompressed member is skipped; a normal one still extracts."""
    monkeypatch.setattr(fi, "_ZIP_MEMBER_MAX", 1000)
    monkeypatch.setattr(fi, "_ZIP_TOTAL_MAX", 10_000)
    zp = tmp_path / "bomb.zip"
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("SEANOR0001_1550.sor", b"x" * 500)        # under cap → kept
        z.writestr("BOMB0002_1550.sor", b"\x00" * 50_000)    # over cap → dropped
    dest = tmp_path / "unz"
    out = fi.extract_zip(str(zp), str(dest))
    names = sorted(os.path.basename(p) for p in out)
    assert "SEANOR0001_1550.sor" in names                    # legit member survives
    assert not (dest / "BOMB0002_1550.sor").exists()         # oversized member dropped


def test_materialize_all_flattens_both_directions(tmp_path):
    # The unified "Load span" gives Secret Sauce ONE combined folder.
    dest = fi.materialize_all(_both_dirs_files(), str(tmp_path / "all"))
    assert os.path.isdir(dest)
    assert len(fi.find_otdr_files(dest)) == 48
    assert set(fi.split_paths_by_direction(fi.find_otdr_files(dest))) == {"ELMMIL", "MILELM"}


def test_default_report_dir_exists():
    assert os.path.isdir(fi.default_report_dir())

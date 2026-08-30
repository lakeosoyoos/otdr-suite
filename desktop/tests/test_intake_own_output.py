"""The hub must not inventory its OWN output as input.

`folder_intake.find_otdr_files` is the loader the Viewer, the Splice Report and
Secret Sauce all share.  Its docstring has always said it filters dot-prefixed
files because "a leading-`.` name has no alpha prefix, so it spawns a junk
direction group".  The code only ever matched the AppleDouble prefix `._`.

The hub writes its own report caches INTO the folder the user picked:

    .sr_grid_cache.json / .srfr_grid_cache.json    app.py:2412
    .uni_result_cache.json                         app.py:3136
    SecretSauce_reports/pairs_cache.json           the engine's out_dir

so every one of those was inventoried as an acquisition, and each spawned
exactly the junk direction group the docstring warns about.  Measured on disk
before the fix:

    SEANOR 6.15.2026                found 434 of 432
        SEANOR 432 | .SR_GRID_CACHE.JSON 1 | .SRFR_GRID_CACHE.JSON 1
    Lumen 432 Boarder Project UNI   found 434 of 432
        LAMBEY 432 | .UNI_RESULT_CACHE.JSON 1 | PAIRS 1

That is folder poisoning, not just a bad count.  SEANOR is a SINGLE-direction
folder, so it correctly raised "Found only 1 direction group" until a report was
run on it - after which it presents three groups and materialize_two_directions
pairs 432 real traces against one cache file.  Running a report on a folder
broke that folder's next intake.

The engine has always been immune: run_secretsauce._inventory skips every
dotfile and prunes _SKIP_DIRS, with a comment naming these same caches and the
LAMBEY folder that exposed them.  Only the shared loader was left behind.
"""
from __future__ import annotations

import os
import shutil

import folder_intake as fi

from conftest import FIXTURE_A_DIR, FIXTURE_B_DIR


def _span(tmp_path, name="span"):
    d = tmp_path / name
    d.mkdir()
    for src in list(FIXTURE_A_DIR.glob("*.sor")) + list(FIXTURE_B_DIR.glob("*.sor")):
        shutil.copy(src, d / src.name)
    return d


def test_report_caches_are_not_acquisitions(tmp_path):
    """The three real cache names the hub writes into the picked folder."""
    d = _span(tmp_path)
    clean = fi.find_otdr_files(str(d))
    for cache in (".sr_grid_cache.json", ".srfr_grid_cache.json",
                  ".uni_result_cache.json"):
        (d / cache).write_text("{}", encoding="utf-8")
    assert fi.find_otdr_files(str(d)) == clean, "a cache file was counted as input"


def test_running_a_report_does_not_poison_the_next_load(tmp_path):
    """The failure in full: a single-direction folder must keep reporting ONE
    direction group after a report has written its cache beside the traces."""
    d = tmp_path / "one_direction"
    d.mkdir()
    for src in FIXTURE_A_DIR.glob("*.sor"):
        shutil.copy(src, d / src.name)
    before = fi.split_paths_by_direction(fi.find_otdr_files(str(d)))
    assert len([k for k, v in before.items() if v]) == 1, before

    (d / ".sr_grid_cache.json").write_text("{}", encoding="utf-8")
    (d / ".srfr_grid_cache.json").write_text("{}", encoding="utf-8")
    after = fi.split_paths_by_direction(fi.find_otdr_files(str(d)))
    assert len([k for k, v in after.items() if v]) == 1, (
        f"caches spawned junk direction groups: {sorted(after)}")


def test_our_own_output_directory_is_pruned(tmp_path):
    """SecretSauce_reports/pairs_cache.json produced a 'PAIRS' direction group
    on the real LAMBEY folder.  The engine prunes this dir; so must the loader."""
    d = _span(tmp_path)
    clean = fi.find_otdr_files(str(d))
    rep = d / "SecretSauce_reports"
    rep.mkdir()
    (rep / "pairs_cache.json").write_text("{}", encoding="utf-8")
    (rep / "report.json").write_text("{}", encoding="utf-8")
    assert fi.find_otdr_files(str(d)) == clean, "our own output was read back as input"
    assert "SecretSauce_reports" in fi.SKIP_DIRS


def test_macos_artifacts_still_skipped(tmp_path):
    """The original rule must survive: AppleDouble sidecars and __MACOSX/."""
    d = _span(tmp_path)
    clean = fi.find_otdr_files(str(d))
    (d / "._SEANOR001_1550.sor").write_bytes(b"junk")
    mac = d / "__MACOSX"
    mac.mkdir()
    (mac / "SEANOR002_1550.sor").write_bytes(b"junk")
    assert fi.find_otdr_files(str(d)) == clean


def test_real_acquisitions_are_untouched(tmp_path):
    """A dot inside the name is fine - only a LEADING dot is our own artifact."""
    d = _span(tmp_path)
    n = len(fi.find_otdr_files(str(d)))
    src = next(FIXTURE_A_DIR.glob("*.sor"))
    shutil.copy(src, d / "Seattle to Spokane d.0431.sor")
    assert len(fi.find_otdr_files(str(d))) == n + 1


def test_the_loader_matches_the_engine_it_feeds():
    """Both the dotfile rule and the skip-dir set exist in the engine already.
    Keeping them in step is the point - the engine was immune to this for
    months while the loader in front of it was not."""
    from conftest import SECRETSAUCE_DIR
    eng = (SECRETSAUCE_DIR / "run_secretsauce.py").read_text(encoding="utf-8")
    assert "_SKIP_DIRS = {'SecretSauce_reports', '__MACOSX'}" in eng
    assert fi.SKIP_DIRS == {"SecretSauce_reports", "__MACOSX"}, fi.SKIP_DIRS

    src = (fi.__file__)
    text = open(src, encoding="utf-8").read()
    i = text.index("def find_otdr_files(")
    body = text[i:i + 3000]
    assert "if fn.startswith('.'):" in body, "loader must skip ALL dotfiles"
    assert "dirs[:] = [d for d in dirs if d not in SKIP_DIRS]" in body


def test_the_zip_walks_prune_our_output_too(tmp_path):
    """The prune has to hold in ALL THREE walks, not just find_otdr_files.

    Caught by adversarial review of this very change: zip_paths' docstring
    claimed "same prune rules as find_otdr_files" while checking only
    __MACOSX, and find_otdr_files_with_zips kept its own private zip walk
    that still matched '._' rather than '.'.  A zip sitting in
    SecretSauce_reports/ was therefore descended into and its traces pulled
    back in as input - 6 files reported where 4 exist.  Nothing writes a zip
    there today, but a half-applied prune is how the next one gets through.
    """
    import zipfile
    d = _span(tmp_path)
    clean = len(fi.find_otdr_files(str(d)))
    src = sorted(FIXTURE_A_DIR.glob("*.sor"))

    rep = d / "SecretSauce_reports"
    rep.mkdir()
    with zipfile.ZipFile(rep / "leftover.zip", "w") as z:
        for p in src[:2]:
            z.write(p, p.name)
    # a LEGITIMATE zip beside the traces must still be descended into
    with zipfile.ZipFile(d / "extra.zip", "w") as z:
        z.write(src[0], "ELMMIL9001_1550.sor")

    assert [os.path.basename(p) for p in fi.zip_paths(str(d))] == ["extra.zip"], (
        "a zip in our own output directory was offered for descent")
    got = fi.find_otdr_files_with_zips(str(d), str(tmp_path / "_ex"))
    assert len(got) == clean + 1, (
        f"expected {clean} loose + 1 from the legitimate zip, got {len(got)}")


def test_all_three_walks_share_one_prune():
    """find_otdr_files_with_zips must not keep a private copy of the walk -
    that is exactly how the two drifted apart."""
    text = open(fi.__file__, encoding="utf-8").read()
    i = text.index("def find_otdr_files_with_zips(")
    body = text[i:i + 1500]
    assert "zips = zip_paths(folder)" in body, (
        "the zip walk was duplicated again instead of reusing zip_paths")
    zp = text[text.index("def zip_paths("):][:600]
    assert "dirs[:] = [d for d in dirs if d not in SKIP_DIRS]" in zp

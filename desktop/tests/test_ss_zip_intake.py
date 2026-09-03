"""Regression tests for Secret Sauce's zip-aware intake and the re-delivery gate.

A span is often delivered as per-direction zips with nothing loose beside them.
Measured on disk before this change, the Secret Sauce runner reported:

    CHE to PLA 1152f        0 loose .sor, 2 zips holding 1152 traces each
    Deming Tie Panel        0 loose .sor, 2 zips holding  288 traces each
        -> {"ok": false, "error": "No .sor, .trc, or .json files found."}

The hub already owned the zip-descending loader (with zip-slip and size-cap
hardening); the Secret Sauce page just never routed through it, because
app.py hands the picked folder straight to run_secretsauce.py.

RE-DELIVERY GATE.  A folder can hold the loose files AND a zip of the same
files: `WInterhaven to Niland Final Traces` carries 576 loose .sor and an
Archive.zip of those same 576.  Taking both would compare every fiber against
its own copy.  A file from a zip is skipped when one with the SAME basename AND
the same sha256 is already in hand.

Name AND content, deliberately - see test_byte_copy_under_a_different_name_survives.
A byte-identical copy under a different name is a duplicate the engine is
SUPPOSED to find (the raw-identity short-circuit exists for exactly that), so
content alone would have silenced a real detection.

Measured after the change:
    Deming Tie Panel   "added 288 file(s)" x2   -> 576 files analysed
    WInterhaven        "skipped, all 576 file(s) already present outside the zip"
                       -> 576 files, 2 flagged (unchanged from the loose-only run)
    EMVSUI Long (no zips) -> byte-identical, 0 pairs moved, no zip_notes key
"""
from __future__ import annotations

import json
import os
import shutil
import zipfile

from conftest import FIXTURE_A_DIR, run_secretsauce


def _fixture_sors():
    return sorted(FIXTURE_A_DIR.glob("*.sor"))


def _zip_of(paths, zip_path, arcnames=None):
    with zipfile.ZipFile(zip_path, "w") as zf:
        for i, p in enumerate(paths):
            zf.write(p, (arcnames[i] if arcnames else os.path.basename(p)))
    return zip_path


def _notes(stderr):
    return [l.split("Zip intake: ", 1)[1]
            for l in (stderr or "").splitlines() if l.startswith("Zip intake: ")]


def test_folder_of_only_zips_is_no_longer_empty(tmp_path):
    """The reported failure: every trace is inside a zip, so the walk found
    nothing and the run dead-ended while the tech was looking at a folder that
    plainly contained the span."""
    d = tmp_path / "zips_only"
    d.mkdir()
    sors = _fixture_sors()
    _zip_of(sors[:2], d / "dirA.zip")
    _zip_of(sors[2:4], d / "dirB.zip")
    assert not list(d.glob("*.sor")), "fixture must have NO loose traces"

    rc, m, err = run_secretsauce(d, tmp_path / "out", "pairs")
    assert rc == 0 and m and m.get("ok"), f"runner failed: {(err or '')[-800:]}"
    assert m["counts"]["sor"] == 4, m["counts"]
    assert m.get("zip_notes"), "a zip was consulted; the manifest must say so"
    assert all("added" in n for n in m["zip_notes"]), m["zip_notes"]


def test_zip_that_redelivers_the_loose_files_is_skipped(tmp_path):
    """WInterhaven's shape: 576 loose .sor plus an Archive.zip of the same 576.
    Taking both compares every fiber against its own copy."""
    d = tmp_path / "redelivery"
    d.mkdir()
    sors = _fixture_sors()
    for p in sors:
        shutil.copy(p, d / p.name)
    _zip_of(sors, d / "Archive.zip")

    rc, m, err = run_secretsauce(d, tmp_path / "out", "pairs")
    assert rc == 0 and m and m.get("ok"), f"runner failed: {(err or '')[-800:]}"
    assert m["counts"]["sor"] == len(sors), (
        f"re-delivered zip inflated the input: {m['counts']}")
    note = " ".join(m.get("zip_notes") or [])
    assert "skipped" in note and "already present" in note, m.get("zip_notes")
    # And the skip must be reported on stderr too, not only in the manifest.
    assert any("skipped" in n for n in _notes(err)), _notes(err)


def test_zip_with_extra_files_contributes_only_the_new_ones(tmp_path):
    """A partial overlap must not cost the extra fibers.  Silently ignoring a
    zip because it *mostly* duplicates would be the same class of bug as
    taking it whole."""
    d = tmp_path / "partial"
    d.mkdir()
    sors = _fixture_sors()
    for p in sors[:2]:
        shutil.copy(p, d / p.name)
    _zip_of(sors, d / "all.zip")          # 2 already loose + 2 new

    rc, m, err = run_secretsauce(d, tmp_path / "out", "pairs")
    assert rc == 0 and m and m.get("ok"), f"runner failed: {(err or '')[-800:]}"
    assert m["counts"]["sor"] == len(sors), m["counts"]
    note = " ".join(m.get("zip_notes") or [])
    assert "added 2" in note and "skipped 2" in note, m.get("zip_notes")


def test_byte_copy_under_a_different_name_survives(tmp_path):
    """The gate keys on basename AND content.  A byte-identical copy under a
    DIFFERENT name is a real duplicate the engine must still see - dropping it
    would have turned an intake fix into a silenced detection."""
    d = tmp_path / "planted"
    d.mkdir()
    sors = _fixture_sors()
    for p in sors:
        shutil.copy(p, d / p.name)
    # same bytes as sors[0], different filename, delivered inside a zip
    _zip_of([sors[0]], d / "extra.zip", arcnames=["ELMMIL9001_1550.sor"])

    rc, m, err = run_secretsauce(d, tmp_path / "out", "pairs")
    assert rc == 0 and m and m.get("ok"), f"runner failed: {(err or '')[-800:]}"
    assert m["counts"]["sor"] == len(sors) + 1, (
        f"a byte copy under a new name was swallowed by the gate: {m['counts']}")
    hits = [p for p in m["pairs"] if p.get("raw_identical")]
    assert hits, "the planted copy must still be caught as a duplicate"


def test_corrupt_zip_is_reported_and_does_not_abort_the_run(tmp_path):
    d = tmp_path / "corrupt"
    d.mkdir()
    for p in _fixture_sors():
        shutil.copy(p, d / p.name)
    (d / "broken.zip").write_bytes(b"PK\x03\x04 this is not a zip")

    rc, m, err = run_secretsauce(d, tmp_path / "out", "pairs")
    assert rc == 0 and m and m.get("ok"), f"a bad zip aborted the run: {(err or '')[-800:]}"
    assert m["counts"]["sor"] == len(_fixture_sors())
    assert any("unreadable" in n for n in (m.get("zip_notes") or [])), m.get("zip_notes")


def test_zip_free_folder_is_untouched(tmp_path):
    """No zips means no behaviour change at all, and no manifest key - the
    additive contract every other optional key here follows."""
    d = tmp_path / "plain"
    d.mkdir()
    for p in _fixture_sors():
        shutil.copy(p, d / p.name)

    rc, m, err = run_secretsauce(d, tmp_path / "out", "pairs")
    assert rc == 0 and m and m.get("ok"), f"runner failed: {(err or '')[-800:]}"
    assert "zip_notes" not in m, "zip_notes must be absent when no zip was seen"
    assert not _notes(err), _notes(err)


# ── the shared OTDR_EXTS constant must not widen ───────────────────────────

def test_trc_is_available_to_secret_sauce_but_not_to_the_shared_loader():
    """OTDR_EXTS feeds the unified span loader the Viewer and Splice Report
    share; widening it there would hand those two files they cannot open.
    Secret Sauce asks for TRC explicitly instead."""
    import folder_intake as fi
    assert fi.OTDR_EXTS == ('.sor', '.json'), (
        "shared loader extension set changed; Viewer/Splice Report would see .trc")
    assert '.trc' in fi.OTDR_EXTS_WITH_TRC


def test_secret_sauce_asks_for_the_trc_extension_set():
    from conftest import SECRETSAUCE_DIR
    src = (SECRETSAUCE_DIR / "run_secretsauce.py").read_text(encoding="utf-8")
    assert "fi.OTDR_EXTS_WITH_TRC" in src, "runner must request the TRC-inclusive set"
    assert "fi.content_key" in src, "the re-delivery gate must key on content"
    assert "fi.zip_paths" in src


def test_extract_dir_is_cleaned_up(tmp_path):
    """The extraction is a temp copy of the whole span; leaving it behind would
    quietly fill the disk on every run of a zipped folder."""
    import tempfile
    d = tmp_path / "cleanup"
    d.mkdir()
    _zip_of(_fixture_sors(), d / "span.zip")
    before = {n for n in os.listdir(tempfile.gettempdir()) if n.startswith("ss_zip_")}
    rc, m, _err = run_secretsauce(d, tmp_path / "out", "pairs")
    assert rc == 0 and m and m.get("ok")
    after = {n for n in os.listdir(tempfile.gettempdir()) if n.startswith("ss_zip_")}
    assert after <= before, f"left extraction dirs behind: {after - before}"

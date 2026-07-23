"""Pytest suite for the OTDR Suite SECRET SAUCE runner.

HARD RULE — namespace isolation
-------------------------------
Secret Sauce ships its OWN sor_reader324802a.py that collides with the
viewer's copy.  This test process must NEVER import the secretsauce package,
report.py, report_sor.py, run_secretsauce, or its sor_reader directly.  The
runner is exercised ONLY through conftest.run_secretsauce(), which launches it
as a subprocess in a clean namespace — that isolation is the whole point.

To keep total runtime down (each subprocess run = SOR parse + xlsx build,
~a few seconds), the happy path runs ONCE and is shared via a module-scoped
fixture; the negative-path runs are cheap (they fail before building xlsx).
"""
from __future__ import annotations

import json

import pytest

from conftest import (
    run_secretsauce,
    mixed_fixture_dir,
    FIXTURE_A_DIR,
)


# ---------------------------------------------------------------------------
# Shared happy-path run (expensive: full SOR parse + xlsx build).  Run once.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def happy_run(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("happy")
    folder = mixed_fixture_dir(tmp_path)
    out_dir = tmp_path / "out"
    rc, manifest, stderr = run_secretsauce(folder, out_dir, "xlsx")
    return rc, manifest, stderr, out_dir


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------
def test_happy_path(happy_run):
    import os

    rc, manifest, stderr, _out_dir = happy_run
    assert rc == 0, f"runner exited {rc}; stderr tail:\n{(stderr or '')[-800:]}"
    assert manifest is not None, "no JSON manifest parsed from stdout"
    assert manifest["ok"] is True, f"manifest not ok: {manifest}"
    assert manifest["counts"]["sor"] == 8, manifest["counts"]

    written = manifest.get("written")
    assert written, "written list is empty"
    for entry in written:
        path = entry["path"]
        assert path.endswith(".xlsx"), f"not an xlsx path: {path}"
        assert os.path.exists(path), f"written path missing on disk: {path}"
        assert entry["n_files"] >= 1, entry
        assert entry["n_pairs"] >= 1, entry


# ---------------------------------------------------------------------------
# 2. Empty folder
# ---------------------------------------------------------------------------
def test_empty_folder(tmp_path):
    folder = tmp_path / "empty"
    folder.mkdir()
    out_dir = tmp_path / "out"
    rc, manifest, stderr = run_secretsauce(folder, out_dir, "xlsx")
    assert manifest is not None, f"no manifest; stderr:\n{(stderr or '')[-800:]}"
    assert manifest["ok"] is False, manifest
    assert "No .sor" in manifest["error"], manifest["error"]


# ---------------------------------------------------------------------------
# 3. Mixed file types (real .sor + a dummy .json) -> rejected
# ---------------------------------------------------------------------------
def test_mixed_file_types(tmp_path):
    folder = tmp_path / "mixed_types"
    folder.mkdir()
    src = next(FIXTURE_A_DIR.glob("ELMMIL*.sor"))
    (folder / src.name).write_bytes(src.read_bytes())
    (folder / "junk.json").write_bytes(b'{"not": "a report"}')

    out_dir = tmp_path / "out"
    rc, manifest, stderr = run_secretsauce(folder, out_dir, "xlsx")
    assert manifest is not None, f"no manifest; stderr:\n{(stderr or '')[-800:]}"
    assert manifest["ok"] is False, manifest
    assert "Mixed" in manifest["error"], manifest["error"]


# ---------------------------------------------------------------------------
# 4. Too few SOR files (one file can't be compared -> clean error)
# ---------------------------------------------------------------------------
def test_too_few_sor_files(tmp_path):
    folder = tmp_path / "solo"
    folder.mkdir()
    src = next(FIXTURE_A_DIR.glob("ELMMIL*.sor"))
    (folder / src.name).write_bytes(src.read_bytes())

    out_dir = tmp_path / "out"
    rc, manifest, stderr = run_secretsauce(folder, out_dir, "xlsx")
    assert manifest is not None, f"no manifest; stderr:\n{(stderr or '')[-800:]}"
    assert manifest["ok"] is False, manifest
    err = manifest["error"]
    # Needs >=2 SOR files to compare (no direction split anymore).
    assert ">=2" in err and "SOR" in err, err


# 4b. No direction split: a mixed-direction folder yields exactly ONE report
# ---------------------------------------------------------------------------
def test_mixed_directions_single_report(tmp_path):
    """The mixed fixture holds files with DIFFERENT location-direction keys
    (ELMMIL vs MILELM).  Secret Sauce must NOT split by direction — it runs on
    the whole folder and emits exactly ONE report covering all files."""
    folder = mixed_fixture_dir(tmp_path)
    out_dir = tmp_path / "out"
    rc, manifest, stderr = run_secretsauce(folder, out_dir, "xlsx")
    assert rc == 0 and manifest and manifest.get("ok"), f"runner failed: {(stderr or '')[-800:]}"
    written = manifest["written"]
    assert len(written) == 1, (
        f"expected ONE report for the whole folder, got {len(written)}: "
        f"{[w.get('key') for w in written]}")
    assert written[0]["n_files"] == 8, written[0]


# ---------------------------------------------------------------------------
# 5. AppleDouble skip: a ._-prefixed file must NOT be counted.
# ---------------------------------------------------------------------------
def test_appledouble_skipped(tmp_path):
    folder = mixed_fixture_dir(tmp_path)
    # Drop an AppleDouble sidecar that LOOKS like a 9th .sor file.
    (folder / "._ELMMIL0001_1550.sor").write_bytes(b"\x00\x05\x16\x07AppleDouble")

    out_dir = tmp_path / "out"
    rc, manifest, stderr = run_secretsauce(folder, out_dir, "xlsx")
    assert rc == 0, f"runner exited {rc}; stderr tail:\n{(stderr or '')[-800:]}"
    assert manifest is not None
    assert manifest["ok"] is True, manifest
    assert manifest["counts"]["sor"] == 8, manifest["counts"]


# ---------------------------------------------------------------------------
# 6. Manifest hygiene: stdout's manifest is valid JSON on a SINGLE line.
# ---------------------------------------------------------------------------
def test_manifest_is_single_json_line(happy_run):
    rc, manifest, _stderr, _out_dir = happy_run
    # run_secretsauce already parsed the LAST JSON line off stdout; if that
    # succeeded we have a dict with an 'ok' key.  Re-assert the contract here.
    assert isinstance(manifest, dict), type(manifest)
    assert "ok" in manifest, manifest
    # And it round-trips cleanly as a single compact line.
    line = json.dumps(manifest)
    assert "\n" not in line
    assert json.loads(line) == manifest


# ---------------------------------------------------------------------------
# 7. (xfail, strict) Desired-but-unimplemented: content-sniff a .sor file.
#
# TODO: The runner trusts the .sor EXTENSION and never checks the file body is
# really a Telcordia SOR blob.  Two garbage *.sor files that share a direction
# key (so they DO form a >=2 group and reach the parser) currently fail with a
# leaky, generic parser error ("Not enough usable .sor files ...") instead of a
# clear up-front "this .sor isn't a valid SOR file" rejection.  The note below
# pins the desired behavior: a manifest error that names SOR/format validity.
# Remove the xfail once the runner content-sniffs the SOR magic/header and
# emits a SOR-specific error.  (Filenames share their first 8 chars so the
# basename[:8] fallback key groups them together — otherwise each garbage file
# forms its own group of 1 and we'd only exercise the >=2 rule, not content.)
# ---------------------------------------------------------------------------
@pytest.mark.xfail(strict=True, reason="runner trusts .sor extension; no content sniff / SOR-specific error (TODO)")
def test_fake_sor_content_is_rejected(tmp_path):
    folder = tmp_path / "fake_sor"
    folder.mkdir()
    # Two *.sor files with an IDENTICAL first-8-char prefix => same fallback
    # group key => one group of 2 that reaches the SOR parser.  Content is junk.
    (folder / "FAKESORX_a.sor").write_bytes(b"this is not a SOR file" * 10)
    (folder / "FAKESORX_b.sor").write_bytes(b"neither is this one" * 10)

    out_dir = tmp_path / "out"
    rc, manifest, _stderr = run_secretsauce(folder, out_dir, "xlsx")
    assert manifest is not None
    assert manifest["ok"] is False, manifest
    # Desired: the error should name SOR/format validity, not leak a generic
    # "not enough usable files" message from inside the parser.
    assert "SOR" in manifest["error"] and "usable" not in manifest["error"], manifest["error"]


def test_inventory_ignores_dotfiles(tmp_path):
    """The hub writes report caches INTO analyzed folders
    (.uni_result_cache.json / .sr_grid_cache.json).  A dotfile is never an
    acquisition — counting one as JSON aborted a pure-SOR run with the bogus
    'Mixed file types' (click-through audit, LAMBEY uni folder)."""
    import importlib.util
    import os
    _root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    spec = importlib.util.spec_from_file_location(
        'ss_runner_dotfile_test',
        os.path.join(_root, 'secretsauce', 'run_secretsauce.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    (tmp_path / 'LAMBEY001_1550.sor').write_bytes(b'x')
    (tmp_path / '.uni_result_cache.json').write_text('{}')
    (tmp_path / '.sr_grid_cache.json').write_text('{}')
    (tmp_path / '._LAMBEY002_1550.sor').write_bytes(b'x')
    sor, trc, jsn = mod._inventory(str(tmp_path))
    assert len(sor) == 1 and jsn == [] and trc == []

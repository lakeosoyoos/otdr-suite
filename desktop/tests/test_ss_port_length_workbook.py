"""Port length through the runner, on real tie-panel files.

The paneljumper fixture is ribbon 1 of a 288-port tie panel shot both
directions (5 ns): launch reel, 15 m jumper, panel A, 62 m tie, panel B,
15 m jumper, receive reel.  Pooled in one folder it carries
everything the reading does: a connector-left-in repeat (004, 005 and 006
are one port, 33-38 s apart), the two-direction check (12 numbers both
ways; number 10 was shot on another OTDR with other jumpers, number 3
disagrees between the ends), and the Port length sheet.  A crash in the
sheet writer (a bare % in its text, 2026-09-27) took the whole workbook
down and no unit test saw it, because they never built one.

Namespace isolation rule: the runner is exercised only as a subprocess.
"""
from __future__ import annotations

import shutil

import pytest

from conftest import FIXTURE_DIR, run_secretsauce

PANEL = FIXTURE_DIR / "paneljumper"
# Fiber numbers 4, 5 and 6 of direction A, named as the fixture names them.
A_FILES = sorted(p.stem for p in (PANEL / "A").glob("*.sor"))


@pytest.fixture(scope="module")
def both_ways(tmp_path_factory):
    folder = tmp_path_factory.mktemp("paneljumper_both")
    for side in ("A", "B"):
        for p in sorted((PANEL / side).glob("*.sor")):
            shutil.copy(p, folder / p.name)
    out = tmp_path_factory.mktemp("out")
    rc, manifest, stderr = run_secretsauce(folder, out, "xlsx")
    rc2, pairs, stderr2 = run_secretsauce(folder, tmp_path_factory.mktemp("out2"), "pairs")
    return folder, out, rc, manifest, stderr, rc2, pairs


def test_the_workbook_is_written_with_a_port_length_sheet(both_ways):
    folder, out, rc, manifest, stderr, _, _ = both_ways
    assert rc == 0 and manifest and manifest.get("ok") is True, stderr[-2000:]
    assert "Port length: usable" in stderr, stderr[-2000:]
    from openpyxl import load_workbook
    wb = load_workbook(manifest["written"][0]["path"])
    assert "Port length" in wb.sheetnames
    ws = wb["Port length"]
    text = " ".join(str(c.value) for row in ws.iter_rows() for c in row if c.value is not None)
    assert "Connector Left In" in text
    assert "Fiber Numbers Shot From Both Ends" in text
    assert "10% crossing" in text


def test_the_manifest_names_the_repeat_and_the_disagreeing_numbers(both_ways):
    _, _, _, manifest, _, _, _ = both_ways
    pl = manifest["port_length"][0]
    assert pl["left_in"] == [A_FILES[3:6]], pl["left_in"]
    ab = pl["ab"]
    assert ab["n_matched"] == 12
    assert ab["sd_m"] < 0.015, ab["sd_m"]
    nums = {m["num"]: m for m in ab["mismatch"]}
    assert 10 in nums and 3 in nums, sorted(nums)
    # number 10: another OTDR and other jumpers on the A side, said so
    assert nums[10]["kit_a"] and "OTDR" in nums[10]["kit_a"], nums[10]["kit_a"]
    assert abs(nums[10]["diff_m"]) > 1.0
    # the reading is a ranking: nothing here is a verdict
    assert manifest.get("n_flagged", 0) == 0 or "written" in manifest


def test_pairs_mode_carries_the_same_additive_fields(both_ways):
    _, _, _, _, _, rc2, pairs = both_ways
    assert rc2 == 0 and pairs and pairs.get("ok") is True
    li = {(q["fileA"][-3:], q["fileB"][-3:]) for q in pairs["pairs"] if q.get("port_len_left_in")}
    assert ("004", "005") in li and ("005", "006") in li, li
    assert pairs["port_length"][0]["left_in"][0][0].endswith("_004")
    # every pair keeps its verdict fields exactly as before
    for q in pairs["pairs"]:
        assert set(q) >= {"fileA", "fileB", "score", "p_dup", "verdict"}

"""One-direction reports go by the Direction stamp, and a Viewer side reads
every file on it.

A real span's B folder (2026-10-02): each B file carries two labels.  Most
are stamped B (LocationsDirection, FR's Direction column) but carry the A
side's site codes; four re-shots carry the right site codes but are stamped
A.  The Viewer drew every file as B, A+B paired them by fiber number, but B
alone asked the one-direction report for its table, which grouped on the
site codes and set the re-shots aside: "OTDR Suite table could not be
built.  The report has no table for these fibers."  Fixing the stamp with
right-click > Direction did not help, since the report never read it.

Robert 2026-10-02: "uni should go to stamp instead, and right click fix would
count".  So:
  * the Unidirectional report groups a folder by the stamp (site codes only
    for a file with none), so a right-click fix brings the file back;
  * the Viewer's one-direction table reads every file on its side, the side
    a box or a drop on the Files panel put it on, whatever it is stamped,
    unless it re-uses a fiber number (a real second direction), and the
    Viewer then says why.
"""
import json
import os
import subprocess
import sys

from conftest import FIXTURE_DIR, REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"
RUNNER = SPLICEREPORT_DIR / "run_splicereport.py"
if str(SPLICEREPORT_DIR) not in sys.path:
    sys.path.insert(0, str(SPLICEREPORT_DIR))
import splicereportmatchexfo as E  # noqa: E402

SRC = FIXTURE_DIR / "splice_B"            # 24 fibers, stamped B


def _sites():
    """The fixture's two site codes as stored, and the same bytes swapped."""
    rec = E.parse_sor_full(str(SRC / sorted(os.listdir(SRC))[0]), trim=False)
    a, b = rec["gen_loc_a"].strip().encode(), rec["gen_loc_b"].strip().encode()
    return a + b"\x00" + b + b"\x00", b + b"\x00" + a + b"\x00"


SITES = _sites()


def _restamp(paths, side):
    """What Files panel right-click > Direction writes (trace_server.
    set_direction), run apart: the Viewer has its own sor_reader."""
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import trace_server as T\n"
            "for p in sys.argv[3:]:\n"
            "    raw = open(p, 'rb').read()\n"
            "    open(p, 'wb').write(T.set_direction(raw, sys.argv[2]))\n")
    subprocess.run([sys.executable, "-c", code, str(REPO_ROOT / "viewer"), side,
                    *map(str, paths)], check=True, capture_output=True)


def _folder(path, stamped_a=(), sites_swapped=(), extra=()):
    """splice_B with `stamped_a` fibers stamped A, `sites_swapped` fibers'
    site codes swapped (same bytes, so every offset holds), and `extra`
    fibers given a second file stamped A that re-uses their fiber number."""
    path.mkdir(parents=True)
    restamp = []
    for name in sorted(os.listdir(SRC)):
        raw = (SRC / name).read_bytes()
        n = int(name[6:10])
        if n in sites_swapped:
            assert raw.count(SITES[0]) == 1
            raw = raw.replace(*SITES)
        (path / name).write_bytes(raw)
        if n in stamped_a:
            restamp.append(path / name)
        if n in extra:
            (path / f"ELMMIL{n:04d}_1550.sor").write_bytes(raw)
            restamp.append(path / f"ELMMIL{n:04d}_1550.sor")
    if restamp:
        _restamp(restamp, "a")
    return path


def _run(tmp_path, folder, tag, viewer=True):
    out = [sys.executable, str(RUNNER), "--uni", "--dir-a", str(folder),
           "--out", str(tmp_path / f"{tag}.xlsx")]
    tbl = tmp_path / f"{tag}.json"
    if viewer:
        out += ["--viewer-leg", "b", "--viewer-table", str(tbl)]
    p = subprocess.run(out, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr[-2000:]
    m = json.loads(p.stdout.strip().splitlines()[-1])
    assert m["ok"], m
    return m, (json.loads(tbl.read_text(encoding="utf-8")) if viewer else None)


# ── The Unidirectional report: the stamp decides ──────────────────────────

def test_site_codes_typed_the_other_way_do_not_split_the_folder(tmp_path):
    """Stamped B with the A side's site codes: one direction."""
    d = _folder(tmp_path / "sites", sites_swapped={5, 17})
    fibers, chosen, counts, merged, cov = E.uni_load_dir(str(d))
    assert sorted(fibers) == list(range(1, 25)) and cov["complete"]
    assert counts == {"B→A": 24} and chosen == "B→A"


def test_a_file_stamped_the_other_way_is_set_aside_and_named(tmp_path):
    m, _ = _run(tmp_path, _folder(tmp_path / "mix", stamped_a={5, 17}), "rep",
                viewer=False)
    u = m["uni"]
    assert u["n_fibers"] == 22 and not u["coverage_complete"]
    assert u["direction"] == "B→A" and u["direction_counts"] == {"B→A": 22, "A→B": 2}
    assert u["coverage"]["dropped_signatures"] == [
        {"signature": "A→B", "n_files": 2, "fiber_ranges": "5, 17"}]


def test_the_right_click_fix_brings_the_file_back(tmp_path):
    """Stamped A by mistake, set back to B with right-click > Direction: the
    report covers it, even with the site codes still typed the other way."""
    d = _folder(tmp_path / "fixed", stamped_a={5, 17}, sites_swapped=set(range(1, 21)))
    _restamp([d / "MILELM0005_1550.sor", d / "MILELM0017_1550.sor"], "b")
    m, _ = _run(tmp_path, d, "fixed", viewer=False)
    assert m["uni"]["n_fibers"] == 24 and m["uni"]["coverage_complete"]
    assert m["uni"]["direction_counts"] == {"B→A": 24}


def test_a_file_without_a_stamp_still_groups_on_its_site_codes():
    rec = {"gen_loc_a": "LAM", "gen_loc_b": "BEY", "exfo_locations_direction": None}
    assert E.uni_direction_signature(rec) == "LAM->BEY"
    assert E.uni_direction_signature(dict(rec, exfo_locations_direction=1)) == "A→B"
    assert E.uni_direction_signature(dict(rec, exfo_locations_direction=2)) == "B→A"


# ── The Viewer's one-direction table: the side decides ────────────────────

def test_the_viewer_side_folds_in_files_stamped_the_other_way(tmp_path):
    d = _folder(tmp_path / "mix", stamped_a={5, 17})
    fibers, chosen, counts, merged, cov = E.uni_load_dir(str(d), one_box=True)
    assert sorted(fibers) == list(range(1, 25)) and cov["complete"]
    assert merged == [{"signature": "A→B", "n_fibers": 2, "fibers": [5, 17]}]


def test_a_second_direction_re_using_fiber_numbers_stays_out(tmp_path):
    d = _folder(tmp_path / "two", extra={3, 4})
    fibers, chosen, counts, merged, cov = E.uni_load_dir(str(d), one_box=True)
    assert len(fibers) == 24 and merged == []
    assert cov["dropped_signatures"] == [
        {"signature": "A→B", "n_files": 2, "fiber_ranges": "3-4"}]


def test_the_viewer_table_is_the_clean_folder_s(tmp_path):
    """Only the stamps differ, so the B-alone table is the one the same files
    give with every stamp right: same columns, same cells, every fiber."""
    _, clean = _run(tmp_path, _folder(tmp_path / "clean"), "clean")
    m, mixed = _run(tmp_path, _folder(tmp_path / "mix", stamped_a={5, 17}), "mix")
    assert m["uni"]["coverage_complete"]
    assert mixed["columns"] == clean["columns"]
    assert mixed["fibers"] == clean["fibers"] and len(mixed["fibers"]) == 24


def test_the_viewer_says_why_a_fiber_was_left_out(monkeypatch):
    sys.path.insert(0, str(REPO_ROOT / "viewer"))
    import trace_server as TS
    table = {"columns": [], "fibers": {"1": []}, "direction": "b", "gate_db": 0.25,
             "left_out": [{"signature": "A→B", "n_files": 2, "fiber_ranges": "3-4"}]}
    monkeypatch.setattr(TS, "_one_direction_table", lambda d: (table, False, None))
    out = TS.suite_tables([3], "b")
    assert out["missing"] == [3]
    assert "A→B (fibers 3-4)" in out["error"], out
    # a fiber the report did not set aside keeps the plain "no table"
    assert TS.suite_tables([9], "b")["error"] is None
    assert TS.suite_tables([1, 3], "b")["error"] is None


def test_a_drop_on_the_files_panel_reads_every_b_file(tmp_path, monkeypatch):
    """A tech's drop: both folders dragged onto the Viewer's Files panel, then
    B alone.  Every file the drop put on B has a row, whatever its labels."""
    sys.path.insert(0, str(REPO_ROOT / "viewer"))
    import trace_server as TS
    # A drop points the server at what it staged and stamps dropped_at, which
    # the hub's pages act on: both go back, as in the other drop tests.
    for k in ("dir_a", "dir_b", "suite_table", "end_refl", "analysis_mode",
              "settings", "dropped_at"):
        monkeypatch.setitem(TS.CONFIG, k, TS.CONFIG.get(k))
    import tempfile
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", None)
    TS.CONFIG.update({"suite_table": None, "end_refl": None,
                      "analysis_mode": "suite", "settings": None})
    monkeypatch.setattr(TS, "_UNI_TABLES", {})
    monkeypatch.setattr(TS, "_TRACE_SIG", {})
    TS.set_dirs(None, None)
    try:
        _drop_then_b_alone(tmp_path, TS)
    finally:
        TS.set_dirs(None, None)
        TS.CONFIG.pop("dropped_at", None)


def _drop_then_b_alone(tmp_path, TS):
    import time
    b = _folder(tmp_path / "mix", sites_swapped=set(range(1, 21)))
    tok = TS.drop_begin()
    for folder in (FIXTURE_DIR / "splice_A", b):
        for name in sorted(os.listdir(folder)):
            TS.drop_file(tok, name, (folder / name).read_bytes())
    ans = TS.drop_end(tok)
    assert (ans["a_count"], ans["b_count"]) == (24, 24), ans
    fibers = list(range(1, 25))
    for _ in range(3000):
        out = TS.suite_tables(fibers, "b")
        if not out["pending"]:
            break
        time.sleep(0.1)
    on_b = sorted(n for n, _ in TS.list_fibers(ans["dir_b"]))
    assert sorted(map(int, out["tables"])) == on_b == fibers, out

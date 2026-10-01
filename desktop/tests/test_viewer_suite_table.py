"""The Viewer's A+B table in OTDR Suite mode is the Splice Report's own.

The boss, 2026-09-28: the Viewer shows the Suite's numbers and columns in
OTDR Suite mode and FastReporter's in FastReporter mode.  The Suite's columns
are found across the whole cable, so the Viewer cannot build them from the
fibres on screen: the report run writes them to a table (`--viewer-table`),
with the numbers it worked from for every fibre at every column, flagged or
not.  The flagged cells are the report's results; the rest are the readings
the passes judged and dropped, kept on the side (_note_passing).

Pinned here:
  * asking for the table changes nothing in the report;
  * every cell the report prints is in the table with its number, its flag
    and its words, under the report's own column;
  * the cells that pass carry the pair's readings and their mean;
  * each leg sits where its file stores the event (the Viewer's own frame);
  * a panel span prints its two ends in its Connector columns, with the
    two known fails of the panelreels_east fixture on the readings that failed;
  * the server hands over the table of the report on screen, and runs the
    report itself when there is none or the folders changed under it;
  * the Viewer picks the table by mode (no Node here: checked at the source).

Engine tests run in a clean subprocess (three engines, three sor_reader
copies, never one process).
"""
import json
import os
import subprocess
import sys
import textwrap
import time

import pytest

from conftest import REPO_ROOT, import_trace_server

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"
RUNNER = SPLICEREPORT_DIR / "run_splicereport.py"
FIX = REPO_ROOT / "desktop" / "tests" / "fixtures"
SPLICE_A, SPLICE_B = FIX / "splice_A", FIX / "splice_B"
EAST = FIX / "panelreels_east"
VIEWER = (REPO_ROOT / "viewer" / "viewer.html").read_text(encoding="utf-8")
SERVER = (REPO_ROOT / "viewer" / "trace_server.py").read_text(encoding="utf-8")
APP = (REPO_ROOT / "app.py").read_text(encoding="utf-8")


def _fn(name):
    i = VIEWER.index('function ' + name + '(')
    return VIEWER[i:VIEWER.index('\n}\n', i) + 3]


def _report(dir_a, dir_b, out_dir, table=True, extra=()):
    cmd = [sys.executable, str(RUNNER), "--dir-a", str(dir_a), "--dir-b", str(dir_b),
           "--out", str(out_dir / "report.xlsx"), *extra]
    path = out_dir / "table.json"
    if table:
        cmd += ["--viewer-table", str(path)]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr[-2000:]}"
    man = json.loads(p.stdout.strip().splitlines()[-1])
    assert man["ok"], man
    return man, (json.loads(path.read_text(encoding="utf-8")) if path.exists() else None)


# A job under 80 fibres lists events (E.EVENT_JOB_MAX_FIBERS, Robert
# 2026-10-01); these tests read the 24-fibre fixture's CLOSURE layout, so
# they lower the cutoff for the run.
CLOSURES = ("--overrides", json.dumps({"EVENT_JOB_MAX_FIBERS": 0}))


@pytest.fixture(scope="module")
def splice(tmp_path_factory):
    return _report(SPLICE_A, SPLICE_B, tmp_path_factory.mktemp("suite_table"),
                   extra=CLOSURES)


@pytest.fixture(scope="module")
def east(tmp_path_factory):
    return _report(EAST / "A", EAST / "B", tmp_path_factory.mktemp("suite_table_east"))


def _cells(table, fiber):
    return {c["col"]: c for c in table["fibers"][str(fiber)]}


# ─── the report does not move ─────────────────────────────────────────────

def test_asking_for_the_table_changes_nothing_in_the_report(splice, tmp_path):
    man, table = splice
    plain, none = _report(SPLICE_A, SPLICE_B, tmp_path, table=False, extra=CLOSURES)
    assert none is None and "viewer_table" not in plain
    assert table is not None and os.path.isfile(man["viewer_table"])
    a, b = dict(man), dict(plain)
    for d in (a, b):
        d.pop("xlsx", None)
        d.pop("viewer_table", None)
    assert a == b


def test_fastreporter_mode_writes_no_table(tmp_path):
    man, table = _report(SPLICE_A, SPLICE_B, tmp_path, extra=("--analysis", "fr"))
    assert table is None and "viewer_table" not in man


def test_the_table_is_json_a_browser_can_read(splice):
    man, _ = splice
    raw = open(man["viewer_table"], encoding="utf-8").read()
    assert "NaN" not in raw and "Infinity" not in raw


# ─── columns and flagged cells are the report's ───────────────────────────

def test_the_columns_are_the_report_s_between_its_two_ends(splice):
    man, table = splice
    cols = table["columns"]
    assert [c["kind"] for c in cols] == (
        ["end"] + [c["kind"] for c in man["columns"]] + ["end"])
    # no site typed: each end takes the name its own files store
    assert (cols[0]["title"], cols[0]["end"]) == ("A-End ILA: ELMDALE", "A")
    assert (cols[-1]["title"], cols[-1]["end"]) == ("B-End ILA: MILLER", "B")
    # named as the report's header row names them
    titles = [c["title"] for c in cols[1:-1]]
    nums = [c["num"] for c in man["columns"] if c["kind"] == "splice"]
    assert [t for t in titles if t.startswith("Splice")] == [f"Splice {n}" for n in nums]
    assert {t for t, c in zip(titles, man["columns"]) if c["kind"] == "bend"} <= {"Bends"}
    # and in the report's order along the cable
    kms = [c["km"] for c in cols]
    assert kms == sorted(kms) and kms[0] == 0.0 and kms[-1] == table["span_km"]


def test_every_cell_the_report_prints_is_in_the_table(splice):
    man, table = splice
    assert man["n_flagged"] == 1 and len(man["cells"]) == 1
    for c in man["cells"]:
        x = _cells(table, c["fiber"])[c["splice"] + 1]      # +1: the A end leads
        assert round(x["loss"], 3) == c["loss"], (c, x)
        assert x["flag"] is True and x["label"] == c["label"]
        # both directions read it, and each leg is a stored event with a place
        for leg in (x["a"], x["b"]):
            assert leg["loss"] is not None and leg["km"] is not None
            assert leg["grey"] is False and leg["flag"] is False
    flagged = sum(1 for f in table["fibers"].values() for c in f
                  if c["flag"] or any((c[w] or {}).get(k) for w in "ab"
                                      for k in ("flag", "flag_refl")))
    assert flagged == 1


def test_fiber_20_splice_13_reads_as_the_report_prints_it(splice):
    """'20 .200': A .273, B .127, the pair .200 at the 0.160 gate."""
    _, table = splice
    x = _cells(table, 20)[13]
    assert table["columns"][13]["title"] == "Splice 13"
    assert (round(x["a"]["loss"], 3), round(x["b"]["loss"], 3), round(x["loss"], 3)) == (
        0.273, 0.127, 0.200)
    assert x["label"] == "20 .200" and x["category"] == "bidir"


# ─── the cells that pass ──────────────────────────────────────────────────

def test_passing_cells_carry_the_pair_and_its_mean(splice):
    man, table = splice
    gate = man["thresholds"]["REBURN_THRESHOLD"]
    splice_cols = {i for i, c in enumerate(table["columns"]) if c["kind"] == "splice"}
    passing = [c for f in table["fibers"].values() for c in f
               if c["col"] in splice_cols and c["category"] == "passing"]
    # 24 fibres x 14 closures, less the one cell that flagged and the few
    # fibres with no event of their own at a closure
    assert len(passing) >= 85
    paired = [c for c in passing if c["a"] and c["b"]
              and c["a"]["loss"] is not None and c["b"]["loss"] is not None]
    assert len(paired) >= 80
    for c in paired:
        assert c["flag"] is False and c["label"] == ""
        assert abs(c["loss"] - (c["a"]["loss"] + c["b"]["loss"]) / 2.0) < 1e-4
        assert round(abs(c["loss"]), 3) < gate               # it passed
    # a reading from one direction only prints no Average
    for c in passing:
        if not (c["a"] and c["b"]):
            assert c["loss"] is None


def test_a_leg_sits_where_its_file_stores_the_event(splice):
    """The report's km is measured from the panel; a leg's is the file's own,
    a launch length further along (what the Viewer draws and clicks on)."""
    body = textwrap.dedent(f"""
        import sys, json
        sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})
        import sor_reader324802a as sr
        import glob, os
        out = {{}}
        for side, d in (('a', {str(SPLICE_A)!r}), ('b', {str(SPLICE_B)!r})):
            for p in sorted(glob.glob(os.path.join(d, '*.sor'))):
                r = sr.parse_sor_full(p, trim=False)
                out.setdefault(side, {{}})[os.path.basename(p)] = [
                    [e['dist_km'], e['splice_loss']] for e in r['events']]
        print(json.dumps(out))
    """)
    p = subprocess.run([sys.executable, "-c", body], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    stored = json.loads(p.stdout.strip().splitlines()[-1])
    every = {side: {(round(km, 4), loss) for evs in files.values() for km, loss in evs}
             for side, files in stored.items()}
    _, table = splice
    seen = 0
    for f in table["fibers"].values():
        for c in f:
            for side in "ab":
                leg = c[side]
                if not leg or leg["grey"] or leg["km"] is None or leg["loss"] is None:
                    continue
                if table["columns"][c["col"]]["kind"] == "end":
                    continue
                near = [l for km, l in every[side] if abs(km - round(leg["km"], 4)) < 1e-9]
                assert any(abs(l - leg["loss"]) <= 0.00051 for l in near), (c, side)
                seen += 1
    assert seen >= 150


def test_the_side_table_keeps_the_nearer_reading_and_the_earlier_pass():
    body = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})
        import splicereportmatchexfo as E
        E._note_passing(None, 1, 0, 1.0, .1, .1, .1)          # no table asked for
        pop = {{}}
        E._note_passing(pop, 1, 0, 10.0, .05, .07, .06, rank=2, col_dist=0.30)
        E._note_passing(pop, 1, 0, 10.1, .02, .04, .03, rank=2, col_dist=0.10)
        assert pop[(1, 0)]['bidir_loss'] == .03               # nearer its column
        E._note_passing(pop, 1, 0, 10.2, .08, .08, .08, rank=2, col_dist=0.20)
        assert pop[(1, 0)]['bidir_loss'] == .03
        E._note_passing(pop, 1, 0, 10.0, .11, .13, .12, rank=1)
        assert pop[(1, 0)]['bidir_loss'] == .12               # pass 1 judged it
        E._note_passing(pop, 1, 0, 10.0, .01, .01, .01, rank=2, col_dist=0.0)
        assert pop[(1, 0)]['bidir_loss'] == .12               # ... and keeps it
        assert pop[(1, 0)]['is_flagged'] is False
        print('OK')
    """)
    p = subprocess.run([sys.executable, "-c", body], capture_output=True, text=True)
    assert p.returncode == 0 and p.stdout.strip().endswith("OK"), p.stdout + p.stderr


# ─── a panel span: the ends ARE its connector columns ─────────────────────

def test_a_panel_span_prints_its_ends_in_the_connector_columns(east):
    """The panelreels_east fixture, a 31 m tie between 1 km reels, confirmed
    in the field 2026-09-17: F74 -49.8 and F126 -47.0 fail on the B shot at
    end A; F2 is clean."""
    man, table = east
    assert man["panel_span"] is True
    assert [(c["title"], c["kind"], c.get("end")) for c in table["columns"]] == [
        ("Connector", "connector", "A"), ("Section 31m", "section", None),
        ("Connector", "connector", "B")]
    for fiber, refl in ((74, -49.8), (126, -47.0)):
        x = _cells(table, fiber)[0]
        assert x["b"]["flag_refl"] is True and round(x["b"]["refl"], 1) == refl
        assert x["a"]["flag_refl"] is False and x["flag"] is False
        assert x["tags"] == [f"REFL{refl:+.1f}dB"]
        assert _cells(table, fiber)[2]["tags"] == []
    for x in _cells(table, 2).values():
        assert not x["flag"] and not x["tags"]
        assert not any((x[w] or {}).get(k) for w in "ab" for k in ("flag", "flag_refl"))
    # the same verdicts the manifest hands over, on the same readings
    assert sorted((v["fiber"], v["dir"], v["refl"]) for v in man["end_refl"]) == [
        (74, "B", -49.8), (126, "B", -47.0)]
    # a section is glass between connectors: one reading, no stored event
    sec = _cells(table, 74)[1]
    assert sec["b"] is None and sec["a"]["km"] is None and sec["a"]["grey"] is False


# ─── the server ───────────────────────────────────────────────────────────

@pytest.fixture()
def server(tmp_path, monkeypatch):
    ts = import_trace_server()
    a, b = tmp_path / "A", tmp_path / "B"
    a.mkdir()
    b.mkdir()
    for k in ("dir_a", "dir_b", "suite_table", "end_refl", "analysis_mode"):
        monkeypatch.setitem(ts.CONFIG, k, ts.CONFIG.get(k))
    ts.CONFIG.update({"dir_a": str(a), "dir_b": str(b), "suite_table": None,
                      "end_refl": None, "analysis_mode": "suite"})
    monkeypatch.setattr(ts, "_END_VERDICTS", {})
    monkeypatch.setattr(ts, "_SUITE_TABLE_FILE", {})
    monkeypatch.setattr(ts, "_TRACE_SIG", {})
    (a / "X_0007_1550.sor").write_bytes(b"a")
    (b / "Y_0007_1550.sor").write_bytes(b"b")
    runs = []

    def fake_run(key):
        runs.append(key)
        with ts._END_VERDICTS_LOCK:
            ts._END_VERDICTS[key] = {
                "end_refl": [], "panel_span": False, "error": None,
                "suite_table": {"columns": [{"title": "Splice 1", "kind": "splice", "km": 5.0}],
                                "fibers": {"7": [{"col": 0, "loss": 0.05}]},
                                "launch_a_km": 1.0, "span_km": 20.0}}
    monkeypatch.setattr(ts, "_run_end_verdicts", fake_run)
    return ts, a, b, runs


def _write_table(path, a, b, fibers=None):
    # stamped as the runner stamps it (the two are checked against each
    # other on a real run below)
    _trace_folder_sig = import_trace_server()._trace_folder_sig
    path.write_text(json.dumps({
        "columns": [{"title": "Splice 1", "kind": "splice", "km": 5.0}],
        "fibers": fibers or {"7": [{"col": 0, "loss": 0.2}]},
        "dir_a": str(a), "dir_b": str(b),
        "sig_a": _trace_folder_sig(str(a)), "sig_b": _trace_folder_sig(str(b)),
        "launch_a_km": 1.0, "span_km": 20.0}),
        encoding="utf-8")


def _settle(ts, fibers, tries=50):
    for _ in range(tries):
        out = ts.suite_tables(fibers)
        if not out["pending"]:
            return out
        time.sleep(0.05)
    raise AssertionError("the run never landed")


def test_the_server_hands_over_the_table_of_the_report_on_screen(server, tmp_path):
    ts, a, b, runs = server
    path = tmp_path / "table.json"
    _write_table(path, a, b)
    ts.set_suite_table(str(path))
    out = ts.suite_tables([7, 8])
    assert out["source"] == "report" and out["pending"] is False and runs == []
    assert out["tables"] == {"7": [{"col": 0, "loss": 0.2}]} and out["missing"] == [8]
    assert out["columns"][0]["title"] == "Splice 1" and out["launch_a_km"] == 1.0


def test_with_no_report_table_the_server_runs_the_report_once(server):
    ts, a, b, runs = server
    out = _settle(ts, [7])
    assert out["source"] == "viewer" and out["tables"]["7"] == [{"col": 0, "loss": 0.05}]
    _settle(ts, [7])
    assert len(runs) == 1 and runs[0][0] == "suite"


def test_a_table_about_other_folders_is_not_used(server, tmp_path):
    ts, a, b, runs = server
    path = tmp_path / "table.json"
    _write_table(path, tmp_path / "elsewhere", b)
    ts.set_suite_table(str(path))
    assert _settle(ts, [7])["source"] == "viewer" and len(runs) == 1


def test_a_trace_added_after_the_report_ran_retires_its_table(server, tmp_path):
    ts, a, b, runs = server
    path = tmp_path / "table.json"
    _write_table(path, a, b)
    ts.set_suite_table(str(path))
    assert ts.suite_tables([7])["source"] == "report"
    # the report saved beside the traces is not a trace: the table stands
    (a / "A_to_B_SpliceReport.xlsx").write_bytes(b"x")
    assert ts.suite_tables([7])["source"] == "report" and runs == []
    # a trace edit writes a new file: the table is about other glass now
    (a / "X_0009_1550.sor").write_bytes(b"x")
    assert _settle(ts, [7])["source"] == "viewer" and len(runs) == 1


def test_the_runner_and_the_server_stamp_a_folder_alike(server, splice):
    """The engines share no module, so each carries the few lines; a real
    run's stamp has to be the one the server works out for the same folder."""
    ts, a, b, _ = server
    _, table = splice
    assert table["sig_a"] == ts._trace_folder_sig(str(SPLICE_A))
    assert table["sig_b"] == ts._trace_folder_sig(str(SPLICE_B))
    assert table["sig_a"][0] == 24 and os.path.samefile(table["dir_a"], SPLICE_A)
    # and the same files count as traces on both sides
    runner = (SPLICEREPORT_DIR / "run_splicereport.py").read_text(encoding="utf-8")
    for src in (runner, SERVER):
        assert "_TRACE_EXTS = ('.sor', '.bdr', '.trc', '.json')" in src
        assert "if e.name.lower().endswith(_TRACE_EXTS) and e.is_file():" in src
        assert "newest = max(newest, e.stat().st_mtime_ns)" in src


def test_a_failed_run_says_why(server, monkeypatch):
    ts, a, b, _ = server

    def failed(key):
        with ts._END_VERDICTS_LOCK:
            ts._END_VERDICTS[key] = {"end_refl": None, "panel_span": None,
                                     "suite_table": None, "error": "engine failed: boom"}
    monkeypatch.setattr(ts, "_run_end_verdicts", failed)
    out = _settle(ts, [7])
    assert out["error"] == "engine failed: boom" and out["tables"] == {} and out["missing"] == [7]


def test_the_end_verdicts_keep_their_shape(server):
    """The same run feeds both; /api/end_verdicts answers as it always has."""
    ts, a, b, _ = server
    _settle(ts, [7])
    assert ts.end_verdicts() == {"end_refl": [], "panel_span": False, "end_pending": False}


def test_the_run_asks_for_the_table_in_suite_mode_only():
    body = SERVER.split("def _run_end_verdicts(key):", 1)[1].split("\ndef ", 1)[0]
    assert "if mode == 'suite':\n            cmd += ['--viewer-table', table_path]" in body
    assert "u.path == '/api/suite_table'" in SERVER


# ─── the hub and the Viewer ───────────────────────────────────────────────

def test_the_hub_asks_every_report_for_its_table_and_hands_it_over():
    cmd = APP.split("def splicereport_cmd(", 1)[1].split("\ndef ", 1)[0]
    assert "common += ['--viewer-table', viewer_table]" in cmd
    assert "viewer_table=_viewer_table_path(_da, _db))})" in APP
    assert APP.count("trace_server.set_suite_table(res.get('viewer_table'))") == 1
    # the uni grid has no A+B table, and a cleared report takes its own away
    assert APP.count("trace_server.set_suite_table(None)") == 2
    drop = APP.split("def _drop_report(which):", 1)[1].split("\ndef ", 1)[0]
    assert "trace_server.set_suite_table(None)" in drop
    assert "if _is_viewer_table(_vt):" in drop and "os.remove(_vt)" in drop


def test_the_table_follows_the_analysis_mode():
    body = _fn('renderEventTable')
    assert ("if (gAnalysisMode === 'suite') {\n"
            "    if (renderSuiteBidiGrid(visible, host, hint)) return;\n"
            "  } else if (renderFrBidiGrid(visible, host, hint)) return;\n"
            "  renderFastReporterGrid(visible, host, hint);") in body
    ask = _fn('renderSuiteBidiGrid')
    # a load from one direction asks for that direction's table (Robert
    # 2026-09-30: "it has to work for one direction OR bidi, equally"); a
    # mix of lone A and lone B traces still has no Suite table
    assert "if (dirs.size !== 1) return false;" in ask
    assert "fetch(`/api/suite_table?${oneDir ? `dir=${oneDir}&` : ''}fibers=${pairs.map(p => p.fiber).join(',')}`)" in ask
    # the report may still be running: the table waits for it ...
    assert "if (res.pending) {" in ask and "gSuitePoll = setTimeout(ask, 3000);" in ask
    assert "if (seq !== gSuiteTableSeq) return;" in ask
    # ... and a table still on its way is dropped when the traces change,
    # paired or not (the call sits above the no-traces return)
    assert "suiteTableReset();" in body[:body.index("if (visible.length === 0) {")]
    reset = _fn('suiteTableReset')
    assert "gSuiteTableSeq++;" in reset
    assert "if (gSuitePoll) { clearTimeout(gSuitePoll); gSuitePoll = null; }" in reset
    # no report table: an error saying why, never FastReporter's table in
    # its place (Robert 2026-09-30: "Error, no FR stand-in")
    assert "renderFrBidiGrid" not in ask and "renderFastReporterGrid" not in ask
    assert "noTable(res.error || 'The report has no table for these fibers');" in ask
    assert "OTDR Suite table could not be built." in ask
    assert "switch to FastReporter mode for FastReporter's table." in ask
    assert 'gSuiteNote' not in VIEWER


def test_the_suite_table_is_the_report_s_columns_and_three_rows_per_fibre():
    body = _fn('paintSuiteBidiGrid')
    # the report's columns, in its order, under its own names and distances
    assert "const cols = (res.columns || []).map(c => ({" in body
    assert "if (cols[cell.col]) cols[cell.col].ev[fi] = cell;" in body
    assert "`${esc(c.title)} (${n}/${have.length})</th>`" in body
    assert '<span class="fr-km">${kmFt(c.km)}</span>' in body
    # zoom lands on the file's frame: the report's km plus A's launch length
    # (a one-direction table's km goes on through its trace to the chart)
    assert "viewKm: toView((Number(c.km) || 0) + launchA)," in body
    # three rows per fibre, as in the FastReporter table
    assert "['a', 'b', 'avg']" in body
    assert "which === 'a' ? 'A→B' : which === 'b' ? 'B→A' : 'Average'" in body
    # no sections, and no Sections switch to go with them
    assert "fr-sec" not in body and "secOf" not in body
    assert "if (secLab) secLab.style.display = 'none';" in body
    assert "if (secLab) secLab.style.display = '';" in _fn('suiteTableReset')
    # a leg the report measured on the silent side is grey
    assert "if (leg && leg.grey) cls.push('fr-synth');" in body
    # rows carry what the span menu and the trace-label click need
    assert 'data-dir="${t.dir}" data-src="${t.src || t.dir}" data-fiber="${p.fiber}"' in body
    assert "tb.addEventListener('contextmenu'" in body and 'gGridGoTo = (t, e) =>' in body


def test_the_report_s_flag_is_the_verdict_and_the_ends_take_nothing_else():
    body = _fn('paintSuiteBidiGrid')
    assert "return !!x.flag || (!c.isEnd && clearsAt(x.loss, gateFor(!!x.reflective, false)));" in body
    assert "|| (!c.isEnd && legOk(leg) && clearsAt(leg.loss, gateFor(!!x.reflective, true)));" in body
    assert "isEnd: c.kind === 'end' || !!c.end," in body
    # reflectance is the report's verdict alone
    assert "const reflFlagged = (x, which) => which !== 'avg' && !!(x[which] && x[which].flag_refl);" in body
    assert "reflFails(" not in body
    # a measured (grey) leg is never judged by its number
    assert "const legOk = leg => !!leg && !leg.grey && leg.loss != null;" in body
    # a flagged cell carries the report's words
    assert 'title="Splice Report: ${esc(said)}"' in body


# ─── every kind of cell, on a fabricated cable ────────────────────────────

FAKE = """
    def ev(km, loss, refl=0.0, typ='0F9999LS', end=False):
        return {'dist_km': km, 'splice_loss': loss, 'reflection': refl, 'type': typ,
                'is_end': end, 'is_reflective': typ[:1] in ('1', '2'),
                'time_of_travel': int(km * 1e5)}
    def rec(events, launch=1.0):
        # the file's own list, and the report's copy with the launch reel off
        raw = [ev(0.0, 0.0, -45.0, '1F9999LS')] + [
            dict(e, dist_km=round(e['dist_km'] + launch, 4)) for e in events]
        return {'events': [dict(e) for e in events], '_raw_events': raw,
                '_trace_offset_km': launch}
    A = lambda *e: rec([ev(0.0, 0.30, -55.0, '1F9999LS')] + list(e)
                       + [ev(40.0, 0.0, -30.0, '1E9999LS', True)])
    B = A
    S1 = {'position_km': 10.0, 'splice_display_num': 1}
    S2 = {'position_km': 20.0, 'splice_display_num': 2}
    DMG = {'position_km': 15.0, 'column_kind': 'damage'}
    def res(f, si, **kw):
        base = {'fiber': f, 'splice_idx': si, 'bidir_loss': None, 'a_loss': None,
                'b_loss': None, 'bidir_dist': None, 'is_flagged': True, 'label': ''}
        base.update(kw)
        return base
"""


def _engine(body):
    header = ("import sys, json\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import splicereportmatchexfo as E\n")
    p = subprocess.run([sys.executable, "-c", header + textwrap.dedent(FAKE)
                        + textwrap.dedent(body)], capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


def test_each_kind_of_cell_lands_on_the_row_that_carries_it():
    _engine("""
        fa = {1: A(ev(10.0, 0.25), ev(20.0, 0.04)),
              2: A(ev(10.0, 0.31)),
              3: A(ev(10.0, 0.05), ev(20.0, 0.02, -41.0, '1F9999LS')),
              4: A()}
        fb = {1: B(ev(30.0, 0.15), ev(20.0, 0.02)),
              2: B(ev(20.0, 0.012)),
              3: B(ev(30.0, 0.03), ev(20.0, 0.01)),
              4: B(ev(30.0, 0.22))}
        allr = {
            (1, 0): res(1, 0, bidir_loss=0.20, a_loss=0.25, b_loss=0.15, bidir_dist=10.0,
                        label='1 .200', event_source='bidir'),
            (2, 0): res(2, 0, a_loss=0.31, bidir_dist=10.0, is_a_only=True,
                        label='2 .310 (A)', event_source='a_only'),
            (3, 1): res(3, 1, bidir_loss=0.015, a_loss=0.02, b_loss=0.01, bidir_dist=20.0,
                        is_ref=True, label='3 REFL .015 (-41dB)', event_source='ref'),
            (4, 0): res(4, 0, bidir_loss=0.22, b_loss=0.22, bidir_dist=10.0, is_bfill=True,
                        label='4 .220 (B-fill)', event_source='bfill'),
            (4, 1): res(4, 1, bidir_dist=20.0, is_broke=True, label='4 broke@15.0k',
                        event_source='broke'),
        }
        hidden = {(1, 1): res(1, 1, bidir_loss=0.03, a_loss=0.04, b_loss=0.02,
                              bidir_dist=20.0, label='1 bend .030', event_source='bend')}
        pop = {}
        E._note_passing(pop, 3, 0, 10.0, 0.05, 0.03, 0.04,
                        ea=fa[3]['events'][1], eb=fb[3]['events'][1])
        E._note_passing(pop, 1, 0, 10.0, 9.9, 9.9, 9.9)      # the report has this cell
        E._note_passing(pop, 2, 1, 20.0, 0.010, 0.012, 0.011, a_grey=True,
                        eb=fb[2]['events'][1])
        t = E.suite_viewer_table(fa, fb, [S1, S2], allr, population=pop,
                                 pre_split=[S1, S2], hidden=hidden, span_km=40.0)
        assert [c['title'] for c in t['columns']] == [
            'A-End ILA', 'Splice 1', 'Splice 2', 'B-End ILA']
        cell = lambda f, col: {c['col']: c for c in t['fibers'][str(f)]}.get(col)

        x = cell(1, 1)                       # a pair: the Average carries the flag
        assert x['flag'] and x['loss'] == 0.20 and x['label'] == '1 .200'
        assert not x['a']['flag'] and not x['b']['flag']
        assert (x['a']['km'], x['b']['km']) == (11.0, 31.0)  # each file's own frame

        x = cell(2, 1)                       # one direction: that row carries it
        assert x['a']['flag'] and not x['flag'] and x['loss'] is None and x['b'] is None

        x = cell(4, 1)                       # B-fill: B's row, and no Average
        assert x['b']['flag'] and not x['flag'] and x['loss'] is None and x['a'] is None

        x = cell(3, 2)                       # in-line reflective: A's reflectance
        assert x['a']['flag_refl'] and x['a']['refl'] == -41.0
        assert not x['flag'] and x['reflective'] and x['loss'] == 0.015

        x = cell(4, 2)                       # no number at all: the cell, in words
        assert x['flag'] and x['loss'] is None and x['label'] == '4 broke@15.0k'
        assert x['a'] is None and x['b'] is None

        x = cell(1, 2)                       # switched off in the report: number, no flag
        assert x['loss'] == 0.03 and not x['flag'] and x['label'] == ''

        x = cell(3, 1)                       # passed: the pair the report judged
        assert x['loss'] == 0.04 and not x['flag'] and x['category'] == 'passing'
        assert (x['a']['km'], x['b']['km']) == (11.0, 31.0)

        x = cell(2, 2)                       # a leg measured on the silent side is grey
        assert x['a']['grey'] and x['a']['km'] is None and x['a']['loss'] == 0.010
        assert not x['b']['grey'] and x['b']['km'] == 21.0 and x['loss'] == 0.011

        assert cell(1, 1)['loss'] != 9.9     # never over a cell the report printed
        json.dumps(t)                        # nothing in it that JSON cannot carry
        print('OK')
    """)


def test_passing_cells_follow_their_column_when_the_report_adds_one():
    """The passes run before the off-splice columns are split out; a reading
    kept under the old index has to land under the same closure after it."""
    _engine("""
        fa = {1: A(ev(10.0, 0.05), ev(20.0, 0.06))}
        fb = {1: B(ev(30.0, 0.03), ev(20.0, 0.02))}
        pop = {}
        E._note_passing(pop, 1, 1, 20.0, 0.06, 0.02, 0.04)
        t = E.suite_viewer_table(fa, fb, [S1, DMG, S2], {}, population=pop,
                                 pre_split=[S1, S2], span_km=40.0)
        assert [c['title'] for c in t['columns']] == [
            'A-End ILA', 'Splice 1', 'Damage', 'Splice 2', 'B-End ILA']
        cols = {c['col']: c for c in t['fibers']['1']}
        assert cols[3]['loss'] == 0.04 and 2 not in cols
        print('OK')
    """)


def test_the_ends_carry_the_report_s_tags_on_the_reading_they_are_about():
    _engine("""
        fa = {7: A(ev(10.0, 0.05))}
        fb = {7: B(ev(30.0, 0.03))}
        issues = {7: {'a_tags': ['REFL-45.0dB', 'REFL-48.0dB', '.73 LAUNCH'],
                      'b_tags': ['.81 LAUNCH B side'],
                      'refl_rules': {'A': ['launch', 'tailbox'], 'B': []}}}
        readings = {(7, 'A'): {'launch': fa[7]['events'][0], 'far_refl': -52.0},
                    (7, 'B'): {'launch': fb[7]['events'][0], 'far_refl': -48.0},
                    (7, 'endA'): {'near_loss': 0.80, 'far_loss': 0.66, 'far_synth': False,
                                  'near_evt': fa[7]['_raw_events'][1], 'far_evt': None},
                    (7, 'endB'): {'near_loss': 0.81, 'far_loss': 0.10, 'far_synth': True,
                                  'near_evt': fb[7]['_raw_events'][1], 'far_evt': None}}
        t = E.suite_viewer_table(fa, fb, [S1], {}, launch_issues=issues,
                                 readings=readings, span_km=40.0,
                                 site_a='PNA', site_b='PNB')
        assert [c['title'] for c in t['columns']] == [
            'A-End ILA: PNA', 'Splice 1', 'B-End ILA: PNB']
        ends = {c['col']: c for c in t['fibers']['7']}
        a_end, b_end = ends[0], ends[2]
        # end A: A's own launch reading and B's far reading both failed, and
        # the pair's loss fired the bidirectional gate
        assert a_end['a']['flag_refl'] and a_end['b']['flag_refl'] and a_end['flag']
        assert (a_end['a']['refl'], a_end['b']['refl']) == (-55.0, -48.0)
        assert abs(a_end['loss'] - 0.73) < 1e-9 and a_end['a']['km'] == 1.0
        assert a_end['tags'] == issues[7]['a_tags']
        # end B: one side's loss, on that side's row; the far leg was measured
        assert b_end['b']['flag'] and not b_end['a']['flag'] and not b_end['flag']
        assert b_end['a']['grey'] and not b_end['b']['grey']
        assert not b_end['a']['flag_refl'] and not b_end['b']['flag_refl']
        print('OK')
    """)


def test_only_the_newest_viewer_tables_are_kept(tmp_path, monkeypatch):
    """A table is a few megabytes on a big cable and every span leaves one."""
    monkeypatch.setenv("OTDR_CACHE_DIR", str(tmp_path))
    sys.path.insert(0, str(REPO_ROOT))
    import importlib
    hub = importlib.import_module("app")
    paths = []
    for i in range(5):
        p = hub._viewer_table_path(f"/span{i}/A", f"/span{i}/B")
        assert hub._is_viewer_table(p) and os.path.dirname(p) == str(tmp_path)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("{}")
        os.utime(p, (1_000_000 + i, 1_000_000 + i))
        paths.append(p)
    other = tmp_path / "abc_.sr_grid_cache.json"
    other.write_text("{}", encoding="utf-8")
    hub._prune_viewer_tables(keep=2)
    assert [os.path.exists(p) for p in paths] == [False, False, False, True, True]
    assert other.exists()                        # nothing but its own files
    assert not hub._is_viewer_table(str(other)) and not hub._is_viewer_table(None)


def test_the_suite_table_filters_and_collapses_like_the_other_two():
    """What the event panel's view switches do in the other tables, they do
    here.  Checked in a browser on a 432-fibre span with five fibres loaded
    (one clean, one broken, two over the gate, one failing at an end):
    "flagged rows only" dropped the clean fibre; "failing cells only" kept 4
    of 15 columns and dropped the clean fibre; "warning cells only" kept the
    columns with a yellow cell; the hint named each view."""
    body = _fn('paintSuiteBidiGrid')
    # the row filter
    assert "(!gFlaggedOnly || rowFails[i])" in body
    assert "const LEGS = oneDir ? [oneDir] : ['a', 'b', 'avg'];" in body
    assert "const rowFails = have.map((_p, fi) => LEGS.some(w => legFails(fi, w)));" in body
    # the cell filters: what is kept, and that the rest prints blank and
    # uncoloured.  A loss is kept on the Average row's verdict alone (boss
    # 2026-09-29), or on a direction the report itself flagged (the one
    # direction that read the spot, a launch side); reflectance per direction.
    # A connector keeps each direction that fails on its own row (boss
    # 2026-09-29, Robert: every connector): an end column on the report's
    # per-direction flag, a mid-span one at the one-direction gate.
    assert ("const lossKept = (c, x, which) => which === 'avg'\n"
            "    ? (gFailCellsOnly && lossFails(c, x, which)) || (gWarnCellsOnly && lossWarns(c, x, which))\n"
            "    : oneDir ? gFailCellsOnly && lossFails(c, x, which)\n"
            "    : gFailCellsOnly && (x.reflective ? lossFails(c, x, which)\n"
            "                                      : !!(x[which] && x[which].flag && !gainerHidden(x[which].loss)));") in body
    assert ("const cellKept = (c, x, which) => lossKept(c, x, which)"
            " || (gFailCellsOnly && reflFlagged(x, which));") in body
    assert "if (cellFilterOn() && !lossKept(c, x, which)) return `<td${attrs}></td>`;" in body
    assert "if (cellFilterOn() && !(gFailCellsOnly && bad)) return '<td></td>';" in body
    # either one collapses the table around what it keeps: a column nobody
    # keeps leaves (header, rows, footer), a fibre with nothing kept leaves,
    # and a fibre that stays keeps its Average row
    assert "const collapse = cellFilterOn();" in body
    assert "const keepCol = cols.map(c => !collapse" in body
    assert body.count("if (!keepCol[i]) return;") == 3
    assert "(!collapse || LEGS.some(w => legKept(i, w)))" in body
    assert "!collapse || w === 'avg' || legKept(fi, w)" in body
    # a warning is never a failure, and the ends have no warning level
    assert "if (oneDir || c.isEnd || lossFails(c, x, which)) return false;" in body
    # the hint names whichever view is on, and nothing else (Robert, 2026-09-29)
    for words in ("'flagged rows only'", "'failing cells only'", "'warning cells only'"):
        assert words in body, words
    assert "Splice Report's columns" not in body
    assert "right-click an event" not in body
    # judging is defined before the header uses it
    assert body.index("const cellFails = ") < body.index("const keepCol = ")
    assert body.index("const keepCol = ") < body.index("// ── Header:")


def test_the_suite_section_sits_outside_the_other_painters():
    """The tests of the other two tables cut their painter out of the file by
    the section that follows it.  A new section between them would be judged
    as part of the FastReporter painter."""
    fr = VIEWER.split("function paintFrBidiGrid(", 1)[1].split("\n// ─── Declaring the span", 1)[0]
    uni = VIEWER.split("function renderFastReporterGrid(", 1)[1].split("\n// ─── FastReporter mode", 1)[0]
    for cut in (fr, uni):
        assert "paintSuiteBidiGrid" not in cut and "renderSuiteBidiGrid" not in cut
    assert VIEWER.index("// ─── Declaring the span") < VIEWER.index("// ─── OTDR Suite mode: the Splice Report")

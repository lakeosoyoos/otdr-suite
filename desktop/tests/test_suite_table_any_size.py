"""The Viewer's OTDR Suite table pairs every splice's A and B readings however
many fibres are loaded.

Robert, 2026-09-30: "Viewer should always correctly pair the events in OTDR
mode even if we only have one fiber from each direction" and "we need it to
work no matter how many traces we drop in".  On a 55 km route one fibre from
each direction printed no event column at all, and cells of a 20-79 fibre job
could average one splice's A reading with the next splice's B reading:

- One or two fibres of a route find no closure (no population reaches the
  floor), so the span-structure pass published the launch reel and far end
  as "connector / section / connector" and that blocked the event columns.
  A panel span has no events but its panels; a route does
  (E.structure_is_panel_span).
- When the event columns replaced a lone connector column, its cells stayed
  keyed by that column's index and landed on "Event 1".
- A closure too sparse to discover left its B reading free to pair with the
  next closure's A reading up to 1.5 km away (E._b_event_is_anothers).

Engine tests run in a clean subprocess (3-engine sor_reader isolation).
"""
import json
import shutil
import subprocess
import sys
import textwrap

from conftest import FIXTURE_DIR, REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"
RUNNER = SPLICEREPORT_DIR / "run_splicereport.py"


def _engine(body):
    src = ("import sys\n"
           f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
           "import splicereportmatchexfo as E\n" + textwrap.dedent(body))
    p = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True)
    assert p.returncode == 0, f"{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


def _job(tmp_path, fibres):
    """A copy of the splice_A/B fixture (a 67.5 km route, 14 closures)
    holding only `fibres`."""
    for side in ("A", "B"):
        d = tmp_path / side
        d.mkdir(parents=True)
        for src in sorted((FIXTURE_DIR / f"splice_{side}").glob("*.sor")):
            if int(src.name[6:10]) in fibres:
                shutil.copy(src, d / src.name)
    return tmp_path / "A", tmp_path / "B"


def _run(tmp_path, a, b):
    out, tbl = tmp_path / "r.xlsx", tmp_path / (a.parent.name + "_t.json")
    p = subprocess.run([sys.executable, str(RUNNER), "--dir-a", str(a),
                        "--dir-b", str(b), "--out", str(out),
                        "--viewer-table", str(tbl)],
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stderr[-2000:]
    m = json.loads(p.stdout.strip().splitlines()[-1])
    return m, json.loads(tbl.read_text(encoding="utf-8")), p.stderr


def _assert_lined_up(t):
    """Every cell names a column that exists, once per fibre, at that
    column's place (a cell 0.5 km off its column is someone else's)."""
    cols = t["columns"]
    for fnum, cells in t["fibers"].items():
        seen = [c["col"] for c in cells]
        assert len(seen) == len(set(seen)), (fnum, seen)
        for c in cells:
            assert 0 <= c["col"] < len(cols), (fnum, c)
            if c.get("category") != "end":
                assert abs(cols[c["col"]]["km"] - c["km"]) < 0.5, (fnum, c, cols[c["col"]])


# ─── the runner, one and two fibres of a route ─────────────────────────────

def _check_small_route(tmp_path, fibres):
    a, b = _job(tmp_path, fibres)
    m, t, err = _run(tmp_path, a, b)
    assert "panel-to-panel" not in err or "event column(s)" in err
    kinds = [c["kind"] for c in t["columns"]]
    assert "connector" not in kinds and "section" not in kinds, kinds
    # the route's 14 closures, each an event column
    events = [c for c in t["columns"] if str(c["title"]).startswith("Event ")]
    assert len(events) == 14 and len(kinds) == 16, t["columns"]
    _assert_lined_up(t)
    # the closure at 36.7 km: fibre 1's stored A and B readings in ONE cell,
    # averaged
    cell = next(c for c in t["fibers"]["1"]
                if c.get("category") != "end" and abs(c["km"] - 36.7) < 0.3)
    assert cell["a"]["km"] is not None and cell["b"]["km"] is not None, cell
    assert abs(cell["loss"] - (cell["a"]["loss"] + cell["b"]["loss"]) / 2) < 0.002, cell
    return m, t


def _pairs(cells):
    """(A km, B km, average) per reading; a grey (measured) leg has no km."""
    return sorted(((c["a"] or {}).get("km") or -1.0, (c["b"] or {}).get("km") or -1.0,
                   round(c["loss"], 3)) for c in cells
                  if c.get("category") != "end" and c.get("loss") is not None)


def test_one_fibre_from_each_direction_pairs_its_events(tmp_path):
    """Main: a lone far-end connector column, no event column, and no table."""
    m, t = _check_small_route(tmp_path / "one", {1})
    assert m["event_job"] and m["viewer_table"]
    # every reading paired and averaged as on all 24 fibres of the fixture
    _, full, _ = _run(tmp_path, FIXTURE_DIR / "splice_A", FIXTURE_DIR / "splice_B")
    assert _pairs(t["fibers"]["1"]) == _pairs(full["fibers"]["1"])


def test_two_fibres_are_a_route_not_a_panel_span(tmp_path):
    """Main: the launch reel and the far end made "connector / section /
    connector" on a 67.5 km route."""
    _check_small_route(tmp_path / "two", {1, 2})


# ─── the engine pieces ──────────────────────────────────────────────────────

def test_structure_is_a_panel_span_only_when_the_events_sit_on_its_connectors():
    _engine("""
    def col(km, kind):
        return {'position_km': km, 'position_km_refined': km, 'column_kind': kind}
    tie = [col(1.008, 'connector'), col(1.02, 'section'),
           col(1.039, 'connector'), col(1.55, 'section'), col(2.067, 'connector')]
    assert E.structure_is_panel_span(tie, [col(1.024, 'splice')])
    assert E.structure_is_panel_span(tie, [])
    route = [col(1.002, 'connector'), col(35.0, 'section'), col(69.55, 'connector')]
    assert not E.structure_is_panel_span(route, [col(1.002, 'splice'), col(7.0, 'splice')])
    assert not E.structure_is_panel_span([col(69.55, 'connector')], [col(21.9, 'splice')])
    assert not E.structure_is_panel_span([], [])
    print('OK')
    """)


def test_a_b_reading_nearer_another_a_event_is_not_this_ones_twin():
    _engine("""
    def ev(km, end=False):
        return {'dist_km': km, 'splice_loss': 0.04, 'type': '0F9999LS',
                'is_end': end}
    r = {'events': [ev(0.0), ev(20.66), ev(21.85), ev(55.0, end=True)]}
    ea = r['events'][2]
    # B's reading at 20.69 (in A's frame) is 20.66's, not 21.85's
    assert E._b_event_is_anothers(r, ea, 20.69)
    # the same closure read 115 m apart by the two ends stays one pair
    assert not E._b_event_is_anothers(r, ea, 21.965)
    # far from ea, but no nearer A event: still ea's (A 21.85 alone)
    r2 = {'events': [ev(0.0), ev(21.85), ev(55.0, end=True)]}
    assert not E._b_event_is_anothers(r2, r2['events'][1], 20.69)
    print('OK')
    """)


# ─── one direction loaded ──────────────────────────────────────────────────
# Robert 2026-09-30: "it has to work for one direction OR bidi, equally".  A
# load from one end has no pair to average, so the table is the
# Unidirectional report's (run_splicereport --uni --viewer-table,
# E.uni_viewer_table): its columns, gates and verdicts, every stored reading
# in the loaded direction's own frame.

def _uni(tmp_path, folder, leg):
    tbl = tmp_path / f"u_{leg}.json"
    p = subprocess.run([sys.executable, str(RUNNER), "--uni", "--dir-a", str(folder),
                        "--viewer-leg", leg, "--out", str(tmp_path / f"u_{leg}.xlsx"),
                        "--viewer-table", str(tbl)], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr[-2000:]
    m = json.loads(p.stdout.strip().splitlines()[-1])
    assert m["ok"] and m["viewer_table"] == str(tbl), m
    return m, json.loads(tbl.read_text(encoding="utf-8"))


def _stored(folder, fibre):
    """A fibre's stored in-span readings, raw km -> loss, from the file."""
    src = ("import sys, json, io, contextlib\n"
           f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
           "import splicereportmatchexfo as E\n"
           "with contextlib.redirect_stdout(io.StringIO()):\n"
           f"    f, *_ = E.uni_load_dir({str(folder)!r})\n"
           f"    E.uni_normalize_all(f)\n"
           f"r = f[{fibre}]\n"
           "eof = next(e['dist_km'] for e in r['events'] if e.get('is_end'))\n"
           "shift = r['_uni_event_offset_km']\n"
           "print(json.dumps({round(e['dist_km'] + shift, 4): e['splice_loss'] for e in r['events']\n"
           "    if not e.get('is_end') and 0.3 < e['dist_km'] < eof\n"
           "    and E._is_inspan_event_type(e.get('type') or '')}))\n")
    p = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return {float(k): v for k, v in json.loads(p.stdout.strip().splitlines()[-1]).items()}


def _check_one_direction(tmp_path, folder, leg, fibre):
    m, t = _uni(tmp_path, folder, leg)
    assert t["direction"] == leg and t["gate_db"] > 0
    _assert_lined_up(t)
    kms = [c["km"] for c in t["columns"]]
    assert kms == sorted(kms), kms            # the loaded direction's own order
    other = "b" if leg == "a" else "a"
    cells = t["fibers"][str(fibre)]
    assert all(c[other] is None for c in cells)
    # every stored reading printed once, at its own number
    got = {round(c[leg]["km"], 4): c[leg]["loss"] for c in cells
           if c[leg] and c[leg]["km"] is not None and c["category"] != "end"}
    want = _stored(folder, fibre)
    for km, loss in want.items():
        assert km in got and abs(got[km] - loss) < 1e-9, (km, loss, sorted(got))
    return t


def test_a_only_one_fibre_prints_every_reading(tmp_path):
    a, _ = _job(tmp_path / "one", {1})
    t = _check_one_direction(tmp_path, a, "a", 1)
    # the far end of the 67.5 km route closes the table
    assert abs(t["columns"][-1]["km"] - 67.5) < 0.2, t["columns"][-1]


def test_b_only_reads_in_b_s_own_frame(tmp_path):
    """B shot alone is drawn from B's end; its table reads the same way: the
    closure A reads at 32.54 km is 35.0 km into the B shot."""
    _, b = _job(tmp_path / "one", {1})
    t = _check_one_direction(tmp_path, b, "b", 1)
    assert any(abs(c["km"] - 35.0) < 0.1 for c in t["columns"]), t["columns"]


def test_one_direction_flags_match_the_full_folder(tmp_path):
    """A one-direction job judged alone flags what the same fibres flag on
    the whole folder: the report's gate, not the job's size, decides."""
    _, b = _job(tmp_path / "few", {1, 2, 3})
    _, few = _uni(tmp_path / "few", b, "b")
    _, full = _uni(tmp_path, FIXTURE_DIR / "splice_B", "b")

    def flags(t, f):
        return sorted(round(c["b"]["km"], 3) for c in t["fibers"][f]
                      if c["flag"] and c["b"] and c["b"]["km"] is not None)
    for f in ("1", "2", "3"):
        assert flags(few, f) == flags(full, f), f


def test_the_viewer_gets_a_one_direction_table(monkeypatch):
    """A Viewer opened on a B folder alone asks for dir=b and gets the
    Unidirectional report's table, in B's frame."""
    import time
    sys.path.insert(0, str(REPO_ROOT / "viewer"))
    import trace_server as TS
    for k in ("dir_a", "dir_b", "suite_table", "end_refl", "analysis_mode", "settings"):
        monkeypatch.setitem(TS.CONFIG, k, TS.CONFIG.get(k))
    TS.CONFIG.update({"dir_a": None, "dir_b": str(FIXTURE_DIR / "span_B"),
                      "suite_table": None, "end_refl": None,
                      "analysis_mode": "suite", "settings": None})
    monkeypatch.setattr(TS, "_UNI_TABLES", {})
    monkeypatch.setattr(TS, "_TRACE_SIG", {})
    fibers = [1, 2, 3, 4]
    for _ in range(3000):
        out = TS.suite_tables(fibers, "b")
        if not out["pending"]:
            break
        time.sleep(0.1)
    assert out["direction"] == "b" and out["gate_db"], out
    assert sorted(map(int, out["tables"])) == fibers and not out["missing"], out
    for cells in out["tables"].values():
        assert all(c["a"] is None for c in cells)
    # the bidirectional ask still says why it has nothing
    assert TS.suite_tables(fibers)["error"] == "both folders are needed"

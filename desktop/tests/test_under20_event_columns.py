"""A job under 80 fibres shows its events and makes no splice or bend call.

Robert, 2026-09-29: "jobs under 20 need to show events and not try to
determine splice or bend"; 2026-10-01: "under 80 we don't try to determine
bend or splice" (E.EVENT_JOB_MAX_FIBERS / UNI_EVENT_JOB_MAX, 79 inclusive).
On such a job:

- Splice Report (bidir, OTDR Suite mode): one "Event N" column per place where
  events line up across the fibres, from either end.  Every fibre's A, B and
  average are read there; the loss gate, breaks, reflectance and the ILA end
  columns still flag.  No Splice/Bends/Damage column, no bend cell.
- Unidirectional report: the same, at the uni gate (UNI_EVENT_JOB_MAX).
- The Viewer shows the report's own table for such a job (Robert 2026-09-30,
  reversing 2026-09-29's FR stand-in): one row per event column, A and B
  paired and averaged, with one fibre from each direction as with nineteen.
- A panel tie keeps its own layout (Robert's choice): bidir when the span
  structure pass recognises it, uni when the span is no longer than
  LAUNCH_FIBER_MAX (reels and panels, not a route).

Engine tests run in a clean subprocess (3-engine sor_reader isolation).
"""
import json
import subprocess
import sys
import textwrap

import openpyxl

from conftest import FIXTURE_DIR, REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"
RUNNER = SPLICEREPORT_DIR / "run_splicereport.py"


def _runner(tmp_path, *args):
    out = tmp_path / "r.xlsx"
    p = subprocess.run([sys.executable, str(RUNNER), *map(str, args), "--out", str(out)],
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stderr[-2000:]
    return json.loads(p.stdout.strip().splitlines()[-1]), out, p.stderr


def _engine(body):
    src = ("import sys\n"
           f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
           "import splicereportmatchexfo as E\n" + textwrap.dedent(body))
    p = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True)
    assert p.returncode == 0, f"{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


# ─── the engine pieces ──────────────────────────────────────────────────────

def test_event_job_is_under_eighty_loaded_fibres():
    """Robert 2026-10-01: "under 80 we don't try to determine bend or
    splice".  79 fibres is an event job, 80 is not; MIN_POP_SPLICE, which
    other population rules read, stays 20."""
    _engine("""
    assert E.EVENT_JOB_MAX_FIBERS == 79 and E.MIN_POP_SPLICE == 20
    assert not E.event_job({})
    assert E.event_job({f: {} for f in range(1, 20)})
    assert E.event_job({f: {} for f in range(1, 80)})
    assert not E.event_job({f: {} for f in range(1, 81)})
    print('OK')
    """)


def test_bidir_cutoff_is_inclusive_on_the_runner(tmp_path):
    """The 24-fibre splice fixture lists events at a cutoff of 24 and calls
    closures again at 23 (the 79/80 boundary, scaled to the fixture)."""
    def kinds(limit):
        m, _, _ = _runner(tmp_path, "--dir-a", FIXTURE_DIR / "splice_A",
                          "--dir-b", FIXTURE_DIR / "splice_B",
                          "--overrides", json.dumps({"EVENT_JOB_MAX_FIBERS": limit}))
        return {c["kind"] for c in m["columns"]}, m["event_job"]
    at, ev = kinds(24)
    assert ev and at == {"event"}, at
    below, ev = kinds(23)
    assert not ev and "splice" in below and "event" not in below, below


def test_event_columns_come_from_either_end():
    """A-only events and B-only events each make a column; one place seen by
    both ends is one column, centred on A's reading."""
    _engine("""
    def ev(km, end=False, typ='0F9999LS'):
        return {'dist_km': km, 'splice_loss': 0.05, 'type': '1E9999LS' if end else typ,
                'is_end': end, 'is_reflective': False}
    SPAN = 30.0
    fa = {1: {'events': [ev(0.0, typ='1F9999LS'), ev(10.0), ev(SPAN, True)]},
          2: {'events': [ev(0.0, typ='1F9999LS'), ev(10.02), ev(SPAN, True)]}}
    fb = {1: {'events': [ev(0.0, typ='1F9999LS'), ev(SPAN - 10.0), ev(SPAN - 20.0), ev(SPAN, True)]},
          2: {'events': [ev(0.0, typ='1F9999LS'), ev(SPAN, True)]}}
    cols = E.discover_event_columns(fa, fb)
    kms = [round(c['position_km_refined'], 2) for c in cols]
    assert kms == [10.01, 20.0], kms           # 10 from both ends, 20 from B alone
    assert all(c['is_event_column'] and c['column_kind'] == 'splice' for c in cols)
    assert cols[0]['position_km_refined'] == 10.01   # A's readings, not B's mirror
    print('OK')
    """)


def test_a_bend_cell_becomes_a_gated_loss_reading():
    """neutralize_event_job: a bend column is an event column; a bend cell over
    the gate prints as a plain loss, one under it is dropped; a break stays."""
    _engine("""
    splices = [{'position_km': 20.0, 'position_km_refined': 20.0, 'column_kind': 'bend'},
               {'position_km': 5.0, 'position_km_refined': 5.0, 'column_kind': 'splice'}]
    res = {(1, 0): {'bidir_loss': 0.30, 'is_bend': True, 'label': '1 BEND .300 bidi',
                    'is_break': False, 'is_broke': False},
           (2, 0): {'bidir_loss': 0.09, 'is_bend': True, 'label': '2 bend .090 bidi',
                    'is_break': False, 'is_broke': False},
           (3, 1): {'bidir_loss': None, 'is_break': True, 'is_broke': False,
                    'label': '3 broke@5.0k'}}
    out, cols = E.neutralize_event_job(res, splices, 0.160)
    assert [c['position_km'] for c in cols] == [5.0, 20.0]
    assert all(c['column_kind'] == 'splice' and c['is_event_column'] for c in cols)
    assert set(out) == {(1, 1), (3, 0)}, out.keys()
    assert out[(1, 1)]['is_bend'] is False and out[(1, 1)]['label'] == '1 .300'
    assert out[(3, 0)]['is_break'] is True
    print('OK')
    """)


# ─── the Splice Report on a route under 20 fibres ──────────────────────────

def test_bidir_route_under_twenty_prints_events(tmp_path):
    """portoutlier: 4 fibres of a 48 km route.  Main called 13 splices; now
    every column is an event, and the four over-gate readings at 42.29 km still
    flag, at the same numbers (F2's other reading, .191, flags on main too)."""
    m, xlsx, err = _runner(tmp_path, "--dir-a", FIXTURE_DIR / "portoutlier" / "A",
                           "--dir-b", FIXTURE_DIR / "portoutlier" / "B",
                           "--viewer-table", tmp_path / "t.json")
    assert "event column(s), no closure or bend calls" in err
    kinds = {c["kind"] for c in m["columns"]}
    assert kinds == {"event"}, kinds
    assert not any(c["category"] in ("bend", "damage") for c in m["cells"])
    got = sorted((c["fiber"], round(c["km"], 2), c["loss"])
                 for c in m["cells"] if c["category"] == "reburn")
    assert [g for g in got if g[1] == 42.29] == [
        (2, 42.29, 0.623), (3, 42.29, 0.634), (4, 42.29, 0.291), (5, 42.29, 0.24)], got
    heads = [c.value for c in openpyxl.load_workbook(xlsx)["Splice Report"][3] if c.value]
    events = [h for h in heads if str(h).startswith("Event ")]
    assert events and not any(str(h).startswith(("Splice ", "Bends")) for h in heads)
    # the Viewer's Suite table is written, its cells on its own columns
    t = json.loads((tmp_path / "t.json").read_text(encoding="utf-8"))
    assert m["viewer_table"] == str(tmp_path / "t.json")
    for cells in t["fibers"].values():
        cols = [c["col"] for c in cells]
        assert len(cols) == len(set(cols)) and all(0 <= c < len(t["columns"]) for c in cols)


def test_bidir_breaks_still_flag_on_an_event_job(tmp_path):
    """doublebreak (11 fibres): the seven break cells (#358/#360, incl. the
    B-side double breaks) print exactly as they do on a closure layout."""
    m, _, _ = _runner(tmp_path, "--dir-a", FIXTURE_DIR / "doublebreak" / "A",
                      "--dir-b", FIXTURE_DIR / "doublebreak" / "B")
    assert {c["kind"] for c in m["columns"]} == {"event"}
    broke = sorted((c["fiber"], round(c["km"], 2), c["label"])
                   for c in m["cells"] if c["category"] == "broke")
    assert broke == [
        (421, 98.34, '421 broke@98.4k (B-fill OK)'),
        (427, 92.69, '427 broke@92.6k | DZ 92.6-98.3k'),
        (427, 98.34, '427 broke@98.3k (B-only)'),
        (428, 12.56, '428 broke@12.6k | DZ 12.6-98.3k'),
        (428, 98.34, '428 broke@98.3k (B-only)'),
        (432, 92.69, '432 broke@92.6k | DZ 92.6-98.3k'),
        (432, 98.34, '432 broke@98.3k (B-only)')], broke


def test_bidir_panel_tie_keeps_its_structure(tmp_path):
    """A 12-fibre panel tie is under 20 but keeps panel / section / panel."""
    m, _, err = _runner(tmp_path, "--dir-a", FIXTURE_DIR / "panelmint" / "A",
                        "--dir-b", FIXTURE_DIR / "panelmint" / "B")
    assert "event column" not in err
    assert [c["kind"] for c in m["columns"]] == ["connector", "section", "connector"]


# ─── the Unidirectional report ─────────────────────────────────────────────

def test_uni_route_under_twenty_prints_events(tmp_path):
    """portoutlier A alone: the 42.29 km readings main called Bend/Damage are an
    event column now, same fibres, same numbers; no Splice or Bend/Damage."""
    m, _, _ = _runner(tmp_path, "--uni", "--dir-a", FIXTURE_DIR / "portoutlier" / "A")
    labels = [c["label"] for c in m["uni"]["grid_columns"]]
    assert any(l.startswith("Event ") for l in labels)
    assert not any(l.startswith(("Splice ", "Bend/Damage", "Entry")) for l in labels), labels
    lab = {i: c["label"] for i, c in enumerate(m["uni"]["grid_columns"])}
    got = sorted((c["fiber"], c["loss"]) for c in m["uni"]["cells"]
                 if str(lab.get(c["col"], "")).startswith("Event "))
    assert got == [(2, 0.623), (3, 0.633), (4, 0.293)], got


def test_uni_panel_tie_keeps_its_layout(tmp_path):
    """panelmint A (12 fibres, a 1.06 km span through the reel) is no longer
    than LAUNCH_FIBER_MAX: its columns are what they were, no Event column."""
    m, _, _ = _runner(tmp_path, "--uni", "--dir-a", FIXTURE_DIR / "panelmint" / "A")
    labels = [c["label"] for c in m["uni"]["grid_columns"]]
    assert labels == ["Launch Connector", "Connector 2", "Splice 1"], labels


# ─── the Viewer ─────────────────────────────────────────────────────────────

def test_the_viewer_gets_the_suite_table_for_an_event_job(monkeypatch):
    """The Viewer's own run on a job under 20 fibres gets the report's Suite
    table for every loaded fibre (Robert 2026-09-30: "Viewer should always
    correctly pair the events in OTDR mode even if we only have one fiber
    from each direction")."""
    import time
    sys.path.insert(0, str(REPO_ROOT / "viewer"))
    import trace_server as TS
    for k in ("dir_a", "dir_b", "suite_table", "end_refl", "analysis_mode"):
        monkeypatch.setitem(TS.CONFIG, k, TS.CONFIG.get(k))
    TS.CONFIG.update({"dir_a": str(FIXTURE_DIR / "portoutlier" / "A"),
                      "dir_b": str(FIXTURE_DIR / "portoutlier" / "B"),
                      "suite_table": None, "end_refl": None,
                      "analysis_mode": "suite"})
    monkeypatch.setattr(TS, "_END_VERDICTS", {})
    monkeypatch.setattr(TS, "_SUITE_TABLE_FILE", {})
    monkeypatch.setattr(TS, "_TRACE_SIG", {})
    fibers = [2, 3, 4, 5]
    for _ in range(3000):
        out = TS.suite_tables(fibers)
        if not out["pending"]:
            break
        time.sleep(0.1)
    assert sorted(map(int, out["tables"])) == fibers and not out["missing"], out


def test_uni_events_reach_seventy_nine_fibres_inclusive(tmp_path):
    """Robert 2026-10-01: under 80 a uni job uses events (was "up to 50").
    The limit is UNI_EVENT_JOB_MAX (79) and it is inclusive: the 24-fibre
    splice_A job lists events at a limit of 24 and calls closures again at
    23 (the 79/80 boundary, scaled to the fixture)."""
    _engine("""
    assert E.UNI_EVENT_JOB_MAX == 79
    print('OK')
    """)
    def labels(limit):
        m, _, _ = _runner(tmp_path, "--uni", "--dir-a", FIXTURE_DIR / "splice_A",
                          "--overrides", json.dumps({"UNI_EVENT_JOB_MAX": limit}))
        return [c["label"] for c in m["uni"]["grid_columns"]]
    at = labels(24)
    assert any(l.startswith("Event ") for l in at), at
    assert not any(l.startswith(("Splice ", "Bend/Damage")) for l in at), at
    below = labels(23)
    assert not any(l.startswith("Event ") for l in below), below
    assert any(l.startswith("Splice ") for l in below), below

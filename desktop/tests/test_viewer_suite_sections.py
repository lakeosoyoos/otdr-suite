"""Sections in the Viewer's OTDR Suite table.

Robert, 2026-10-02: "in Suite mode, we want Sections back.  we want to be
able to turn them on in our drop down menu that has our show failed events".
Since #345 the Suite table was the report's columns alone and the Sections
Off switch hid itself there.  Now the report run measures the glass between
each fibre's readings in neighbouring columns (E.viewer_table_sections, the
report's own section fit, _section_stats_from_trace) and the Viewer prints it
as FastReporter mode does: Loss and Att. per direction, the Average row their
mean, behind the same Sections Off switch in the gear menu.

On a real span (fibers 1-4, 2026-10-02) the Suite sections read within
1 mdB of FastReporter mode's on every printed cell checked, e.g. F1 Event 1 ->
Event 2: A 1.063 / 0.186, B 1.037 / 0.182, Average 1.050 / 0.184 (FR: 1.063,
1.036, 1.050).

Engine tests run in a clean subprocess (three engines, three sor_reader
copies, never one process).
"""
import json
import subprocess
import sys
import textwrap

import pytest

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"
RUNNER = SPLICEREPORT_DIR / "run_splicereport.py"
FIX = REPO_ROOT / "desktop" / "tests" / "fixtures"
SPLICE_A, SPLICE_B = FIX / "splice_A", FIX / "splice_B"
EAST = FIX / "panelreels_east"
VIEWER = (REPO_ROOT / "viewer" / "viewer.html").read_text(encoding="utf-8")


def _fn(name):
    i = VIEWER.index('function ' + name + '(')
    return VIEWER[i:VIEWER.index('\n}\n', i) + 3]


def _run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr[-2000:]}"
    man = json.loads(p.stdout.strip().splitlines()[-1])
    assert man["ok"], man
    return man


def _table(out_dir, *args):
    path = out_dir / "table.json"
    _run([sys.executable, str(RUNNER), *args, "--out", str(out_dir / "r.xlsx"),
          "--viewer-table", str(path)])
    return json.loads(path.read_text(encoding="utf-8"))


CLOSURES = ("--overrides", json.dumps({"EVENT_JOB_MAX_FIBERS": 0}))


@pytest.fixture(scope="module")
def splice(tmp_path_factory):
    return _table(tmp_path_factory.mktemp("suite_sections"),
                  "--dir-a", str(SPLICE_A), "--dir-b", str(SPLICE_B), *CLOSURES)


def _sections(table):
    return [(f, c) for f, cells in table["fibers"].items() for c in cells
            if c.get("section")]


# ─── the plumbing, on made-up cells ───────────────────────────────────────

def test_a_section_runs_between_a_fibre_s_readings_in_neighbouring_columns():
    body = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})
        import splicereportmatchexfo as E
        calls = []
        def fit(r, a, b):
            calls.append((r['n'], round(a, 4), round(b, 4)))
            return (None, r['att'])
        E._section_stats_from_trace = fit
        rec = {{'a': {{1: {{'n': 'A', 'att': 0.20, 'trace': [0], 'user_offset_km': 0.5}}}},
               'b': {{1: {{'n': 'B', 'att': 0.30, 'trace': [0]}}}}}}
        leg = lambda km: {{'km': km, 'loss': 0.1}}
        cols = [{{'kind': 'end'}}, {{'kind': 'splice'}}, {{'kind': 'splice'}},
                {{'kind': 'section'}}, {{'kind': 'connector'}}]
        cells = [
            {{'col': 0, 'a': leg(1.0), 'b': leg(9.0)}},
            {{'col': 1, 'a': leg(3.0), 'b': leg(7.0)}},
            {{'col': 2, 'a': {{'km': None, 'loss': 0.0, 'grey': True}}, 'b': leg(5.0)}},
            {{'col': 3, 'a': leg(6.0), 'b': leg(4.0)}},
            {{'col': 4, 'a': leg(7.0), 'b': leg(3.0)}},
        ]
        E.viewer_table_sections({{'1': cells}}, rec, cols)
        s = cells[0]['section']
        # A runs forward from 1.0 to 3.0 km on its trace (+ its declared
        # start); B the other way, 7.0 to 9.0 km of its own frame
        assert calls[:2] == [('A', 1.5, 3.5), ('B', 7.0, 9.0)], calls
        assert s['a'] == {{'length_m': 2000.0, 'loss': 0.4, 'att_db_km': 0.2}}, s
        assert s['b'] == {{'length_m': 2000.0, 'loss': 0.6, 'att_db_km': 0.3}}, s
        assert (s['length_m'], s['loss'], s['att_db_km']) == (2000.0, 0.5, 0.25), s
        # a grey A leg with no km: B alone, and no Average (FR's rule)
        s = cells[1]['section']
        assert s['a'] is None and s['b']['length_m'] == 2000.0, s
        assert s['loss'] is None and s['att_db_km'] is None, s
        # ... unless the table can place it
        cells2 = [dict(c) for c in cells]
        E.viewer_table_sections({{'1': cells2}}, rec, cols,
                                km_of=lambda side, f, c: 5.0 if side == 'a' else None)
        assert cells2[1]['section']['a']['length_m'] == 2000.0
        # next to a report's own Section column nothing is added, and the
        # last column has nothing after it
        assert 'section' not in cells[2] and 'section' not in cells[3]
        assert 'section' not in cells[4]
        # a one-direction table: the merged figures are that direction's own
        one = [{{'col': 0, 'a': None, 'b': leg(1.0)}}, {{'col': 1, 'a': None, 'b': leg(4.0)}}]
        E.viewer_table_sections({{'1': one}}, {{'b': rec['b']}}, cols[:2])
        s = one[0]['section']
        assert s['a'] is None and (s['length_m'], s['loss'], s['att_db_km']) == (3000.0, 0.9, 0.3), s
        # a fit that fails costs that section, never the table
        def boom(*a):
            raise ValueError('no trace')
        E._section_stats_from_trace = boom
        c3 = [{{'col': 0, 'a': leg(1.0), 'b': None}}, {{'col': 1, 'a': leg(2.0), 'b': None}}]
        E.viewer_table_sections({{'1': c3}}, rec, cols[:2])
        assert 'section' not in c3[0]
        print('OK')
    """)
    p = subprocess.run([sys.executable, "-c", body], capture_output=True, text=True)
    assert p.returncode == 0 and p.stdout.strip().endswith("OK"), p.stdout + p.stderr


# ─── a real span ──────────────────────────────────────────────────────────

def test_the_report_run_measures_each_fibre_s_sections(splice):
    table = splice
    secs = _sections(table)
    # 24 fibres, ~16 columns: nearly every neighbouring pair has a section
    assert len(secs) >= 300, len(secs)
    both = 0
    for f, c in secs:
        s = c["section"]
        cells = {x["col"]: x for x in table["fibers"][f]}
        nxt = cells[c["col"] + 1]                     # the next column, never further
        for w in "ab":
            leg = s[w]
            if leg is None:
                continue
            # the slope over the whole event-to-event length
            assert leg["length_m"] > 0
            assert abs(leg["loss"] - leg["att_db_km"] * leg["length_m"] / 1000.0) < 1e-3
            k0, k1 = (c[w] or {}).get("km"), (nxt[w] or {}).get("km")
            if k0 is not None and k1 is not None:
                assert abs(leg["length_m"] - abs(k1 - k0) * 1000.0) < 0.02
        if s["a"] and s["b"]:
            both += 1
            assert abs(s["loss"] - (s["a"]["loss"] + s["b"]["loss"]) / 2.0) < 1e-3
            assert abs(s["length_m"] - (s["a"]["length_m"] + s["b"]["length_m"]) / 2.0) < 0.02
            # one stretch of glass, read from both ends
            assert abs(s["a"]["length_m"] - s["b"]["length_m"]) < 100.0
    assert both >= 0.9 * len(secs)
    # glass reads like glass: a long section's attenuation is a fibre's
    long_ = [s[w]["att_db_km"] for _f, c in secs for s in [c["section"]]
             for w in "ab" if s[w] and s[w]["length_m"] > 1000.0]
    assert long_ and sum(0.15 <= a <= 0.45 for a in long_) >= 0.95 * len(long_)


def test_a_panel_tie_keeps_the_report_s_own_section_column(tmp_path):
    """Connector, Section 31m, Connector: the report's Section column already
    describes that glass, so the table adds none beside it."""
    table = _table(tmp_path, "--dir-a", str(EAST / "A"), "--dir-b", str(EAST / "B"))
    assert [c["kind"] for c in table["columns"]] == ["connector", "section", "connector"]
    assert _sections(table) == []


def test_a_one_direction_table_has_that_direction_s_sections(tmp_path):
    for leg, folder in (("a", SPLICE_A), ("b", SPLICE_B)):
        out = tmp_path / leg
        out.mkdir()
        table = _table(out, "--uni", "--dir-a", str(folder), "--viewer-leg", leg)
        secs = _sections(table)
        assert len(secs) >= 100, (leg, len(secs))
        other = "b" if leg == "a" else "a"
        for _f, c in secs:
            s = c["section"]
            assert s[other] is None and s[leg] is not None
            assert (s["length_m"], s["loss"]) == (s[leg]["length_m"], s[leg]["loss"])


# ─── the Viewer prints them, behind the Sections Off switch ───────────────

def test_the_suite_table_prints_sections_behind_the_switch():
    body = _fn('paintSuiteBidiGrid')
    # the report's figures, printed only: a section to the NEXT column
    assert "return x && x.section && cols[i + 1] && cols[i + 1].ev[fi] ? x.section : null;" in body
    # the same switch as FastReporter mode's, and the cell filters fold them
    assert "const showSec = gShowSections && !collapse;" in body
    assert "const secCol = cols.map((_c, i) => showSec && keepCol[i] && i < cols.length - 1" in body
    # the switch shows whenever the table carries sections
    assert "if (secLab) secLab.style.display = anySec ? '' : 'none';" in body
    assert "secLab.style.display = 'none';       // the report has no sections" not in VIEWER
    # FastReporter's header and cells: Section, its length, Loss and Att.
    # with FR's count of the fibers that have a section there
    assert "h1 += `<th colspan=\"2\" class=\"fr-sechdr\">Section (${nSec}/${have.length})</th>`;" in body
    assert "<th class=\"fr-sub fr-sec\">Att.<br>(dB/km)</th>" in body
    assert "const v = secFig(secOf(fi, i), which);" in body
    assert "(which === 'avg' || oneDir) ? s : s[which]" in body
    # the spacer rows span them, and Minimum / Maximum / Average cover them
    assert "+ secCol.filter(Boolean).length * 2;" in body
    assert "const ss = have.map((_p, fi) => secFig(secOf(fi, i), 'avg')).filter(Boolean);" in body
    # the switch repaints the table it sits under
    i = VIEWER.index("document.getElementById('set-sections-off').onchange")
    assert "renderEventTable();" in VIEWER[i:i + 300]

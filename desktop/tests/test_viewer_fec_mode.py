"""Viewer FEC mode (Robert 2026-10-01).

FEC shots are short traces from each END of a span, so A and B never see the
same glass.  In FEC mode the Viewer neither mirrors B nor pairs it with A, and
its event table grades each trace's panel connector on its own with the
Splice Report FEC tool's rule, through /api/fec_table -> the runner's
--fec-table (the server never imports the engine).
"""
import os
import re
import shutil
import subprocess
import sys
import textwrap

import pytest

from conftest import (FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR, REPO_ROOT,
                      VIEWER_DIR)

RUNNER = REPO_ROOT / "splicereport" / "run_splicereport.py"
HTML = (VIEWER_DIR / "viewer.html").read_text(encoding="utf-8")


def _run(body, tmp_path):
    da, db = tmp_path / "A", tmp_path / "B"
    da.mkdir(); db.mkdir()
    # one fibre from each end, copied under neutral names (fibre 17)
    shutil.copy(sorted(FIXTURE_SPLICE_A_DIR.glob("*.sor"))[16], da / "ENDA0017_1550.sor")
    shutil.copy(sorted(FIXTURE_SPLICE_B_DIR.glob("*.sor"))[16], db / "ENDB0017_1550.sor")
    header = ("import sys, os, json, subprocess\n"
              f"sys.path.insert(0, {str(VIEWER_DIR)!r})\n"
              "import trace_server as T\n"
              f"T.CONFIG['dir_a'] = {str(da)!r}\n"
              f"T.CONFIG['dir_b'] = {str(db)!r}\n"
              f"T.CONFIG['engine_argv'] = [sys.executable, {str(RUNNER)!r}]\n")
    p = subprocess.run([sys.executable, "-c", header + textwrap.dedent(body)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


def test_each_side_is_graded_on_its_own_and_cached(tmp_path):
    _run("""
        res = T.fec_tables([17, 5])
        assert res['error'] is None, res['error']
        a, b = res['grades']['A']['17'], res['grades']['B']['17']
        for g in (a, b):
            assert g['found'] and 'loss' in g and 'fail_loss' in g, g
            assert g['conn_km'] > 0.05, g          # past the OTDR port
        assert '5' not in res['grades']['A']        # no such fibre
        assert res['gates']['FEC_LOSS_GATE'] == 0.5
        real = subprocess.run
        def boom(*a, **k): raise AssertionError('engine run twice for an unchanged file')
        subprocess.run = boom
        try:
            again = T.fec_tables([17])
        finally:
            subprocess.run = real
        assert again['grades']['A']['17'] == a and again['grades']['B']['17'] == b
        print('OK')
    """, tmp_path)


def test_the_profile_gates_reach_the_engine(tmp_path):
    """A gate change is a new cache key and is what the engine grades by."""
    _run("""
        base = T.fec_tables([17])['grades']['A']['17']
        T.set_fec_gates({'FEC_LOSS_GATE': 0.0, 'FEC_LOSS_STRICT': 0,
                         'FEC_REFL_GATE': -50.0, 'FEC_COMBINE_M': 150.0})
        res = T.fec_tables([17])
        g = res['grades']['A']['17']
        assert res['gates']['FEC_LOSS_GATE'] == 0.0, res['gates']
        assert g['loss'] == base['loss'] and g['fail_loss'] == (g['loss'] >= 0.0)
        T.set_fec_gates(None)
        assert T.CONFIG['fec_gates'] is None
        print('OK')
    """, tmp_path)


def test_fec_mode_switches_the_mirror_off():
    flip = re.search(r"function isFlipped\(t\) \{([^}]*)\}", HTML).group(1)
    assert '!gFecMode' in flip
    yoff = re.search(r"function yOffsetFor\(t\) \{([^}]*)\}", HTML).group(1)
    assert '!gFecMode' in yoff
    assert 'if (gFecMode || !gStacked || !gHaveA) return;' in HTML


def test_fec_mode_has_its_own_table_ahead_of_the_pairing_grids():
    body = HTML[HTML.index('function renderEventTable() {'):]
    body = body[:body.index('\n}\n')]
    assert body.index('renderFecGrid') < body.index('renderSuiteBidiGrid')
    assert "fetch(`/api/fec_table?fibers=" in HTML
    # the side is the folder a trace came from, never a pairing
    assert "t.src === 'b' ? 'B' : 'A'" in HTML


def test_fec_mode_comes_only_from_the_viewer_fec_link():
    """No switch on the gear (Robert 2026-10-01): only the Viewer FEC tool's
    ?fec=1 address turns it on, so nothing is remembered between visits."""
    assert 'id="set-fec"' not in HTML
    assert 'otdr_viewer_fec' not in HTML
    assert "gFecMode = new URLSearchParams(location.search).get('fec') === '1';" in HTML
    assert "'FEC mode'].filter(Boolean)" not in HTML


def test_the_short_shot_warning_points_to_fec_mode():
    assert 'For facility-entrance (FEC) shots, use the Viewer FEC tool.' in HTML


def test_fec_table_ends_with_min_max_average_that_pins():
    """Robert 2026-10-01: the FEC table had no Min/Max/Average strip, so
    "Pin Min/Max/Average" did nothing there.  It now ends in a <tfoot> like
    the other grids (the sticky/unpinned CSS works on any fr-table tfoot)."""
    body = HTML[HTML.index('function paintFecGrid('):]
    body = body[:body.index('\n}\n')]
    for label in ("'Minimum'", "'Maximum'", "'Average'"):
        assert label in body
    assert '<tfoot>${aggRows.join' in body
    assert 'class="fr-agg"' in body
    # Min/Max name their trace and a click finds the row; centred in the
    # window the pinned footer leaves
    assert 'class="fr-own"' in body and 'pinnedFootH(table)' in body
    assert 'table.fr-table tfoot { position: sticky; bottom: 0;' in HTML
    assert '#event-panel.agg-unpinned table.fr-table tfoot { position: static; }' in HTML


JSC = ('/System/Library/Frameworks/JavaScriptCore.framework/Versions/'
       'Current/Helpers/jsc')


def _paint_fec(switches, with_marks=False):
    """Run the real paintFecGrid under JavaScriptCore on three made-up
    grades and return the table markup it builds.  F1 fails on loss (the
    connector plus one event behind it), F2 on reflectance, F3 passes with a
    negative connector loss (a gainer)."""
    if not os.path.exists(JSC):
        pytest.skip('JavaScriptCore shell not available')
    import json, tempfile

    def fn(name):
        i = HTML.index(f'function {name}(')
        return HTML[i:HTML.index('\n}\n', i) + 3]

    def line(pat):
        return re.search(pat, HTML).group(0) + '\n'

    grades = {'A': {
        '1': {'found': True, 'conn_km': 1.0121, 'conn_loss': 0.306, 'loss': 0.549,
              'combined': [{'loss': 0.243, 'km': 1.06}], 'refl': -53.0,
              'fail_loss': True, 'fail_refl': False},
        '2': {'found': True, 'conn_km': 1.0117, 'conn_loss': 0.100, 'loss': 0.100,
              'combined': [], 'refl': -45.2, 'fail_loss': False, 'fail_refl': True},
        '3': {'found': True, 'conn_km': 1.0050, 'conn_loss': -0.032, 'loss': -0.032,
              'combined': [], 'refl': -58.0, 'fail_loss': False, 'fail_refl': False},
    }}
    traces = [{'fiber': f, 'src': 'a', 'key': f'a-{f}', 'color': '#000',
               'data': {'wavelength_nm': 1550}} for f in (1, 2, 3)]
    js = ("var gInfo=null, gGridGoTo=null, gTableExport=null, gPickKey=null, gDrawerMarks=[];"
          " const FR_ROW_H=22; var window={getSelection:()=>''};\n"
          "function draw(){} function zoomToKm(){} function pinnedFootH(){return 0}"
          " function setReadout(){} function pickRow(){}\n"
          "var OUT='';\n"
          "function el(){return {className:'',style:{},_ih:'',"
          "set innerHTML(v){this._ih=v; if(this.className==='fr-table') OUT=v;},"
          " get innerHTML(){return this._ih}, appendChild(){}, querySelectorAll(){return []},"
          " addEventListener(){}, tFoot:null, tHead:null, get tBodies(){return [el()]}};}\n"
          "var document={createElement:el};\n"
          + line(r"function flagsOff\(\).*")
          + line(r"const pfClass = .*") + line(r"const pfMark = .*")
          + line(r"let gFailCellsOnly = false;") + line(r"let gWarnCellsOnly = false;")
          + line(r"const cellFilterOn = .*") + "let gShowGainers = true;\n"
          + line(r"const gainerHidden = .*")
          + fn('isPicked') + fn('fecGateText') + fn('paintFecGrid')
          + ''.join(f"{k} = {json.dumps(v)};\n" for k, v in switches.items())
          + f"var hint=el(); paintFecGrid({json.dumps(traces)}, {json.dumps({'grades': grades})}, el(), hint);\n"
          + "print(hint.textContent); print(JSON.stringify(gDrawerMarks)); print(OUT);\n")
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False) as fh:
        fh.write(js)
    try:
        p = subprocess.run([JSC, fh.name], capture_output=True, text=True)
    finally:
        os.unlink(fh.name)
    assert p.returncode == 0 and 'Exception' not in p.stdout, p.stdout + p.stderr
    hint, marks, out = p.stdout.split('\n', 2)
    body = re.search(r'<tbody>(.*)</tbody>', out, re.S).group(1)
    foot = re.search(r'<tfoot>(.*)</tfoot>', out, re.S)
    if with_marks:
        return json.loads(marks)
    return hint, body, foot.group(1) if foot else ''


def _cell(html, fiber, col):
    row = re.search(rf'<tr[^>]*data-key="a-{fiber}"[^>]*>(.*?)</tr>', html, re.S)
    if not row:
        return None
    return re.search(rf'<td data-col="{col}"[^>]*>(.*?)</td>', row.group(1)).group(1)


def _agg(foot, label, col_index):
    row = re.search(rf'<td class="fr-rowlab">{label}</td>(.*?)</tr>', foot, re.S).group(1)
    return re.sub(r'<[^>]+>', '', re.findall(r'<td[^>]*>.*?</td>', row)[col_index])


def test_fec_table_all_switches_off_prints_every_cell():
    hint, body, foot = _paint_fec({})
    assert [_cell(body, f, 'loss') for f in (1, 2, 3)] == ['0.549', '0.100', '-0.032']
    assert _cell(body, 2, 'refl') == '-45.2'
    # Minimum / Maximum / Average strip; columns: 3 blanks, km, conn, comb, loss, refl
    assert _agg(foot, 'Minimum', 6) == '-0.032' and _agg(foot, 'Maximum', 6) == '0.549'


def test_fec_failing_cells_only_keeps_what_failed():
    hint, body, foot = _paint_fec({'gFailCellsOnly': True})
    assert 'failing cells only' in hint
    assert _cell(body, 3, 'loss') is None                 # a passing trace leaves
    assert _cell(body, 1, 'loss') == '0.549' and _cell(body, 1, 'conn') == '0.306'
    assert '0.243 @ 1.060 km' in body                     # what the loss adds up
    assert _cell(body, 1, 'refl') == ''                   # F1's refl passed
    assert _cell(body, 2, 'loss') == '' and _cell(body, 2, 'refl') == '-45.2'
    # the strip is over the figures that print
    assert _agg(foot, 'Maximum', 6) == '0.549' and _agg(foot, 'Minimum', 6) == '0.549'
    assert _agg(foot, 'Maximum', 7) == '-45.2'


def test_fec_warning_cells_only_says_fec_has_no_warnings():
    hint, body, foot = _paint_fec({'gWarnCellsOnly': True})
    assert 'warning cells only: FEC has no warning band' in hint
    assert 'data-key=' not in body
    hint, body, _ = _paint_fec({'gWarnCellsOnly': True, 'gFailCellsOnly': True})
    assert _cell(body, 1, 'loss') == '0.549' and _cell(body, 3, 'loss') is None


def test_fec_gainers_off_blanks_negative_losses_and_leaves_the_strip():
    hint, body, foot = _paint_fec({'gShowGainers': False})
    assert 'gainers hidden' in hint
    assert _cell(body, 3, 'loss') == '' and _cell(body, 3, 'conn') == ''
    assert _cell(body, 3, 'refl') == '-58.0'
    assert _agg(foot, 'Minimum', 6) == '0.100' and _agg(foot, 'Minimum', 4) == '0.100'


def test_fec_rows_pick_their_trace_and_the_chart_finds_the_row():
    body = HTML[HTML.index('function paintFecGrid('):]
    body = body[:body.index('\n}\n')]
    assert 'pickRow([tr.dataset.key])' in body
    assert 'gGridGoTo = (t) =>' in body
    assert "tr.classList.toggle('fr-pick', isPicked(tr.dataset.key))" in body
    hint, html, _ = _paint_fec({'gPickKey': 'a-2'})
    assert re.search(r'data-key="a-2" class="fr-pick"', html)
    assert not re.search(r'data-key="a-1" class="fr-pick"', html)


def test_fec_marks_the_chart_at_the_connector_and_its_combined_events():
    """Show Event Labels / Show Failed Event Labels draw from gDrawerMarks:
    the FEC table hands the chart the connector (its FEC loss), each event it
    combines (its own loss, failing with the sum) and the reflectance."""
    (D,) = _paint_fec({}, with_marks=True)
    assert D['mode'] == 'fec' and D['single'] is True
    # FEC grades only the connector, so the traces keep their event numbers
    assert D['keepNumbers'] is True
    assert 'if (!D.keepNumbers) D.have.forEach(' in HTML
    cols = {c['title']: c for c in D['cols']}
    assert set(cols) == {'Panel Connector', 'Combined Event', 'Connector Refl.'}
    conn = {c['fi']: c for c in cols['Panel Connector']['cells']}
    assert conn[0]['avg'] == 0.549 and conn[0]['avgFail'] and conn[0]['legs']['a']['km'] == 1.0121
    assert not conn[2]['avgFail']
    (comb,) = cols['Combined Event']['cells']
    assert comb['fi'] == 0 and comb['km'] == 1.06 and comb['avg'] == 0.243 and comb['avgFail']
    refl = {c['fi']: (c['avg'], c['avgFail']) for c in cols['Connector Refl.']['cells']}
    assert refl == {0: (-53.0, False), 1: (-45.2, True), 2: (-58.0, False)}
    assert cols['Connector Refl.']['word'] == 'refl'
    # the filters reach the chart: failing cells only marks only what failed
    (D,) = _paint_fec({'gFailCellsOnly': True}, with_marks=True)
    cols = {c['title']: [x['fi'] for x in c['cells']] for c in D['cols']}
    assert cols == {'Panel Connector': [0], 'Combined Event': [0], 'Connector Refl.': [1]}


def test_viewer_script_still_parses():
    jsc = ('/System/Library/Frameworks/JavaScriptCore.framework/Versions/'
           'Current/Helpers/jsc')
    if not os.path.exists(jsc):
        pytest.skip('JavaScriptCore shell not available')
    scripts = re.findall(r'<script>(.*?)</script>', HTML, re.S)
    src = max(scripts, key=len)
    import tempfile
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False) as fh:
        fh.write(src)
        path = fh.name
    try:
        p = subprocess.run([jsc, '-e', f"checkSyntax('{path}')"],
                           capture_output=True, text=True, timeout=60)
        assert p.returncode == 0 and 'Error' not in (p.stdout + p.stderr), \
            p.stdout + p.stderr
    finally:
        os.remove(path)

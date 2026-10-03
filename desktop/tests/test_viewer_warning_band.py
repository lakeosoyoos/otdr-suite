"""Warning cells in the Viewer's event panel (Robert 2026-09-26).

Each loss row of the Splice Report's OTDR Settings (bidir splice, 1-direction
splice, bidir connector, 1-direction connector) has a Warning beside its Fail.
A reading at or over Warning but under Fail prints bright yellow in the
Viewer's event panel, and ONLY there: the report and the uni report stay flag
or blank.  Warning equal to Fail, every profile's default, changes nothing.

The path, end to end, and what each test pins:
  1. the hub sends a Warning only when it opens a band below Fail;
  2. the runner echoes it on the bidirectional manifest without touching the
     engine, so the report is byte-for-byte what it was;
  3. the trace server hands it to the Viewer, and drops it with the report;
  4. the Viewer colours the band and never counts it as a failure.
No Node here, so the JS is checked at the source, like the other Viewer tests.
"""
import ast
import os
import re

import pytest

from conftest import (
    run_splicereport, import_trace_server,
    FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR, REPO_ROOT,
)

APP = REPO_ROOT / 'app.py'
VIEWER = (REPO_ROOT / 'viewer' / 'viewer.html').read_text(encoding='utf-8')
TS = import_trace_server()
WARN_NAMES = {'REBURN_WARN_DB', 'SINGLE_DIR_WARN_DB',
              'BIDIR_CONNECTOR_WARN_DB', 'LAUNCH_CONN_UNI_WARN_DB'}


def _app_namespace():
    """_overrides_from_settings and the tables it reads, WITHOUT importing
    app.py (importing boots Streamlit)."""
    tree = ast.parse(APP.read_text(encoding='utf-8'))
    wanted = {'_OTDR_KEY_TO_ENGINE_GLOBAL', '_OTDR_KEY_TO_WARN_GLOBAL',
              '_OTDR_KEY_TO_VIEWER_WARN', '_OTDR_DISABLE_SENTINEL',
              '_OTDR_KEY_DISABLE_VALUE', '_overrides_from_settings'}
    body = [n for n in tree.body
            if (isinstance(n, ast.Assign)
                and any(getattr(t, 'id', '') in wanted for t in n.targets))
            or (isinstance(n, ast.FunctionDef) and n.name in wanted)]
    ns = {}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(APP), 'exec'), ns)
    return ns


NS = _app_namespace()


def _settings(**rows):
    """Every mapped row ticked at a Warning equal to its Fail, then `rows`."""
    base = {k: {'apply': True, 'fail': 0.5, 'warning': 0.5}
            for k in NS['_OTDR_KEY_TO_ENGINE_GLOBAL']}
    base.update(rows)
    return base


# ── 1. The hub ─────────────────────────────────────────────────────────────
def test_every_loss_row_has_a_viewer_warning():
    assert set(NS['_OTDR_KEY_TO_VIEWER_WARN'].values()) == WARN_NAMES
    # a Viewer Warning is never an engine global the report would read
    assert not WARN_NAMES & set(NS['_OTDR_KEY_TO_ENGINE_GLOBAL'].values())
    assert not WARN_NAMES & set(NS['_OTDR_KEY_TO_WARN_GLOBAL'].values())


def test_warning_equal_to_fail_sends_nothing():
    """Every profile's default: the run is exactly what it was."""
    out = NS['_overrides_from_settings'](_settings())
    assert not WARN_NAMES & set(out)


def test_a_warning_below_fail_is_sent():
    out = NS['_overrides_from_settings'](_settings(
        bidir_splice_loss={'apply': True, 'fail': 0.160, 'warning': 0.120},
        unidir_connector_loss={'apply': True, 'fail': 0.649, 'warning': 0.4}))
    assert out['REBURN_WARN_DB'] == 0.120
    assert out['LAUNCH_CONN_UNI_WARN_DB'] == 0.4
    assert out['REBURN_THRESHOLD'] == 0.160       # Fail itself unchanged


@pytest.mark.parametrize('row', [
    {'apply': False, 'fail': 0.160, 'warning': 0.120},   # row off
    {'apply': True, 'fail': 0.160, 'warning': 0.200},    # Warning over Fail
    {'apply': True, 'fail': 0.160, 'warning': 0.0},      # no band
    {'apply': True, 'fail': 0.160, 'warning': None},     # blank field
])
def test_no_band_sends_no_warning(row):
    out = NS['_overrides_from_settings'](_settings(bidir_splice_loss=row))
    assert 'REBURN_WARN_DB' not in out


def test_the_panel_unlocks_warning_on_the_loss_rows():
    src = APP.read_text(encoding='utf-8')
    assert "or key in _OTDR_KEY_TO_VIEWER_WARN)" in src
    comp = (REPO_ROOT / 'components' / 'otdr_settings' / 'index.html').read_text(
        encoding='utf-8')
    # an untouched Warning moves with Fail, or raising Fail opens a band
    assert 'row.warnFollowsFail' in comp
    assert 'if (follow) state[row.key].warning = v;' in comp


# ── 2. The runner ──────────────────────────────────────────────────────────
def test_manifest_carries_the_warning_and_the_report_is_unchanged(tmp_path):
    ov = {'REBURN_THRESHOLD': 0.160}
    rc0, man0, err0 = run_splicereport(
        FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR, tmp_path / 'a.xlsx',
        overrides=ov)
    rc1, man1, err1 = run_splicereport(
        FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR, tmp_path / 'b.xlsx',
        overrides={**ov, 'REBURN_WARN_DB': 0.100, 'SINGLE_DIR_WARN_DB': 0.15,
                   'BIDIR_CONNECTOR_WARN_DB': float('nan')})
    assert rc0 == 0 and man0 and man0.get('ok'), err0[-2000:]
    assert rc1 == 0 and man1 and man1.get('ok'), err1[-2000:]
    assert man1['thresholds']['REBURN_WARN_DB'] == 0.100
    assert man1['thresholds']['SINGLE_DIR_WARN_DB'] == 0.15
    assert 'BIDIR_CONNECTOR_WARN_DB' not in man1['thresholds']   # NaN dropped
    assert not WARN_NAMES & set(man0['thresholds'])
    # the report never sees a Warning: same cells, same gates
    assert man1['thresholds']['REBURN_THRESHOLD'] == 0.160
    strip = lambda m: {k: v for k, v in m.items()
                       if k not in ('thresholds', 'out', 'xlsx', 'elapsed_s', 'timings')}
    assert strip(man0) == strip(man1)
    assert _cells(tmp_path / 'a.xlsx') == _cells(tmp_path / 'b.xlsx')


def _cells(path):
    import openpyxl
    wb = openpyxl.load_workbook(path)
    return {ws.title: [[(c.value, c.fill.fgColor.rgb) for c in row]
                       for row in ws.iter_rows()]
            for ws in wb.worksheets}


def test_the_uni_manifest_never_carries_a_warning():
    src = (REPO_ROOT / 'splicereport' / 'run_splicereport.py').read_text(
        encoding='utf-8')
    uni = src[src.index("emit({'ok': True, 'out': args.out, 'uni': summary"):]
    uni = uni[:uni.index('return')]
    assert '_viewer_warn' not in uni
    assert "'thresholds': {**_effective_gates(), **_viewer_warn," in src


# ── 3. The trace server ────────────────────────────────────────────────────
@pytest.fixture
def _clean_gate():
    TS.set_thresholds(None)
    yield
    TS.set_thresholds(None)


def test_server_passes_the_warning_and_drops_it_with_the_report(_clean_gate):
    assert TS.engine_thresholds()['reburn_warn'] == 0
    TS.set_thresholds({'REBURN_THRESHOLD': 0.16, 'REBURN_WARN_DB': 0.12,
                       'LAUNCH_CONN_UNI_WARN_DB': -1})
    t = TS.engine_thresholds()
    assert t['reburn_warn'] == 0.12
    assert t['connector_uni_warn'] == 0       # a bad value keeps no band
    TS.set_thresholds(None)
    assert TS.engine_thresholds()['reburn_warn'] == 0


# ── 4. The Viewer ──────────────────────────────────────────────────────────
def _fn(name):
    i = VIEWER.index('function ' + name + '(')
    return VIEWER[i:VIEWER.index('\n}\n', i) + 3]


def test_warn_for_follows_the_gate_it_sits_under():
    body = _fn('warnFor')
    # uni, and a one-direction load graded as uni: none
    assert "if (gSourceReport === 'uni' || oneDirOnly()) return null;" in body
    assert 'gGateOverride != null) return null;' in body         # loss box
    assert 'return (w > 0 && w < g && g < GATE_OFF) ? w : null;' in body
    for key in ('reburn_warn', 'single_dir_warn',
                'connector_warn', 'connector_uni_warn'):
        assert 'T.' + key in body


def test_both_tables_colour_the_band_yellow_and_never_as_a_failure():
    assert re.search(r'td\.fr-warn\s*\{\s*background:\s*#ffeb00', VIEWER)
    # single-fibre table: fail first, then warning
    assert ("else if (overGate(v)) cls = ' class=\"fr-hi\"';   // the REPORT's verdict\n"
            "      else if (clearsAt(v, warnGate)) cls = ' class=\"fr-warn\"';"
            ) in VIEWER
    # the report's gate and its Warning
    assert "const overGate = clearsGate;" in VIEWER
    assert "const warnGate = warnFor(false, false);" in VIEWER
    # A+B table: every loss cell gets its own row's Warning
    assert "else if (!synthetic && clearsAt(v, warn)) cls.push('fr-warn');" in VIEWER
    assert 'gateFor(isRefl(x), false),\n' in VIEWER and 'warnFor(isRefl(x), false))' in VIEWER
    # a connector's direction has its gate and warning with the cell filters
    # off (under them only the Average judges a loss, boss 2026-09-29, except
    # an end connector's direction, at its fail gate with no warning); a
    # splice's direction has neither (Robert 2026-10-02, see legGateFor)
    assert 'gated ? legGateFor(isRefl(x)) : null,' in VIEWER
    assert 'judged ? legWarnFor(isRefl(x)) : null)' in VIEWER
    assert "lossCell(v, false, '', g, warnFor(refl, false))" in VIEWER
    # "failing cells only" keeps failures only; a yellow cell is not one
    assert "const keep = (gFailCellsOnly && cls.includes('fr-hi'))" in VIEWER
    assert "const keep = (gFailCellsOnly && (cls === ' class=\"fr-hi\"' || cls === ' class=\"fr-brk\"'))" in VIEWER
    assert 'fr-warn' not in _fn('reflFails')


# ── 5. "Show warning cells only" (Robert 2026-09-26) ───────────────────────
def test_warning_cells_only_is_a_box_and_a_menu_item():
    title = VIEWER.split('<div id="evt-title">', 1)[1].split("</div>", 1)[0]
    assert 'id="set-warncells"' in title and 'Show Warning Cells Only' in title
    assert "let gWarnCellsOnly = false;" in VIEWER
    item = VIEWER.split("function viewItems() {", 1)[1].split("\n}", 1)[0]
    assert "${gWarnCellsOnly ? '✓ ' : ''}Show Only Warning Cells" in item
    assert 'data-view="warncells"' in item
    tog = VIEWER.split("function toggleView(which) {", 1)[1].split("\n}", 1)[0]
    assert "else if (which === 'warncells') gWarnCellsOnly = !gWarnCellsOnly;" in tog
    assert "if (wb) wb.checked = gWarnCellsOnly;" in tog     # menu and box in step
    assert "if (e.target.checked !== gWarnCellsOnly) toggleView('warncells');" in VIEWER


def test_warning_cells_only_keeps_yellow_and_collapses_like_failing():
    # either filter blanks everything it does not keep, in every table that
    # prints a loss through a keep rule: FastReporter's A+B table, the OTDR
    # Suite A+B table (2026-09-28) and the single-direction grid
    assert "const cellText = (txt) => cellFilterOn() ? '' : txt;" in VIEWER
    assert VIEWER.count("if (cellFilterOn() && !keep) return `<td${attrs}></td>`;") == 2
    assert "if (cellFilterOn() && !lossKept(c, x, which)) return `<td${attrs}></td>`;" in VIEWER
    assert "|| (gWarnCellsOnly && cls === ' class=\"fr-warn\"');" in VIEWER
    assert "|| (gWarnCellsOnly && cls.includes('fr-warn'));" in VIEWER
    # all three tables collapse on either filter, around what it keeps, and
    # the FEC table too (2026-10-01; it has no warning band, see
    # test_viewer_fec_mode.py)
    assert VIEWER.count("const collapse = cellFilterOn();") == 4
    assert "|| (gWarnCellsOnly && warnsGate(v));" in VIEWER
    # in the two A+B tables only the Average row's loss is kept (2026-09-29)
    assert "? (gFailCellsOnly && cellFails(x, which)) || (gWarnCellsOnly && cellWarns(x, which))" in VIEWER
    assert "? (gFailCellsOnly && lossFails(c, x, which)) || (gWarnCellsOnly && lossWarns(c, x, which))" in VIEWER
    # a reflectance failure is not a warning, so warning-only blanks it
    assert "if (cellFilterOn() && !(gFailCellsOnly && bad)) return '<td></td>';" in VIEWER
    # the hint says which filter is on, in each of the three tables (every
    # caption is only the filter words since 2026-09-30)
    for painter in ("paintSuiteBidiGrid", "renderFastReporterGrid", "paintFrBidiGrid"):
        body = VIEWER.split("function " + painter + "(", 1)[1].split("\n}\n", 1)[0]
        assert "'warning cells only'" in body, painter


def test_a_failure_outranks_the_yellow_beside_it():
    """A pale fail tint next to bright yellow made a failing row look as if it
    failed on its warnings (Robert 2026-09-26, F1 B→A 0.222 among yellows)."""
    assert re.search(r'td\.fr-hi\s*\{\s*background:\s*#e74c3c;\s*color:\s*#ffffff', VIEWER)


def test_only_red_and_yellow_shade_the_event_panel():
    """Red fails, yellow warns, everything else prints plain: a passing gainer
    lost its green shading (Robert 2026-09-26).  A failing gainer is judged by
    its size and is red like any failure."""
    assert 'fr-gain' not in VIEWER
    assert re.search(r'td\.fr-brk\s*\{\s*background:\s*#e74c3c;', VIEWER)
    assert 'return Math.round(Math.abs(loss) * 1000) / 1000 >= gate - 1e-9;' in VIEWER


def test_the_span_menu_resets_to_the_full_span():
    """The ⋯ menu's clear item reads "Reset to Full Span" (Robert 2026-09-26)."""
    assert "['clear', 'Reset to Full Span']," in VIEWER
    assert 'Clear ${arrow} span' not in VIEWER


def test_the_event_panel_has_no_events_heading():
    """The "Events" title at the panel's top left is gone (Robert 2026-09-26);
    the view boxes sit there on their own."""
    title = VIEWER.split('<div id="evt-title">', 1)[1].split("</div>", 1)[0]
    assert '<h2>' not in title and 'id="set-failcells"' in title

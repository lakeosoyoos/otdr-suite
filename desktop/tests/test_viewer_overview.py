"""Viewer: whole-cable overview (1152 fibers, one direction).

The trace cap was 48 because a cable-scale load is two separate walls:
1152 x 39,173 points is 340 MB of JSON, and one fetch per trace is thousands
of serial round trips against a single-threaded server.  Both are solved
server-side — bulk endpoint + spike-preserving decimation — so the client can
raise the cap.

The decimation is the load-bearing part and is what these tests pin.  Plain
striding would silently delete the very features the overview exists to show:
measured on WSC_SUIsh F19, whose real glint is 0.943 dB deep and ~4 samples
wide, striding to ~1000 points kept 0.111 dB of it while bucketed min/max
kept 0.957.
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, 'viewer'))

import trace_server as TS  # noqa: E402

from conftest import FIXTURE_A_DIR  # noqa: E402

VIEWER_HTML = os.path.join(ROOT, 'viewer', 'viewer.html')


def _spiky(n=12000, spike_at=None, depth=8.0):
    y = np.full(n, 60.0)
    if spike_at is None:
        spike_at = n // 2
    y[min(spike_at, n - 1)] = 60.0 - depth          # one-sample spike
    x = (np.arange(n) * 0.08) / 1000.0
    return list(x), list(y)


def test_decimation_preserves_a_one_sample_spike():
    x, y = _spiky()
    for target in (2000, 1000, 500):
        dx, dy = TS.decimate_minmax(x, y, target)
        assert min(dy) == 52.0, f"spike lost at target {target}: min {min(dy)}"


def test_decimation_keeps_x_monotonic():
    """The two extremes per bucket are emitted in the order they occur, so the
    polyline never doubles back on itself."""
    x, y = _spiky()
    dx, _ = TS.decimate_minmax(x, y, 1000)
    assert all(dx[i] <= dx[i + 1] for i in range(len(dx) - 1))


def test_decimation_respects_the_budget():
    x, y = _spiky(n=40000)
    for target in (2000, 1000):
        _, dy = TS.decimate_minmax(x, y, target)
        assert len(dy) <= target + 2, (target, len(dy))


def test_short_trace_is_returned_untouched():
    x, y = _spiky(n=300)
    dx, dy = TS.decimate_minmax(x, y, 2000)
    assert dx is x and dy is y


def test_no_decimation_when_maxpts_absent():
    x, y = _spiky()
    for bad in (None, 0, -5):
        dx, dy = TS.decimate_minmax(x, y, bad)
        assert len(dy) == len(y)


def test_bulk_route_and_ceiling_exist():
    src = open(os.path.join(ROOT, 'viewer', 'trace_server.py'), encoding='utf-8').read()
    assert "'/api/traces'" in src
    # a cable is 1152 fibers; a larger query must not be able to pin the server
    assert '[:1152]' in src
    assert 'max(200, min(max_pts, 20000))' in src


# ─── the ENTRY POINT, not just the maths ─────────────────────────────────
#
# Every decimation test above calls decimate_minmax() directly, and the cache
# test below used to grep load_trace's SOURCE for 't = dict(t)'.  Both kept
# passing when PR #65 inserted an early `return {**t, ...}` ABOVE the
# `if max_pts:` block: the text was still there, just unreachable, so
# /api/traces honoured maxpts in the URL and ignored it in the code and shipped
# 1152 fibers at FULL resolution -- the exact wall this feature exists to
# avoid.  These drive load_trace itself.

def _fixture_fiber():
    """(dir, fiber_num) for a real 39,173-point A-direction trace."""
    d = str(FIXTURE_A_DIR)
    TS.set_dirs(d, None)
    return d, TS.list_fibers(d)[0][0]


def test_load_trace_decimates_when_maxpts_is_given():
    """THE REGRESSION: the overview path must actually decimate."""
    d, f = _fixture_fiber()
    t = TS.load_trace('a', f, max_pts=2000)
    assert t['num_points'] <= 2100, t['num_points']
    assert t['decimated_from'] > 30000, t.get('decimated_from')


def test_load_trace_is_full_resolution_without_maxpts():
    """The detail path must be untouched — this is what zooming one fiber uses."""
    d, f = _fixture_fiber()
    t = TS.load_trace('a', f)
    assert t['num_points'] > 30000
    assert 'decimated_from' not in t


def test_the_frame_is_identical_decimated_or_not():
    """launch_km / far_conn_km are what stacked mode aligns A and B on, so the
    overview must not shift them.  _trace_frame falls back to dist_km[-1] when
    a trace carries no end event, so it is derived BEFORE decimation."""
    d, f = _fixture_fiber()
    full = TS.load_trace('a', f)
    dec = TS.load_trace('a', f, max_pts=2000)
    assert (full['launch_km'], full['far_conn_km']) == (dec['launch_km'], dec['far_conn_km'])


def test_cache_keeps_full_resolution():
    """Decimation must apply to a COPY — zooming into one fiber after an
    overview load has to still get every sample.  Driven, not grepped: the
    grep version survived the feature being unreachable for 100 engines."""
    d, f = _fixture_fiber()
    TS.load_trace('a', f, max_pts=2000)          # overview first
    after = TS.load_trace('a', f)                # then zoom in
    assert after['num_points'] > 30000, 'overview poisoned the cache'


def test_the_bulk_payload_is_json_serialisable():
    """decimate_minmax returns numpy internally; anything leaking out of
    load_trace would make json.dumps raise and kill the whole overview."""
    import json
    d, f = _fixture_fiber()
    t = TS.load_trace('a', f, max_pts=2000)
    assert isinstance(t['dist_km'], list) and isinstance(t['trace_db'], list)
    json.dumps(TS._finite(t))                    # must not raise


def test_client_raises_the_cap_for_either_direction_count():
    """This used to assert the opposite -- that overview was a SINGLE-direction
    regime, "because A+B at cable scale is 2304 traces and defeats the point".

    The real reason was the event grid: its column clustering was cubic, so
    2,304 traces would not render.  That is fixed, and the field asked for the
    cable in both directions, so the restriction went.  Measured afterwards on
    864 fibers both ways -- 1,728 traces, 22,767 events -- clustering takes
    348 ms and the fetch 36 s, which is transfer and is the tech's to spend.

    The cap is now per DIRECTION.  Capping on traces instead would quietly hand
    back half a cable the moment someone picked A+B.
    """
    html = open(VIEWER_HTML, encoding='utf-8').read()
    assert 'MAX_OVERVIEW_FIBERS = 1728' in html
    assert 'MAX_DETAIL_TRACES = 48' in html
    assert 'const overview = tasks.length > MAX_DETAIL_TRACES' in html
    assert 'MAX_OVERVIEW_FIBERS * dirs.length' in html


def test_a_files_panel_selection_gets_the_same_two_regimes():
    """2026-09-18, the field: "we also seem to only be able to select up to 48
    in the Files section".  applyFileSelection carried its own flat MAX = 48
    and cut the task list to it, so marking a whole cable in the FILES list
    loaded 48 of it while typing the same fibers into the Fibers box loaded
    the lot.  It now takes the same detail/overview decision addFibers does."""
    html = open(VIEWER_HTML, encoding='utf-8').read()
    fn = html.split('async function applyFileSelection(want) {', 1)[1].split('\n}', 1)[0]
    assert 'const MAX = 48;' not in fn
    assert 'const overview = tasks.length > MAX_DETAIL_TRACES;' in fn
    assert 'const MAX = overview ? MAX_OVERVIEW_FIBERS * 2 : MAX_DETAIL_TRACES;' in fn
    assert 'await loadOverview(tasks);' in fn
    assert 'loadOne(x.key, x.f, x.d)' in fn          # detail path kept
    # and the readout no longer sends the tech to the Fibers box for a cable
    assert 'use the Fibers box for a whole cable' not in html


def test_files_panel_scrolls_beside_the_chart():
    """1152 chips across the top pushed the canvas off-screen.  Files now sit
    in a side panel that scrolls on its own, so a whole cable cannot."""
    html = open(VIEWER_HTML, encoding='utf-8').read()
    assert 'id="files-panel"' in html
    css = html[html.index('#files-list {'):][:120]
    assert 'overflow-y: auto' in css
    assert 'chip-strip' not in html


def test_event_panel_is_virtualised():
    """A real <tr> per event built a 172,140 px table at cable scale."""
    html = open(VIEWER_HTML, encoding='utf-8').read()
    assert 'renderVirtualEventList' in html
    assert 'EVENT_ROW_H' in html
    assert 'evt-scroll' in html


def test_viewer_flag_defaults_track_the_engine():
    """The panel highlights what a uni report would flag, so its thresholds
    must be the engine's — a drift here would mislead the tech.

    Read the engine's constants from SOURCE, not by importing it.  viewer/ is
    already on sys.path in this module and it carries its own deliberately
    divergent sor_reader324802a; importing the splicereport engine here
    resolves that name to the WRONG copy and dies at its import line.  The
    three copies are isolated on purpose."""
    import re
    eng = open(os.path.join(ROOT, 'splicereport', 'splicereportmatchexfo.py'),
               encoding='utf-8').read()

    def const(name):
        m = re.search(rf'^{name}\s*=\s*(-?[\d.]+)', eng, re.M)
        assert m, f'{name} not found in the engine'
        return float(m.group(1))

    html = open(VIEWER_HTML, encoding='utf-8').read()
    line = next(l for l in html.splitlines() if 'const gViewerSettings' in l)
    assert f'lossDb: {const("UNI_BEND_THRESHOLD"):.3f}' in line, line
    assert f'reflLo: {const("UNI_REFL_FLOOR_DB"):.1f}' in line, line
    assert f'reflHi: {const("UNI_REFL_CEIL_DB"):.1f}' in line, line


def test_flagging_scope_is_documented_not_reimplemented():
    """Closure discovery and at-splice classification stay in the engine; a
    second copy of those rules in JavaScript would drift from it."""
    html = open(VIEWER_HTML, encoding='utf-8').read()
    assert 'per-event rules' in html
    assert 'would drift from it' in html


def test_files_panel_selects_like_a_file_list():
    """Click selects one file, Shift+click a range, Ctrl/Cmd+click toggles one."""
    html = open(VIEWER_HTML, encoding='utf-8').read()
    fn = html[html.index("getElementById('files-list').addEventListener('click'"):][:1800]
    assert 'ev.shiftKey' in fn and 'ev.ctrlKey || ev.metaKey' in fn
    assert 'applyFileSelection(' in fn


def test_api_list_names_the_files():
    src = open(os.path.join(os.path.dirname(VIEWER_HTML), 'trace_server.py'), encoding='utf-8').read()
    assert "'files_a':" in src and "'files_b':" in src


def test_traces_use_the_standard_fiber_colour_code():
    """TIA-598 order, by position in the 12-fiber ribbon, keyed on the fiber
    number rather than load order."""
    import re
    html = open(VIEWER_HTML, encoding='utf-8').read()
    block = html[html.index('const PALETTE = ['):][:400]
    hexes = re.findall(r"'(#[0-9a-f]{6})'", block.split('];', 1)[0])
    assert hexes == ['#0072ce', '#ff7f00', '#00a651', '#8b4513', '#708090', '#ffffff',
                     '#e31b23', '#000000', '#ffd700', '#8a2be2', '#ff66cc', '#00ced1']
    fn = html[html.index('function nextColor('):][:700]
    assert '(fiber - 1) % PALETTE.length' in fn
    assert "LIGHT_EDGE[t.color]" in html, 'white/yellow need an edge on the white chart'


def test_fiber_colours_toggle_off_to_fastreporter_blue_and_black():
    """The toolbar "fiber colors" box turns the 12-colour code off; with it off
    every A trace draws in FastReporter's blue and every B trace in black
    (sampled from an FR3 bidirectional overlay), and the choice is remembered."""
    html = open(VIEWER_HTML, encoding='utf-8').read()
    assert "const FR_COLORS = { a: '#0000f7', b: '#000000' }" in html
    assert 'id="cb-colors" checked' in html
    fn = html[html.index('function nextColor('):][:700]
    assert '!gFiberColors' in fn and "startsWith('b-') ? FR_COLORS.b : FR_COLORS.a" in fn
    tog = html[html.index('function setFiberColors('):][:600]
    assert 'COLORS_USED.clear()' in tog and 'for (const t of gTraces) t.color = nextColor(' in tog
    assert "localStorage.setItem('otdr_viewer_fiber_colors'" in tog


def test_event_table_section_columns_can_be_hidden():
    """The events-panel "sections" box drops the Section column groups AND the
    Section statistics from the FR-layout table; the choice is remembered."""
    html = open(VIEWER_HTML, encoding='utf-8').read()
    assert '<input id="set-sections-off" type="checkbox"> Sections off' in html
    assert "gShowSections = !e.target.checked;" in html
    assert "localStorage.setItem('otdr_viewer_sections'" in html
    grid = html[html.index('function renderFastReporterGrid('):html.index('function renderFrBidiGrid(')]
    assert grid.count('showSec && i < cols.length - 1') == 3, \
        'header, per-trace row and aggregate row all gate their section cells'
    # FastReporter mode's own grid honours the same box (header + rows)
    fr = html[html.index('function paintFrBidiGrid('):]
    assert fr.count('showSec && i < cols.length - 1') == 3   # header, rows, Min/Max/Average strip
    assert 'const showSec = gShowSections && !collapse;' in fr   # collapsed view drops them
    assert "const STAT_SEC = showSec ? ['Section Loss (dB)', 'Section Att. (dB/km)'] : []" in html
    assert 'const NCELL = LEAD.length + nKept * 2 + (showSec ? NSEC * (cols.length - 1) : 0) + NSTAT' in html


def test_viewer_opens_with_no_fiber_loaded():
    """No auto-loaded F64: the chart stays blank until a fiber is picked from
    the Files panel (or typed)."""
    html = open(VIEWER_HTML, encoding='utf-8').read()
    fn = html[html.index('async function autoloadDefault('):][:200]
    assert 'addFibers' not in fn and 'includes(64)' not in fn


def test_files_panel_right_click_sets_direction_like_fr():
    """FastReporter: right-click a file > Direction > A->B / B->A.  The trace is
    still fetched from its own folder (`src`) but drawn and paired as `dir`."""
    html = open(VIEWER_HTML, encoding='utf-8').read()
    assert 'function showFileDirMenu(' in html and 'function setFilesDirection(' in html
    cm = html[html.index("getElementById('files-list').addEventListener('contextmenu'"):][:400]
    assert 'showFileDirMenu(' in cm and 'ev.ctrlKey' in cm, 'Ctrl+click on a Mac must still select'
    assert 'src: dir, dir: effDir(dir, fiber)' in html
    assert 'src: dir, dir: effDir(dir, data.fiber)' in html
    # Settings edits must go to the file's real folder, not its shown direction.
    assert "showEditDialog(src || dir, fiber)" in html


def test_popout_viewer_links_back_to_its_report():
    """Report cells open the Viewer in its own window, which had no way back to
    the Uni or Splice Report page.  The toolbar now carries a Back button."""
    html = open(VIEWER_HTML, encoding='utf-8').read()
    assert 'id="btn-back"' in html and 'function renderBackButton(' in html
    fn = html[html.index("getElementById('btn-back').addEventListener('click'"):][:1400]
    assert "window.open('', 'otdr_hub')" in fn, 'must reuse the hub tab, not open a second hub'
    for nav in ("'uni'", "'sr'"):
        assert nav in fn
    assert 'srfr' not in fn, 'the FR (beta) page is retired'
    src = open(os.path.join(os.path.dirname(VIEWER_HTML), 'trace_server.py'), encoding='utf-8').read()
    assert "'hub_url':" in src and "'hub_port': None" in src
    app = open(os.path.join(ROOT, 'app.py'), encoding='utf-8').read()
    assert app.count('window.top.name = "otdr_hub"') == 2, 'both pop-out buttons name the hub tab'
    nav = app[app.index('def _handle_nav():'):][:3000]
    assert "'uni': 'Unidirectional'" in nav and "'sr': 'Splice Report'" in nav
    assert 'srfr' not in nav
    assert "trace_server.CONFIG['hub_port']" in app

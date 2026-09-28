"""The Viewer's Report: the chart and the event panel as a PDF or an Excel
workbook (Robert, 2026-09-28: "create a report that shows the traces and the
event panel from viewer ... an option to do it in pdf or excel sheet").

The browser hands the server the chart as a PNG and the event table as plain
cells (text, colours resolved from the page's CSS, colspan / rowspan); the
server lays them out.  These tests pin the server half end to end and the
browser half's contract with the three event grids."""
import json
import os
import re
import struct
import sys
import threading
import zlib
from http.server import HTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'viewer'))
import trace_server as T  # noqa: E402

SRC = open(os.path.join(ROOT, 'viewer', 'viewer.html'), encoding='utf-8').read()


def _png(w=60, h=24):
    """A real, tiny PNG (solid red), built by hand so the test needs no Pillow."""
    raw = b''.join(b'\x00' + b'\xe7\x4c\x3c' * w for _ in range(h))

    def chunk(t, d):
        return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


def _data_url(png):
    import base64
    return 'data:image/png;base64,' + base64.b64encode(png).decode('ascii')


# Styles as the browser sends them: one entry per distinct look.
HDR, PLAIN, FAIL, LAB = 0, 1, 2, 3
STYLES = [
    {'bg': '#e4edf7', 'fg': '#1f2d3d', 'b': True, 'al': 'c'},
    {'bg': None, 'fg': '#1f2d3d', 'b': False, 'al': 'c'},
    {'bg': '#e74c3c', 'fg': '#ffffff', 'b': True, 'al': 'c'},
    {'bg': '#eef3f8', 'fg': '#2e86de', 'b': True, 'al': 'l'},
]


def _suite_like_table(n_events=3, fibres=(64, 65)):
    """The shape of the Viewer's bidirectional grids: four identifier columns
    spanning a three-row header, then a Loss / Refl. pair per event."""
    lead = [{'t': t, 's': LAB if i == 0 else HDR, 'rs': 3}
            for i, t in enumerate(['Identifiers', 'P/F', 'λ (nm)', 'Dir.'])]
    h1 = lead + [{'t': f'Splice {i + 1} (2/2)', 's': HDR, 'cs': 2} for i in range(n_events)]
    h2 = [{'t': f"{i + 1}.0000km, {3281 * (i + 1):,}'", 's': HDR, 'cs': 2} for i in range(n_events)]
    h3 = [c for _ in range(n_events)
          for c in ({'t': 'Loss\n(dB)', 's': HDR}, {'t': 'Refl.\n(dB)', 's': HDR})]
    body = []
    for f in fibres:
        for d in ('A→B', 'B→A', 'Average'):
            row = [{'t': f'F{f}', 's': LAB, 'dot': '#ffffff'}, {'t': '✓', 's': PLAIN},
                   {'t': '1550', 's': PLAIN}, {'t': d, 's': PLAIN}]
            for i in range(n_events):
                bad = (f == 64 and d == 'Average' and i == 0)
                row.append({'t': '0.213' if bad else '0.041', 's': FAIL if bad else PLAIN,
                            **({'tip': 'Splice Report: 64 .213 (reburn)'} if bad else {})})
                row.append({'t': '---', 's': PLAIN})
            body.append(row)
    foot = [[{'t': lab, 's': LAB}, {'t': '', 's': PLAIN}, {'t': '', 's': PLAIN}, {'t': '', 's': PLAIN}]
            + [c for _ in range(n_events) for c in ({'t': '0.041', 's': PLAIN}, {'t': '', 's': PLAIN})]
            for lab in ('Minimum', 'Maximum', 'Average')]
    return {'title': 'Event Table', 'note': 'F64: OTDR Suite table', 'lead': 4,
            'head': [h1, h2, h3], 'body': body, 'foot': foot}


def _payload(fmt, **kw):
    p = {'format': fmt, 'dest': '', 'name': 'Viewer Report F64',
         'title': 'OTDR Viewer Report: TESTJOB', 'subtitle': 'Generated today',
         'meta': [['Mode', 'OTDR Suite'], ['Fibres', '64-65 (2 fibres, 4 traces)']],
         'images': [{'caption': 'Traces', 'png': _data_url(_png()), 'note': 'Distance 0 to 1 km'}],
         'key': [{'label': 'F64 A→B', 'color': '#1f77b4'}],
         'styles': STYLES, 'tables': [_suite_like_table()]}
    p.update(kw)
    return p


@pytest.fixture
def downloads(tmp_path, monkeypatch):
    d = tmp_path / 'Downloads'
    d.mkdir()
    monkeypatch.setenv('OTDR_DOWNLOADS_DIR', str(d))
    monkeypatch.setitem(T.CONFIG, 'dir_a', None)
    monkeypatch.setitem(T.CONFIG, 'dir_b', None)
    return d


# ─── the server half ──────────────────────────────────────────────────────

def test_a_pdf_lands_in_downloads_and_never_over_an_earlier_one(downloads):
    one = T.write_viewer_report(_payload('pdf'))
    two = T.write_viewer_report(_payload('pdf'))
    assert one['path'] == str(downloads / 'Viewer Report F64.pdf')
    assert two['path'] == str(downloads / 'Viewer Report F64 (2).pdf')
    for p in (one['path'], two['path']):
        with open(p, 'rb') as f:
            assert f.read(5) == b'%PDF-'
    assert not [n for n in os.listdir(downloads) if n.endswith('.part')]


def test_a_wide_table_goes_over_several_pages_of_columns(downloads):
    """40 events is far wider than a landscape page: the PDF must still build
    (reportlab would otherwise run the table off the page edge)."""
    wide = _payload('pdf', tables=[_suite_like_table(n_events=40, fibres=range(1, 30))])
    out = T.write_viewer_report(wide)
    assert os.path.getsize(out['path']) > 5000


def test_the_workbook_keeps_numbers_colours_merges_and_a_frozen_header(downloads):
    from openpyxl import load_workbook
    out = T.write_viewer_report(_payload('xlsx'))
    assert out['name'] == 'Viewer Report F64.xlsx'
    wb = load_workbook(out['path'])
    assert wb.sheetnames == ['Report', 'Event Table']
    rep = wb['Report']
    assert rep['A1'].value == 'OTDR Viewer Report: TESTJOB'
    assert len(rep._images) == 1                        # the chart is IN the sheet
    ws = wb['Event Table']
    # title row 1, note row 2, the table from row 4: three header rows, then
    # the body; frozen below the header and right of the four identifiers
    assert ws.freeze_panes == 'E7'
    merged = {str(r) for r in ws.merged_cells.ranges}
    assert 'A4:A6' in merged                            # Identifiers spans the header
    assert 'E4:F4' in merged                            # an event's Loss + Refl. pair
    bad = ws['E9']                                      # F64 Average, first event
    assert bad.value == pytest.approx(0.213) and bad.number_format == '0.000'
    assert bad.fill.fgColor.rgb.endswith('E74C3C')
    assert bad.font.b and bad.font.color.rgb.endswith('FFFFFF')
    assert bad.comment is not None and '.213' in bad.comment.text
    assert ws['E7'].value == pytest.approx(0.041) and ws['E7'].fill.fill_type is None
    assert ws['F7'].value == '---'                      # text stays text
    assert ws['C7'].value == 1550 and ws['C7'].number_format == '0'
    assert ws['E6'].value == 'Loss\n(dB)' and ws['E6'].alignment.wrap_text


def test_numbers_are_numbers_and_everything_else_stays_text():
    assert T._xl_value('0.113') == (0.113, '0.000')
    assert T._xl_value('-53.9') == (-53.9, '0.0')
    assert T._xl_value('1550') == (1550.0, '0')
    assert T._xl_value("0.2600km, 853'") == ("0.2600km, 853'", None)
    assert T._xl_value('---') == ('---', None)
    assert T._xl_value('F64') == ('F64', None)


def test_the_grid_places_cells_under_rowspans():
    rows = [[{'t': 'a', 'rs': 2}, {'t': 'b', 'cs': 2}], [{'t': 'c'}, {'t': 'd'}]]
    anchors, ncols = T._report_grid(rows)
    assert ncols == 3
    assert [(r, c, rs, cs, x['t']) for r, c, rs, cs, x in anchors] == [
        (0, 0, 2, 1, 'a'), (0, 1, 1, 2, 'b'), (1, 1, 1, 1, 'c'), (1, 2, 1, 1, 'd')]


def test_column_blocks_never_cut_an_event_pair_and_repeat_the_lead():
    rows, _nh, _nb = T._report_rows(_suite_like_table(n_events=12))
    anchors, ncols = T._report_grid(rows)
    cut = T._report_cuts(anchors, ncols)
    widths = [40.0] * ncols
    blocks = T._report_col_blocks(widths, cut, 4, 400.0)
    assert blocks[0][0] == 4 and blocks[-1][1] == ncols
    for (s, e), (s2, _e2) in zip(blocks, blocks[1:]):
        assert e == s2                                  # contiguous, nothing lost
    for s, e in blocks:
        assert (s - 4) % 2 == 0 and (e - 4) % 2 == 0    # Loss + Refl. stay together
        assert 4 * 40 + (e - s) * 40 <= 400
    # one block wider than the page is kept whole rather than looping
    assert T._report_col_blocks([10.0] * 4 + [500.0, 500.0], [True] * 4 + [True, False, True], 4, 300.0) \
        == [(4, 6)]


def test_reports_stay_out_of_the_trace_folders(tmp_path, downloads, monkeypatch):
    a, b = tmp_path / 'A', tmp_path / 'B'
    a.mkdir()
    b.mkdir()
    monkeypatch.setitem(T.CONFIG, 'dir_a', str(a))
    monkeypatch.setitem(T.CONFIG, 'dir_b', str(b))
    for bad in (str(a), str(a / 'reports'), str(b)):
        with pytest.raises(ValueError, match='trace folder'):
            T.report_dest(bad)
    assert T.report_dest('') == str(downloads)
    assert T.report_dest('Span 7') == str(downloads / 'Span 7')
    assert T.report_dest(str(tmp_path / 'elsewhere')) == str(tmp_path / 'elsewhere')
    with pytest.raises(ValueError):
        T.report_dest('sub/dir')


def test_a_file_name_cannot_escape_the_folder(downloads):
    out = T.write_viewer_report(_payload('pdf', name='..\\..\\evil:name?.pdf'))
    assert os.path.dirname(out['path']) == str(downloads)
    assert not re.search(r'[\\/:?]', out['name']) and out['name'].endswith('evil_name_.pdf')
    assert not out['name'].startswith('.')             # no hidden file either


def test_open_only_opens_a_report_this_viewer_wrote(downloads, monkeypatch):
    calls = []
    monkeypatch.setattr(T.subprocess, 'Popen', lambda *a, **k: calls.append(a))
    if hasattr(T.os, 'startfile'):
        monkeypatch.setattr(T.os, 'startfile', lambda p: calls.append(p))
    with pytest.raises(ValueError, match='not a report'):
        T.open_report(str(downloads / 'something.pdf'))
    out = T.write_viewer_report(_payload('pdf'))
    T.open_report(out['path'])
    T.open_report(out['path'], reveal=True)
    assert len(calls) == 2


def _post(port, path, body, origin=None):
    req = Request(f'http://127.0.0.1:{port}{path}', data=json.dumps(body).encode('utf-8'),
                  headers={'Content-Type': 'application/json', **({'Origin': origin} if origin else {})})
    try:
        with urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode('utf-8'))
    except HTTPError as e:
        raw = e.read().decode('utf-8', 'replace')
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {'raw': raw}


def test_the_route_writes_the_report_and_refuses_a_foreign_page(downloads):
    srv = HTTPServer(('127.0.0.1', 0), T.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_port
        with urlopen(f'http://127.0.0.1:{port}/api/report_defaults', timeout=30) as r:
            assert json.loads(r.read().decode('utf-8')) == {'dest': str(downloads)}
        st, j = _post(port, '/api/report', _payload('xlsx'))
        assert st == 200 and j['ok'] and os.path.isfile(j['path'])
        st, j = _post(port, '/api/report', _payload('docx'))
        assert st == 400 and 'format' in j['error']
        st, _j = _post(port, '/api/report', _payload('pdf'), origin='http://evil.example')
        assert st == 403
        st, j = _post(port, '/api/report_open', {'path': '/etc/hosts'})
        assert st == 400 and 'not a report' in j['error']
    finally:
        srv.shutdown()
        srv.server_close()


# ─── the browser half ─────────────────────────────────────────────────────

def _fn(name):
    body = SRC.split(f'function {name}(', 1)[1]
    return body.split('\nfunction ', 1)[0]


def test_the_toolbar_has_a_report_button():
    group = SRC.split('<button id="btn-report"', 1)[0].rsplit('<div class="group">', 1)[1]
    assert '</div>' not in group                          # inside its own toolbar group
    assert "document.getElementById('btn-report').onclick = showReportDialog;" in SRC


def test_every_grid_hands_the_report_every_row_not_the_slice_on_screen():
    """The panel is virtual: the DOM only holds the rows on screen.  Each of
    the three grids registers its FULL row list, and a fresh render clears the
    last one so a table that is still loading is never reported stale."""
    assert "gTableExport = { table, headExtra: statNameRow, rows: () => shown.map(rowHtml) };" \
        in _fn('renderFastReporterGrid')
    assert "gTableExport = { table, rows: () => descs.map(rowHtml) };" in _fn('paintFrBidiGrid')
    assert "gTableExport = { table, rows: () => descs.map(rowHtml) };" in _fn('paintSuiteBidiGrid')
    assert 'gTableExport = null;' in _fn('renderEventTable')
    snap = _fn('reportEventTable')
    assert 'ex.rows()' in snap and "classList.contains('fr-spacer')" in snap


def test_cell_colours_come_from_the_page_css():
    probe = _fn('reportStyleProbe')
    assert "table.className = 'fr-table';" in probe       # the panel's own class
    assert 'getComputedStyle(td)' in probe


def test_the_chart_is_drawn_at_print_scale_and_put_back():
    fn = _fn('reportChartPng')
    assert 'gExportDpr = 2;' in fn and 'gMouse = null;' in fn
    assert fn.index('finally') < fn.index('resizeCanvas();')
    assert 'gView = keep.view;' in fn.split('finally', 1)[1]
    # plotRect and draw measure the canvas at the export scale while it runs
    assert 'canvas.width  / canvasDpr()' in _fn('plotRect')
    assert 'canvas.width / canvasDpr()' in _fn('draw')
    # declared with the other globals, long before the first draw can run
    assert SRC.index('let gExportDpr = 0;') < SRC.index("const canvas = document.getElementById('chart');")


def test_a_failing_column_average_is_red_in_the_pinned_strip():
    """The pinned Min / Max / Average strip's grey background outranked the red
    fill, so a failing column Average printed white on grey: invisible, on
    screen and in the report that copies the screen."""
    css = SRC.split('<style>', 1)[1].split('</style>', 1)[0]
    rule = re.search(r'([^{}]*)\{\s*background:\s*#e74c3c;\s*\}', css.split('tr.fr-pick td.fr-rowlab', 1)[1])
    assert rule and 'tfoot tr.fr-agg td.fr-hi' in rule.group(1) and 'tr.fr-pick td.fr-hi' in rule.group(1)
    assert re.search(r'tfoot tr\.fr-agg td\.fr-warn,\s*table\.fr-table tr\.fr-pick td\.fr-warn \{ background: #ffeb00; \}', css)

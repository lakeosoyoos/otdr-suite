"""The hub's Unidirectional grid prints the workbook's Cable End cells
(Robert 2026-10-01).

The grid used to print every fiber's end reflectance in the Cable End
column, one line per fiber: 12 lines a ribbon, so on a 432-fiber span each
row stood about 150 px tall and two ribbons fit on the screen.  The workbook
prints one short cell a ribbon (uni_format_end_cell): the ribbon's strongest
end reflectance, or 'end' when none is stored, and when some fibers broke
before the end, the fibers that reach it with that tag.  The grid now prints
the same text, and the cell opens the Viewer on the fiber with the strongest
end reflectance, at the end.

The hub cannot import the engine module (it would load an engine's reader
into the hub), so app.py keeps its own copy of the rule: these tests hold the
two to the same text.
"""
from __future__ import annotations

import ast
import html as _html
import os
import re
import shutil
import sys
import types
from html.parser import HTMLParser

import pytest

from conftest import run_streamlit, go_tab, FIXTURE_SPLICE_A_DIR

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, 'splicereport'))

import splicereportmatchexfo as E  # noqa: E402


def _hub_rule():
    src = open(os.path.join(ROOT, 'app.py'), encoding='utf-8').read()
    fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef)
              and n.name == '_uni_end_cell')
    mod = types.ModuleType('uni_end_cell')
    exec(compile(ast.Module(body=[fn], type_ignores=[]), 'app.py', 'exec'),
         mod.__dict__)
    return mod._uni_end_cell


CASES = [
    # a full ribbon, every end stored
    ([(f, -45.0 - f) for f in range(1, 13)], range(1, 13)),
    # a full ribbon, no end reflectance stored
    ([(f, None) for f in range(1, 13)], range(1, 13)),
    # a full ribbon, some ends stored
    ([(f, (-50.25 if f == 7 else None)) for f in range(13, 25)], range(13, 25)),
    # a partial ribbon: fibers 3 and 9 broke before the end
    ([(f, -48.5) for f in range(1, 13) if f not in (3, 9)], range(1, 13)),
    # a partial ribbon with no reflectance
    ([(25, None), (31, None)], range(25, 37)),
    # the last, short ribbon of a 430-fiber span, full
    ([(f, -47.123) for f in range(421, 431)], range(421, 431)),
    ([], range(1, 13)),
]


@pytest.mark.parametrize('entries,ribbon', CASES)
def test_the_hub_copy_of_the_rule_is_the_workbooks(entries, ribbon):
    assert _hub_rule()(entries, ribbon) == E.uni_format_end_cell(entries, ribbon)


# ── the grid on the page ─────────────────────────────────────────────────

class _Grid(HTMLParser):
    """The grid's header labels and each row's cell texts."""

    def __init__(self):
        super().__init__()
        self.head, self.rows, self.links = [], [], []
        self._in = None
        self._buf = []
        self._bold = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'tr':
            self.rows.append([])
        elif tag in ('th', 'td'):
            self._in, self._buf = tag, []
        elif tag == 'br':
            self._buf.append('\n')
        elif tag == 'div' and self._in == 'th' and 'font-weight:600' in (a.get('style') or ''):
            self._bold = True
        elif tag in ('a', 'span') and self._in == 'td':
            self.links.append((len(self.rows) - 1, len(self.rows[-1]), a))

    def handle_endtag(self, tag):
        if tag == 'div':
            self._bold = False
        if tag in ('th', 'td') and self._in == tag:
            text = ''.join(self._buf).strip()
            if tag == 'th':
                self.head.append(text)
            else:
                self.rows[-1].append(text)
            self._in = None

    def handle_data(self, data):
        if self._in == 'th' and not self._bold and self.head is not None:
            # only the label line of a header, not its km / landmark lines
            return
        if self._in:
            self._buf.append(data)


def _grid_html(at):
    grid = ''.join(m.value for m in at.markdown if 'Ribbon</th>' in m.value)
    if not grid:          # the grid may be drawn in its own frame instead
        grid = ''.join(str(getattr(e.proto, 'srcdoc', '') or '')
                       for e in at.get('iframe'))
        m = re.search(r'<div style="overflow:auto.*?</table></div>', grid, re.S)
        grid = m.group(0) if m else grid
    assert 'Ribbon</th>' in grid, 'no grid drawn'
    p = _Grid()
    p.feed(grid)
    return p


def _manifest(folder, ends, max_fiber=24):
    """A finished Uni report on `folder` with one splice and a Cable End
    column.  `ends` is {fiber: end reflectance or None}."""
    cols = [{'km': 3.2, 'kind': 'splice', 'label': 'Event 1', 'landmark': ''},
            {'km': 10.0, 'kind': 'end', 'label': 'Cable End', 'landmark': ''}]
    cells = [{'fiber': 5, 'col': 0, 'km': 3.2, 'kind': 'splice', 'loss': 0.121}]
    cells += [{'fiber': f, 'col': 1, 'km': 10.0, 'kind': 'end', 'loss': r}
              for f, r in sorted(ends.items())]
    return {'ok': True, '_folder': folder, 'out': '',
            'uni': {'n_fibers': max_fiber, 'span_km': 10.0, 'direction': 'X->Y',
                    'direction_label': 'X → Y', 'ribbon_size': 12,
                    'max_fiber': max_fiber, 'ribbons': list(range((max_fiber + 11) // 12)),
                    'launch_offset_km': 0.0, 'grid_columns': cols, 'cells': cells,
                    'splice_columns': [3.2], 'end_column_km': 10.0}}


@pytest.fixture
def page(tmp_path, monkeypatch):
    monkeypatch.setenv('OTDR_CACHE_DIR', str(tmp_path / 'cache'))
    folder = str(tmp_path / 'shots')
    shutil.copytree(FIXTURE_SPLICE_A_DIR, folder)

    def show(ends, max_fiber=24, popout=False):
        at = run_streamlit(default_timeout=120).run()
        go_tab(at, 'Unidirectional')
        at.session_state['uni_folder_input'] = folder
        at.session_state['uni_click_target'] = (
            'Separate window' if popout else 'This tab (Viewer page)')
        at.session_state['uni_result'] = _manifest(folder, ends, max_fiber)
        at.run()
        assert not at.exception, at.exception
        return at
    return show


# Ribbon 1 whole (strongest -45.5 on fiber 7); ribbon 2 lost fibers 15 and 20
# before the end (strongest -47.0 on fiber 18).
ENDS = {**{f: (-45.5 if f == 7 else -49.0) for f in range(1, 13)},
        **{f: (-47.0 if f == 18 else None) for f in range(13, 25) if f not in (15, 20)}}


def _end_cells(grid):
    ci = grid.head.index('Cable End')
    return [row[ci] for row in grid.rows[1:]]


@pytest.mark.parametrize('popout', [False, True])
def test_each_cable_end_cell_is_the_workbooks_cell(page, popout):
    at = page(ENDS, popout=popout)
    got = _end_cells(_grid_html(at))
    want = [E.uni_format_end_cell([(f, r) for f, r in ENDS.items()
                                   if (f - 1) // 12 == ri],
                                  range(ri * 12 + 1, ri * 12 + 13))
            for ri in (0, 1)]
    assert want == ['REFL-45.5dB',
                    'F13,F14,F16,F17,F18,F19,F21,F22,F23,F24 REFL-47.0dB']
    assert got == want
    assert all('\n' not in c for c in got)        # one line a ribbon


def test_a_cable_end_cell_opens_the_strongest_fiber_at_the_end(page):
    at = page(ENDS)
    grid = _grid_html(at)
    ci = grid.head.index('Cable End')
    links = {r: a for r, c, a in grid.links if c == ci}
    assert len(links) == 2                        # one link a cell
    h1, h2 = (_html.unescape(links[r]['href']) for r in sorted(links))
    assert 'fiber=7&km=10.0&dir=a' in h1 and 'src=uni' in h1
    assert 'fiber=18&km=10.0&dir=a' in h2


def test_with_no_reflectance_stored_the_cell_opens_the_ribbons_first_fiber(page):
    at = page({f: None for f in range(1, 13)}, max_fiber=12, popout=True)
    grid = _grid_html(at)
    ci = grid.head.index('Cable End')
    assert _end_cells(grid) == ['end']
    (a,) = [a for _r, c, a in grid.links if c == ci]
    assert a['data-fiber'] == '1' and a['data-km'] == '10.0'


def test_the_cable_end_cell_has_no_fill(page):
    at = page(ENDS)
    grid = ''.join(m.value for m in at.markdown if 'Ribbon</th>' in m.value)
    tds = re.findall(r"<td style='([^']*)'>(?:<a [^>]*>)REFL-45\.5dB", grid)
    assert tds and 'background' not in tds[0]

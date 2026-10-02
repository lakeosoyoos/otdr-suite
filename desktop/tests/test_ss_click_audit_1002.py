"""Secret Sauce click audit, 2026-10-02 (24 fibers of a long span, A and B
loaded in the left panel).

1. The report followed the session, not the folders: emptying the B box
   left the 48-file A+B report under "Traces: the A folder", and adding B
   back to an A-only report left the 24-file list under "the A and B
   folders".  A pair clicked from that list came Back to a different report.
2. The Output choice had no key: PDF or Stay in App went back to Excel after
   a trip to another tool, or a pair click and "← Back", and the next run
   made an Excel workbook.
3. With A and B loaded no pair could be clicked: every fiber number is in
   both directions, so in the one flat folder the run reads none is unique,
   and the rows did not say which direction a file was.  A pair now opens
   the Viewer on the panel's own two folders, in its own direction (both
   for one A file and one B file), and its row names the direction.
"""
from __future__ import annotations

import html
import os
import re
import shutil
from urllib.parse import parse_qs

from conftest import (run_streamlit, finish_engine_run, FIXTURE_A_DIR,
                      FIXTURE_B_DIR)


def _span(tmp_path, same_names=False):
    """An A and a B folder, four traces each, every fiber number in both.
    With `same_names` both folders use F0001_1550.sor ... (the B copies go
    into the run as B_<name>)."""
    a, b = tmp_path / 'A', tmp_path / 'B'
    a.mkdir()
    b.mkdir()
    for src, dst, own in ((FIXTURE_A_DIR, a, 'AAABBB'), (FIXTURE_B_DIR, b, 'BBBAAA')):
        for i, f in enumerate(sorted(os.listdir(src))[:4], 1):
            name = f'F{i:04d}_1550.sor' if same_names else f'{own}{i:04d}_1550.sor'
            shutil.copy2(src / f, dst / name)
    return str(a), str(b)


def _box(at, label):
    return next(t for t in at.sidebar.text_input if t.label == label)


def _hub(tmp_path, monkeypatch, a, b=''):
    monkeypatch.setenv('OTDR_CACHE_DIR', str(tmp_path / 'cache'))
    monkeypatch.setenv('OTDR_DOWNLOADS_DIR', str(tmp_path / 'Downloads'))
    at = run_streamlit(default_timeout=180).run()
    _box(at, 'A Folder').input(a).run()
    if b:
        _box(at, 'B Folder').input(b).run()
    at.sidebar.radio[0].set_value('Secret Sauce').run()
    assert not at.exception, at.exception
    return at


def _output(at):
    return next(r for r in at.main.radio if r.label == 'Output')


def _run(at, output='Stay in App'):
    _output(at).set_value(output).run()
    next(b for b in at.main.button if b.label == 'Run Analysis').click().run()
    finish_engine_run(at, 'ss')
    assert not at.exception, at.exception
    return at


def _summary(at):
    """The report's top line, or None when no report is on screen."""
    for s in at.main.success:
        if ' files · ' in s.value or s.value.startswith('Done:'):
            return s.value
    return None


def _caption(at):
    return ' '.join(c.value for c in at.main.caption)


# ── 1. the report follows the folders ────────────────────────────────────

def test_emptying_b_takes_the_a_and_b_report_off_the_page(tmp_path, monkeypatch):
    a, b = _span(tmp_path)
    at = _run(_hub(tmp_path, monkeypatch, a, b))
    assert _summary(at).startswith('8 files'), _summary(at)
    _box(at, 'B Folder').input('').run()
    assert not at.exception, at.exception
    assert 'the A folder loaded' in _caption(at)
    assert _summary(at) is None, f'old report still on screen: {_summary(at)}'


def test_adding_b_takes_the_a_only_report_off_the_page(tmp_path, monkeypatch):
    a, b = _span(tmp_path)
    at = _run(_hub(tmp_path, monkeypatch, a))
    assert _summary(at).startswith('4 files'), _summary(at)
    _box(at, 'B Folder').input(b).run()
    assert not at.exception, at.exception
    assert 'the A and B folders' in _caption(at)
    assert _summary(at) is None, f'old report still on screen: {_summary(at)}'


def test_the_folders_report_comes_back_with_the_folders(tmp_path, monkeypatch):
    """Not a lost report: load the same folders again and it is there."""
    a, b = _span(tmp_path)
    at = _run(_hub(tmp_path, monkeypatch, a, b))
    _box(at, 'B Folder').input('').run()
    _box(at, 'B Folder').input(b).run()
    assert not at.exception, at.exception
    assert (_summary(at) or '').startswith('8 files'), _summary(at)


def test_the_report_stays_while_the_folders_stay(tmp_path, monkeypatch):
    a, b = _span(tmp_path)
    at = _run(_hub(tmp_path, monkeypatch, a, b))
    at.sidebar.radio[0].set_value('Viewer').run()
    at.sidebar.radio[0].set_value('Secret Sauce').run()
    assert not at.exception, at.exception
    assert (_summary(at) or '').startswith('8 files'), _summary(at)


def test_the_page_box_is_an_input_too(tmp_path, monkeypatch):
    """No left-panel traces: the page's own folder box decides."""
    a, b = _span(tmp_path)
    monkeypatch.setenv('OTDR_CACHE_DIR', str(tmp_path / 'cache'))
    monkeypatch.setenv('OTDR_DOWNLOADS_DIR', str(tmp_path / 'Downloads'))
    at = run_streamlit(default_timeout=180).run()
    at.sidebar.radio[0].set_value('Secret Sauce').run()
    box = next(t for t in at.main.text_input if t.key == 'ss_folder_input')
    box.input(a).run()
    _run(at)
    assert _summary(at).startswith('4 files'), _summary(at)
    next(t for t in at.main.text_input if t.key == 'ss_folder_input').input(b).run()
    assert not at.exception, at.exception
    assert _summary(at) is None, f'old report still on screen: {_summary(at)}'


def test_saved_to_names_the_folder_the_workbook_is_in(tmp_path, monkeypatch):
    """It named the traces folder: on an A+B run, a temp copy."""
    a, b = _span(tmp_path)
    at = _run(_hub(tmp_path, monkeypatch, a, b), output='Excel (xlsx)')
    line = next(c.value for c in at.main.caption if c.value.startswith('Saved to: '))
    where = line[len('Saved to: '):]
    assert os.path.isdir(where), line
    assert any(n.endswith('.xlsx') for n in os.listdir(where)), line
    assert 'otdr_span_all_' not in where, line


# ── 2. the Output choice is kept ─────────────────────────────────────────

def _on_screen(el):
    """What the browser shows after this run: the value the run sent, else
    the element's default (see test_page_state_after_cell_click)."""
    p = el.proto
    return el.options[p.value] if p.set_value else el.options[p.default]


def _pair_click_and_back(at, a, b, fibers='1,2', side='a'):
    """A pair link clicked: a new session with the old one's carry id, the
    Viewer draws, then "← Back to Secret Sauce"."""
    view = run_streamlit(default_timeout=180)
    q = {'nav': 'viewer', 'fibers': fibers, 'dir': side, 'ssfolder': a,
         'pa': a, 'pb': b, 'cs': at.session_state['_carry_id']}
    for k, v in q.items():
        view.query_params[k] = v
    view.run()
    assert not view.exception, view.exception
    view.run()
    next(x for x in view.main.button if 'Back to Secret Sauce' in x.label).click().run()
    assert not view.exception, view.exception
    return view


def test_output_choice_survives_a_trip_to_another_tool(tmp_path, monkeypatch):
    a, b = _span(tmp_path)
    at = _hub(tmp_path, monkeypatch, a, b)
    _output(at).set_value('PDF').run()
    at.sidebar.radio[0].set_value('Viewer').run()
    at.sidebar.radio[0].set_value('Secret Sauce').run()
    assert not at.exception, at.exception
    assert _on_screen(_output(at)) == 'PDF'
    assert _output(at).value == 'PDF'


def test_output_choice_survives_a_pair_click_and_back(tmp_path, monkeypatch):
    a, b = _span(tmp_path)
    at = _hub(tmp_path, monkeypatch, a)
    _output(at).set_value('Stay in App').run()
    back = _pair_click_and_back(at, a, '')
    assert back.session_state['nav_radio'] == 'Secret Sauce'
    assert _on_screen(_output(back)) == 'Stay in App'
    assert _output(back).value == 'Stay in App'


def test_output_defaults_to_excel(tmp_path, monkeypatch):
    a, b = _span(tmp_path)
    at = _hub(tmp_path, monkeypatch, a, b)
    assert _output(at).value == 'Excel (xlsx)'


# ── 3. pairs of an A+B run can be clicked ────────────────────────────────

def _links(at):
    """[(label, {query})] for every pair link on the page."""
    out = []
    for m in at.main.markdown:
        for href, label in re.findall(r"<a href='\?([^']*)'[^>]*>([^<]*)</a>", m.value):
            q = {k: v[0] for k, v in parse_qs(html.unescape(href)).items()}
            out.append((html.unescape(label), q))
    return out


def _rows(at):
    """Every pair label on the page, linked or not."""
    out = []
    for m in at.main.markdown:
        out += [html.unescape(x) for x in
                re.findall(r"<(?:a|span) [^>]*title='[^']*'[^>]*>(F[^<]*)</(?:a|span)>", m.value)]
    return out


def _follow(at, q):
    """Open a pair link the way the browser does: a new session."""
    view = run_streamlit(default_timeout=180)
    for k, v in q.items():
        view.query_params[k] = v
    view.run()
    assert not view.exception, view.exception
    view.run()
    return view


def _check_pairs_link(at, a, b):
    links = _links(at)
    rows = _rows(at)
    assert rows, 'no pair rows on the page'
    assert len(links) == len(rows), f'{len(rows) - len(links)} of {len(rows)} rows not linked'
    for label, q in links:
        assert q['nav'] == 'viewer' and q['ssab'] == '1', q
        assert q['pa'] == a and q['pb'] == b, q
        assert q['dir'] in ('a', 'b', 'both'), q
        if q['dir'] == 'both':
            assert re.fullmatch(r'F\d+ A ↔ F\d+ B', label), label
        else:
            assert label.endswith(f" ({q['dir'].upper()})"), label
    return links


def test_every_pair_of_an_a_and_b_run_can_be_clicked(tmp_path, monkeypatch):
    a, b = _span(tmp_path)
    at = _run(_hub(tmp_path, monkeypatch, a, b))
    links = _check_pairs_link(at, a, b)
    assert {q['dir'] for _l, q in links} == {'a', 'b', 'both'}


def test_b_files_named_like_a_files_link_too(tmp_path, monkeypatch):
    """The B copies go in as B_<name>; the link still finds them in B."""
    a, b = _span(tmp_path, same_names=True)
    at = _run(_hub(tmp_path, monkeypatch, a, b))
    links = _check_pairs_link(at, a, b)
    assert any(q['dir'] == 'b' for _l, q in links)


def test_the_mating_table_of_an_excel_run_links_too(tmp_path, monkeypatch):
    a, b = _span(tmp_path)
    at = _run(_hub(tmp_path, monkeypatch, a, b), output='Excel (xlsx)')
    res = dict(at.session_state['ss_result'])
    sides = res['_sides']
    assert len(sides) == 8, sides
    if not res.get('mating_top'):
        # Four traces a side carry no ranking: give the table one, in the
        # engine's shape (fiber numbers from the flat folder, all shared).
        stems = sorted(sides)
        res['mating_top'] = [
            {'group': 'report', 'fileA': x, 'fileB': y,
             'fiberA': sides[x][1], 'fiberB': sides[y][1],
             'mating_lr': 10.0, 'mating_p': 0.5, 'viewable': False,
             'reason': 'fiber number not unique in folder'}
            for x, y in zip(stems, stems[1:] + stems[:1])]
        at.session_state['ss_result'] = res
        at.run()
    assert any('Mating Likelihood' in m.value for m in at.main.markdown)
    _check_pairs_link(at, a, b)


def test_a_pair_opens_on_the_panels_own_folders_and_comes_back(tmp_path, monkeypatch):
    a, b = _span(tmp_path)
    at = _run(_hub(tmp_path, monkeypatch, a, b))
    label, q = next((l, q) for l, q in _links(at) if q['dir'] == 'b')
    view = _follow(at, q)
    ss = view.session_state
    assert ss['nav_radio'] == 'Viewer'
    assert ss['view_dir_a_input'] == a and ss['view_dir_b_input'] == b
    assert ss['viewer_target'] == {'fibers': q['fibers'], 'dir': 'b'}
    next(x for x in view.main.button if 'Back to Secret Sauce' in x.label).click().run()
    assert not view.exception, view.exception
    assert view.session_state['nav_radio'] == 'Secret Sauce'
    assert (_summary(view) or '').startswith('8 files'), _summary(view)
    assert len(_links(view)) == len(_rows(view))


def test_an_a_only_run_keeps_its_links_and_the_tech_s_empty_b(tmp_path, monkeypatch):
    a, b = _span(tmp_path)
    at = _run(_hub(tmp_path, monkeypatch, a))
    links = _links(at)
    assert links and len(links) == len(_rows(at))
    label, q = links[0]
    assert q['dir'] == 'a' and 'ssab' not in q, q
    assert not label.endswith(')'), label
    view = _follow(at, q)
    assert view.session_state['view_dir_b_input'] == ''

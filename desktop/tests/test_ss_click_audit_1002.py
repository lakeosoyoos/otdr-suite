"""Secret Sauce click audit, 2026-10-02 (24 fibers of a long span, A and B
loaded in the left panel).

1. The report followed the session, not the folders: emptying the B box
   left the 48-file A+B report under "Traces: the A folder", and adding B
   back to an A-only report left the 24-file list under "the A and B
   folders".  A pair clicked from that list came Back to a different report.
2. The Output choice had no key: PDF or Stay in App went back to Excel after
   a trip to another tool, or a pair click and "← Back", and the next run
   made an Excel workbook.
"""
from __future__ import annotations

import os
import shutil

from conftest import (run_streamlit, finish_engine_run, FIXTURE_A_DIR,
                      FIXTURE_B_DIR)


def _span(tmp_path):
    """An A and a B folder, four traces each, every fiber number in both."""
    a, b = tmp_path / 'A', tmp_path / 'B'
    a.mkdir()
    b.mkdir()
    for src, dst, own in ((FIXTURE_A_DIR, a, 'AAABBB'), (FIXTURE_B_DIR, b, 'BBBAAA')):
        for i, f in enumerate(sorted(os.listdir(src))[:4], 1):
            shutil.copy2(src / f, dst / f'{own}{i:04d}_1550.sor')
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

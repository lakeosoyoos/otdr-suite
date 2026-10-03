"""The Traces tab: an A and a B folder loader in place of the old "Load Span
(Both Directions)" box (Robert 2026-09-26), moved from the sidebar to a tab
of its own in the top bar (Robert 2026-10-01).

The two boxes are the shared A/B slots the Viewer and the Splice Report read.
They are drawn on the Traces tab only, so a pick reaches every tool without a
"Load into all tools" click and survives a trip between tools.
"""
from __future__ import annotations

import os

import pytest

from conftest import (run_streamlit, import_trace_server, go_tab, page_of,
                      trace_box, trace_box_value, clear_traces, load_traces,
                      FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR)

A, B = str(FIXTURE_SPLICE_A_DIR), str(FIXTURE_SPLICE_B_DIR)
SIDES = {'A Folder': 'a', 'B Folder': 'b'}


def _box(at, label):
    """The box the Traces tab draws (goes to the Traces tab first)."""
    return trace_box(at, SIDES[label])


def _held(at, label):
    """What a box holds, read without leaving the page."""
    return trace_box_value(at, SIDES[label])


def _boxes_drawn(at):
    return [t for t in at.text_input if t.label in SIDES]


def _tabs(at):
    """The pages the top bar has a tab for, left to right."""
    return [b.key[len('nav_tab_'):] for b in at.button
            if (b.key or '').startswith('nav_tab_')]


def test_the_span_box_is_gone_and_a_b_loaders_take_its_place():
    at = go_tab(run_streamlit().run(), 'Traces')
    assert not at.exception
    labels = [b.label for b in at.button]
    assert not any('Load into all tools' in l for l in labels)
    assert not any('Load Span' in e.label for e in at.expander)
    assert '📁 A-Direction Folder' in labels and '📁 B-Direction Folder' in labels
    # the loader is the bar's first tab, ahead of every tool
    assert _tabs(at)[0] == 'Traces'


TRACE_TOOLS = ['Splice Report', 'Unidirectional', 'Splice Report FEC',
               'Secret Sauce', 'Viewer']
TABS = ['Traces'] + TRACE_TOOLS
APP_TOOLS = ['FQA Builder', 'Field Capture']


@pytest.mark.parametrize('page', TABS)
def test_every_page_draws_once_without_a_duplicate_box(page):
    """The boxes are on the Traces tab, once; no tool draws a second copy."""
    at = go_tab(run_streamlit().run(), page)
    assert not at.exception, at.exception
    assert [t.label for t in _boxes_drawn(at)].count('A Folder') == (
        1 if page == 'Traces' else 0)


@pytest.mark.parametrize('page', APP_TOOLS)
def test_the_app_pages_draw_once_without_a_duplicate_box(page, monkeypatch):
    monkeypatch.setenv('OTDR_SUITE_EDITION', 'OTDR Suite App')
    at = run_streamlit(default_timeout=180).run()
    go_tab(at, page)
    assert not at.exception, at.exception
    assert [t.label for t in _boxes_drawn(at)].count('A Folder') == 0
    go_tab(at, 'Traces')
    assert not at.exception, at.exception
    assert [t.label for t in _boxes_drawn(at)].count('A Folder') == 1


def test_the_suite_lists_the_trace_tools_only(monkeypatch):
    """FQA Builder and Field Capture belong to OTDR Suite App (Robert
    2026-09-28).  The App's launcher exports OTDR_SUITE_EDITION."""
    monkeypatch.delenv('OTDR_SUITE_EDITION', raising=False)
    at = run_streamlit().run()
    assert not at.exception
    assert _tabs(at) == TABS
    monkeypatch.setenv('OTDR_SUITE_EDITION', 'OTDR Suite App')
    at = run_streamlit().run()
    assert _tabs(at) == TABS + APP_TOOLS


def test_the_two_pages_still_ship():
    """Off the Suite's list, not out of the build: the App shows them, and a
    file leaving the tree would change the update's file set."""
    from conftest import REPO_ROOT
    for rel in ('fqa/ui.py', 'fqa/__init__.py', 'fieldcapture/server.py',
                'fieldcapture/web/index.html'):
        assert (REPO_ROOT / rel).is_file(), rel


def test_a_pick_reaches_every_tool_and_survives_a_trip():
    tv = import_trace_server()
    at = run_streamlit().run()
    _box(at, 'A Folder').input(A).run()
    _box(at, 'B Folder').input(B).run()
    assert at.session_state['view_dir_a_input'] == A
    assert at.session_state['view_dir_b_input'] == B
    # the tab counts what each folder holds
    captions = [c.value for c in at.caption]
    assert f'A: {len(tv.list_fibers(A))} fibers' in captions
    assert f'B: {len(tv.list_fibers(B))} fibers' in captions
    # Secret Sauce gets one folder holding both directions
    ss = at.session_state['ss_folder_input']
    assert os.path.isdir(ss) and len(os.listdir(ss)) == (
        len(os.listdir(A)) + len(os.listdir(B)))
    # the Splice Report runs on the Traces tab's pair and draws no boxes of
    # its own (test_tools_use_left_panel.py covers the tools' side of this)
    go_tab(at, 'Splice Report')
    assert not at.exception
    assert not _boxes_drawn(at)
    sites = {t.label: t.value for t in at.main.text_input if 'ILA' in t.label}
    assert sites == {'A-Direction ILA / Site': 'ELMDALE',
                     'B-Direction ILA / Site': 'MILLER'}
    # ...and nothing is lost on the way to other tools and back
    go_tab(at, 'Secret Sauce')
    go_tab(at, 'Viewer')
    assert not at.exception
    assert _held(at, 'A Folder') == A and _held(at, 'B Folder') == B
    go_tab(at, 'Traces')
    assert _box(at, 'A Folder').value == A and _box(at, 'B Folder').value == B


def _clear(at):
    if page_of(at) != 'Traces':
        go_tab(at, 'Traces')
    return next(b for b in at.button if b.label == 'Clear Traces')


def _popup(at, label):
    """A button of the Clear Traces pop-up."""
    return next(b for b in at.button if b.label == label)


def _popup_open(at):
    return any(b.key == 'clear_traces_allow' for b in at.button)


def _clear_and_allow(at):
    return clear_traces(at, allow=True)


def _loaded():
    at = run_streamlit().run()
    _box(at, 'A Folder').input(A).run()
    _box(at, 'B Folder').input(B).run()
    at.session_state['sr_result'] = {'ok': True}
    at.session_state['viewer_target'] = {'fiber': '3'}
    return at.run()


def test_clear_traces_asks_before_it_clears():
    """A report can be minutes of engine time: the button says what it will
    take and waits for its own Clear Traces button (Robert 2026-09-28; named
    for what it does 2026-10-02, it read 'Allow')."""
    at = _loaded()
    assert not _popup_open(at)
    _clear(at).click().run()
    assert not at.exception, at.exception
    dialog = at.get('dialog')
    assert len(dialog) == 1
    said = ' '.join(m.value for m in dialog[0].markdown)
    assert 'traces' in said and 'reports' in said
    assert [b.label for b in dialog[0].button] == ['Cancel', 'Clear Traces']
    # nothing has gone yet
    assert _box(at, 'A Folder').value == A and _box(at, 'B Folder').value == B
    assert 'sr_result' in at.session_state and 'viewer_target' in at.session_state


def test_cancel_leaves_everything_as_it_was():
    tv = import_trace_server()
    at = _loaded()
    go_tab(at, 'Viewer')
    _clear(at).click().run()
    _popup(at, 'Cancel').click().run()
    assert not at.exception, at.exception
    assert not _popup_open(at)
    assert _box(at, 'A Folder').value == A and _box(at, 'B Folder').value == B
    assert 'sr_result' in at.session_state and 'viewer_target' in at.session_state
    assert at.session_state['ss_folder_input']
    assert tv.CONFIG['dir_a'] == A and tv.CONFIG['dir_b'] == B


def test_allow_clears_once_and_the_popup_closes():
    at = _clear_and_allow(_loaded())
    assert not at.exception, at.exception
    assert not _popup_open(at)
    assert '_clear_traces_go' not in at.session_state
    assert _box(at, 'A Folder').value == ''
    # a folder picked next is not cleared by a flag left behind
    _box(at, 'A Folder').input(A).run()
    at.run()
    assert _box(at, 'A Folder').value == A


def test_clear_traces_sits_under_the_folder_boxes():
    at = go_tab(run_streamlit().run(), 'Traces')
    assert not at.exception
    labels = [b.label for b in at.button]
    assert labels.index('📁 B-Direction Folder') < labels.index('Clear Traces')
    assert labels.count('Clear Traces') == 1
    # ...and on the Traces tab only
    go_tab(at, 'Viewer')
    assert 'Clear Traces' not in [b.label for b in at.button]


def test_clear_traces_empties_every_tool(tmp_path):
    tv = import_trace_server()
    at = run_streamlit().run()
    _box(at, 'A Folder').input(A).run()
    _box(at, 'B Folder').input(B).run()
    go_tab(at, 'Viewer')                         # pushes A/B to the server
    assert tv.CONFIG['dir_a'] == A and tv.CONFIG['dir_b'] == B
    assert at.session_state['ss_folder_input']
    at.session_state['sr_result'] = {'ok': True}
    at.session_state['sr_dirs'] = (A, B)
    at.session_state['sr_result2'] = {'ok': True}
    at.session_state['sr_dirs2'] = (A, B)
    at.session_state['sr_n_spans'] = 2
    at.session_state['uni_result'] = {'ok': True}
    at.session_state['uni_folder_input'] = A
    at.session_state['ss_result'] = {'ok': True, '_folder': A}
    at.session_state['ss_pairs_result'] = {'ok': True, 'mode': 'pairs'}
    at.session_state['viewer_target'] = {'fiber': '3'}
    at.run()

    _clear_and_allow(at)
    assert not at.exception, at.exception
    assert _box(at, 'A Folder').value == '' and _box(at, 'B Folder').value == ''
    for k in ('ss_folder_input', 'uni_folder_input'):
        assert at.session_state[k] == '', k
    for k in ('sr_result', 'sr_dirs', 'sr_result2', 'sr_dirs2', 'uni_result',
              'ss_result', 'ss_pairs_result', 'viewer_target'):
        assert k not in at.session_state, k
    assert at.session_state['sr_n_spans'] == 1
    # the Viewer's folders go too, so a new session is not seeded from them
    assert not tv.CONFIG['dir_a'] and not tv.CONFIG['dir_b']
    # ...and every tool draws empty
    for page in TRACE_TOOLS:
        go_tab(at, page)
        assert not at.exception, (page, at.exception)
        assert _held(at, 'A Folder') == ''
    go_tab(at, 'Traces')
    assert _box(at, 'A Folder').value == '' and _box(at, 'B Folder').value == ''


def test_clear_traces_leaves_the_files_on_disk(tmp_path, monkeypatch):
    """It clears what the Suite has loaded.  The traces are the tech's, and so
    is anything in the cache that is not a report of the cleared folders
    (test_clear_report.py covers the saved copies that do go)."""
    cache = tmp_path / 'cache'
    cache.mkdir()
    (cache / 'saved_report.json').write_text('{}', encoding='utf-8')
    monkeypatch.setenv('OTDR_CACHE_DIR', str(cache))
    before_a, before_b = sorted(os.listdir(A)), sorted(os.listdir(B))
    at = run_streamlit().run()
    _box(at, 'A Folder').input(A).run()
    _box(at, 'B Folder').input(B).run()
    _clear_and_allow(at)
    assert not at.exception
    assert sorted(os.listdir(A)) == before_a and sorted(os.listdir(B)) == before_b
    assert os.listdir(cache) == ['saved_report.json']


def test_the_same_span_loads_again_after_a_clear():
    at = run_streamlit().run()
    _box(at, 'A Folder').input(A).run()
    _box(at, 'B Folder').input(B).run()
    _clear_and_allow(at)
    _box(at, 'A Folder').input(A).run()
    _box(at, 'B Folder').input(B).run()
    assert not at.exception
    ss = at.session_state['ss_folder_input']
    assert os.path.isdir(ss) and len(os.listdir(ss)) == (
        len(os.listdir(A)) + len(os.listdir(B)))


def test_a_new_folder_drops_the_previous_report(tmp_path):
    at = run_streamlit().run()
    at.session_state['sr_result'] = {'ok': True}
    at.session_state['viewer_target'] = {'fiber': '3'}
    at.run()
    _box(at, 'A Folder').input(str(tmp_path)).run()   # a folder not loaded before
    assert 'sr_result' not in at.session_state
    assert 'viewer_target' not in at.session_state


# ── Secret Sauce's one folder, across the pair-click round trip ───────────
# A pair click is a URL navigation: a NEW session, with the Secret Sauce
# folder put in the A box so the Viewer can read the pair from it.

def _pair_click(ss_folder):
    at = run_streamlit(default_timeout=180)
    for k, v in (('nav', 'viewer'), ('fibers', '1,2'), ('dir', 'a'),
                 ('ssfolder', ss_folder)):
        at.query_params[k] = v
    return at.run()


def test_the_same_two_folders_always_give_the_same_secret_sauce_folder():
    """A report is saved under the folder it ran on, and every click into the
    Viewer tab starts a new session: a folder built fresh each time would cost
    the report on the way back, and a copy of the whole span."""
    first = run_streamlit().run()
    _box(first, 'A Folder').input(A).run()
    _box(first, 'B Folder').input(B).run()
    again = run_streamlit().run()
    _box(again, 'A Folder').input(A).run()
    _box(again, 'B Folder').input(B).run()
    assert first.session_state['ss_folder_input'] == again.session_state['ss_folder_input']
    # ...and B then A is another span
    swapped = run_streamlit().run()
    _box(swapped, 'A Folder').input(B).run()
    _box(swapped, 'B Folder').input(A).run()
    assert swapped.session_state['ss_folder_input'] != first.session_state['ss_folder_input']


def test_a_pair_click_keeps_secret_sauce_on_the_folder_its_report_ran_on():
    tv = import_trace_server()
    at = run_streamlit().run()
    _box(at, 'A Folder').input(A).run()
    _box(at, 'B Folder').input(B).run()
    go_tab(at, 'Viewer')
    assert tv.CONFIG['dir_b'] == B          # what seeds the B box of a new session
    ran_on = at.session_state['ss_folder_input']

    clicked = _pair_click(ran_on)
    assert not clicked.exception, clicked.exception
    assert _held(clicked, 'A Folder') == ran_on and _held(clicked, 'B Folder') == B
    assert clicked.session_state['ss_folder_input'] == ran_on
    back = next(b for b in clicked.button if 'Back to Secret Sauce' in b.label)
    back.click().run()
    assert not clicked.exception, clicked.exception
    assert clicked.session_state['ss_folder_input'] == ran_on


def test_a_new_a_folder_after_a_pair_click_builds_its_own_folder(tmp_path):
    at = run_streamlit().run()
    _box(at, 'A Folder').input(A).run()
    _box(at, 'B Folder').input(B).run()
    go_tab(at, 'Viewer')
    ran_on = at.session_state['ss_folder_input']
    clicked = _pair_click(ran_on)
    _box(clicked, 'A Folder').input(B).run()
    _box(clicked, 'B Folder').input(A).run()
    assert clicked.session_state['ss_folder_input'] != ran_on


def test_folders_picked_on_traces_reach_the_viewers_server():
    """With the boxes on the Viewer's own screen (the sidebar) a folder
    picked was a folder the Viewer's server had; the Traces tab sends them
    itself, so a new browser tab or a popped-out Viewer window does not wait
    for a visit to the Viewer."""
    ts = import_trace_server()
    at = run_streamlit().run()
    load_traces(at, FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR)
    assert page_of(at) == 'Traces' and not at.exception
    assert ts.CONFIG['dir_a'] == str(FIXTURE_SPLICE_A_DIR)
    assert ts.CONFIG['dir_b'] == str(FIXTURE_SPLICE_B_DIR)
    trace_box(at, 'b').input('').run()
    assert ts.CONFIG['dir_a'] == str(FIXTURE_SPLICE_A_DIR) and ts.CONFIG['dir_b'] is None

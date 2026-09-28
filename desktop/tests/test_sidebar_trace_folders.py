"""Sidebar Trace Folders: an A and a B folder loader in place of the old
"Load Span (Both Directions)" box (Robert 2026-09-26).

The two boxes are the shared A/B slots the Viewer and the Splice Report read,
drawn on every page, so a pick reaches every tool without a "Load into all
tools" click and survives a trip between tools.
"""
from __future__ import annotations

import os

import pytest

from conftest import (run_streamlit, import_trace_server,
                      FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR)

A, B = str(FIXTURE_SPLICE_A_DIR), str(FIXTURE_SPLICE_B_DIR)


def _box(at, label):
    return next(t for t in at.sidebar.text_input if t.label == label)


def test_the_span_box_is_gone_and_a_b_loaders_take_its_place():
    at = run_streamlit().run()
    assert not at.exception
    labels = [b.label for b in at.sidebar.button]
    assert not any('Load into all tools' in l for l in labels)
    assert not any('Load Span' in e.label for e in at.sidebar.expander)
    assert '📁 A-direction folder' in labels and '📁 B-direction folder' in labels
    # the loader sits ABOVE the tool list
    md = [m.value for m in at.sidebar.markdown]
    assert md.index('##### Trace Folders') < md.index('##### Select Tool')


TRACE_TOOLS = ['Viewer', 'Splice Report', 'Unidirectional', 'Secret Sauce']
APP_TOOLS = ['FQA Builder', 'Field Capture']


@pytest.mark.parametrize('page', TRACE_TOOLS)
def test_every_page_draws_once_without_a_duplicate_box(page):
    at = run_streamlit().run()
    at.sidebar.radio[0].set_value(page).run()
    assert not at.exception, at.exception
    assert [t.label for t in at.sidebar.text_input].count('A folder') == 1


@pytest.mark.parametrize('page', APP_TOOLS)
def test_the_app_pages_draw_once_without_a_duplicate_box(page, monkeypatch):
    monkeypatch.setenv('OTDR_SUITE_EDITION', 'OTDR Suite App')
    at = run_streamlit(default_timeout=180).run()
    at.sidebar.radio[0].set_value(page).run()
    assert not at.exception, at.exception
    assert [t.label for t in at.sidebar.text_input].count('A folder') == 1


def test_the_suite_lists_the_four_trace_tools_only(monkeypatch):
    """FQA Builder and Field Capture belong to OTDR Suite App (Robert
    2026-09-28).  The App's launcher exports OTDR_SUITE_EDITION."""
    monkeypatch.delenv('OTDR_SUITE_EDITION', raising=False)
    at = run_streamlit().run()
    assert not at.exception
    assert list(at.sidebar.radio[0].options) == TRACE_TOOLS
    monkeypatch.setenv('OTDR_SUITE_EDITION', 'OTDR Suite App')
    at = run_streamlit().run()
    assert list(at.sidebar.radio[0].options) == TRACE_TOOLS + APP_TOOLS


def test_the_two_pages_still_ship():
    """Off the Suite's list, not out of the build: the App shows them, and a
    file leaving the tree would change the update's file set."""
    from conftest import REPO_ROOT
    for rel in ('fqa/ui.py', 'fqa/__init__.py', 'fieldcapture/server.py',
                'fieldcapture/web/index.html'):
        assert (REPO_ROOT / rel).is_file(), rel


def test_a_pick_reaches_every_tool_and_survives_a_trip():
    at = run_streamlit().run()
    _box(at, 'A folder').input(A).run()
    _box(at, 'B folder').input(B).run()
    assert at.session_state['view_dir_a_input'] == A
    assert at.session_state['view_dir_b_input'] == B
    # Secret Sauce gets one folder holding both directions
    ss = at.session_state['ss_folder_input']
    assert os.path.isdir(ss) and len(os.listdir(ss)) == (
        len(os.listdir(A)) + len(os.listdir(B)))
    # the Splice Report shows the sidebar's pair instead of its own boxes
    at.sidebar.radio[0].set_value('Splice Report').run()
    assert not at.exception
    assert A in [c.value for c in at.code] and B in [c.value for c in at.code]
    assert not [t for t in at.main.text_input if t.label in ('A folder', 'B folder')]
    # ...and nothing is lost on the way to another tool and back
    at.sidebar.radio[0].set_value('Secret Sauce').run()
    at.sidebar.radio[0].set_value('Viewer').run()
    assert _box(at, 'A folder').value == A and _box(at, 'B folder').value == B


def _clear(at):
    return next(b for b in at.sidebar.button if b.label == 'Clear Traces')


def test_clear_traces_sits_under_the_folder_boxes_above_the_tool_list():
    at = run_streamlit().run()
    assert not at.exception
    labels = [b.label for b in at.sidebar.button]
    assert labels.index('📁 B-direction folder') < labels.index('Clear Traces')
    assert labels.count('Clear Traces') == 1


def test_clear_traces_empties_every_tool(tmp_path):
    tv = import_trace_server()
    at = run_streamlit().run()
    _box(at, 'A folder').input(A).run()
    _box(at, 'B folder').input(B).run()
    at.sidebar.radio[0].set_value('Viewer').run()          # pushes A/B to the server
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

    _clear(at).click().run()
    assert not at.exception, at.exception
    assert _box(at, 'A folder').value == '' and _box(at, 'B folder').value == ''
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
        at.sidebar.radio[0].set_value(page).run()
        assert not at.exception, (page, at.exception)
        assert _box(at, 'A folder').value == ''


def test_clear_traces_leaves_the_files_on_disk(tmp_path, monkeypatch):
    """It clears what the Suite has loaded.  The traces and the saved reports
    are the tech's."""
    cache = tmp_path / 'cache'
    cache.mkdir()
    (cache / 'saved_report.json').write_text('{}', encoding='utf-8')
    monkeypatch.setenv('OTDR_CACHE_DIR', str(cache))
    before_a, before_b = sorted(os.listdir(A)), sorted(os.listdir(B))
    at = run_streamlit().run()
    _box(at, 'A folder').input(A).run()
    _box(at, 'B folder').input(B).run()
    _clear(at).click().run()
    assert not at.exception
    assert sorted(os.listdir(A)) == before_a and sorted(os.listdir(B)) == before_b
    assert os.listdir(cache) == ['saved_report.json']


def test_the_same_span_loads_again_after_a_clear():
    at = run_streamlit().run()
    _box(at, 'A folder').input(A).run()
    _box(at, 'B folder').input(B).run()
    _clear(at).click().run()
    _box(at, 'A folder').input(A).run()
    _box(at, 'B folder').input(B).run()
    assert not at.exception
    ss = at.session_state['ss_folder_input']
    assert os.path.isdir(ss) and len(os.listdir(ss)) == (
        len(os.listdir(A)) + len(os.listdir(B)))


def test_a_new_folder_drops_the_previous_report(tmp_path):
    at = run_streamlit().run()
    at.session_state['sr_result'] = {'ok': True}
    at.session_state['viewer_target'] = {'fiber': '3'}
    at.run()
    _box(at, 'A folder').input(str(tmp_path)).run()   # a folder not loaded before
    assert 'sr_result' not in at.session_state
    assert 'viewer_target' not in at.session_state

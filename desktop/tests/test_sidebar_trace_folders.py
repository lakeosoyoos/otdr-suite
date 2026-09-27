"""Sidebar Trace Folders: an A and a B folder loader in place of the old
"Load Span (Both Directions)" box (Robert 2026-09-26).

The two boxes are the shared A/B slots the Viewer and the Splice Report read,
drawn on every page, so a pick reaches every tool without a "Load into all
tools" click and survives a trip between tools.
"""
from __future__ import annotations

import os

import pytest

from conftest import run_streamlit, FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR

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


@pytest.mark.parametrize('page', ['Viewer', 'Splice Report', 'Unidirectional',
                                  'Secret Sauce', 'FQA Builder', 'Field Capture'])
def test_every_page_draws_once_without_a_duplicate_box(page):
    at = run_streamlit().run()
    at.sidebar.radio[0].set_value(page).run()
    assert not at.exception, at.exception
    assert [t.label for t in at.sidebar.text_input].count('A folder') == 1


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


def test_a_new_folder_drops_the_previous_report(tmp_path):
    at = run_streamlit().run()
    at.session_state['sr_result'] = {'ok': True}
    at.session_state['viewer_target'] = {'fiber': '3'}
    at.run()
    _box(at, 'A folder').input(str(tmp_path)).run()   # a folder not loaded before
    assert 'sr_result' not in at.session_state
    assert 'viewer_target' not in at.session_state

"""The Secret Sauce page opens with one line saying what the tool does, as
every other tool page does (audit 2026-10-02: it never said it looks for
duplicate traces)."""
from __future__ import annotations

from conftest import run_streamlit, FIXTURE_A_DIR

INTRO = 'Finds traces that are copies of each other'


def _page(**state):
    at = run_streamlit().run()
    for k, v in state.items():
        at.session_state[k] = v
    at.sidebar.radio[0].set_value('Secret Sauce').run()
    assert not at.exception, list(at.exception)
    return at


def test_the_first_line_says_it_finds_duplicates():
    at = _page()
    first = at.main.caption[0].value
    assert first.startswith(INTRO), first
    assert 'ranked by how likely it is a duplicate' in first
    # the empty page still says where to start
    assert 'Pick a folder of' in first


def test_the_line_stays_with_the_left_panel_loaded():
    at = _page(view_dir_a_input=str(FIXTURE_A_DIR))
    first = at.main.caption[0].value
    assert first.startswith(INTRO), first
    assert 'Pick a folder of' not in first
    assert '—' not in first                      # no em dash on screen

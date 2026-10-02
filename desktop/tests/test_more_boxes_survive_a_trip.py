"""The boxes a tool page draws for itself keep what the tech put in them
across a trip to another tool.

Streamlit drops a widget's state on any run that does not draw it, so every
box that lives on one page was emptied by a trip to another tool and
back.  test_page_boxes_survive_a_trip.py covers the Splice Report's site
names and every 'Save Reports To' box.  This covers the rest:

    Splice Report   Select Traces, and the one-folder box (Traces tab empty)
                    every added span's Select Traces and folder boxes
    Unidirectional  its own folder box (Traces tab empty), Job Landmarks,
                    and the Direction pick after a run on a mixed folder

Each box keeps what it shows in a slot no widget owns and is seeded from it
before it is drawn.  Anything that writes a box from off its page (Clear
Traces, a new pair on the Traces tab, Remove span) keeps the slot in step,
so the old text cannot come back after it.
"""
from __future__ import annotations

import os

import pytest

from conftest import (run_streamlit, finish_engine_run, go_tab, trace_box,
                      clear_traces, FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR)

A, B = str(FIXTURE_SPLICE_A_DIR), str(FIXTURE_SPLICE_B_DIR)
TWO, ONE = 'Two folders (A + B)', 'One folder / zip (both directions)'


def _open(at, page):
    go_tab(at, page)
    assert not at.exception, at.exception
    return at


def _hub(a='', b=''):
    at = run_streamlit(default_timeout=180).run()
    if a:
        trace_box(at, 'a').input(a).run()
    if b:
        trace_box(at, 'b').input(b).run()
    assert not at.exception, at.exception
    return at


def _trip(at, back_to, via='Viewer'):
    _open(at, via)
    at.run()                    # a few runs away, as a tech spends there
    return _open(at, back_to)


def _clear_traces_on(at, page):
    """Away on `page`, then Clear Traces (on the Traces tab, itself away
    from the page whose boxes are watched)."""
    _open(at, page)
    at.run()
    clear_traces(at)
    assert not at.exception, at.exception
    at.run()
    return at


def _click(at, label):
    next(b for b in at.main.button if b.label == label).click().run()
    assert not at.exception, at.exception
    return at


# ── Splice Report, Traces tab empty ───────────────────────────────────────

def _one_folder(at, key='sr_input_mode', box='sr_one_folder', folder=A):
    at.radio(key=key).set_value(ONE).run()
    at.text_input(key=box).input(folder).run()
    assert not at.exception, at.exception
    return at


def test_the_splice_reports_one_folder_choice_and_box_survive_a_trip():
    at = _one_folder(_open(_hub(), 'Splice Report'))
    _trip(at, 'Splice Report')
    assert at.radio(key='sr_input_mode').value == ONE
    assert at.text_input(key='sr_one_folder').value == A


def test_clear_traces_on_another_tool_still_empties_the_splice_reports_loader():
    at = _one_folder(_open(_hub(), 'Splice Report'))
    _clear_traces_on(at, 'Viewer')
    _open(at, 'Splice Report')
    assert at.radio(key='sr_input_mode').value == TWO
    at.radio(key='sr_input_mode').set_value(ONE).run()
    assert at.text_input(key='sr_one_folder').value == ''


def test_a_pair_loaded_on_another_tool_still_puts_select_traces_back_to_two():
    at = _one_folder(_open(_hub(), 'Splice Report'))
    _open(at, 'Viewer')
    trace_box(at, 'a').input(A).run()
    trace_box(at, 'b').input(B).run()
    trace_box(at, 'a').input('').run()
    trace_box(at, 'b').input('').run()
    at.run()
    _open(at, 'Splice Report')
    assert at.radio(key='sr_input_mode').value == TWO


# ── Splice Report, an added span ──────────────────────────────────────────

def _span_2(at):
    return _click(at, '➕ Add Span…')


def test_an_added_spans_folders_and_site_names_survive_a_trip():
    at = _span_2(_open(_hub(A, B), 'Splice Report'))
    at.text_input(key='sr2_dir_a').input(B).run()
    at.text_input(key='sr2_dir_b').input(A).run()
    sites = (at.text_input(key='sr2_site_a').value,
             at.text_input(key='sr2_site_b').value)
    assert sites != ('A', 'B')
    _trip(at, 'Splice Report')
    assert at.text_input(key='sr2_dir_a').value == B
    assert at.text_input(key='sr2_dir_b').value == A
    assert (at.text_input(key='sr2_site_a').value,
            at.text_input(key='sr2_site_b').value) == sites


def test_an_added_spans_one_folder_choice_and_box_survive_a_trip():
    at = _one_folder(_span_2(_open(_hub(A, B), 'Splice Report')),
                     'sr2_input_mode', 'sr2_one_folder', B)
    _trip(at, 'Splice Report')
    assert at.radio(key='sr2_input_mode').value == ONE
    assert at.text_input(key='sr2_one_folder').value == B


def test_a_span_removed_and_added_again_starts_empty():
    at = _span_2(_open(_hub(A, B), 'Splice Report'))
    at.text_input(key='sr2_dir_a').input(B).run()
    _click(at, '✖ Remove Span 2')
    at.run()
    _span_2(at)
    assert at.text_input(key='sr2_dir_a').value == ''
    assert at.radio(key='sr2_input_mode').value == TWO


def test_clear_traces_on_another_tool_leaves_no_added_span_behind():
    at = _span_2(_open(_hub(A, B), 'Splice Report'))
    at.text_input(key='sr2_dir_a').input(B).run()
    _clear_traces_on(at, 'Viewer')
    _open(at, 'Splice Report')
    trace_box(at, 'a').input(A).run()
    trace_box(at, 'b').input(B).run()
    _open(at, 'Splice Report')
    _span_2(at)
    assert at.text_input(key='sr2_dir_a').value == ''


# ── Unidirectional ────────────────────────────────────────────────────────

def test_unidirectionals_own_folder_box_survives_a_trip():
    at = _open(_hub(), 'Unidirectional')
    at.text_input(key='uni_folder_input').input(A).run()
    _trip(at, 'Unidirectional')
    assert at.text_input(key='uni_folder_input').value == A


def test_clear_traces_on_another_tool_still_empties_unidirectionals_box():
    at = _open(_hub(), 'Unidirectional')
    at.text_input(key='uni_folder_input').input(A).run()
    _clear_traces_on(at, 'Viewer')
    _open(at, 'Unidirectional')
    assert at.text_input(key='uni_folder_input').value == ''


def test_the_job_landmarks_survive_a_trip():
    at = _open(_hub(A), 'Unidirectional')
    at.text_area(key='uni_landmarks_text').input('0.57, Hut 7\n4.05, HH8').run()
    _trip(at, 'Unidirectional')
    assert at.text_area(key='uni_landmarks_text').value == '0.57, Hut 7\n4.05, HH8'


@pytest.fixture
def mixed(tmp_path, monkeypatch):
    """One folder holding two directions, so a Uni run reports both and the
    page offers the Direction pick: six of the fixture's shots as they are,
    and the same six with one of the two sites in their GenParams swapped
    for another name of the same length (every offset stays valid, as in
    test_uni_coverage.py).  Same fiber numbers, so the engine keeps the two
    apart instead of folding one into the other as a typo."""
    import folder_intake as fi
    monkeypatch.setenv('OTDR_CACHE_DIR', str(tmp_path / 'cache'))
    folder = tmp_path / 'both'
    folder.mkdir()
    shots = sorted(n for n in os.listdir(A) if n.lower().endswith('.sor'))[:6]
    site = fi.sor_header(os.path.join(A, shots[0]))['loc_pair'][0]
    other = 'Q' * len(site)
    assert site and other != site
    for name in shots:
        raw = open(os.path.join(A, name), 'rb').read()
        (folder / name).write_bytes(raw)
        (folder / f'other_{name}').write_bytes(
            raw.replace(site.encode(), other.encode()))
        assert other in fi.sor_header(str(folder / f'other_{name}'))['loc_pair']
    out = tmp_path / 'saved reports'
    out.mkdir()
    return str(folder), str(out)


def _uni_run(at, dest):
    at.session_state['uni_report_dest'] = dest
    at.run()
    _click(at, 'Run Unidirectional Report')
    finish_engine_run(at, 'uni')
    # The Direction pick sits above the spot where the page takes the
    # result in, so it is drawn from the run after that.
    at.run()
    assert not at.exception, at.exception
    return at


def test_the_direction_pick_survives_a_trip(mixed):
    folder, dest = mixed
    # Uni's own folder box, the Traces tab empty: on the Traces tab a folder
    # holding two directions is split into A and B (_panel_dirs), so Uni
    # would run on one direction there and offer no Direction pick.
    at = _open(_hub(), 'Unidirectional')
    at.text_input(key='uni_folder_input').input(folder).run()
    at = _uni_run(at, dest)
    pick = at.selectbox(key='uni_dir_pick')
    assert len(pick.options) == 3, pick.options       # most populous + 2
    pick.set_value(pick.options[2]).run()
    _trip(at, 'Unidirectional')
    assert at.selectbox(key='uni_dir_pick').value == pick.options[2]


def test_a_kept_direction_that_is_no_longer_offered_is_left_out(mixed):
    folder, dest = mixed
    # Uni's own folder box, the Traces tab empty: on the Traces tab a folder
    # holding two directions is split into A and B (_panel_dirs), so Uni
    # would run on one direction there and offer no Direction pick.
    at = _open(_hub(), 'Unidirectional')
    at.text_input(key='uni_folder_input').input(folder).run()
    at = _uni_run(at, dest)
    _open(at, 'Viewer')
    # As after a new run whose directions (or fibre counts) differ.
    at.session_state['uni_dir_pick_saved'] = 'NOT->OFFERED  (6 fibers)'
    at.run()
    _open(at, 'Unidirectional')
    assert at.selectbox(key='uni_dir_pick').value == '(most populous)'

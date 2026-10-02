"""The Splice Report's site names and every report page's 'Save Reports To'
folder survive a trip to another tool.

Seen 2026-09-29: with an A and a B folder loaded in the left panel, the
Splice Report's "A-direction ILA / site" and "B-direction ILA / site" boxes
showed the site names read from the traces, and "Save reports to" showed the
folder the tech had chosen.  After Select Tool -> Viewer -> Splice Report the
site boxes read "A" and "B" and the save box was empty again, so the next
report was named A_to_B_SpliceReport.xlsx and landed in Downloads.

Streamlit drops a widget's state on any run that does not draw it.  The
page re-derives the site names only when the folder pair changes, and the
pair had not changed, so nothing put the names back.  The boxes now keep
what they show in a slot no widget owns, and the page seeds a box Streamlit
forgot from it before the box is drawn.
"""
from __future__ import annotations

import pytest

from conftest import run_streamlit, FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR

A, B = str(FIXTURE_SPLICE_A_DIR), str(FIXTURE_SPLICE_B_DIR)
SITE_A, SITE_B = 'A-Direction ILA / Site', 'B-Direction ILA / Site'


def _box(at, label, where='main'):
    return next(t for t in getattr(at, where).text_input if t.label == label)


def _open(at, page):
    at.sidebar.radio[0].set_value(page).run()
    assert not at.exception, at.exception
    return at


def _hub(a=A, b=B):
    at = run_streamlit(default_timeout=180).run()
    if a:
        _box(at, 'A Folder', 'sidebar').input(a).run()
    if b:
        _box(at, 'B Folder', 'sidebar').input(b).run()
    assert not at.exception, at.exception
    return at


def _sites(at):
    return _box(at, SITE_A).value, _box(at, SITE_B).value


def _trip(at, back_to, via='Viewer'):
    _open(at, via)
    return _open(at, back_to)


# ── the Splice Report's site names ────────────────────────────────────────

@pytest.mark.parametrize('via', ['Viewer', 'Unidirectional'])
def test_the_site_names_read_from_the_traces_survive_a_trip(via):
    at = _open(_hub(), 'Splice Report')
    read = _sites(at)
    assert 'A' not in read and 'B' not in read and all(read), read
    _trip(at, 'Splice Report', via)
    assert _sites(at) == read


def test_a_site_name_the_tech_typed_survives_a_trip():
    at = _open(_hub(), 'Splice Report')
    _box(at, SITE_A).input('Hut 7').run()
    _box(at, SITE_B).input('Hut 9').run()
    _trip(at, 'Splice Report')
    assert _sites(at) == ('Hut 7', 'Hut 9')


@pytest.mark.parametrize('loaded', [True, False])
def test_clear_traces_on_another_tool_still_puts_the_site_names_back_to_a_and_b(loaded):
    at = _open(_hub() if loaded else _hub('', ''), 'Splice Report')
    if not loaded:
        _box(at, SITE_A).input('Hut 7').run()
    assert _sites(at) != ('A', 'B')
    _open(at, 'Viewer')
    next(b for b in at.sidebar.button if b.label == 'Clear Traces').click().run()
    next(b for b in at.button if b.key == 'clear_traces_allow').click().run()
    assert not at.exception, at.exception
    # A few more runs away from the page: the "A" and "B" that Clear Traces
    # writes into the boxes are dropped too while the page is not drawn.
    at.run()
    _open(at, 'Unidirectional').run()
    _open(at, 'Splice Report')
    assert _sites(at) == ('A', 'B')


def test_a_new_pair_loaded_on_another_tool_does_not_bring_back_the_old_names():
    at = _open(_hub(), 'Splice Report')
    _box(at, SITE_A).input('Hut 7').run()
    _open(at, 'Viewer')
    # The same two folders, the other way round: a different pair.
    _box(at, 'A Folder', 'sidebar').input(B).run()
    _box(at, 'B Folder', 'sidebar').input(A).run()
    _open(at, 'Splice Report')
    assert 'Hut 7' not in _sites(at)


def test_a_span_removed_and_added_again_starts_with_a_and_b():
    at = _open(_hub(), 'Splice Report')
    next(b for b in at.main.button if b.label == '➕ Add Span…').click().run()
    at.text_input(key='sr2_site_a').input('Hut 7').run()
    next(b for b in at.main.button if b.label == '✖ Remove Span 2').click().run()
    next(b for b in at.main.button if b.label == '➕ Add Span…').click().run()
    assert not at.exception, at.exception
    assert (at.text_input(key='sr2_site_a').value,
            at.text_input(key='sr2_site_b').value) == ('A', 'B')


# ── 'Save Reports To' on every report page ────────────────────────────────

@pytest.mark.parametrize('page,key', [
    ('Splice Report', 'sr_report_dest'),
    ('Unidirectional', 'uni_report_dest'),
    ('Secret Sauce', 'ss_report_dest'),
])
def test_the_save_folder_survives_a_trip(page, key, tmp_path):
    at = _open(_hub(), page)
    _box(at, 'Save Reports To').input(str(tmp_path)).run()
    assert not at.exception, at.exception
    _trip(at, page)
    assert _box(at, 'Save Reports To').value == str(tmp_path)
    assert at.session_state[key] == str(tmp_path)


def test_an_emptied_save_box_stays_empty_after_a_trip(tmp_path):
    at = _open(_hub(), 'Splice Report')
    _box(at, 'Save Reports To').input(str(tmp_path)).run()
    _box(at, 'Save Reports To').input('').run()
    _trip(at, 'Splice Report')
    assert _box(at, 'Save Reports To').value == ''



def test_the_fec_folder_boxes_survive_a_trip(tmp_path):
    """Splice Report FEC's own A end / B end boxes: a trip to Viewer FEC (or
    any tool) and back finds them as the tech left them."""
    ea, eb = tmp_path / 'endA', tmp_path / 'endB'
    ea.mkdir(); eb.mkdir()
    at = _open(_hub(None, None), 'Splice Report FEC')
    _box(at, 'A End FEC (Folder or .zip)').input(str(ea)).run()
    _box(at, 'B End FEC (Optional) (Folder or .zip)').input(str(eb)).run()
    _box(at, 'Save Reports To').input(str(tmp_path)).run()
    _trip(at, 'Splice Report FEC', via='Viewer FEC')
    assert not at.exception, at.exception
    assert _box(at, 'A End FEC (Folder or .zip)').value == str(ea)
    assert _box(at, 'B End FEC (Optional) (Folder or .zip)').value == str(eb)
    assert _box(at, 'Save Reports To').value == str(tmp_path)

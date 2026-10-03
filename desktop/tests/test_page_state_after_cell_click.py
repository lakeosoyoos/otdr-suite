"""What the tech set on the Splice Report and Unidirectional pages comes back
after a report-cell click into the Viewer and "← Back".

Seen 2026-10-02 in a click audit: with "Cell Clicks Open In: This Tab", a
flagged cell is a link, and the link starts a NEW session.  After the click
and "← Back to Splice Report" the page had reset without a word:

    Show/Hide in Report   Breaks switched off came back ON, so the next
                          workbook printed the breaks and had no Display sheet
    site names            HUT-A / HUT-B typed over the stored names went
                          back to the stored names, and so did the file name
    Save Reports To       came back empty, so the next report went to Downloads

A trip through the sidebar (the top bar's tabs now) kept all three; only the link lost them.  Two
causes.  The link carried the OTDR Settings and the save box's folder, but
not the slots the page boxes keep what they show in (`*_saved`), so the new
session had nothing to put back.  And a box only shows a value written in
the run that draws it: a value seeded on an earlier run (the carry, on the
Viewer page) stays on the server while the box on screen draws empty, and
the next click sends the empty box back.  So these tests look at what the
browser is told (the element's value when the run sets it, else its
default), not only at the server's slot.
"""
from __future__ import annotations

import pytest

from conftest import (run_streamlit, go_tab, load_traces, clear_traces,
                      FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR)

A, B = str(FIXTURE_SPLICE_A_DIR), str(FIXTURE_SPLICE_B_DIR)
SITE_A, SITE_B = 'A-Direction ILA / Site', 'B-Direction ILA / Site'
UNI_A, UNI_B = 'A-End Site', 'B-End Site'
TWO, ONE = 'Two folders (A + B)', 'One folder / zip (both directions)'


@pytest.fixture(autouse=True)
def _own_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("OTDR_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("OTDR_SETTINGS_DIR", str(tmp_path / "settings"))


def _on_screen(el):
    """What the browser shows for this element after this run: the value
    the run sent, else the element's default.  A radio gives the index of
    the option shown: Streamlit 1.64 (the Windows build) sends a radio's
    value as the option's text (`raw_value`), 1.50 as its index (`value`)."""
    p = el.proto
    if not p.set_value:
        return p.default
    if 'raw_value' in {f.name for f in p.DESCRIPTOR.fields}:
        return list(p.options).index(p.raw_value)
    return p.value


def _box(at, label):
    return next(t for t in at.main.text_input if t.label == label)


def _open(at, page):
    go_tab(at, page)
    assert not at.exception, at.exception
    return at


def _hub(a=A, b=B):
    at = run_streamlit(default_timeout=180).run()
    if a:
        load_traces(at, a=a)
    if b:
        load_traces(at, b=b)
    assert not at.exception, at.exception
    return at


def _n_spans(at):
    ss = at.session_state
    return ss['sr_n_spans'] if 'sr_n_spans' in ss else 1


def _click(at, label):
    next(b for b in at.main.button if b.label == label).click().run()
    assert not at.exception, at.exception
    return at


def _cell_click_and_back(at, page, src, **link):
    """A report cell clicked with "This Tab": the link starts a new session
    with the old one's carry id, the Viewer draws, then "← Back"."""
    view = run_streamlit(default_timeout=180)
    q = {'nav': 'viewer', 'fiber': '3', 'km': '1.0', 'src': src,
         'pa': A, 'pb': B, 'cs': at.session_state['_carry_id'], **link}
    for k, v in q.items():
        view.query_params[k] = v
    view.run()
    assert not view.exception, view.exception
    view.run()                                   # the Viewer page draws again
    next(b for b in view.button if b.label == f'← Back to {page}').click().run()
    assert not view.exception, view.exception
    assert view.session_state['nav_radio'] == page
    return view


def _sr_back(at, **link):
    return _cell_click_and_back(at, 'Splice Report', 'sr',
                                **{'dir': 'both', 'sra': A, 'srb': B, **link})


# ── Splice Report ─────────────────────────────────────────────────────────

def _sr_set_up(tmp_path):
    at = _open(_hub(), 'Splice Report')
    at.toggle(key='sr_show_break').set_value(False).run()
    _box(at, SITE_A).input('HUT-A').run()
    _box(at, SITE_B).input('HUT-B').run()
    _box(at, 'Save Reports To').input(str(tmp_path / 'my reports')).run()
    assert not at.exception, at.exception
    return at


def _sr_kept(view, tmp_path):
    brk = view.toggle(key='sr_show_break')
    assert brk.value is False and _on_screen(brk) is False
    assert view.session_state['sr_show_saved'].get('break') is False
    assert (_on_screen(_box(view, SITE_A)), _on_screen(_box(view, SITE_B))) \
        == ('HUT-A', 'HUT-B')
    assert (view.session_state['sr_site_a'], view.session_state['sr_site_b']) \
        == ('HUT-A', 'HUT-B')
    assert _on_screen(_box(view, 'Save Reports To')) == str(tmp_path / 'my reports')


def test_switches_site_names_and_save_folder_come_back(tmp_path):
    view = _sr_back(_sr_set_up(tmp_path))
    _sr_kept(view, tmp_path)
    # ...and stay on the next runs, which the browser's boxes now feed.
    view.run()
    _sr_kept(view, tmp_path)
    _open(_open(view, 'Viewer'), 'Splice Report')
    _sr_kept(view, tmp_path)


def test_every_switch_is_written_in_the_run_that_draws_it(tmp_path):
    """A switch left ON shows ON too, and the run that draws each switch is
    the run that tells the browser its value."""
    view = _sr_back(_sr_set_up(tmp_path))
    for t in view.toggle:
        if (t.key or '').startswith('sr_show_'):
            assert t.proto.set_value, t.key
            assert t.proto.value == (t.key != 'sr_show_break'), t.key


def test_a_second_click_from_the_new_session_keeps_them_too(tmp_path):
    """The session the first link started files its own state under the
    same carry id, so a second click keeps what the first one brought."""
    view = _sr_back(_sr_back(_sr_set_up(tmp_path)))
    _sr_kept(view, tmp_path)


def test_an_added_spans_folders_and_site_names_come_back(tmp_path):
    at = _click(_open(_hub(), 'Splice Report'), '➕ Add Span…')
    at.text_input(key='sr2_dir_a').input(B).run()
    at.text_input(key='sr2_dir_b').input(A).run()
    at.text_input(key='sr2_site_a').input('HUT-C').run()
    at.text_input(key='sr2_site_b').input('HUT-D').run()
    assert not at.exception, at.exception
    view = _sr_back(at)
    assert view.session_state['sr_n_spans'] == 2
    for key, want in (('sr2_dir_a', B), ('sr2_dir_b', A),
                      ('sr2_site_a', 'HUT-C'), ('sr2_site_b', 'HUT-D')):
        assert _on_screen(view.text_input(key=key)) == want, key


def test_a_removed_span_does_not_come_back(tmp_path):
    at = _click(_open(_hub(), 'Splice Report'), '➕ Add Span…')
    at.text_input(key='sr2_dir_a').input(B).run()
    at.text_input(key='sr2_site_a').input('HUT-C').run()
    _click(at, '✖ Remove Span 2')
    view = _sr_back(at)
    assert _n_spans(view) == 1
    _click(view, '➕ Add Span…')
    assert _on_screen(view.text_input(key='sr2_dir_a')) == ''
    assert _on_screen(view.text_input(key='sr2_site_a')) == 'A'


def test_cleared_traces_do_not_come_back_through_the_link(tmp_path):
    """Clear Traces drops what the boxes kept; the link carries the state
    after the clear, so none of it rides back in.  Where reports go and what
    the report shows are the tech's choices, not the span's: they stay."""
    at = _click(_sr_set_up(tmp_path), '➕ Add Span…')
    at.text_input(key='sr2_dir_a').input(B).run()
    _open(at, 'Viewer')
    clear_traces(at, allow=True)
    assert not at.exception, at.exception
    # The span is loaded again, and its cell clicked.
    load_traces(at, a=A)
    load_traces(at, b=B)
    view = _sr_back(at)
    assert _n_spans(view) == 1
    assert 'HUT-A' not in (_on_screen(_box(view, SITE_A)), _on_screen(_box(view, SITE_B)))
    assert _on_screen(view.toggle(key='sr_show_break')) is False
    assert _on_screen(_box(view, 'Save Reports To')) == str(tmp_path / 'my reports')


def test_the_one_folder_choice_and_box_come_back(tmp_path):
    """Left panel empty, the span in one folder: the link carries the split
    folders, the panel's empty boxes come back on Back, and the page must
    still know it was reading one folder."""
    at = _open(_hub('', ''), 'Splice Report')
    at.radio(key='sr_input_mode').set_value(ONE).run()
    at.text_input(key='sr_one_folder').input(A).run()
    assert not at.exception, at.exception
    view = _cell_click_and_back(at, 'Splice Report', 'sr', dir='both',
                                sra=A, srb=B, pa='', pb='')
    assert _on_screen(view.radio(key='sr_input_mode')) == 1       # ONE's index
    assert view.radio(key='sr_input_mode').value == ONE
    assert _on_screen(view.text_input(key='sr_one_folder')) == A


# ── Unidirectional ────────────────────────────────────────────────────────

def test_unidirectional_switches_site_names_and_landmarks_come_back(tmp_path):
    at = _open(_hub(), 'Unidirectional')
    at.toggle(key='uni_show_break').set_value(False).run()
    _box(at, UNI_A).input('HUT-A').run()
    _box(at, UNI_B).input('HUT-B').run()
    at.text_area(key='uni_landmarks_text').input('4.05, HH8').run()
    assert not at.exception, at.exception
    view = _cell_click_and_back(at, 'Unidirectional', 'uni', dir='a', sra=A,
                                pside='a')
    brk = view.toggle(key='uni_show_break')
    assert brk.value is False and _on_screen(brk) is False
    assert (_on_screen(_box(view, UNI_A)), _on_screen(_box(view, UNI_B))) \
        == ('HUT-A', 'HUT-B')
    assert _on_screen(view.text_area(key='uni_landmarks_text')) == '4.05, HH8'
    view.run()
    assert view.session_state['uni_site_a'] == 'HUT-A'
    assert view.session_state['uni_landmarks_text'] == '4.05, HH8'

"""A report run on a box holding a .zip says so when the zip changes
(Robert 2026-10-02).

A box holding a .zip (or a folder read from the zips in it) is read from an
extracted copy, and a new zip gives a new copy.  The copy a report ran on
never changes, so the record of its folders (test_report_folder_changed.py)
could not see a zip replaced, written over or downloaded again under the same
name.  Worse, the report filed under the old copy was not found again: the
Unidirectional, Secret Sauce and FEC pages dropped it with nothing said, and
the Splice Report never brought a report on zip boxes back in a new session.

Now the record also signs the zip as typed in the box, and a report from zip
boxes is filed under the boxes too.  The page finds it there after the zip
changes and says "The A .zip changed since this report was made. ..."
"""
from __future__ import annotations

import os
import zipfile

import pytest

from conftest import (run_streamlit, finish_engine_run, go_tab, load_traces,
                      clear_traces, FIXTURE_SPLICE_A_DIR,
                      FIXTURE_SPLICE_B_DIR)
from test_report_folder_changed import _helpers


def _zip(path, src, fibers):
    """A .zip of the fixture's shots for `fibers`, written whole each time
    (a zip downloaded again under the same name)."""
    tmp = str(path) + '.part'
    with zipfile.ZipFile(tmp, 'w') as z:
        for p in sorted(src.glob('*.sor')):
            if int(p.name[6:10]) in fibers:
                z.write(p, p.name)
    os.replace(tmp, path)
    return str(path)


def _span(tmp_path, fibers):
    return (_zip(tmp_path / 'A.zip', FIXTURE_SPLICE_A_DIR, fibers),
            _zip(tmp_path / 'B.zip', FIXTURE_SPLICE_B_DIR, fibers))


@pytest.fixture
def _own_cache(tmp_path, monkeypatch):
    monkeypatch.setenv('OTDR_CACHE_DIR', str(tmp_path / 'cache'))
    monkeypatch.setenv('OTDR_SETTINGS_DIR', str(tmp_path / 'settings'))


# ── the signature and the words ───────────────────────────────────────────
def test_a_zip_is_signed_as_typed_and_moves_when_it_is_replaced(tmp_path):
    ns = _helpers()
    sig = ns['_box_zip_sig']
    za, _zb = _span(tmp_path, set(range(1, 5)))
    first = sig(za)
    assert first[:2] == [za, 'zip']
    assert sig(f' "{za}" ') == first                  # as the box may hold it
    _zip(za, FIXTURE_SPLICE_A_DIR, set(range(1, 7)))
    assert sig(za)[2] != first[2]
    # A folder read from its zips: a zip in it replaced moves it too.
    zips = tmp_path / 'zips'
    zips.mkdir()
    inner = _zip(zips / 'A.zip', FIXTURE_SPLICE_A_DIR, set(range(1, 5)))
    got = sig(str(zips))
    assert got[:2] == [str(zips), 'zips']
    _zip(inner, FIXTURE_SPLICE_A_DIR, set(range(1, 7)))
    assert sig(str(zips))[2] != got[2]
    # A plain folder, a folder of traces, nothing: not a zip box.
    plain = tmp_path / 'plain'
    plain.mkdir()
    for p in sorted(FIXTURE_SPLICE_A_DIR.glob('*.sor'))[:3]:
        (plain / p.name).write_bytes(p.read_bytes())
    assert sig(str(plain)) is None and sig('') is None and sig(None) is None


def _text(rec, now, again='Generate the report again'):
    ns = _helpers(_box_zip_sig=lambda raw: now.get(raw),
                  _trace_files_sig=lambda f, *a: ('same', 12))
    ns['os'] = type('os', (), {'path': type('p', (), {'isdir': staticmethod(lambda f: True)})})
    return ns['_folders_changed_text'](rec, again)


def test_the_words_for_a_zip():
    rec = [['/x/a', 'A', 'same', 12, ['/z/A.zip', 'zip', '1']],
           ['/x/b', 'B', 'same', 12, ['/z/B.zip', 'zip', '1']]]
    same = {'/z/A.zip': ['/z/A.zip', 'zip', '1'], '/z/B.zip': ['/z/B.zip', 'zip', '1']}
    assert _text(rec, same) is None
    assert _text(rec, {**same, '/z/A.zip': ['/z/A.zip', 'zip', '2']}) == (
        'The A .zip changed since this report was made. Generate the report '
        'again to read it.')
    assert _text(rec, {'/z/A.zip': ['/z/A.zip', 'zip', '2'],
                       '/z/B.zip': ['/z/B.zip', 'zip', '2']}) == (
        'The A and B .zip files changed since this report was made. Generate '
        'the report again to read them.')
    folder = [['/x/a', 'A', 'same', 12, ['/z/A', 'zips', '1']]]
    assert _text(folder, {'/z/A': ['/z/A', 'zips', '2']},
                 again='Run the report again') == (
        'The zips in the A folder changed since this report was made. Run the '
        'report again to read them.')
    # Gone from the box, or a record from before the zip was signed: nothing.
    assert _text(rec, {}) is None
    assert _text([r[:4] for r in rec], {'/z/A.zip': ['/z/A.zip', 'zip', '2']}) is None
    for w in (_text(rec, {**same, '/z/A.zip': ['/z/A.zip', 'zip', '2']}),
              _text(folder, {'/z/A': ['/z/A', 'zips', '2']})):
        assert '—' not in w and '–' not in w


# ── the pages, end to end ─────────────────────────────────────────────────
def _hub(a, b):
    at = run_streamlit(default_timeout=180).run()
    load_traces(at, a, b or None)          # the top bar's Traces tab
    assert not at.exception, at.exception
    return at


def _open(at, page):
    go_tab(at, page)
    assert not at.exception, at.exception
    return at


def _run(at, label, prefix):
    next(x for x in at.main.button if x.label == label).click().run()
    finish_engine_run(at, prefix)
    assert not at.exception, at.exception
    at.run()
    assert not at.exception, at.exception
    return at


def _notes(at):
    return [w.value for w in at.warning if 'changed since this report' in w.value]


def _sr_line(at):
    return [s.value for s in at.success if '·' in s.value and 'fiber' in s.value]


def test_splice_report_on_zips(tmp_path, _own_cache):
    za, zb = _span(tmp_path, set(range(1, 13)))
    at = _hub(za, zb)
    at.session_state['sr_report_dest'] = str(tmp_path)
    _open(at, 'Splice Report')
    _run(at, 'Generate Splice Report', 'sr')
    assert '  ·  12 fibers  ·  ' in _sr_line(at)[0]
    assert _notes(at) == []
    # A new session finds it under the boxes; the zips are as they were.
    fresh = _open(_hub(za, zb), 'Splice Report')
    assert '  ·  12 fibers  ·  ' in _sr_line(fresh)[0]
    assert _notes(fresh) == []

    _zip(za, FIXTURE_SPLICE_A_DIR, set(range(1, 25)))     # A downloaded again
    at.run()
    assert not at.exception, at.exception
    assert '  ·  12 fibers  ·  ' in _sr_line(at)[0]
    assert _notes(at) == ['The A .zip changed since this report was made. '
                          'Generate the report again to read it.']
    fresh = _open(_hub(za, zb), 'Splice Report')
    assert '  ·  12 fibers  ·  ' in _sr_line(fresh)[0]
    assert len(_notes(fresh)) == 1


def test_unidirectional_on_a_zip(tmp_path, _own_cache):
    za, zb = _span(tmp_path, set(range(1, 13)))
    at = _hub(za, zb)
    at.session_state['uni_report_dest'] = str(tmp_path)
    _open(at, 'Unidirectional')
    _run(at, 'Run Unidirectional Report', 'uni')
    assert [s.value for s in at.success if s.value.startswith('Done: 12 fibers')]
    assert _notes(at) == []
    _zip(za, FIXTURE_SPLICE_A_DIR, set(range(1, 17)))
    at.run()
    assert not at.exception, at.exception
    # The report stays, on the folder it ran on, and says the zip changed.
    assert [s.value for s in at.success if s.value.startswith('Done: 12 fibers')]
    assert _notes(at) == ['The A .zip changed since this report was made. '
                          'Run the report again to read it.']
    fresh = _open(_hub(za, zb), 'Unidirectional')
    assert [s.value for s in fresh.success if s.value.startswith('Done: 12 fibers')]
    assert len(_notes(fresh)) == 1
    # Run On B: not the A report.
    side = next(r for r in fresh.radio if r.label == 'Run On')
    side.set_value('B folder').run()
    assert not fresh.exception, fresh.exception
    assert not [s.value for s in fresh.success if s.value.startswith('Done:')]


def test_secret_sauce_on_zips(tmp_path, _own_cache):
    za, zb = _span(tmp_path, set(range(1, 13)))
    at = _hub(za, zb)
    at.session_state['ss_report_dest'] = str(tmp_path / 'ss')
    _open(at, 'Secret Sauce')
    _run(at, 'Run Analysis', 'ss')
    assert at.session_state['ss_result'].get('ok')
    assert _notes(at) == []
    _zip(zb, FIXTURE_SPLICE_B_DIR, set(range(1, 15)))
    at.run()
    assert not at.exception, at.exception
    assert [s.value for s in at.success if s.value.startswith('Done:')]
    assert _notes(at) == ['The B .zip changed since this report was made. '
                          'Run the analysis again to read it.']
    fresh = _open(_hub(za, zb), 'Secret Sauce')
    assert [s.value for s in fresh.success if s.value.startswith('Done:')]
    assert len(_notes(fresh)) == 1


def test_splice_report_fec_on_a_zip(tmp_path, _own_cache):
    za, _zb = _span(tmp_path, set(range(1, 5)))
    at = run_streamlit(default_timeout=180).run()
    at.session_state['fec_dir_a'] = za
    at.session_state['fec_report_dest'] = str(tmp_path)
    _open(at, 'Splice Report FEC')
    _run(at, 'Run FEC Report', 'fec')
    assert at.session_state['fec_result'].get('ok')
    assert _notes(at) == []
    _zip(za, FIXTURE_SPLICE_A_DIR, set(range(1, 7)))
    at.run()
    assert not at.exception, at.exception
    assert [s.value for s in at.success if s.value.startswith('Done:')]
    assert _notes(at) == ['The A end .zip changed since this report was made. '
                          'Run the report again to read it.']


# ── a cleared report stays cleared ────────────────────────────────────────
def _clear(at, *answers):
    for label in ('Clear Report',) + answers:
        next(b for b in at.button if b.label == label).click().run()
        assert not at.exception, at.exception


@pytest.mark.parametrize('page,label,prefix', [
    ('Splice Report', 'Generate Splice Report', 'sr'),
    ('Unidirectional', 'Run Unidirectional Report', 'uni'),
    ('Secret Sauce', 'Run Analysis', 'ss')])
def test_clear_report_takes_the_copy_filed_under_the_zips(tmp_path, _own_cache,
                                                          page, label, prefix):
    za, zb = _span(tmp_path, set(range(1, 13)))
    at = _hub(za, zb)
    at.session_state[f'{prefix}_report_dest'] = str(tmp_path / prefix)
    _open(at, page)
    _run(at, label, prefix)
    _clear(at, 'Clear Report Only')
    fresh = _open(_hub(za, zb), page)
    assert not [b for b in fresh.button if b.label == 'Clear Report']


def test_clear_traces_takes_the_fec_copy_filed_under_the_zip(tmp_path, _own_cache):
    za, _zb = _span(tmp_path, set(range(1, 5)))
    at = run_streamlit(default_timeout=180).run()
    at.session_state['fec_dir_a'] = za
    at.session_state['fec_report_dest'] = str(tmp_path)
    _open(at, 'Splice Report FEC')
    _run(at, 'Run FEC Report', 'fec')
    _zip(za, FIXTURE_SPLICE_A_DIR, set(range(1, 7)))   # read from a new folder now
    at.run()
    assert [s.value for s in at.success if s.value.startswith('Done:')]
    clear_traces(at)                     # on the Traces tab
    assert not at.exception, at.exception
    fresh = run_streamlit(default_timeout=180).run()
    fresh.session_state['fec_dir_a'] = za
    _open(fresh, 'Splice Report FEC')
    assert not [s.value for s in fresh.success if s.value.startswith('Done:')]

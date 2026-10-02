"""The Unidirectional page's own upload (Robert 2026-10-01).

An upload that holds BOTH directions of a span (the A shots and the B shots
together, loose or in one zip) used to run A alone and print a red PARTIAL
COVERAGE error for the B files, with no say in it.  Now the page asks which
direction to run, A by default, and runs only that one: the side not picked
is not reported as missing.  Which files are A and which are B comes from the
files' own direction stamps, the way the left panel splits such a folder.

The uploader used to add every new drop to the files it held; a new upload
now replaces the one before.

AppTest cannot drop files on an uploader, so st.file_uploader is replaced by
one that hands the page what a drop would: uploaded files with a name, a size
and an upload id, for the uploader key they were dropped on.
"""
from __future__ import annotations

import io
import os
import shutil
import zipfile

import pytest
import streamlit

from conftest import (run_streamlit, finish_engine_run, FIXTURE_SPLICE_A_DIR,
                      FIXTURE_SPLICE_B_DIR)

A, B = FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR


def _pair(folder):
    import folder_intake as fi
    return fi.sor_location_pair(str(sorted(folder.glob('*.sor'))[0]))


# The two ends as each direction's files store them (one pair, A end first),
# in the order each direction runs.
_A_PAIR, _B_PAIR = _pair(A), _pair(B)
A_TO_B = f'{_A_PAIR[0]} → {_A_PAIR[1]}'
B_TO_A = f'{_B_PAIR[1]} → {_B_PAIR[0]}'


class _Upload(io.BytesIO):
    """What st.file_uploader hands the page for one dropped file."""
    _n = 0

    def __init__(self, name, data):
        super().__init__(data)
        _Upload._n += 1
        self.name, self.size = name, len(data)
        self.file_id = f'test-upload-{_Upload._n}'


def _files(folder, fibers, rename=None):
    out = []
    for p in sorted(folder.glob('*.sor')):
        if int(p.name[6:10]) in fibers:
            out.append(_Upload(rename(p.name) if rename else p.name, p.read_bytes()))
    return out


def _zip(members):
    """One uploaded .zip holding {folder in the zip: [uploads]}."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for sub, ups in members.items():
            for u in ups:
                zf.writestr(f'{sub}/{u.name}', u.getvalue())
    return _Upload('both directions.zip', buf.getvalue())


@pytest.fixture
def drops(monkeypatch, tmp_path):
    """{uploader key: [uploads]}: what the next run finds dropped on it.  The
    real uploader keeps what was dropped on it until the tech removes it, so
    a key hands its files back on every run."""
    monkeypatch.setenv('OTDR_CACHE_DIR', str(tmp_path / 'cache'))
    pending = {}
    real = streamlit.file_uploader

    def fake(label, *args, key=None, **kwargs):
        if key and str(key).startswith('uni_drop'):
            return pending.get(key) or None
        return real(label, *args, key=key, **kwargs)

    monkeypatch.setattr(streamlit, 'file_uploader', fake)
    return pending


def _uni_page(tmp_path):
    at = run_streamlit(default_timeout=180).run()
    at.session_state['uni_report_dest'] = str(tmp_path)
    at.sidebar.radio[0].set_value('Unidirectional').run()
    assert not at.exception, at.exception
    return at


def _drop(at, drops, files):
    """Drop `files` on the uploader the page draws now, and run the page."""
    key = f"uni_drop_{at.session_state['uni_drop_gen'] if 'uni_drop_gen' in at.session_state else 0}"
    drops.clear()
    drops[key] = files
    at.run()
    assert not at.exception, at.exception
    return at


def _run_on(at):
    next(b for b in at.main.button if b.label == 'Run Unidirectional Report').click().run()
    finish_engine_run(at, 'uni')
    assert not at.exception, at.exception
    return at


def _run_on_radio(at):
    return next((r for r in at.main.radio if r.label == 'Run On'), None)


def _errors(at):
    return ' '.join(e.value for e in at.error)


def test_a_mixed_upload_offers_the_direction_and_runs_b_when_b_is_picked(tmp_path, drops):
    at = _uni_page(tmp_path)
    _drop(at, drops, _files(A, {1, 2, 3}) + _files(B, {1, 2, 3}))
    side = _run_on_radio(at)
    assert side is not None, [r.label for r in at.main.radio]
    assert side.value == side.options[0]                       # A by default
    assert side.options[0].startswith(f'A Direction ({A_TO_B}, 3 files')
    assert side.options[1].startswith(f'B Direction ({B_TO_A}, 3 files')
    side.set_value(side.options[1]).run()
    _run_on(at)
    ran = at.session_state['uni_result']['_folder']
    assert sorted(os.listdir(ran)) == sorted(p.name for p in _files(B, {1, 2, 3}))
    done = [s.value for s in at.success if s.value.startswith('Done:')]
    assert done and f'direction {B_TO_A}' in done[0], done
    # The A files are not "missing": they were not asked for.
    assert 'PARTIAL COVERAGE' not in _errors(at)
    assert 'INCOMPLETE COVERAGE' not in _errors(at)
    assert 'mixes' not in _errors(at)


def test_a_mixed_upload_runs_a_by_default_with_no_coverage_error(tmp_path, drops):
    at = _uni_page(tmp_path)
    _drop(at, drops, _files(A, {1, 2, 3}) + _files(B, {1, 2, 3}))
    _run_on(at)
    ran = at.session_state['uni_result']['_folder']
    assert sorted(os.listdir(ran)) == sorted(p.name for p in _files(A, {1, 2, 3}))
    done = [s.value for s in at.success if s.value.startswith('Done:')]
    assert done and f'direction {A_TO_B}' in done[0], done
    assert 'COVERAGE' not in _errors(at)


def test_the_sides_come_from_the_files_not_the_names(tmp_path, drops):
    """B's files renamed so their name sorts first: the stamps still say B."""
    at = _uni_page(tmp_path)
    _drop(at, drops, _files(A, {1, 2, 3})
          + _files(B, {1, 2, 3}, rename=lambda n: 'AAAZZZ' + n[6:]))
    side = _run_on_radio(at)
    assert side.options[0].startswith(f'A Direction ({A_TO_B}')
    assert side.options[1].startswith(f'B Direction ({B_TO_A}')


def test_a_zip_holding_both_directions_offers_the_direction(tmp_path, drops):
    at = _uni_page(tmp_path)
    _drop(at, drops, [_zip({'A side': _files(A, {1, 2, 3}),
                            'B side': _files(B, {1, 2, 3})})])
    side = _run_on_radio(at)
    assert side is not None
    assert '3 files' in side.options[0] and '3 files' in side.options[1]
    side.set_value(side.options[1]).run()
    _run_on(at)
    ran = at.session_state['uni_result']['_folder']
    assert sorted(os.listdir(ran)) == sorted(p.name for p in _files(B, {1, 2, 3}))
    assert 'COVERAGE' not in _errors(at)


def test_a_one_direction_upload_runs_as_before(tmp_path, drops):
    at = _uni_page(tmp_path)
    _drop(at, drops, _files(B, {1, 2, 3}))
    assert _run_on_radio(at) is None
    _run_on(at)
    ran = at.session_state['uni_result']['_folder']
    assert ran == os.path.abspath(at.session_state['uni_upload']['dir'])
    assert sorted(os.listdir(ran)) == sorted(p.name for p in _files(B, {1, 2, 3}))


def test_strays_from_another_job_are_not_a_second_direction(tmp_path, drops):
    """One direction with two files of another job dropped in: the strays
    are left out (the foreign-file audit), and nothing asks for a direction."""
    from conftest import FIXTURE_DIR
    strays = [_Upload(p.name, p.read_bytes())
              for p in sorted((FIXTURE_DIR / 'frspan').glob('*.sor'))]
    assert len(strays) == 2
    at = _uni_page(tmp_path)
    _drop(at, drops, _files(A, set(range(1, 25))) + strays)
    assert _run_on_radio(at) is None
    assert 'EXCLUDED' in ' '.join(w.value for w in at.warning)


def test_a_second_upload_replaces_the_first(tmp_path, drops):
    at = _uni_page(tmp_path)
    _drop(at, drops, _files(A, {1, 2, 3}))
    first = at.session_state['uni_upload']['dir']
    assert sorted(os.listdir(first)) == sorted(p.name for p in _files(A, {1, 2, 3}))
    # The uploader is drawn again, empty, under a new key: the next drop
    # lands on its own instead of being added to the first.
    assert at.session_state['uni_drop_gen'] == 1
    _drop(at, drops, _files(B, {4, 5}))
    second = at.session_state['uni_upload']['dir']
    assert second != first
    assert sorted(os.listdir(second)) == sorted(p.name for p in _files(B, {4, 5}))
    assert at.session_state['uni_upload']['n'] == 2
    assert _run_on_radio(at) is None          # B alone: nothing to pick
    caption = ' '.join(c.value for c in at.main.caption)
    assert '2 trace file(s) from the upload' in caption


def test_typing_a_folder_forgets_the_upload(tmp_path, drops):
    at = _uni_page(tmp_path)
    _drop(at, drops, _files(A, {1, 2, 3}))
    drops.clear()
    folder = tmp_path / 'typed'
    shutil.copytree(B, folder)
    next(t for t in at.main.text_input if 'Paste a Folder' in t.label).input(str(folder)).run()
    assert 'uni_upload' not in at.session_state
    _run_on(at)
    assert at.session_state['uni_result']['_folder'] == str(folder)


def test_clear_upload_forgets_it(tmp_path, drops):
    at = _uni_page(tmp_path)
    _drop(at, drops, _files(A, {1, 2, 3}))
    drops.clear()
    next(b for b in at.main.button if b.label == 'Clear Upload').click().run()
    assert 'uni_upload' not in at.session_state
    assert not [b for b in at.main.button if b.label == 'Clear Upload']

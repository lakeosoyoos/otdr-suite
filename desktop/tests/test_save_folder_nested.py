"""'Save Reports To' takes a new folder any number of levels deep (2026-10-02).

Every report page's save row (app._report_dest_row) only checked that the
folder ABOVE the typed one was there.  A tech who typed a new job folder two
levels down (.../Reports/Job 1, neither there yet) was told "That folder
cannot be created" and the report went to Downloads, though the folder could
be made.  Now the row walks up to the nearest folder that is there and takes
the path when that folder can be written in; every run makes the whole
missing path before it writes.  A path that really cannot be made (a file in
the way, a folder that cannot be written in, a drive that is not there, a
name Windows refuses) is still refused, the report still goes to the
default folder, and the warning says why.
"""
from __future__ import annotations

import ntpath
import os
import sys
import types

import pytest

from conftest import run_streamlit, finish_engine_run, FIXTURE_SPLICE_A_DIR


def _hub():
    import app as hub
    return hub


# ── The check itself ──────────────────────────────────────────────────────

def test_a_new_folder_two_levels_deep_is_taken_and_not_made(tmp_path):
    hub = _hub()
    want = tmp_path / 'Reports' / 'Job 1'
    assert hub._report_dir_problem(str(want)) is None
    assert hub._report_dir_problem(str(tmp_path / 'a' / 'b' / 'c' / 'd')) is None
    # Checking is not making: the folder is made when a report is written.
    assert not (tmp_path / 'Reports').exists()


def test_a_folder_that_is_there_is_taken(tmp_path):
    assert _hub()._report_dir_problem(str(tmp_path)) is None


def test_a_path_under_a_file_is_refused(tmp_path):
    f = tmp_path / 'notes.txt'
    f.write_text('x')
    problem = _hub()._report_dir_problem(str(f / 'Reports' / 'Job 1'))
    assert problem == f'{f} is a file, not a folder'
    # A file where the folder should be, too.
    assert _hub()._report_dir_problem(str(f)) == f'{f} is a file, not a folder'


@pytest.mark.skipif(sys.platform == 'win32' or getattr(os, 'geteuid', lambda: 1)() == 0,
                    reason='folder permissions: POSIX, not as root')
def test_a_folder_under_one_that_cannot_be_written_in_is_refused(tmp_path):
    locked = tmp_path / 'locked'
    locked.mkdir()
    locked.chmod(0o555)
    try:
        problem = _hub()._report_dir_problem(str(locked / 'Reports' / 'Job 1'))
    finally:
        locked.chmod(0o755)
    assert problem == f'there is no permission to make a folder in {locked}'


def test_windows_paths_a_missing_drive_or_share_is_refused():
    hub = _hub()
    # ntpath on any machine: no Q: drive and no such share here.
    for p in ('Q:\\Jobs\\Job 1', '\\\\server\\share\\Jobs\\Job 1'):
        assert (hub._report_dir_problem(p, pathmod=ntpath)
                == 'that drive or network share cannot be reached'), p


def _windows_with(there):
    """ntpath, with the folders in `there` the only ones on the machine."""
    return types.SimpleNamespace(
        abspath=ntpath.abspath, dirname=ntpath.dirname,
        basename=ntpath.basename, sep=ntpath.sep,
        isdir=lambda p: p in there, exists=lambda p: p in there)


def test_windows_paths_names_windows_refuses_are_refused(monkeypatch):
    hub = _hub()
    win = _windows_with({'C:\\', 'C:\\Jobs'})
    monkeypatch.setattr(os, 'access', lambda p, mode: True)
    assert hub._report_dir_problem('C:\\Jobs\\Job 1\\Reports', pathmod=win) is None
    assert (hub._report_dir_problem('C:\\Jobs\\Job?1\\Reports', pathmod=win)
            == 'a folder name cannot contain < > : " | ? *')
    assert (hub._report_dir_problem('C:\\Jobs\\CON\\Reports', pathmod=win)
            == '"CON" is a name Windows keeps for itself')
    assert (hub._report_dir_problem('C:\\Jobs\\New\\aux.txt', pathmod=win)
            == '"aux.txt" is a name Windows keeps for itself')


# ── On the page ───────────────────────────────────────────────────────────

@pytest.fixture
def downloads(tmp_path, monkeypatch):
    """The default folder for this test: never the tech's Downloads."""
    monkeypatch.setenv('OTDR_CACHE_DIR', str(tmp_path / 'cache'))
    monkeypatch.setenv('OTDR_SETTINGS_DIR', str(tmp_path / 'settings'))
    d = tmp_path / 'downloads'
    d.mkdir()
    import folder_intake
    monkeypatch.setattr(folder_intake, 'default_report_dir', lambda: str(d))
    return d


def _uni_page(dest):
    at = run_streamlit(default_timeout=180).run()
    next(t for t in at.sidebar.text_input
         if t.label == 'A Folder').input(str(FIXTURE_SPLICE_A_DIR)).run()
    at.session_state['uni_report_dest'] = str(dest)
    at.sidebar.radio[0].set_value('Unidirectional').run()
    assert not at.exception, at.exception
    return at


def _save_warnings(at):
    return [w.value for w in at.warning if 'Reports cannot be saved' in w.value
            or 'cannot be created' in w.value]


def test_uni_report_is_written_into_a_new_folder_two_levels_deep(tmp_path, downloads):
    dest = tmp_path / 'Reports' / 'Job 1'
    at = _uni_page(dest)
    assert _save_warnings(at) == []
    assert not (tmp_path / 'Reports').exists()        # typing made nothing
    next(b for b in at.main.button
         if b.label == 'Run Unidirectional Report').click().run()
    finish_engine_run(at, 'uni')
    assert not at.exception, at.exception
    res = at.session_state['uni_result']
    assert res.get('ok'), res.get('error')
    assert os.path.dirname(res['out']) == str(dest)
    assert [f for f in os.listdir(dest) if f.endswith('.xlsx')]
    assert not [f for f in os.listdir(downloads) if f.endswith('.xlsx')]


def test_uni_path_under_a_file_still_warns_and_goes_to_the_default(tmp_path, downloads):
    f = tmp_path / 'notes.txt'
    f.write_text('x')
    dest = f / 'Job 1'
    at = _uni_page(dest)
    assert _save_warnings(at) == [
        f'Reports cannot be saved to {dest}: {f} is a file, not a folder. '
        f'They will go to {downloads} instead.']


def test_fqa_builder_makes_the_folder_before_it_writes():
    """The FQA Builder borrows the same save row; its build wrote into the
    folder without making it, so any folder not there yet failed."""
    from conftest import REPO_ROOT
    src = (REPO_ROOT / 'fqa' / 'ui.py').read_text(encoding='utf-8')
    body = src.split("out_path = os.path.join(out_dir, out_name)", 1)[1]
    assert body.index('os.makedirs(out_dir, exist_ok=True)') < body.index('build(')

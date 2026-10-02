"""A Viewer Remove reaches the reports (Robert 2026-10-01).

"Should a Viewer Remove also take those files out of the Splice Report (and
Unidirectional and Duplicate Check)?  Yes."  And for the Viewer's own table:
"it might be safer to rerun the entire span in the event panel".

  trace_server.removed_names(folder)   the files of `folder` removed in the
                                       Viewer, for the folders loaded now only
  trace_server.without_removed(folder) the folder, or a copy without them
  every run                            the Viewer's own (both directions and
                                       one), the Splice Report, Unidirectional
                                       and Duplicate Check run on that copy, so
                                       a Remove runs the whole span again
  a report's table                     counts only if it was made on the same
                                       copy (a report run before the Remove is
                                       not the table on screen any more)
"""
from __future__ import annotations

import os
import re

import pytest

from conftest import APP_PATH, VIEWER_DIR, import_trace_server

TS = import_trace_server()
APP = APP_PATH.read_text(encoding='utf-8')
SRC = (VIEWER_DIR / 'viewer.html').read_text(encoding='utf-8')


def _span(tmp_path):
    a, b = tmp_path / 'A Side', tmp_path / 'B Side'
    a.mkdir()
    b.mkdir()
    for n in (1, 2, 3):
        (a / f'SPAN.A.1550.{n:04d}.sor').write_bytes(b'a%d' % n)
        (b / f'SPAN.B.1550.{n:04d}.sor').write_bytes(b'b%d' % n)
    (a / 'notes.txt').write_text('kept', encoding='utf-8')
    return str(a), str(b)


@pytest.fixture
def span(tmp_path, monkeypatch):
    a, b = _span(tmp_path)
    monkeypatch.setitem(TS.CONFIG, 'dir_a', a)
    monkeypatch.setitem(TS.CONFIG, 'dir_b', b)
    monkeypatch.setattr(TS, 'list_fibers', lambda d: [
        (int(f[-8:-4]), os.path.join(d, f)) for f in sorted(os.listdir(d)) if f.endswith('.sor')])
    TS._REMOVED_STAGE.clear()
    yield a, b
    TS.set_viewer_state({'keys': [], 'removed': []})


def test_nothing_removed_runs_the_folder_itself(span):
    a, b = span
    TS.set_viewer_state({'keys': ['a-1'], 'removed': []})
    assert TS.removed_names(a) == [] and TS.without_removed(a) == a


def test_a_removed_file_is_left_out_of_a_copy_named_like_the_folder(span):
    a, b = span
    TS.set_viewer_state({'keys': [], 'removed': ['a-2', 'b-3']})
    assert TS.removed_names(a) == ['SPAN.A.1550.0002.sor']
    assert TS.removed_names(b) == ['SPAN.B.1550.0003.sor']
    ca = TS.without_removed(a)
    assert ca != a and os.path.basename(ca) == 'A Side'
    assert sorted(os.listdir(ca)) == ['SPAN.A.1550.0001.sor', 'SPAN.A.1550.0003.sor', 'notes.txt']
    assert open(os.path.join(ca, 'SPAN.A.1550.0001.sor'), 'rb').read() == b'a1'
    assert TS.without_removed(a) == ca              # made once
    assert sorted(os.listdir(a)) == ['SPAN.A.1550.0001.sor', 'SPAN.A.1550.0002.sor',
                                     'SPAN.A.1550.0003.sor', 'notes.txt']   # untouched
    # put back: the folder itself again
    TS.set_viewer_state({'keys': [], 'removed': []})
    assert TS.without_removed(a) == a


def test_a_state_from_other_folders_removes_nothing(span, monkeypatch):
    a, b = span
    TS.set_viewer_state({'keys': [], 'removed': ['a-2']})
    monkeypatch.setitem(TS.CONFIG, 'dir_b', b + ' other')
    assert TS.removed_names(a) == [] and TS.without_removed(a) == a
    assert TS.removed_names('/somewhere/else') == []


def test_the_viewers_own_runs_use_the_copy(span, monkeypatch):
    a, b = span
    monkeypatch.setattr(TS, '_folder_sig', lambda d: (len(os.listdir(d)),))
    k0 = TS._end_verdict_key()
    assert (k0[1], k0[3]) == (a, b)
    TS.set_viewer_state({'keys': [], 'removed': ['a-2']})
    k1 = TS._end_verdict_key()
    assert k1 != k0 and k1[1] == TS.without_removed(a) and k1[3] == b
    one = TS.__dict__['_one_direction_table']
    assert 'folder = without_removed(folder)' in _src(one)
    rep = _src(TS._report_suite_table)
    assert rep.index('without_removed(a)') < rep.index("_same_folder(table.get('dir_a'), a)")


def _src(fn):
    import inspect
    return inspect.getsource(fn)


def test_every_report_page_runs_on_the_copy_and_says_so():
    assert "'cmd': splicereport_cmd(_run_folder(_da), _run_folder(_db)," in APP
    assert "uni_cmd(_run_folder(folder), out_xlsx," in APP
    assert "_take_panel_ss_folder(_run_folder(_pa)," in APP
    assert "folder = _run_folder(_pa or _pb)" in APP
    assert APP.count('_viewer_removed_note(') == 4          # def + SR, Uni, SS
    run = APP[APP.index('def _run_folder('):APP.index('def _viewer_removed_note(')]
    assert 'trace_server.without_removed(folder)' in run and 'return folder' in run


def test_the_viewer_puts_removed_files_back_and_says_where():
    assert 'function putBackRemovedFiles()' in SRC
    assert 'data-putback="1">Put Back ${gRemovedFiles.size} Removed File' in SRC
    assert 'reopen the Viewer to list' not in SRC
    push = SRC[SRC.index('function pushViewerState()'):SRC.index('async function fetchViewerState()')]
    assert 'if (removedMoved && gTraces.length) renderEventTable();' in push

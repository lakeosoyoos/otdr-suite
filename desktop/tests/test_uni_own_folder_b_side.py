"""A Unidirectional report run on B's files from the page's OWN loader (its
folder box, or an upload) opens the Viewer with those files on B, and
"← Back" shows the upload by name, never a temporary folder's path.

Found 2026-10-01 on a 432-fiber span.  With both of the left panel's boxes
set, a run on the B folder already opened the Viewer on B (the link says
which of the panel's folders the report ran on).  A run on the page's own
folder or upload did not: its grid always said `dir=a`, the Viewer was
pointed at the folder as its A slot, and a click opened B's files in the A
slot, listed as A->B.  The side now comes from the files themselves: the
Run On pick of an upload holding both directions (split by the files'
direction stamps), else the stamps of the folder the report ran on, the rule
a drop on the Viewer uses for one direction.

An upload is staged in a temporary folder, and the way back from the Viewer
put that folder's path in the page's folder box.  The box now shows what was
uploaded, for example "Uploaded Files: B Direction (24 files)", and the page
still runs on the staged files, with the report on screen.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
from urllib.parse import parse_qsl

from conftest import (FIXTURE_SPLICE_A_DIR as PA, FIXTURE_SPLICE_B_DIR as PB,
                      finish_engine_run, import_trace_server)
from test_panel_zip_mixed_folder_uni_jump import (A, B, _back, _click, _held, _hub,
                                                   _open, _server, _texts,
                                                   _viewer_url)
from test_uni_upload_both_directions import (_drop, _files, _run_on_radio, _zip,
                                             drops)  # noqa: F401  (fixture)


ALL = set(range(1, 25))       # every fiber: a short span flags no cell


def _run(at, dest):
    at.session_state['uni_report_dest'] = dest
    at.run()
    next(b for b in at.main.button if b.label == 'Run Unidirectional Report').click().run()
    finish_engine_run(at, 'uni')
    assert not at.exception, at.exception
    return at


def _this_tab_links(at):
    """The grid's cell links in "This tab" mode, each as its query params."""
    next(r for r in at.main.radio if r.label == 'Cell Clicks Open In').set_value(
        'This tab (Viewer page)').run()
    hrefs = re.findall(r"href='\?(nav=viewer[^']*)'", _texts(at.main.markdown))
    assert hrefs, 'no cell to click in the grid'
    return [dict(parse_qsl(h, keep_blank_values=True)) for h in hrefs]


def _popout_dirs(at):
    grid = next(f.proto.srcdoc for f in at.get('iframe')
                if "class='vc'" in (f.proto.srcdoc or ''))
    return set(re.findall(r"data-dir='(\w)'", grid))


def _typed(tmp_path, src, fibers=range(1, 25), name='typed'):
    d = tmp_path / name
    d.mkdir()
    for p in sorted(os.listdir(src)):
        if p.lower().endswith('.sor') and int(p[6:10]) in fibers:
            shutil.copy2(os.path.join(src, p), d / p)
    return str(d)


def _uni_box(at):
    """What the page's folder box shows in the browser.  A value written on
    an earlier run (the way back writes it on the Viewer's run) reaches the
    server and not the box: the browser shows only a value sent with the box
    (`set_value`), else the box's default, empty."""
    box = next(t for t in at.main.text_input if 'Paste a Folder' in t.label)
    return box.value if box.proto.set_value else ''


def _server_counts():
    """(A, B) fibers the Viewer's server lists, as the sidebar counted them.
    Read off the server: the Traces tab that counts them now is a trip off
    the Viewer, which puts the tech's own boxes back (_panel_restore)."""
    tv = import_trace_server()
    return tuple(len(tv.list_fibers(d)) if d else 0 for d in _server())


def _done(at):
    return [s.value for s in at.success if s.value.startswith('Done:')]


def _typed_run(tmp_path, src):
    folder = _typed(tmp_path, src)
    at = _open(_hub(), 'Unidirectional')
    next(t for t in at.main.text_input if 'Paste a Folder' in t.label).input(folder).run()
    return folder, _run(at, str(tmp_path))


# ── 1. a B run on the page's own folder or upload opens the Viewer on B ────

def test_a_typed_b_folder_opens_the_viewer_with_its_files_on_b(tmp_path, monkeypatch):
    monkeypatch.setenv('OTDR_CACHE_DIR', str(tmp_path / 'cache'))
    folder, at = _typed_run(tmp_path, B)
    links = _this_tab_links(at)
    assert {q['dir'] for q in links} == {'b'}                  # was {'a'}
    seen = _click(links[0])
    assert '&dir=b' in _viewer_url(seen) and '&src=uni' in _viewer_url(seen)
    assert _server() == (None, folder)                         # was (folder, None)
    assert _held(seen, 'A Folder') == ''
    assert _held(seen, 'B Folder') == folder
    assert _server_counts() == (0, 24)
    _back(seen, 'Unidirectional')
    # the page's own box again, with the report, and the left panel empty
    assert _held(seen, 'A Folder') == '' and _held(seen, 'B Folder') == ''
    assert _uni_box(seen) == folder
    assert _done(seen)


def test_a_typed_b_folder_opens_the_separate_window_on_b(tmp_path, monkeypatch):
    monkeypatch.setenv('OTDR_CACHE_DIR', str(tmp_path / 'cache'))
    folder, at = _typed_run(tmp_path, B)
    assert _server() == (None, folder)                         # was (folder, None)
    assert _popout_dirs(at) == {'b'}                           # was {'a'}


def test_a_typed_a_folder_still_opens_on_a(tmp_path, monkeypatch):
    monkeypatch.setenv('OTDR_CACHE_DIR', str(tmp_path / 'cache'))
    folder, at = _typed_run(tmp_path, A)
    assert _server() == (folder, None)
    assert _popout_dirs(at) == {'a'}
    links = _this_tab_links(at)
    assert {q['dir'] for q in links} == {'a'}
    seen = _click(links[0])
    assert '&dir=a' in _viewer_url(seen)
    assert _held(seen, 'A Folder') == folder and _held(seen, 'B Folder') == ''
    # Back: the box shows the folder again (it came back empty, and the next
    # click sent the empty box and the report went), with the report.
    _back(seen, 'Unidirectional')
    assert _uni_box(seen) == folder
    assert _done(seen)
    seen.run()               # the browser keeps what it was sent
    assert next(t for t in seen.main.text_input
                if 'Paste a Folder' in t.label).value == folder
    assert _done(seen)


def test_an_upload_of_both_directions_run_on_b_opens_the_viewer_on_b(tmp_path, drops):
    at = _open(_hub(), 'Unidirectional')
    _drop(at, drops, _files(PA, ALL) + _files(PB, ALL))
    side = _run_on_radio(at)
    side.set_value(side.options[1]).run()
    drops.clear()
    _run(at, str(tmp_path))
    ran = at.session_state['uni_result']['_folder']
    links = _this_tab_links(at)
    assert {q['dir'] for q in links} == {'b'}                  # was {'a'}
    seen = _click(links[0])
    assert '&dir=b' in _viewer_url(seen)
    assert _server() == (None, ran)
    # The left panel's boxes: A empty, and B names the upload, not the
    # temporary folder it was staged in.
    assert _held(seen, 'A Folder') == ''
    assert _held(seen, 'B Folder') == 'Uploaded Files: B Direction (24 files)'
    assert _server_counts() == (0, 24)
    # ── 2. Back shows a plain label in the page's box, and the report ──
    _back(seen, 'Unidirectional')
    assert _uni_box(seen) == 'Uploaded Files: B Direction (24 files)'
    assert tempfile.gettempdir() not in _uni_box(seen)
    assert _done(seen)
    assert seen.session_state['uni_result']['_folder'] == ran
    assert _held(seen, 'A Folder') == '' and _held(seen, 'B Folder') == ''


def test_a_one_direction_upload_of_b_opens_the_viewer_on_b(tmp_path, drops):
    at = _open(_hub(), 'Unidirectional')
    _drop(at, drops, _files(PB, ALL))
    drops.clear()
    _run(at, str(tmp_path))
    ran = at.session_state['uni_result']['_folder']
    links = _this_tab_links(at)
    assert {q['dir'] for q in links} == {'b'}
    seen = _click(links[0])
    assert _server() == (None, ran)
    _back(seen, 'Unidirectional')
    assert _uni_box(seen) == 'Uploaded Files: B Direction (24 files)'
    assert _done(seen)


def test_a_zip_upload_is_named_after_the_zip(tmp_path, drops):
    at = _open(_hub(), 'Unidirectional')
    _drop(at, drops, [_zip({'A side': _files(PA, ALL),
                            'B side': _files(PB, ALL)})])
    drops.clear()
    _run(at, str(tmp_path))                                    # A by default
    links = _this_tab_links(at)
    assert {q['dir'] for q in links} == {'a'}
    seen = _click(links[0])
    assert _held(seen, 'A Folder') == 'both directions.zip (Uploaded): A Direction (24 files)'
    _back(seen, 'Unidirectional')
    assert _uni_box(seen) == 'both directions.zip (Uploaded): A Direction (24 files)'
    assert _done(seen)


def test_the_labelled_box_still_runs_on_the_staged_files(tmp_path, drops):
    """After Back the box shows the label; a trip to another tool and back,
    and a new run, still read the staged files."""
    at = _open(_hub(), 'Unidirectional')
    _drop(at, drops, _files(PB, ALL))
    drops.clear()
    _run(at, str(tmp_path))
    ran = at.session_state['uni_result']['_folder']
    seen = _click(_this_tab_links(at)[0])
    _back(seen, 'Unidirectional')
    _open(seen, 'Splice Report')
    _open(seen, 'Unidirectional')
    assert _uni_box(seen) == 'Uploaded Files: B Direction (24 files)'
    assert _done(seen)
    _run(seen, str(tmp_path))
    assert seen.session_state['uni_result']['_folder'] == ran

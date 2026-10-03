"""A drop on the Viewer's FILES panel reaches whichever tool is clicked next.

Click-through audit 2026-09-29 (main 70bd1a8): files dropped on the Viewer's
FILES panel point the trace server at a staged folder (trace_server drop_begin
/ drop_file / drop_end stamp CONFIG['dropped_at']).  Only page_viewer looked
for that stamp, so a tech who dropped 3 + 3 files and then clicked Splice
Report kept the OLD folders in the sidebar boxes: the report ran on the old
span while the Viewer showed the dropped one.  The check now runs on every
page before the page draws (it was the sidebar's Trace Folders loader; the
boxes are on the Traces tab since 2026-10-01).
"""
from __future__ import annotations

import os

import pytest

from conftest import (run_streamlit, import_trace_server, go_tab, trace_box,
                      trace_box_value, FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR)
from test_sor_writer import make_sor

TS = import_trace_server()
X_A, X_B = str(FIXTURE_SPLICE_A_DIR), str(FIXTURE_SPLICE_B_DIR)


def _shown(d):
    """What a box shows for a staged drop folder: the drop's own name (here
    the site the file names carry), never the staging path."""
    name = TS.drop_name(d)
    assert name and 'otdr_viewer_drop_' not in name and os.sep not in name
    return f'{name} (dropped)'


@pytest.fixture(autouse=True)
def _own_temp(tmp_path, monkeypatch):
    monkeypatch.setenv('TMPDIR', str(tmp_path))
    import tempfile
    tempfile.tempdir = None
    yield
    tempfile.tempdir = None
    TS.CONFIG.pop('dropped_at', None)


def _box(at, label):
    """The Traces tab's box (goes to the Traces tab first)."""
    return trace_box(at, label[0])


def _held(at, label):
    """What a Traces tab box holds, read without leaving the page."""
    return trace_box_value(at, label[0])


def _drop_three_and_three():
    """What the Viewer page does when the tech has removed every trace and
    drops both directions of another cable: span 1 is now the dropped one.
    (Dropped beside a loaded span, another cable is added as span 2, a
    Viewer-only span; the hub's tools stay on span 1, Robert 2026-10-02.)"""
    tok = TS.drop_begin()
    for name in ('ROMTUC001_1550.sor', 'ROMTUC002_1550.sor', 'ROMTUC003_1550.sor',
                 'TUCROM001_1550.sor', 'TUCROM002_1550.sor', 'TUCROM003_1550.sor'):
        assert TS.drop_file(tok, name, make_sor(ior=1.47))['files'] == 1
    out = TS.drop_end(tok, emptied='ab')
    assert out['added'] == 'AB' and len(out['new_keys']) == 6
    assert out['dir_a'] != X_A and out['dir_b'] != X_B
    return out['dir_a'], out['dir_b']


@pytest.mark.parametrize('page', ['Splice Report', 'Unidirectional', 'Viewer'])
def test_a_drop_then_a_tool_click_loads_the_dropped_folders(page):
    at = run_streamlit().run()
    _box(at, 'A Folder').input(X_A).run()
    _box(at, 'B Folder').input(X_B).run()
    go_tab(at, 'Viewer')                         # the Viewer shows the old span
    assert not at.exception, at.exception
    assert (TS.CONFIG['dir_a'], TS.CONFIG['dir_b']) == (X_A, X_B)

    y_a, y_b = _drop_three_and_three()
    assert os.path.isdir(y_a) and os.path.isdir(y_b)

    # Straight to the tool: no Viewer pass in between.
    go_tab(at, page)
    assert not at.exception, at.exception
    # The boxes name what was dropped, never the staging folder (#31)...
    assert _held(at, 'A Folder') == _shown(y_a)
    assert _held(at, 'B Folder') == _shown(y_b)
    # ...and stand for the staged folders, which the tools run on.
    assert (TS.CONFIG['dir_a'], TS.CONFIG['dir_b']) == (y_a, y_b)
    assert at.session_state['_drop_box_a'] == (_shown(y_a), y_a)
    assert at.session_state['_drop_box_b'] == (_shown(y_b), y_b)
    # ...and a hub rerun does not put the old paths back.
    at.run()
    assert (_held(at, 'A Folder'), _held(at, 'B Folder')) == (_shown(y_a), _shown(y_b))
    assert (TS.CONFIG['dir_a'], TS.CONFIG['dir_b']) == (y_a, y_b)
    # ...nor does a trip to the Traces tab, whose boxes show the drop.
    assert (_box(at, 'A Folder').value, _box(at, 'B Folder').value) == (_shown(y_a), _shown(y_b))
    assert (TS.CONFIG['dir_a'], TS.CONFIG['dir_b']) == (y_a, y_b)


def test_a_drop_is_taken_once_so_a_later_pick_stands():
    at = run_streamlit().run()
    y_a, y_b = _drop_three_and_three()
    go_tab(at, 'Splice Report')
    assert _held(at, 'A Folder') == _shown(y_a)
    assert TS.CONFIG['dir_a'] == y_a
    _box(at, 'A Folder').input(X_A).run()
    _box(at, 'B Folder').input(X_B).run()
    at.run()
    assert not at.exception, at.exception
    assert (_box(at, 'A Folder').value, _box(at, 'B Folder').value) == (X_A, X_B)

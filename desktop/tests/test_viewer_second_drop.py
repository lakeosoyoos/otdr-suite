"""The second folder dropped on the Viewer loads too (the boss, 2026-09-30,
a 432-fiber job): "I can only dump A or B, if I try both the other set goes straight
to downloads and isn't populating."

Two holes, both on the hub page around the Viewer frame:

* Nothing on the hub took a dropped file, so a folder let go of a little off
  the Viewer frame (the sidebar's B box, the settings box above it) went to
  Chrome's Downloads.  The hub page now catches file drops everywhere and
  refuses them (HUB_DROP_CATCH_JS): only the Viewer's FILES panel takes a
  drop (Robert 2026-10-01), and a drag over the hub shows the panel as the
  place to drop ('otdr-drag' message).
* The first drop reloaded the Viewer frame on the next hub rerun: the frame's
  address carried a hash of the folders, which the drop had just changed, and
  the "Pick an A and/or B folder" note above the frame went away and moved it
  up a place.  The Viewer came back empty, and a folder dropped while it came
  back went to Downloads.  The frame now stays put after its own drop.
"""
from __future__ import annotations

import os
import re

import pytest

from conftest import run_streamlit, import_trace_server, APP_PATH
from test_sor_writer import make_sor

TS = import_trace_server()


@pytest.fixture(autouse=True)
def _own_temp(tmp_path, monkeypatch):
    monkeypatch.setenv('TMPDIR', str(tmp_path))
    import tempfile
    tempfile.tempdir = None
    TS.set_dirs(None, None)
    TS.CONFIG.pop('dropped_at', None)
    yield
    tempfile.tempdir = None
    TS.CONFIG.pop('dropped_at', None)
    TS.set_dirs(None, None)


def _viewer_frame(at):
    """(where the Viewer frame sits on the page, its src)."""
    def walk(node, path=()):
        for i, ch in getattr(node, 'children', {}).items():
            yield path + (i,), ch
            yield from walk(ch, path + (i,))
    hits = [(p, ch.proto.src) for p, ch in walk(at.main)
            if getattr(ch, 'type', None) == 'iframe'
            and '127.0.0.1' in (getattr(ch.proto, 'src', '') or '')]
    assert len(hits) == 1, hits
    return hits[0]


def _drop(prefix):
    """What the Viewer page does when the tech drops one direction's folder."""
    tok = TS.drop_begin()
    for n in (1, 2, 3):
        assert TS.drop_file(tok, f'{prefix}00{n}_1550.sor', make_sor(ior=1.47))['files'] == 1
    return TS.drop_end(tok)


def test_a_then_b_dropped_on_the_viewer_never_reload_its_frame():
    at = run_streamlit().run()
    assert not at.exception, at.exception
    assert any('Pick an A and/or B folder' in i.value for i in at.info)
    before = _viewer_frame(at)

    out = _drop('ROMTUC')
    assert out['a_count'] == 3
    at.run()
    assert not at.exception, at.exception
    # The note is gone and the boxes show the dropped folder...
    assert not any('Pick an A and/or B folder' in i.value for i in at.info)
    shown = f"{TS.drop_name(TS.CONFIG['dir_a'])} (dropped)"
    assert 'otdr_viewer_drop_' not in shown
    assert at.session_state['view_dir_a_input'] == shown
    assert at.session_state['_drop_box_a'] == (shown, TS.CONFIG['dir_a'])
    # ...and the frame has neither moved nor changed its address.
    assert _viewer_frame(at) == before

    out = _drop('TUCROM')
    assert out['a_count'] == 3 and out['b_count'] == 3
    at.run()
    assert not at.exception, at.exception
    assert _viewer_frame(at) == before
    at.run()                                    # any later rerun as well
    assert _viewer_frame(at) == before


def test_folders_picked_in_the_boxes_still_reload_the_viewer():
    at = run_streamlit().run()
    _drop('ROMTUC')
    at.run()
    path, src = _viewer_frame(at)
    other = os.path.join(os.environ['TMPDIR'], 'other_a')
    os.makedirs(other)
    next(t for t in at.sidebar.text_input if t.label == 'A folder').input(other).run()
    assert not at.exception, at.exception
    new_path, new_src = _viewer_frame(at)
    assert new_path == path and new_src != src


def test_the_hub_page_refuses_a_stray_drop():
    app = open(APP_PATH, encoding='utf-8').read()
    m = re.search(r'HUB_DROP_CATCH_JS = r"""(.*?)"""', app, re.S)
    assert m, 'the hub drop catcher is gone'
    js = m.group(1)
    # Put into the hub page itself, so it outlives the frame that carried it.
    assert "createElement('script')" in js and 'w.document.head.appendChild' in js
    # Takes every file drop on the page and on the hub's own frames, so
    # nothing goes to Downloads...
    for need in ("addEventListener('dragover'", "addEventListener('drop'",
                 'preventDefault()', "'Files'"):
        assert need in js, need
    # ...but never one a file box of a page has taken already...
    assert js.count('ev.defaultPrevented') == 2
    # ...and loads nothing: only the Viewer's FILES panel takes a drop.
    assert "dropEffect = 'none'" in js and 'otdr-drop' not in js
    # A drag over the hub shows the panel as the place to drop.
    assert "postMessage({ type: 'otdr-drag' }" in js
    assert re.search(r'^_install_hub_drop_catch\(\)$', app, re.M), 'not installed on every page'



def test_a_viewer_opened_from_a_report_cell_keeps_its_frame_through_both_drops():
    """The boss again, 2026-10-01 (after #466): B still went to Downloads.
    The Viewer had been opened from a report cell, so its address carried
    that cell's fiber.  The first drop is a new span and lets go of the
    cell's link, and the address without it reloaded the frame; the hub now
    reruns by itself just after a drop, so the reload came as B was dropped."""
    at = run_streamlit().run()
    at.session_state['viewer_target'] = {'fiber': '354', 'km': '12.3', 'dir': 'both'}
    at.run()
    assert not at.exception, at.exception
    before = _viewer_frame(at)
    assert 'fiber=354' in before[1]

    _drop('ROMTUC')
    at.run()
    assert not at.exception, at.exception
    assert 'viewer_target' not in at.session_state    # the old span's link goes
    assert _viewer_frame(at) == before                 # but the frame stays put
    _drop('TUCROM')
    at.run()
    assert _viewer_frame(at) == before
    at.run()
    assert _viewer_frame(at) == before

    # A cell clicked after the drop still moves the Viewer.
    at.session_state['viewer_target'] = {'fiber': '12', 'dir': 'a'}
    at.run()
    path, src = _viewer_frame(at)
    assert path == before[0] and 'fiber=12' in src and 'fiber=354' not in src


def test_back_on_the_viewer_after_a_drop_the_old_cell_link_is_gone():
    at = run_streamlit().run()
    at.session_state['viewer_target'] = {'fiber': '354', 'km': '12.3', 'dir': 'both'}
    at.run()
    _drop('ROMTUC')
    at.run()
    assert 'fiber=354' in _viewer_frame(at)[1]
    at.sidebar.radio(key='nav_radio').set_value('Splice Report').run()
    assert not at.exception, at.exception
    at.sidebar.radio(key='nav_radio').set_value('Viewer').run()
    assert not at.exception, at.exception
    assert 'fiber=' not in _viewer_frame(at)[1]

"""A Unidirectional report run on the left panel's B folder opens the
pop-out Viewer on B, and "← Back" lands on the B folder's report.

Found 2026-10-01 in an audit of where the Viewer treats A and B differently.
A click into the Viewer TAB already kept both of the panel's folders and
opened the fibre on the side the report ran on (_handle_nav, 2026-09-29).
The separate-window path (the default) did not: it pointed the Viewer's A
slot at the report's folder and its cells said `dir=a`, so a B-folder run's
files were loaded and labelled A->B in that window, the panel's real A
folder was dropped, and the Viewer's one-direction table ran the report's
A leg on B's files.

Now the window keeps both of the panel's folders and the cells open on the
run's side.  Its "← Back" (used when the hub tab that opened it is gone)
carried only the A slot, which is right only while the A slot held the
report's folder, so it now also carries the B folder and the side, and the
hub puts the Unidirectional page back on that side.  A run on a folder that
is not one of the panel's works as before.
"""
from __future__ import annotations

import os
import re

import pytest

from conftest import VIEWER_DIR, finish_engine_run, trace_box_value
from test_panel_zip_mixed_folder_uni_jump import A, B, _click, _hub, _open, _server

HTML = (VIEWER_DIR / "viewer.html").read_text(encoding="utf-8")


@pytest.fixture
def dest(tmp_path, monkeypatch):
    """Where the run below saves: never the tech's Downloads folder."""
    monkeypatch.setenv('OTDR_CACHE_DIR', str(tmp_path / 'cache'))
    out = tmp_path / 'saved reports'
    out.mkdir()
    return str(out)


def _popout_grid(at):
    return next(f.proto.srcdoc for f in at.get('iframe') if "class='vc'" in (f.proto.srcdoc or ''))


def _run_on(at, side, dest):
    next(r for r in at.main.radio if r.label == 'Run On').set_value(side).run()
    at.session_state['uni_report_dest'] = dest
    at.run()
    next(b for b in at.main.button if b.label == 'Run Unidirectional Report').click().run()
    finish_engine_run(at, 'uni')
    assert not at.exception, at.exception
    return at


def test_a_b_folder_run_opens_the_window_on_b_with_both_folders(dest):
    at = _run_on(_open(_hub(A, B), 'Unidirectional'), 'B folder', dest)
    assert _server() == (A, B)                         # was (B, None): B loaded as A
    dirs = set(re.findall(r"data-dir='(\w)'", _popout_grid(at)))
    assert dirs == {'b'}                                # was {'a'}


def test_an_a_folder_run_still_opens_on_a(dest):
    at = _run_on(_open(_hub(A, B), 'Unidirectional'), 'A folder', dest)
    assert _server() == (A, B)
    assert set(re.findall(r"data-dir='(\w)'", _popout_grid(at))) == {'a'}


def test_back_from_the_window_lands_on_the_side_the_report_ran_on():
    seen = _click({'nav': 'uni', 'sra': A, 'srb': B, 'pside': 'b'})
    assert trace_box_value(seen, 'a') == A and trace_box_value(seen, 'b') == B
    assert next(r for r in seen.main.radio if r.label == 'Run On').value == 'B folder'


def test_back_without_a_side_works_as_before():
    seen = _click({'nav': 'uni', 'sra': A})
    assert trace_box_value(seen, 'a') == A
    assert seen.session_state['uni_folder_input'] == A


def test_the_viewer_sends_the_side_back():
    # a Unidirectional link's direction is the side its report ran on
    assert ("gUniSide = (t.src === 'uni' && (t.dir === 'a' || t.dir === 'b')) ? t.dir : null;"
            in HTML)
    back = HTML.split("function goBackToReport()", 1)[1]
    back = back.split('\n});', 1)[0]
    assert "if (gInfo.dir_b && (nav !== 'uni' || uniSide)) q.set('srb', gInfo.dir_b);" in back
    assert "if (uniSide) q.set('pside', uniSide);" in back

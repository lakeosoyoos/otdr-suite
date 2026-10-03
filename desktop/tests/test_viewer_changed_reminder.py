"""Leaving the Viewer with its loss / Reflectance box changed (Robert 2026-10-01).

  "so if we leave viewer to go to splice report or uni we need a window that
   reminds the tech they had changed the setting"

The boxes change only what the Viewer flags.  The Viewer files what was typed
with its server (/api/viewer_changed); the hub's Thresholds Carried Over
pop-up says it on the way to the Splice Report or the Uni report, and the
pop-out Viewer's own "← Back" says it before going.
"""
from __future__ import annotations

import pytest

from conftest import REPO_ROOT, run_streamlit, import_trace_server

TS = import_trace_server()
VIEWER = (REPO_ROOT / "viewer" / "viewer.html").read_text(encoding="utf-8")
TITLE = "Thresholds Carried Over from Previous Tool"
CHANGED = {'loss_name': 'Bidirectional Loss', 'loss': 0.15, 'loss_report': 0.16,
           'refl': -70, 'refl_report': -80}


@pytest.fixture(autouse=True)
def _clean():
    TS.set_viewer_changed(None)
    TS.set_thresholds(None)
    TS.set_settings(None)
    yield
    TS.set_viewer_changed(None)
    TS.set_thresholds(None)
    TS.set_settings(None)


def test_the_server_files_only_a_real_change():
    TS.set_viewer_changed(CHANGED)
    assert TS.viewer_changed() == {'loss_name': 'Bidirectional Loss', 'loss': 0.15,
                                   'loss_report': 0.16, 'refl': -70.0,
                                   'refl_report': -80.0}
    TS.set_viewer_changed({'loss': None, 'refl': None})
    assert TS.viewer_changed() is None
    TS.set_viewer_changed({'loss': 'x', 'refl': 'a'})      # junk is not a change
    assert TS.viewer_changed() is None
    TS.set_viewer_changed({'refl': -60})
    assert TS.viewer_changed()['refl'] == -60.0
    TS.set_viewer_changed(None)
    assert TS.viewer_changed() is None


def test_the_lines_read_plainly():
    import app as hub
    assert hub._viewer_change_lines(CHANGED) == [
        'Bidirectional Loss: 0.150 dB (the report uses 0.160 dB)',
        'Reflectance: -70 dB (the report uses -80 dB)']
    assert hub._viewer_change_lines({'refl': 0, 'refl_report': -80}) == [
        'Reflectance: off (the report uses -80 dB)']
    assert hub._viewer_change_lines(None) == []


def _hub():
    at = run_streamlit(default_timeout=180).run()
    assert not at.exception, at.exception
    return at


def _open(at, page):
    at.sidebar.radio[0].set_value(page).run()
    assert not at.exception, at.exception
    return at


def _said(at):
    (d,) = [d for d in at.get("dialog") if d.proto.dialog.title == TITLE]
    return " ".join(m.value for m in d.markdown)


@pytest.mark.parametrize("to", ["Splice Report", "Unidirectional"])
def test_leaving_the_viewer_with_a_change_says_so(to):
    at = _hub()                                   # opens on the Viewer
    TS.set_viewer_changed(CHANGED)
    said = _said(_open(at, to))
    assert "You changed these in the Viewer:" in said
    assert "<li>Bidirectional Loss: 0.150 dB (the report uses 0.160 dB)</li>" in said
    assert "<li>Reflectance: -70 dB (the report uses -80 dB)</li>" in said
    assert f"<b>{to}</b> flags at its OTDR Settings" in said


def test_no_change_no_reminder():
    said = _said(_open(_hub(), "Splice Report"))
    assert "You changed these in the Viewer" not in said


def test_only_on_leaving_the_viewer():
    at = _open(_hub(), "Secret Sauce")
    TS.set_viewer_changed(CHANGED)
    assert "You changed these in the Viewer" not in _said(_open(at, "Splice Report"))


def test_the_viewer_files_its_boxes_and_back_reminds():
    sync = VIEWER[VIEWER.index('function syncGateUI'):]
    sync = sync[:sync.index('\n}\n')]
    assert sync.rstrip().endswith('postViewerChanged();')
    assert "fetch('/api/viewer_changed'" in VIEWER
    assert 'if (flagsOff() || (gGateOverride == null && gReflOverride == null)) return null;' in VIEWER
    back = VIEWER[VIEWER.index("document.getElementById('btn-back').addEventListener('click'"):]
    back = back[:back.index('\n});\n')]
    assert 'if (c) showBackReminder(c, goBackToReport);' in back
    assert 'else goBackToReport();' in back
    assert '<h3>You changed the Viewer\'s settings</h3>' in VIEWER
    assert 'Stay in Viewer' in VIEWER and 'Go back' in VIEWER

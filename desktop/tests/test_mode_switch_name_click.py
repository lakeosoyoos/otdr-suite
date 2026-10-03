"""Clicking a name beside a sidebar switch picks that side (audit
2026-10-02).

The Theme switch (Dark | knob | Light) and the Analysis Mode switch (FR Mode
| knob | OTDR Mode) drew the names as text beside a keyless toggle, so a
click on a name did nothing; only the knob worked.  A small script on the
page (MODE_NAME_CLICK_JS) now makes a click on the name not in use click the
switch itself, so everything after it is the knob's own path.  The toggles
stay keyless (a keyed one drew the knob in an old position after the App's
home screen and flipped the mode unseen).

AppTest cannot run the script, so the script is run under jsc against a
small stand-in for the page, and AppTest checks it is on every page and that
a click on the switch (what the script does) moves the theme and the halo.
"""
from __future__ import annotations

import json
import os
import re
import subprocess

import pytest

from conftest import REPO_ROOT, run_streamlit
from test_viewer_overview_failures import JSC, needs_jsc

SRC = (REPO_ROOT / 'app.py').read_text(encoding='utf-8')


def _script():
    m = re.search(r'MODE_NAME_CLICK_JS = """\n<script>\n(.*?)</script>\n"""', SRC, re.S)
    assert m, 'app.py no longer has MODE_NAME_CLICK_JS'
    return m.group(1)


# A sidebar switch box, as far as the script looks at it: two names, one
# with class mode-on (in use) and one mode-off, either side of the toggle's
# checkbox.  Knob right = checked (Light, OTDR Mode).
_PAGE = r"""
var clicks = 0, listeners = [];
function mkBox(boxCls, checked) {
  var box = { cls: boxCls };
  var sw = { checked: checked, disabled: false,
             click: function () { clicks++; this.checked = !this.checked; } };
  box.querySelector = function (sel) {
    return sel === '[data-testid="stCheckbox"] input[type="checkbox"]' ? sw : null;
  };
  function name(text, side) {
    return { text: text, side: side,
             get cls() { return (side === 'left') === !sw.checked ? 'mode-on' : 'mode-off'; },
             closest: function (sel) {
               if (sel === '.mode-off') return this.cls === 'mode-off' ? this : null;
               if (sel === '.st-key-theme_box, .st-key-analysis_mode_box') return box;
               return null; },
             // DOCUMENT_POSITION_FOLLOWING (4): the switch comes after the name
             compareDocumentPosition: function (other) { return side === 'left' ? 4 : 2; } };
  }
  box.sw = sw; box.left = name('L', 'left'); box.right = name('R', 'right');
  return box;
}
var doc = {
  addEventListener: function (t, fn) { listeners.push(fn); },
  removeEventListener: function (t, fn) { listeners = listeners.filter(function (f) { return f !== fn; }); }
};
var window = { parent: { document: doc } };
function load() {
""" + '__SCRIPT__' + r"""
}
function click(target) { listeners.forEach(function (fn) { fn({ target: target }); }); }
"""

_DRIVER = r"""
load(); load();                                     // the frame loaded twice
var out = { listeners: listeners.length };
var theme = mkBox('theme_box', false);              // Dark: knob left
click(theme.left);                                  // "Dark", in use
out.inUse = [theme.sw.checked, clicks];
click(theme.right);                                 // "Light"
out.other = [theme.sw.checked, clicks];
click(theme.right);                                 // again before the run lands
out.again = [theme.sw.checked, clicks];
click(theme.left);                                  // back to "Dark"
out.back = [theme.sw.checked, clicks];
var mode = mkBox('analysis_mode_box', true);        // OTDR Mode: knob right
click(mode.left);                                   // "FR Mode"
out.fr = [mode.sw.checked, clicks];
click({ closest: function () { return null; } });   // anywhere else
click({});                                          // a target with no closest()
out.elsewhere = clicks;
mode.sw.disabled = true;
click(mode.right);
out.disabled = [mode.sw.checked, clicks];
print('OUT ' + JSON.stringify(out));
"""


@needs_jsc
def test_a_click_on_the_other_name_clicks_the_switch(tmp_path):
    prog = _PAGE.replace('__SCRIPT__', _script()) + _DRIVER
    path = tmp_path / 'names.js'
    path.write_text(prog, encoding='utf-8')
    r = subprocess.run([JSC, str(path)], capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    assert 'OUT ' in out, out[-2000:]
    got = json.loads(out.split('OUT ', 1)[1].strip())
    assert got['listeners'] == 1            # a reload replaces, never doubles
    assert got['inUse'] == [False, 0]       # the name in use does nothing
    assert got['other'] == [True, 1]        # "Light" moves the knob right
    assert got['again'] == [True, 1]        # a second click does not flip it back
    assert got['back'] == [False, 2]        # "Dark" moves it left
    assert got['fr'] == [False, 3]          # "FR Mode" moves its knob left
    assert got['elsewhere'] == 3
    assert got['disabled'] == [False, 3]


def test_the_names_show_they_can_be_clicked():
    css = SRC.split('_MODE_SWITCH_CSS = (', 1)[1].split("'</style>')", 1)[0]
    assert ('.st-key-analysis_mode_box .mode-off,.st-key-theme_box .mode-off'
            '{cursor:pointer}') in css
    # the halo is untouched
    assert ('.st-key-analysis_mode_box .mode-on,.st-key-theme_box .mode-on{display:inline-block;'
            "'\n    'padding:0 4px;") in SRC


def test_the_switches_stay_keyless():
    theme = SRC.split('def _render_theme_control(where):', 1)[1].split('\ndef ', 1)[0]
    mode = SRC.split('def _render_analysis_mode_control():', 1)[1].split('\ndef ', 1)[0]
    assert "m.toggle('Theme', value=not dark, label_visibility='collapsed')" in theme
    assert "m.toggle('Analysis Mode', value=not _on, label_visibility='collapsed')" in mode
    assert 'MODE_NAME_CLICK_JS' not in theme + mode    # nothing new in the switches


def _frames(at):
    out = []

    def walk(n):
        for c in getattr(n, 'children', {}).values():
            if type(c).__name__ == 'UnknownElement' and c.type == 'iframe':
                out.append(c.proto.srcdoc)
            walk(c)
    walk(at._tree)
    return out


@pytest.mark.parametrize('page', ['Viewer', 'Splice Report', 'Secret Sauce'])
def test_the_script_is_on_every_page(page):
    at = run_streamlit().run()
    at.sidebar.radio[0].set_value(page).run()
    assert not at.exception, list(at.exception)
    assert sum('__otdrModeNameClick' in f for f in _frames(at)) == 1


def test_a_click_on_the_switch_moves_the_theme_and_the_halo():
    """What the script does is click the switch: that path picks the side."""
    at = run_streamlit().run()
    assert at.session_state['ui_theme'] == 'dark'
    assert any('class="mode-on">Dark<' in m.value for m in at.sidebar.markdown)
    theme = next(t for t in at.sidebar.toggle if t.label == 'Theme')
    assert theme.value is False and theme.key is None
    theme.set_value(True).run()
    assert not at.exception, list(at.exception)
    assert at.session_state['ui_theme'] == 'light'
    md = [m.value for m in at.sidebar.markdown]
    assert any('class="mode-on">Light<' in m for m in md)
    assert any('class="mode-off">Dark<' in m for m in md)

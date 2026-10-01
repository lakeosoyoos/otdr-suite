"""Light / Dark theme (the sidebar's Theme switch).

Light must be exactly the palette the hub always had, every start must be
Light (Dark is not saved), the Viewer must arrive marked dark only when the
hub is dark with its trace plot and event panel kept light, an open Viewer
must follow the switch, and the theme must add NO engine file (an installed
exe refuses any signed update whose file set differs from its own).
"""
import json
import os
import re
import socket
import subprocess
import threading
import urllib.request

import pytest

from conftest import REPO_ROOT, import_trace_server

T = import_trace_server()
SRC = (REPO_ROOT / 'app.py').read_text(encoding='utf-8')


def _theme():
    """The theme section of app.py, run on its own (app.py is a page script,
    not importable)."""
    block = SRC.split('# ─── Light / Dark theme', 1)[1].split('\n', 1)[1]
    block = block.split('# ─── end Light / Dark theme', 1)[0]
    ns = {}
    exec(compile('import json, os, re\n' + block, 'app.py[theme]', 'exec'), ns)
    return ns


TH = _theme()


def test_light_is_the_config_toml_palette():
    """Nobody who leaves the switch alone sees a change."""
    toml = (REPO_ROOT / '.streamlit' / 'config.toml').read_text(encoding='utf-8')
    for key in ('base', 'primaryColor', 'backgroundColor',
                'secondaryBackgroundColor', 'textColor'):
        m = re.search(r'^%s\s*=\s*"([^"]+)"' % key, toml, re.M)
        assert m, key
        assert TH['THEME_STREAMLIT']['light'][key] == m.group(1), key


def test_both_themes_name_the_same_colours():
    assert set(TH['THEME_VARS']['light']) == set(TH['THEME_VARS']['dark'])
    assert set(TH['THEME_STREAMLIT']['light']) == set(TH['THEME_STREAMLIT']['dark'])


def test_every_start_is_light_and_dark_is_not_saved():
    """Robert, 2026-10-01: "we want to start in light mode always".  The boss
    started the Suite after an update with Dark saved and it came up only part
    dark until he flipped the switch back and forth."""
    assert TH['THEME_DEFAULT'] == 'light'
    assert "st.session_state['ui_theme'] = THEME_DEFAULT" in SRC
    for gone in ('load_theme', 'save_theme', '_theme_settings_path'):
        assert gone not in SRC, gone
    switch = SRC.split('def _render_theme_control(where):', 1)[1].split('\ndef ', 1)[0]
    assert 'settings' not in switch


def test_recolor_swaps_palette_colours_only():
    html = ('<b style="background:#eef3f8;color:#000000;border:1px solid #C9D5E1">'
            '<i style="color:#e74c3c;background:#eef3f8a0"></i>')
    assert TH['theme_recolor'](html, 'light') == html
    out = TH['theme_recolor'](html, 'dark')
    assert 'background:#1c1917' in out and 'color:#fafaf9' in out
    assert 'border:1px solid #44403c' in out
    assert '#e74c3c' in out            # a flag colour stays a flag colour
    assert '#eef3f8a0' in out          # not a palette colour: left alone


def test_every_hub_variable_is_in_the_palette():
    """A var(--otdr-x) in app.py with no x in the palette paints nothing."""
    used = set(re.findall(r'var\(--otdr-([a-z0-9-]+)', SRC))
    assert used and used <= set(TH['THEME_VARS']['light'])


def test_no_new_engine_file():
    """The theme lives in app.py: a new engine file would make every
    installed exe refuse the next signed update."""
    assert not (REPO_ROOT / 'app_theme.py').exists()
    launcher = (REPO_ROOT / 'desktop' / 'launcher.py').read_text(encoding='utf-8')
    assert 'theme' not in launcher.split('ENGINE_FILES = [', 1)[1].split(']', 1)[0]


def test_theme_switch_is_dark_left_light_right_and_keyless():
    """Robert, 2026-09-29: "Theme" above "Dark | switch | Light", knob right =
    Light.  Keyless, so a page without the sidebar cannot leave the knob in
    an old position that flips the theme on the next run."""
    body = SRC.split('def _render_theme_control(where):', 1)[1].split('\ndef ', 1)[0]
    assert "box.markdown('**Theme**')" in body
    assert body.index("'Dark'") < body.index('m.toggle(') < body.index("'Light'")
    assert "m.toggle('Theme', value=not dark, label_visibility='collapsed')" in body
    assert '_render_theme_control(st.sidebar)' in SRC


def _get_viewer(monkeypatch, theme):
    monkeypatch.setitem(T.CONFIG, 'theme', theme)
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        free = s.getsockname()[1]
    srv, port = T.find_free_port(free, count=20)
    th = threading.Thread(target=srv.handle_request, daemon=True)
    th.start()
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/', timeout=10) as r:
            return r.read().decode('utf-8')
    finally:
        th.join(5)
        srv.server_close()


def test_viewer_served_dark_only_when_the_hub_is_dark(monkeypatch):
    light = _get_viewer(monkeypatch, 'light')
    dark = _get_viewer(monkeypatch, 'dark')
    assert '<html lang="en">' in light and 'data-theme' not in light.split('<head>')[0]
    assert '<html data-theme="dark" lang="en">' in dark


def test_viewer_keeps_plot_and_event_panel_light_in_dark():
    """Robert, 2026-09-29: the Viewer's two sections stay a very light grey."""
    html = (REPO_ROOT / 'viewer' / 'viewer.html').read_text(encoding='utf-8')
    m = re.search(r'html\[data-theme="dark"\] #main \{([^}]*)\}', html)
    assert m, 'no light block for the plot + event panel'
    block = m.group(1)
    assert '--plot-bg: #f4f4f5' in block and '--text: #000000' in block
    assert "ctx.fillStyle = plotBg();" in html


def test_api_mode_carries_the_theme():
    """The Viewer asks /api/mode every 1.5 s; the hub's Light / Dark rides along."""
    port = T.start_in_thread(8797)
    was = T.CONFIG.get('theme')
    try:
        for theme, want in (('dark', 'dark'), ('light', 'light'), (None, 'light')):
            T.CONFIG['theme'] = theme
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/mode', timeout=4) as r:
                assert json.loads(r.read())['theme'] == want
    finally:
        T.CONFIG['theme'] = was


JSC = ('/System/Library/Frameworks/JavaScriptCore.framework/'
       'Versions/Current/Helpers/jsc')


@pytest.mark.skipif(not os.path.exists(JSC), reason='JavaScriptCore shell (macOS) not present')
def test_open_viewer_follows_the_switch(tmp_path):
    """Flipping the switch left the embedded Viewer in the theme it loaded
    with.  The mode poll now hands it the theme: the page's mark changes and
    the plot is drawn again, only on a change, with no reload."""
    html = (REPO_ROOT / 'viewer' / 'viewer.html').read_text(encoding='utf-8')
    assert 'if (j && j.theme) applyHubTheme(j.theme);' in html
    m = re.search(r'function applyHubTheme\(t\) \{.*?\n\}\n', html, re.S)
    assert m, 'viewer.html no longer defines applyHubTheme()'
    js = r"""
var attrs = {}, draws = 0;
var document = { documentElement: {
  getAttribute: function (k) { return k in attrs ? attrs[k] : null; },
  setAttribute: function (k, v) { attrs[k] = v; },
  removeAttribute: function (k) { delete attrs[k]; } } };
function draw() { draws++; }
""" + m.group(0) + r"""
var out = [];
applyHubTheme('dark');  out.push([attrs['data-theme'] || '', draws]);
applyHubTheme('dark');  out.push([attrs['data-theme'] || '', draws]);
applyHubTheme('light'); out.push([attrs['data-theme'] || '', draws]);
applyHubTheme('light'); out.push([attrs['data-theme'] || '', draws]);
print('OUT ' + JSON.stringify(out));
"""
    path = tmp_path / 'theme.js'
    path.write_text(js, encoding='utf-8')
    r = subprocess.run([JSC, str(path)], capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    assert 'OUT ' in out, out[-2000:]
    assert json.loads(out.split('OUT ', 1)[1].strip()) == [
        ['dark', 1], ['dark', 1], ['', 2], ['', 2]]


def test_the_three_dot_menu_is_hidden():
    """Robert, 2026-10-01: its Settings theme picker overrides the switch."""
    m = re.search(r"st\.markdown\('<style>([^<]*stAppDeployButton[^<]*)</style>'", SRC.replace("'\n            '", ''))
    assert m, 'the header hide rule moved'
    assert '[data-testid="stMainMenu"]' in m.group(1) and '#MainMenu' in m.group(1)
    assert '_install_theme_pick_clear()' in SRC


@pytest.mark.skipif(not os.path.exists(JSC), reason='JavaScriptCore shell (macOS) not present')
def test_a_theme_picked_in_the_menu_is_cleared_once(tmp_path):
    """A Light / Dark / system pick from the ⋮ menu made Streamlit ignore the
    hub's theme for good.  It is removed and the page reloads once; the
    theme Streamlit keeps for the hub ("Custom Theme") is left alone."""
    body = SRC.split('THEME_PICK_CLEAR_JS = """', 1)[1].split('"""', 1)[0]
    script = body.split('<script>', 1)[1].split('</script>', 1)[0]
    js = r"""
function run(store) {
  var reloads = 0, keys = Object.keys(store);
  var ls = { get length() { return keys.length; },
             key: function (i) { return keys[i]; },
             getItem: function (k) { return store[k]; },
             removeItem: function (k) { delete store[k]; keys = Object.keys(store); } };
  var window = { parent: { document: {}, localStorage: ls,
                           location: { reload: function () { reloads++; } } } };
""" + script + r"""
  return [Object.keys(store).sort(), reloads];
}
var custom = JSON.stringify({ name: 'Custom Theme', themeInput: {} });
print('OUT ' + JSON.stringify([
  run({ 'stActiveTheme-/-v1': JSON.stringify({ name: 'Light' }), other: 'x' }),
  run({ 'stActiveTheme-/-v1': JSON.stringify({ name: 'Use system setting' }) }),
  run({ 'stActiveTheme-/-v1': custom }),
  run({}),
]));
"""
    path = tmp_path / 'pick.js'
    path.write_text(js, encoding='utf-8')
    r = subprocess.run([JSC, str(path)], capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    assert 'OUT ' in out, out[-2000:]
    assert json.loads(out.split('OUT ', 1)[1].strip()) == [
        [['other'], 1], [[], 1], [['stActiveTheme-/-v1'], 0], [[], 0]]

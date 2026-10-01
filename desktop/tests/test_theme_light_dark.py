"""Light / Dark theme (the sidebar's Theme switch).

Light must be exactly the palette the hub always had, the choice must survive
in settings.json without touching the other keys, the Viewer must arrive
marked dark only when the hub is dark with its trace plot and event panel
kept light, and the theme must add NO engine file (an installed exe refuses
any signed update whose file set differs from its own).
"""
import json
import re
import socket
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


def test_saved_theme_round_trip_keeps_other_settings(tmp_path, monkeypatch):
    monkeypatch.setenv('OTDR_SETTINGS_DIR', str(tmp_path))
    (tmp_path / 'settings.json').write_text(json.dumps({'analysis_mode': 'fr'}), encoding='utf-8')
    assert TH['load_theme']() == 'light'
    TH['save_theme']('dark')
    assert TH['load_theme']() == 'dark'
    data = json.loads((tmp_path / 'settings.json').read_text(encoding='utf-8'))
    assert data == {'analysis_mode': 'fr', 'theme': 'dark'}


def test_damaged_or_unknown_theme_falls_back_to_light(tmp_path, monkeypatch):
    monkeypatch.setenv('OTDR_SETTINGS_DIR', str(tmp_path))
    (tmp_path / 'settings.json').write_text('{not json', encoding='utf-8')
    assert TH['load_theme']() == 'light'
    (tmp_path / 'settings.json').write_text(json.dumps({'theme': 'purple'}), encoding='utf-8')
    assert TH['load_theme']() == 'light'
    with pytest.raises(ValueError):
        TH['save_theme']('purple')


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

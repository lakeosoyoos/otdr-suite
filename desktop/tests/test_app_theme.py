"""Light / Dark theme: the App-only checks.

The theme itself is main's (app.py, #448) and main's
test_theme_light_dark.py covers it: the Light palette, the saved choice,
the fallback, recolor, and the Viewer served dark with its plot kept light.
What stays here is what only the App has, or what main does not check.
"""
import re

from conftest import REPO_ROOT

SRC = (REPO_ROOT / 'app.py').read_text(encoding='utf-8')


def _palette(name):
    """THEME_VARS[name] as written in app.py (a literal dict)."""
    import ast
    for node in ast.parse(SRC).body:
        if (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == 'THEME_VARS'):
            return ast.literal_eval(node.value)[name]
    raise AssertionError('THEME_VARS not found in app.py')


def test_css_vars_cover_the_whole_palette():
    """theme_css_vars() writes one --otdr-* variable per palette colour."""
    body = SRC.split('def theme_css_vars(', 1)[1].split('\ndef ', 1)[0]
    assert "f'--otdr-{k}:{v}'" in body and 'pal.items()' in body
    assert set(_palette('light')) == set(_palette('dark'))


def test_the_theme_switch_sits_in_the_top_bar_on_every_screen():
    """OTDR App: the Light / Dark switch was in the sidebar footer the App
    pinned to the bottom of the panel; with the sidebar gone (2026-10-01) it
    is in the top bar, which every screen draws: the Home screen, New
    Project, a project and Quick Analysis."""
    bar = SRC.split('\ndef _render_top_nav(', 1)[1].split('\ndef ', 1)[0]
    assert '_render_theme_control(st)' in bar
    assert SRC.count('_render_theme_control(') == 2      # its def and the bar
    assert 'sidebar_footer' not in SRC
    assert '_render_theme_control(st.sidebar)' not in SRC
    assert SRC.count('\n    _render_top_nav(') == 4


def test_no_app_theme_module_is_left():
    assert not (REPO_ROOT / 'app_theme.py').exists()
    assert not re.search(r'\bapp_theme\b', SRC)

"""Light / Dark theme for the OTDR Suite App.

Dark is modelled on a dark dashboard the boss liked (shadcn's "stone"
palette: warm near-black page, dark grey panels, off-white lettering, a blue
accent).  Light is the palette the hub has always had, so a tech who never
touches the switch sees no change.

Robert, 2026-09-29: the Viewer's two sections -- the trace plot and the event
panel -- stay light (a very light grey) in Dark; everything around them goes
dark.  That keeps the FastReporter trace colours (blue A, black B) readable.

The choice lives in settings.json beside the analysis mode.  Streamlit reads
its theme from config, so apply_streamlit_theme() writes the palette into the
running server's config; the browser takes it on the next run (the new-session
message carries the theme on every run).  Everything the hub draws itself
uses the --otdr-* CSS variables from css_vars(); HTML that renders in its own
iframe (components.html) cannot see those, so it takes hex values from color().
"""
from __future__ import annotations

import json
import os

THEMES = ('light', 'dark')
THEME_DEFAULT = 'light'

# What Streamlit itself draws: pages, sidebar, widgets, st.dataframe.
STREAMLIT = {
    'light': {
        'base': 'light',
        'primaryColor': '#2c5b8a',
        'backgroundColor': '#ffffff',
        'secondaryBackgroundColor': '#eef3f8',
        'textColor': '#000000',
        'borderColor': '#d5dde6',
        'dataframeBorderColor': '#dbe4ee',
        'dataframeHeaderBackgroundColor': '#eef3f8',
    },
    'dark': {
        'base': 'dark',
        'primaryColor': '#3b82f6',
        'backgroundColor': '#0c0a09',
        'secondaryBackgroundColor': '#1c1917',
        'textColor': '#fafaf9',
        'borderColor': '#292524',
        'dataframeBorderColor': '#292524',
        'dataframeHeaderBackgroundColor': '#1c1917',
    },
}

# What the hub draws in its own HTML.  Light = the exact colours those pages
# used before the switch existed.
VARS = {
    'light': {
        'text': '#000000',        # lettering
        'text-sec': '#6b7480',    # quieter labels
        'bg': '#ffffff',          # table / card body
        'panel': '#eef3f8',       # headers, buttons, sidebar footer
        'panel-2': '#f7fafc',     # sticky first column
        'hover': '#dde7f1',
        'soft': '#f5f8fb',        # hovered card
        'line': '#dbe4ee',        # header cell borders
        'line-soft': '#eef2f6',   # body cell borders
        'line-row': '#e3e9f0',    # sticky column borders
        'edge': '#c9d5e1',        # box outlines
        'edge-2': '#b9c9da',      # tab / button outlines
        'rule': '#d5dde6',        # dividers
        'accent': '#2c5b8a',
        'accent-2': '#16324f',    # the dark "OK" button
        'accent-3': '#0b1c2e',    # ...hovered
        'on-accent': '#ffffff',
        'ok-bg': '#eaf6ec',       # the Final shoot row
        'ok-bg-2': '#e7f5ea',     # "carried over" note
        'ok-edge': '#9fd3aa',
        'ok-text': '#14532d',
    },
    'dark': {
        'text': '#fafaf9',
        'text-sec': '#a8a29e',
        'bg': '#0c0a09',
        'panel': '#1c1917',
        'panel-2': '#171412',
        'hover': '#292524',
        'soft': '#1c1917',
        'line': '#292524',
        'line-soft': '#1f1b19',
        'line-row': '#292524',
        'edge': '#44403c',
        'edge-2': '#44403c',
        'rule': '#292524',
        'accent': '#3b82f6',
        'accent-2': '#2563eb',
        'accent-3': '#1d4ed8',
        'on-accent': '#ffffff',
        'ok-bg': '#0f2a1a',
        'ok-bg-2': '#0f2a1a',
        'ok-edge': '#166534',
        'ok-text': '#86efac',
    },
}

_current = THEME_DEFAULT


def _settings_path():
    """Same file as the analysis mode (~/.otdrSuite/settings.json)."""
    d = os.environ.get('OTDR_SETTINGS_DIR') or os.environ.get(
        'OTDR_SUITE_APP_DIR') or os.path.join(os.path.expanduser('~'), '.otdrSuite')
    return os.path.join(d, 'settings.json')


def load_theme():
    """The saved theme, or Light.  Never raises."""
    try:
        with open(_settings_path(), encoding='utf-8') as fh:
            name = json.load(fh).get('theme')
    except (OSError, ValueError, AttributeError):
        return THEME_DEFAULT
    return name if name in THEMES else THEME_DEFAULT


def save_theme(name):
    """Persist the theme; other keys in settings.json are kept.  A write
    failure is not fatal (this session still shows the chosen theme)."""
    if name not in THEMES:
        raise ValueError(name)
    path = _settings_path()
    try:
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data['theme'] = name
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(data, fh)
    except OSError:
        pass


def current():
    return _current


def apply_streamlit_theme(name):
    """Point the running server's theme config at `name`.  True when the
    config changed, which means the page on screen still shows the old theme
    until the next run.  Uses Streamlit's internal config setter (theme
    options cannot be set with st.set_option); if that ever goes away the
    App keeps config.toml's Light and nothing breaks."""
    global _current
    if name not in THEMES:
        name = THEME_DEFAULT
    _current = name
    try:
        from streamlit import config as _cfg
    except Exception:
        return False
    changed = False
    for key, val in STREAMLIT[name].items():
        opt = f'theme.{key}'
        try:
            if _cfg.get_option(opt) != val:
                _cfg.set_option(opt, val)
                changed = True
        except Exception:
            pass
    return changed


def color(key, name=None):
    """A palette colour as hex, for HTML inside a components.html iframe."""
    return VARS[name or _current][key]


def css_vars(name=None):
    """The --otdr-* variables for the page, as a <style> block."""
    pal = VARS[name or _current]
    body = ';'.join(f'--otdr-{k}:{v}' for k, v in pal.items())
    return f'<style>:root{{{body}}}</style>'


def recolor(html, name=None):
    """HTML written in the Light colours, in the current theme's.  For pages
    inside a components.html iframe, which cannot see the --otdr-* variables.
    Only the palette's own colours move; flag colours are left alone."""
    name = name or _current
    if name == 'light':
        return html
    import re
    swap = {}
    for k, light in VARS['light'].items():
        if k != 'on-accent':
            swap.setdefault(light.lower(), VARS[name][k])
    pat = re.compile('(?:' + '|'.join(re.escape(h) for h in sorted(swap, key=len, reverse=True))
                     + r')(?![0-9a-fA-F])', re.I)
    return pat.sub(lambda m: swap[m.group(0).lower()], html)

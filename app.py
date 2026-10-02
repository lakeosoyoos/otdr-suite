"""
OTDR Suite — desktop hub
========================
One Streamlit app with a sidebar that switches between:

  • Viewer        — EXFO-style bidirectional trace viewer (zoom/pan, A/B
                    stacking).  Rendered by a small canvas server that runs
                    as a background thread inside this process; embedded here
                    via an iframe.
  • Secret Sauce — duplicate classifier.  Runs in a clean
                    subprocess (it ships its own divergent sor_reader copy,
                    which can't share this process's namespace).

Dev run:   streamlit run app.py
Packaged:  launched by desktop/launcher.py inside OTDRSuite.exe (phase 2).
"""
from __future__ import annotations

import json
import contextlib
import os
import re
import subprocess
import sys
import tempfile
import time

import streamlit as st

from streamlit.components.v1 import iframe as st_iframe
from streamlit.components.v1 import html as st_components_html

# In a frozen build the launcher exports OTDR_SUITE_HOME (the bundle root);
# in dev it's just this file's directory.
HERE = os.environ.get('OTDR_SUITE_HOME') or os.path.dirname(os.path.abspath(__file__))
VIEWER_DIR = os.path.join(HERE, 'viewer')
SECRETSAUCE_DIR = os.path.join(HERE, 'secretsauce')
SPLICEREPORT_DIR = os.path.join(HERE, 'splicereport')
FROZEN = bool(getattr(sys, 'frozen', False))


# ── Analysis mode: OTDR Suite / FastReporter ──────────────────────────────
# One setting, chosen once in the sidebar and remembered across launches,
# that every tool reads:
#
#   'suite'  OTDR Suite   -- our own analysis: the numbers and columns we can
#                           defend from the trace, with our gates and vetoes.
#   'fr'     FastReporter -- reproduce EXFO FastReporter's analysis from the
#                           same .sor pair, to the digit: its event pairing,
#                           positions, losses and types, with only the tech's
#                           pass/fail thresholds applied on top.
#
# This commit is the SETTING and its plumbing only: the value reaches the
# three engine subprocesses (--analysis) and the Viewer (trace_server.CONFIG)
# and is echoed in every manifest, but nothing changes its answer yet.  The
# FastReporter rules land behind it one column at a time, each gated on the
# .bdr answer keys.  Robert, 2026-09-21.
ANALYSIS_MODES = ('suite', 'fr')
# The product's name where a person reads it.  The App's launcher sets
# OTDR_SUITE_EDITION to "OTDR App" (Robert, 2026-10-01: no "Suite" anyone
# sees in the App); the regular exe leaves it unset.  Stored values, file
# formats and identifiers keep the old spelling.
PRODUCT_NAME = os.environ.get('OTDR_SUITE_EDITION') or 'OTDR Suite'
ANALYSIS_MODE_LABELS = {'suite': PRODUCT_NAME, 'fr': 'FastReporter'}
ANALYSIS_MODE_DEFAULT = 'suite'


def _analysis_settings_path():
    """~/.otdrSuite/settings.json -- the launcher already owns that folder
    (engine.meta.json, the update log).  OTDR_SETTINGS_DIR overrides it for
    tests."""
    d = os.environ.get('OTDR_SETTINGS_DIR') or os.environ.get(
        'OTDR_SUITE_APP_DIR') or os.path.join(os.path.expanduser('~'), '.otdrSuite')
    return os.path.join(d, 'settings.json')


def load_analysis_mode():
    """The persisted analysis mode, or the default.  Never raises: a missing
    or damaged settings file means OTDR Suite, the mode every report ran in
    before the setting existed."""
    try:
        with open(_analysis_settings_path(), encoding='utf-8') as fh:
            mode = json.load(fh).get('analysis_mode')
    except (OSError, ValueError, AttributeError):
        return ANALYSIS_MODE_DEFAULT
    return mode if mode in ANALYSIS_MODES else ANALYSIS_MODE_DEFAULT


def save_analysis_mode(mode):
    """Persist the mode.  Other keys in settings.json are kept; a write
    failure is not fatal (the session still runs in the chosen mode)."""
    if mode not in ANALYSIS_MODES:
        raise ValueError(mode)
    path = _analysis_settings_path()
    data = {}
    try:
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data['analysis_mode'] = mode
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(data, fh)
    except OSError:
        pass


def analysis_mode():
    """The mode this session runs in: the sidebar's choice when the app is
    up, else the persisted one.  The engine command builders read it here so
    every subprocess a page launches carries the same mode the sidebar shows."""
    try:
        mode = st.session_state.get('analysis_mode')
    except Exception:
        mode = None
    return mode if mode in ANALYSIS_MODES else load_analysis_mode()


# The Analysis Mode switch (Robert, 2026-09-30): the name of the mode in use
# wears a green halo, and the switch is always drawn "on" (the theme's
# primary colour); only the knob moves, left for FR Mode, right for OTDR
# Mode.  Scoped to the switch's own box so no other toggle changes.
# ─── Light / Dark theme ─────────────────────────────────────────────────
# Robert, 2026-09-29: the boss asked for a dark look, after a dark dashboard
# he liked (warm near-black page, dark grey panels, off-white lettering, a
# blue accent).  Light is the palette the hub has always had.  The
# Viewer's trace plot and event panel stay light (a very light grey) in
# Dark, so the FastReporter trace colours read as always.
#
# Lives here, not in a module of its own: a new engine file would make every
# installed exe refuse the next signed update (the launcher only takes a
# manifest whose file set matches its own ENGINE_FILES).
#
# Every start is Dark, fully: the first frame included (Robert, 2026-10-01:
# "start up in dark mode ... it needs to start fully in dark").  Light lasts
# until the hub is closed or the page reloads, and nothing is saved.  The
# server starts in Dark too (desktop/launcher.py's STREAMLIT_THEME_* and
# .streamlit/config.toml = THEME_STREAMLIT['dark']), so a new page is not
# painted Light first and switched after.  Streamlit reads its theme from
# config, so apply_streamlit_theme() writes the palette into the running
# server's config; the browser takes it on the next run.  What
# the hub draws itself uses the --otdr-* CSS variables (theme_css_vars());
# HTML inside a components.html iframe cannot see them and goes through
# theme_recolor().  The Viewer learns the theme from trace_server.CONFIG,
# when it loads and again on every /api/mode ask, so an open Viewer follows
# the switch.
THEMES = ('light', 'dark')
THEME_DEFAULT = 'dark'

# What Streamlit itself draws: pages, sidebar, widgets, st.dataframe.
THEME_STREAMLIT = {
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
THEME_VARS = {
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

_theme_current = THEME_DEFAULT


def theme_current():
    return _theme_current


def apply_streamlit_theme(name):
    """Point the running server's theme config at `name`.  True when the
    config changed, which means the page on screen still shows the old theme
    until the next run.  Uses Streamlit's internal config setter (theme
    options cannot be set with st.set_option); if that ever goes away the
    App keeps config.toml's Light and nothing breaks."""
    global _theme_current
    if name not in THEMES:
        name = THEME_DEFAULT
    _theme_current = name
    try:
        from streamlit import config as _cfg
    except Exception:
        return False
    changed = False
    for key, val in THEME_STREAMLIT[name].items():
        opt = f'theme.{key}'
        try:
            if _cfg.get_option(opt) != val:
                _cfg.set_option(opt, val)
                changed = True
        except Exception:
            pass
    return changed


def theme_color(key, name=None):
    """A palette colour as hex, for HTML inside a components.html iframe."""
    return THEME_VARS[name or _theme_current][key]


def theme_css_vars(name=None):
    """The --otdr-* variables for the page, as a <style> block."""
    pal = THEME_VARS[name or _theme_current]
    body = ';'.join(f'--otdr-{k}:{v}' for k, v in pal.items())
    return f'<style>:root{{{body}}}</style>'


def theme_recolor(html, name=None):
    """HTML written in the Light colours, in the current theme's.  For pages
    inside a components.html iframe, which cannot see the --otdr-* variables.
    Only the palette's own colours move; flag colours are left alone."""
    name = name or _theme_current
    if name == 'light':
        return html
    swap = {}
    for k, light in THEME_VARS['light'].items():
        if k != 'on-accent':
            swap.setdefault(light.lower(), THEME_VARS[name][k])
    pat = re.compile('(?:' + '|'.join(re.escape(h) for h in sorted(swap, key=len, reverse=True))
                     + r')(?![0-9a-fA-F])', re.I)
    return pat.sub(lambda m: swap[m.group(0).lower()], html)


# ─── end Light / Dark theme ─────────────────────────────────────────────


# The two sidebar switches (Analysis Mode, Theme): title, then
# "left label | switch | right label", every piece centred.
_MODE_SWITCH_CSS = (
    '<style>'
    '.st-key-analysis_mode_box p,.st-key-theme_box p{text-align:center}'
    '.st-key-analysis_mode_box [data-testid="stMarkdownContainer"],'
    '.st-key-theme_box [data-testid="stMarkdownContainer"]'
    '{display:flex;justify-content:center}'
    '.st-key-analysis_mode_box [data-testid="stCheckbox"],'
    '.st-key-theme_box [data-testid="stCheckbox"]{display:flex;justify-content:center}'
    '.st-key-analysis_mode_box [data-testid="stCheckbox"] label,'
    '.st-key-theme_box [data-testid="stCheckbox"] label{margin:0 auto}'
    # The name in use wears a green halo, and the switch is always drawn
    # "on" (the theme's accent colour); only the knob moves (Robert,
    # 2026-09-30).  Names wrap between words, never inside one.
    '.st-key-analysis_mode_box .mode-on,.st-key-theme_box .mode-on{display:inline-block;'
    'padding:0 4px;text-align:center;overflow-wrap:normal;word-break:keep-all;'
    'border-radius:6px;font-weight:700;'
    'box-shadow:0 0 0 2px #22c55e,0 0 8px 2px rgba(34,197,94,.55)}'
    '.st-key-analysis_mode_box .mode-off,.st-key-theme_box .mode-off{display:inline-block;'
    'padding:0 4px;text-align:center;overflow-wrap:normal;word-break:keep-all}'
    '.st-key-analysis_mode_box [data-testid="stCheckbox"] label[data-baseweb="checkbox"]>div:first-child,'
    '.st-key-theme_box [data-testid="stCheckbox"] label[data-baseweb="checkbox"]>div:first-child'
    '{background-color:var(--otdr-accent,#2c5b8a) !important}'
    '.st-key-analysis_mode_box [data-testid="stCheckbox"] label[data-baseweb="checkbox"]>div:first-child>div,'
    '.st-key-theme_box [data-testid="stCheckbox"] label[data-baseweb="checkbox"]>div:first-child>div'
    '{background-color:#ffffff !important}'
    '</style>')


def _mode_name(name, on):
    """One name beside a sidebar switch: haloed when it is the one in use."""
    return '<span class="%s">%s</span>' % ('mode-on' if on else 'mode-off', name)


def _render_theme_control(where):
    """Dark | switch | Light under a "Theme" title (Robert, 2026-09-29): the
    same shape as the Analysis Mode switch.  ui_theme holds the theme; the
    knob is read every run (no on_change).  Knob right = Light."""
    box = where.container(key='theme_box')
    box.markdown(_MODE_SWITCH_CSS, unsafe_allow_html=True)
    box.markdown('**Theme**')
    dark = st.session_state.get('ui_theme') == 'dark'
    # No key: the knob's start is the theme itself (value=), and a keyless
    # widget's identity includes that value, so after every change the
    # switch is a new widget that starts where the theme is.  Keyed versions
    # failed twice in testing: once the sidebar was hidden (the home screen)
    # and shown again, the browser drew the knob in its old position while
    # the theme stayed put, and the next click anywhere flipped it back.
    l, m, r = box.columns([5, 3, 5], vertical_alignment='center')
    l.markdown(_mode_name('Dark', dark), unsafe_allow_html=True)
    _right = m.toggle('Theme', value=not dark, label_visibility='collapsed')
    r.markdown(_mode_name('Light', not dark), unsafe_allow_html=True)
    name = 'light' if _right else 'dark'
    if name != st.session_state.get('ui_theme'):
        st.session_state['ui_theme'] = name
        st.rerun()


def _render_analysis_mode_control():
    """The OTDR Suite / FastReporter switch, right under the Tool list so
    it is visible on every page.  Seeded from settings.json on the first run of a
    session and written back on every change, so a tech's choice survives a
    restart.  session_state.analysis_mode holds the mode; the toggle has no
    key and starts at the mode (value=)."""
    if 'analysis_mode' not in st.session_state:
        st.session_state['analysis_mode'] = load_analysis_mode()
    _on = st.session_state['analysis_mode'] == 'fr'
    # A toggle, not a radio (Robert, 2026-09-22): the setting is one of two
    # states and reads as a switch -- off is OTDR Suite, on is FastReporter.
    # session_state.analysis_mode holds the mode.
    # Robert, 2026-09-24: both modes on show, FR Mode on the left and OTDR
    # Mode on the right, the switch between them; the knob points at the
    # mode in use and that name is bold.  Knob right = OTDR Mode.  A new key
    # (the old 'analysis_toggle' meant the opposite), and value= only when the
    # key is not already set, so value= never fights key=.
    # Centred in the sidebar (Robert, 2026-09-29), the Theme switch below it
    # laid out the same way.
    box = st.container(key='analysis_mode_box')
    box.markdown(_MODE_SWITCH_CSS, unsafe_allow_html=True)
    box.markdown('**Analysis Mode**')
    # No key (2026-09-30): with key='analysis_switch', the App's home screen
    # (no sidebar) and back redrew the knob in its old position while the
    # mode stayed put, and the next page change silently switched OTDR Mode
    # to FR Mode.  Keyless, the knob starts at the mode itself (value=) and
    # is a new widget after every change.
    l, m, r = box.columns([5, 3, 5], vertical_alignment='center')
    l.markdown(_mode_name('FR Mode', _on), unsafe_allow_html=True,
               help=("FR Mode: reproduce EXFO FastReporter's analysis from the same "
                     "files, to the digit, with only your pass/fail thresholds on top."))
    _right = m.toggle('Analysis Mode', value=not _on, label_visibility='collapsed')
    r.markdown(_mode_name('OTDR Mode', not _on), unsafe_allow_html=True,
               help=("OTDR Mode: our own analysis, the numbers and columns we can "
                     "defend from the trace."))
    _picked = not _right                      # True = FR Mode, as before
    _mode = 'fr' if _picked else 'suite'
    if _mode != st.session_state['analysis_mode']:
        st.session_state['analysis_mode'] = _mode
        save_analysis_mode(_mode)
        try:
            trace_server.CONFIG['analysis_mode'] = _mode
        except Exception:
            pass
        st.rerun()


# The Viewer moves the trace server's folders from inside its own page: a
# drop points it at the staged files, and Remove taking the last file off a
# side lets go of that side's folder (trace_server.unload_sides).  Both stamp
# CONFIG['dropped_at'], and the Trace Folders block puts the new folders in
# the A/B boxes on the hub's next run.  Nothing made that run happen, so the
# boxes kept the old path until the tech clicked something (Robert,
# 2026-10-01: removing the files must clear the boxes).  This fragment looks
# every VIEWER_FOLDERS_TICK_S and asks for a whole-page run only when a stamp
# is newer than the one this session last took.
VIEWER_FOLDERS_TICK_S = 1.5


@st.fragment(run_every=VIEWER_FOLDERS_TICK_S)
def _follow_viewer_folders():
    if (trace_server.CONFIG.get('dropped_at') or 0) > st.session_state.get('view_drop_seen', 0):
        st.rerun()


def secretsauce_cmd(folder, out_dir, fmt):
    """Argv to run the Secret Sauce engine in a clean subprocess.
    Frozen: re-invoke this exe with the --run-secretsauce sentinel (the
    launcher dispatches it).  Dev: run the runner .py with python."""
    # The Analysis setting is not passed: Secret Sauce works on the trace
    # samples, not on what FastReporter displays.
    common = ['--folder', folder, '--out-dir', out_dir, '--format', fmt]
    if FROZEN:
        return [sys.executable, '--run-secretsauce', *common]
    return [sys.executable, os.path.join(SECRETSAUCE_DIR, 'run_secretsauce.py'), *common]


def splicereport_cmd(dir_a, dir_b, out_xlsx, site_a, site_b, overrides=None,
                     contract=None, show=None, analysis=None,
                     viewer_table=None):
    """Argv to run the Splice Report engine in a clean subprocess (its own
    sor_reader copy).  Frozen: --run-splicereport sentinel; dev: the runner.

    `overrides` is the engine-global threshold dict from the OTDR settings
    panel (e.g. {'REBURN_THRESHOLD': 0.12, ...}).  It's serialized to JSON
    and forwarded as --overrides so the subprocess can apply it to the
    engine module BEFORE the pipeline runs (the panel lives in this process;
    the engine lives in the subprocess, so the values cross as JSON)."""
    common = ['--dir-a', dir_a, '--dir-b', dir_b, '--out', out_xlsx,
              '--site-a', site_a, '--site-b', site_b,
              '--analysis', analysis or analysis_mode()]
    if overrides:
        common += ['--overrides', json.dumps(overrides)]
    if show:
        common += ['--show', json.dumps(show)]
    if viewer_table:
        # Where the run writes its table for the Viewer's OTDR Suite mode
        # (the report's columns and its numbers for every fibre).
        common += ['--viewer-table', viewer_table]
    if contract:
        # The customer contract's figures for the acquisition audit -- a
        # separate channel from --overrides because it is a different kind
        # of input: overrides change what gets flagged, this is checked and
        # printed and changes nothing.
        common += ['--contract', json.dumps(contract)]
    if FROZEN:
        return [sys.executable, '--run-splicereport', *common]
    return [sys.executable, os.path.join(SPLICEREPORT_DIR, 'run_splicereport.py'), *common]


# How long to let an engine subprocess run before we give up.  A real batch is
# minutes, not hours; past this we assume the engine is wedged.  Headroom for
# large spans (high-resolution 15-second acquisitions with many fibers) — the
# connection fix keeps the UI responsive while it runs, so a longer ceiling is
# safe and lets the boss's big spans finish instead of timing out mid-run.
ENGINE_TIMEOUT_S = 1200


def _read_engine_log(path):
    """Read a temp engine log file back as text, tolerant of odd bytes."""
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as fh:
            return fh.read()
    except OSError:
        return ''


def run_engine(cmd):
    """Run an engine argv in a clean subprocess and return a CompletedProcess.

    Hardened for the frozen Windows build AND to keep the Streamlit server
    answering the browser while a heavy report runs — the boss's
    "Streamlit server is not responding" disconnect on big spans:
      • Engine output is streamed to on-disk temp files, NOT buffered in RAM
        (the old capture_output).  A chatty engine on a large span could
        balloon this process and starve / OOM the server; writing straight to
        disk also removes any OS pipe-buffer deadlock on very verbose runs.
      • The engine runs at BELOW-NORMAL priority (Windows) / nice +10 (POSIX)
        so the OS keeps scheduling the Streamlit server thread.  The browser
        watches a websocket heartbeat answered on that thread; CPU starvation
        by a full-throttle engine is what was dropping it ("not responding").
      • timeout so a wedged engine can't hang forever (TimeoutExpired
        propagates to the caller, which surfaces it in the UI).
      • CREATE_NO_WINDOW on win32 so a windowed build doesn't flash a console.

    Returns a subprocess.CompletedProcess with .stdout/.stderr (str) and
    .returncode, so existing callers are unchanged.
    """
    out_fd, out_path = tempfile.mkstemp(prefix='otdr_eng_out_', suffix='.log')
    err_fd, err_path = tempfile.mkstemp(prefix='otdr_eng_err_', suffix='.log')
    os.close(out_fd)
    os.close(err_fd)
    popen_kwargs = {}
    if sys.platform == 'win32':
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        flags |= getattr(subprocess, 'BELOW_NORMAL_PRIORITY_CLASS', 0)
        popen_kwargs['creationflags'] = flags
    try:
        with open(out_path, 'wb') as fo, open(err_path, 'wb') as fe:
            proc = subprocess.Popen(cmd, stdout=fo, stderr=fe, **popen_kwargs)
            if sys.platform != 'win32':
                # Drop priority post-spawn — thread-safe, no fork-unsafe preexec_fn.
                try:
                    os.setpriority(os.PRIO_PROCESS, proc.pid, 10)
                except (OSError, AttributeError, ValueError):
                    pass
            try:
                proc.wait(timeout=ENGINE_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                raise
        return subprocess.CompletedProcess(
            cmd, proc.returncode,
            stdout=_read_engine_log(out_path),
            stderr=_read_engine_log(err_path))
    finally:
        for p in (out_path, err_path):
            try:
                os.unlink(p)
            except OSError:
                pass


# ─── Background engine runs with live progress (keeps the server responsive) ──
# subprocess.Popen runs the engine concurrently, so the Streamlit script thread
# stays free and the server keeps answering the browser's websocket heartbeat.
# We poll it across reruns and tail its (unbuffered) stderr for a live status
# line + a Cancel button — so big spans never freeze the page or drop the
# connection, and the tech can see it's working.  This is the "harden further"
# path; run_engine() above remains for any synchronous caller.
def _engine_start(cmd):
    """Launch an engine subprocess in the background (non-blocking).  Output is
    streamed to temp files, the engine runs at lowered priority, and its child
    Python is unbuffered so the UI can tail live progress.  Returns a job dict
    held in st.session_state across reruns."""
    out_fd, out_path = tempfile.mkstemp(prefix='otdr_eng_out_', suffix='.log')
    err_fd, err_path = tempfile.mkstemp(prefix='otdr_eng_err_', suffix='.log')
    os.close(out_fd)
    os.close(err_fd)
    fo = open(out_path, 'wb')
    fe = open(err_path, 'wb')
    env = dict(os.environ)
    env['PYTHONUNBUFFERED'] = '1'          # flush engine stderr live for the tail
    popen_kwargs = dict(stdout=fo, stderr=fe, env=env)
    if sys.platform == 'win32':
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        flags |= getattr(subprocess, 'BELOW_NORMAL_PRIORITY_CLASS', 0)
        popen_kwargs['creationflags'] = flags
    proc = subprocess.Popen(cmd, **popen_kwargs)
    if sys.platform != 'win32':
        try:
            os.setpriority(os.PRIO_PROCESS, proc.pid, 10)
        except (OSError, AttributeError, ValueError):
            pass
    return {'proc': proc, 'fo': fo, 'fe': fe, 'out_path': out_path,
            'err_path': err_path, 'started': time.monotonic(),
            'state': 'running', 'result': None}


def _engine_finish_files(job):
    for fh in (job.get('fo'), job.get('fe')):
        try:
            if fh and not fh.closed:
                fh.flush()
                fh.close()
        except (OSError, ValueError):
            pass


def _engine_poll(job, timeout_s):
    """Return 'running' | 'done' | 'timeout' | 'cancelled'.  On finish, fills
    job['result'] with a subprocess.CompletedProcess."""
    if job['state'] != 'running':
        return job['state']
    rc = job['proc'].poll()
    if rc is None:
        if time.monotonic() - job['started'] > timeout_s:
            job['proc'].kill()
            job['proc'].wait()
            job['state'] = 'timeout'
            _engine_finish_files(job)
        return job['state']
    job['state'] = 'done'
    _engine_finish_files(job)
    job['result'] = subprocess.CompletedProcess(
        job['proc'].args, rc,
        stdout=_read_engine_log(job['out_path']),
        stderr=_read_engine_log(job['err_path']))
    return 'done'


def _engine_tail(job, n=1, stream='err'):
    """Last n non-empty lines of the engine's (live) stdout or stderr log.

    `stream='out'` matters on a timeout: the engine narrates its phases on
    STDOUT ("Loaded 120 .sor files from ...", "Computing pair metrics for 120
    files (7140 pairs)...", "XLSX: ..."), and that narration is the only
    record of how far it got before it was killed."""
    key = 'out_path' if stream == 'out' else 'err_path'
    try:
        with open(job[key], 'r', encoding='utf-8', errors='replace') as fh:
            lines = [ln.strip() for ln in fh.read().splitlines() if ln.strip()]
        return lines[-n:]
    except (OSError, KeyError):
        return []


def _engine_cancel(job):
    try:
        job['proc'].kill()
        job['proc'].wait(timeout=5)
    except Exception:
        pass
    job['state'] = 'cancelled'
    _engine_finish_files(job)


def _engine_cleanup(job):
    _engine_finish_files(job)
    for p in (job.get('out_path'), job.get('err_path')):
        try:
            os.unlink(p)
        except (OSError, TypeError):
            pass


def _flag_cancel(cancel_key):
    st.session_state[cancel_key] = True


def _count_input_files(folder):
    """(n_files, n_bytes) under `folder`, or (None, None) if it can't be read.

    Answers the first question every timeout report raises and nobody could
    previously answer: how much was it actually asked to chew?  Mark Jack's
    1200 s timeout (error #20) cost an investigation that a file count would
    have settled outright — the engine never reports on timeout, so the hub
    has to count.  Cheap (a stat per file) and only ever runs on the error
    path.  Capped so a pathological tree can't stall the error report itself."""
    try:
        n = 0
        total = 0
        for root, _dirs, files in os.walk(folder):
            for fn in files:
                if not fn.lower().endswith(('.sor', '.trc', '.json')):
                    continue
                n += 1
                try:
                    total += os.path.getsize(os.path.join(root, fn))
                except OSError:
                    pass
                if n >= 100_000:            # absurd-tree guard
                    return n, total
        return n, total
    except Exception:
        return None, None


# The run-time panel redraws itself twice a second, so the whole seconds it
# prints never skip a number.
ENGINE_TICK_S = 0.5


@st.fragment(run_every=ENGINE_TICK_S)
def _engine_live_panel(prefix, running_title, timeout_s):
    """The run-time panel: seconds so far, the engine's current step, Cancel.

    A fragment, so keeping it up to date re-runs THIS FUNCTION ONLY.  The
    panel used to be redrawn by re-running the whole page every 0.8 s, which
    made the count only as steady as everything above it on the page: the
    sidebar's update check (a web request every 5 minutes, 3 s or more when
    the connection is poor) and every folder check on the way down.  While a
    slow pass was on its way here the old panel stayed on screen, Streamlit
    fades anything not redrawn within half a second, and the count stood
    still.  A tech saw exactly that: the panel went dim and bright and the
    seconds stopped for a few seconds (2026-09-28).

    Who asks for the next redraw.  The page draws the panel once.  The
    browser's timer (run_every) asks for the first redraw on its own, and
    from then on each redraw asks for the next one itself, from here.  The
    browser's timer alone is not enough: a browser slows its timers in a
    window that is not in front, and the panel tells the tech they can go
    and do something else.

    The wait comes AFTER the drawing, never before it: what is on screen from
    the last redraw fades while a redraw is on its way.

    Hands back to the page with a whole-page rerun as soon as there is
    something for the page to do: the run finished, timed out, was cancelled,
    or is gone."""
    on_page_pass = st.session_state.pop(f'{prefix}_panel_once', False)
    job = st.session_state.get(f'{prefix}_job')
    cancel_key = f'{prefix}_cancel'
    if (job is None or st.session_state.get(cancel_key)
            or _engine_poll(job, timeout_s) != 'running'):
        st.rerun()
    elapsed = int(time.monotonic() - job['started'])
    st.info(f'⏳ {running_title}: {elapsed}s elapsed. '
            'You can leave this open or keep working; cancel below if needed.')
    tail = _engine_tail(job, 1)
    if tail:
        st.caption(f'current step · {tail[0][:140]}')
    st.button('Cancel Run', key=f'{prefix}_cancel_btn',
              on_click=_flag_cancel, args=(cancel_key,))
    if on_page_pass:
        return          # a redraw of this panel alone cannot be asked for from a page pass
    time.sleep(ENGINE_TICK_S)
    # Reading the state lets Streamlit take over here for a click made on the
    # page during the wait, before the next redraw is asked for.
    st.session_state.get(cancel_key)
    st.rerun(scope='fragment')


def run_engine_live(prefix, *, running_title, timeout_s=None):
    """Drive a background engine run across reruns with a live progress panel and
    a Cancel button.  Start it by setting st.session_state[f'{prefix}_pending_cmd'].

    Returns the finished subprocess.CompletedProcess when done, or None if there
    is nothing to run, the run was cancelled, or it is still running.  While
    the engine is running it draws the progress panel, which keeps itself up
    to date from then on (see _engine_live_panel); the caller draws nothing
    of its own under it and returns, so the rest of the page is drawn as
    usual.  Raises subprocess.TimeoutExpired if the engine exceeds the
    timeout, so the caller's existing TimeoutExpired handler fires."""
    timeout_s = ENGINE_TIMEOUT_S if timeout_s is None else timeout_s
    pend_key = f'{prefix}_pending_cmd'
    job_key = f'{prefix}_job'
    cancel_key = f'{prefix}_cancel'

    # Start a pending run.
    if job_key not in st.session_state and pend_key in st.session_state:
        st.session_state[job_key] = _engine_start(st.session_state.pop(pend_key))
        st.session_state.pop(cancel_key, None)

    job = st.session_state.get(job_key)
    if job is None:
        return None

    # Cancel requested (set by the Cancel button's on_click before this rerun).
    if st.session_state.pop(cancel_key, False):
        _engine_cancel(job)
        _engine_cleanup(job)
        st.session_state.pop(job_key, None)
        st.info('Run canceled.')
        return None

    state = _engine_poll(job, timeout_s)
    if state == 'running':
        st.session_state[f'{prefix}_panel_once'] = True
        _engine_live_panel(prefix, running_title, timeout_s)
        return None

    proc = job.get('result')
    args = job['proc'].args
    # A timeout kills the engine and _engine_cleanup then DELETES its logs, so
    # the one artifact that says HOW FAR IT GOT was being discarded at exactly
    # the moment it mattered.  Every timeout therefore arrived as a bare
    # "engine exceeded 1200s" with nothing to diagnose — five of them so far,
    # none ever explained.  Read the tail before cleanup and carry it on the
    # exception, which is enough to name the phase: no "Loaded N files" line
    # means it died STAGING (copying the folder), which is the expensive part
    # when the source is a network or Parallels share.
    tail_out, tail_err = [], []
    if state == 'timeout':
        tail_out = _engine_tail(job, n=8, stream='out')
        tail_err = _engine_tail(job, n=4, stream='err')
    _engine_cleanup(job)
    st.session_state.pop(job_key, None)
    if state == 'timeout':
        raise subprocess.TimeoutExpired(
            args, timeout_s,
            output='\n'.join(tail_out) or None,
            stderr='\n'.join(tail_err) or None)
    return proc


# Repo root on path so the stdlib-only error_report module imports (in the hub
# AND in trace_server, which lives in viewer/).
if HERE not in sys.path:
    sys.path.insert(0, HERE)
try:
    from error_report import report_error, version_labels, maybe_report_update
except Exception:                                  # reporting is best-effort
    def report_error(*a, **k):
        pass

    def version_labels(*a, **k):                   # build identity unknown → dev
        return ('dev', 'dev')

    def maybe_report_update(*a, **k):
        return False


def _app_version():
    """Human-readable app build — "build 54 (2026-07-14)" from the CI-written
    version.json bundled next to the exe, or "dev" in a dev checkout.  The
    lookup lives in error_report.version_labels (stdlib-only, shared with the
    Slack error payload) so the sidebar and the error reports can never
    disagree about which build this is."""
    try:
        return version_labels()[0]
    except Exception:
        return 'dev'


def _engine_version():
    """Which engine code this session runs: 'bundled' (as frozen into the exe),
    'update N applied' (launcher-verified signed update from the cache — N is
    the manifest version the launcher records in ~/.otdrSuite/engine.meta.json
    on every verified swap), or 'dev' outside the launcher."""
    try:
        return version_labels()[1]
    except Exception:
        return 'dev'


# ─── Update plumbing (shared: startup nudge + sidebar-footer button) ──────
# These live up here because the startup nudge renders ABOVE the page radio,
# while the manual '🔄 Check for updates' footer renders last.  Both call the
# SAME helpers — there is exactly one copy of the version compare and of the
# restart, and only the launcher ever applies an update.
RESTART_ENV = 'OTDR_SUITE_RESTART_FROM'   # must match desktop/launcher.py
# Env the launcher DERIVES at every boot.  The relaunched exe must work them
# out afresh, not inherit this session's answers (a cleared cache pin, a
# different engine dir).
_LAUNCHER_DERIVED_ENV = ('OTDR_SUITE_HOME', 'OTDR_SUITE_SOURCE',
                         'OTDR_SUITE_CACHE_PINNED', 'OTDR_SUITE_NEEDS_INSTALL',
                         'OTDR_SUITE_ENGINE_FILES')
STALE_RECHECK_S = 300                # re-ask the manifest at most every 5 min


def _parse_engine_version(appv, engv):
    """Best-effort integer version of the RUNNING engine, for the update
    check.  'update N applied …' → N; 'bundled…' → the app build number (a
    build-N exe bundles engine N); anything else (dev) → None."""
    import re as _re
    m = _re.search(r'update (\d+) applied', engv or '')
    if m:
        return int(m.group(1))
    if (engv or '').startswith('bundled'):
        m = _re.search(r'build (\d+)', appv or '')
        if m:
            return int(m.group(1))
    return None


def _latest_manifest(timeout=8):
    """The live signed manifest, parsed — DISPLAY-ONLY.  No code is fetched
    and nothing here is trusted: applying an update stays exclusively in the
    frozen launcher's signed fetch/verify/swap at boot (the signing key lives
    there; an auto-updatable file must never carry the trust anchor).  The hub
    reads two things off it: the version, to say whether this session is
    behind, and the file list, to say whether a restart could even catch up
    (see _needs_install).  Returns None when the server is unreachable or the
    body is not a manifest.  None too when this build does not auto-update
    (OTDR_SUITE_NO_UPDATE): no banner offers an update it would never apply."""
    if os.environ.get('OTDR_SUITE_NO_UPDATE'):
        return None
    import urllib.request
    # The launcher names the feed it applies (OTDR_SUITE_MANIFEST_URL) and the
    # channel it accepts, so the banner never reports an update the launcher
    # would refuse.  Unset: main's feed, no channel (the regular edition).
    url = (os.environ.get('OTDR_SUITE_MANIFEST_URL')
           or 'https://raw.githubusercontent.com/lakeosoyoos/otdr-suite/main/'
              'update_manifest.json')
    channel = os.environ.get('OTDR_SUITE_UPDATE_CHANNEL', '')
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'OTDRSuite'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            manifest = json.loads(r.read().decode('utf-8'))
        int(manifest['version'])
        if channel and manifest.get('channel', '') != channel:
            return None
        return manifest
    except Exception:
        return None


def _latest_manifest_version(timeout=8):
    """Version number of the live manifest, or None (see _latest_manifest)."""
    manifest = _latest_manifest(timeout)
    return None if manifest is None else int(manifest['version'])


def _nudge_check(fetch, applied):
    """Decision half of the startup nudge — split out with its fetcher and the
    applied version as PARAMETERS so it is testable with no network and no
    browser.  Returns (latest, applied) when the published version is newer
    than the engine this session runs, else None.

    Fail-silent by construction: an unknown running version (a dev checkout,
    which can't be updated anyway) short-circuits BEFORE the fetch, and any
    fetch failure — offline, timeout, garbled manifest — returns None.  A tech
    never sees an error they can't act on; the manual 'Check for updates'
    button stays the loud path that explains what went wrong."""
    if applied is None:
        return None
    try:
        latest = fetch()
    except Exception:
        return None
    if latest is None:
        return None
    return (latest, applied) if latest > applied else None


def _stale_check(store, now, ttl, fetch, applied_fn):
    """TTL cache around _nudge_check — the ONE staleness answer the whole hub
    uses, for both the sidebar banner and the report block.

    Split out with its store, clock, fetcher and version-reader as PARAMETERS
    (the _nudge_check pattern) so the caching is testable with a plain dict and
    no network, no clock and no browser.

    Why a TTL rather than the once-per-session cache this replaces: an
    always-on machine holds one Streamlit session for days, so a one-shot check
    at first render means a publish that lands at 09:00 is never noticed.  A
    TTL re-asks on the first rerun after `ttl` seconds — a tech clicking
    around does not re-fetch, an idle tab does not poll, and the answer still
    goes stale within minutes rather than never.

    FAILS OPEN in every direction: `applied_fn` raising (a dev checkout, a
    garbled version.json) degrades to None, which makes _nudge_check
    short-circuit BEFORE the fetch, and _nudge_check already swallows every
    fetch failure.  A negative answer here always means 'not known to be
    stale', never 'could not tell'."""
    hit = store.get('upd_state')
    if hit is not None and (now - hit[0]) < ttl:
        return hit[1]
    try:
        applied = applied_fn()
    except Exception:
        applied = None
    res = _nudge_check(fetch, applied)
    store['upd_state'] = (now, res)
    return res


def _update_state():
    """(latest, running) when this session's engine is behind the published
    one, else None.  Binds _stale_check to Streamlit's session store and the
    real clock/fetcher; every caller in the hub goes through here.

    The fetch also keeps the manifest's file list in the session, under the
    same TTL as the answer, for _needs_install: one fetch, one window, so the
    banner can never say 'restart' off one manifest and 'install' off
    another."""
    def fetch():
        manifest = _latest_manifest(timeout=3)
        st.session_state['upd_manifest_files'] = list(
            (manifest or {}).get('files') or ())
        return None if manifest is None else int(manifest['version'])
    return _stale_check(
        st.session_state, time.time(), STALE_RECHECK_S, fetch,
        lambda: _parse_engine_version(_app_version(), _engine_version()))


# ── Forced update: only at the top of the hour ───────────────────────────
# A published update does not stop reports the moment this copy sees it.
# Every merge publishes one, and blocking every open copy within five minutes
# stopped techs mid-job several times a day.  A copy that falls behind keeps
# running reports until the next top of the hour on its own clock; from then
# on reports pause until it updates to whatever is current.  So updates can
# go out at any time, and a tech is made to update at most once an hour,
# always on the hour.
def _next_top_of_hour(since, utc_offset_s=0):
    """Epoch seconds of the first top of the hour AFTER `since`, on a clock
    `utc_offset_s` ahead of UTC.  Pure, for the tests.  The offset only
    matters in a half-hour time zone: whole-hour zones share UTC's minute
    hand.  A `since` exactly on the hour gets the whole next hour."""
    local = since + utc_offset_s
    return (local // 3600 + 1) * 3600 - utc_offset_s


def _behind_since_path():
    return os.path.join(os.path.expanduser('~'), '.otdrSuite',
                        'update_behind_since.json')


def _behind_since(running, now):
    """When this machine first saw engine `running` behind the published one.
    Kept on disk so closing and reopening the app does not restart the clock;
    keyed by the running version, so updating starts a fresh one.  A time
    later than `now` (the clock was wrong when it was written) is replaced,
    or the hour it names might never come."""
    path = _behind_since_path()
    try:
        with open(path, encoding='utf-8') as f:
            rec = json.load(f)
        if int(rec.get('running')) == running and float(rec['since']) <= now:
            return float(rec['since'])
    except Exception:
        pass
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'running': running, 'since': now}, f)
    except OSError:
        pass
    return now


def _update_due(running, now=None):
    """(due, deadline) for a copy that is behind.  `deadline` is the first top
    of the hour after this machine saw engine `running` fall behind, in epoch
    seconds; `due` is True once it has passed and reports must wait."""
    now = time.time() if now is None else now
    since = _behind_since(running, now)
    deadline = _next_top_of_hour(since, time.localtime(since).tm_gmtoff)
    return now >= deadline, deadline


def _fmt_clock(ts):
    """'11:00 AM' on this machine's clock."""
    t = time.localtime(ts)
    return (f'{t.tm_hour % 12 or 12}:{t.tm_min:02d} '
            f'{"AM" if t.tm_hour < 12 else "PM"}')


def _deadline_note(running):
    """The sentence the sidebar adds while reports still run, or '' once the
    hour has passed.  Leading space: it follows another sentence."""
    _pn = globals().get('PRODUCT_NAME', 'OTDR Suite')   # alone in a test: the default
    due, deadline = _update_due(running)
    if due:
        return ''
    return (f' Reports keep working until {_fmt_clock(deadline)}, then pause '
            'until ' + _pn + ' is updated.')


# Shown on a report page while this copy is behind but the hour has not come.
UPDATE_HEADS_UP_MSG = (
    'Update {latest} is available (running {running}). Reports keep working '
    'until {when}. After that, OTDR Suite needs to update before it runs '
    'another report.'
)

# Shown in place of a report when the engine is behind.  It has to answer the
# tech's first question — "why won't it let me?" — or the next move is a phone
# call, not a restart.
STALE_BLOCK_MSG = (
    '🔒 **Report generation is paused: OTDR Suite needs a restart.**\n\n'
    'This session is running **engine {running}**, but **engine {latest}** '
    'has been published. Different engines can print different numbers for '
    'the same traces, so reports are held until this copy is up to date.\n\n'
    '**Nothing is lost.** Finish what you are doing, then restart when you '
    'are ready. The update is verified and applied at launch.'
)

# The same block when a restart cannot clear it: the published engine adds
# files this exe cannot download (see _needs_install).  Telling the tech to
# restart here is an instruction that can never work.
INSTALL_BLOCK_MSG = (
    '🔒 **Report generation is paused: OTDR Suite needs a fresh install.**\n\n'
    'This session is running **engine {running}**, but **engine {latest}** '
    'has been published, and it adds files this copy of OTDR Suite cannot '
    'download on its own, so Update & Restart will not apply it. Different '
    'engines can print different numbers for the same traces, so reports are '
    'held until this copy is up to date.\n\n'
    '**Nothing is lost.** Finish what you are doing, close OTDR Suite '
    'completely, then download and run the installer: {url}'
)
# The product's name where the tech reads it (the App: "OTDR App").  A second
# assignment, as INSTALLER_URL's below, so the literals stay readable to the
# tests that lift them.
UPDATE_HEADS_UP_MSG, STALE_BLOCK_MSG, INSTALL_BLOCK_MSG = (
    m.replace('OTDR Suite', PRODUCT_NAME)
    for m in (UPDATE_HEADS_UP_MSG, STALE_BLOCK_MSG, INSTALL_BLOCK_MSG))


def _report_gate(key):
    """Block report generation on a stale engine.  Renders the explanation
    plus the SAME one-click restart the banner and footer use, and returns the
    (latest, running) pair when the caller must disable its Run/Generate
    control — falsy when the tech may run.

    The block starts at the top of the hour, not at the publish (see
    _update_due).  Until then the page says when it will start and the
    report runs.

    A hard block is only safe with a way forward, so the restart button is
    rendered right next to the message: a tech who is told 'no' and given no
    button is stranded, which is worse than the staleness.

    FAILS OPEN.  Offline, a timed-out manifest, a garbled version, a dev
    checkout — anything that is not a positively-determined newer version
    returns None and the report runs.  A tech in a truck with no signal must
    still be able to work; blocking on a FAILED CHECK would be an outage of
    our own making."""
    _pn = globals().get('PRODUCT_NAME', 'OTDR Suite')   # alone in a test: the default
    try:
        stale = _update_state()
    except Exception:
        return None                       # never block on a broken check
    if not stale:
        return None
    latest, running = stale
    try:
        due, deadline = _update_due(running)
    except Exception:
        return None                       # never block on a broken check
    if not due:
        st.info(UPDATE_HEADS_UP_MSG.format(latest=latest, running=running,
                                           when=_fmt_clock(deadline)))
        return None
    if _needs_install():
        st.error(INSTALL_BLOCK_MSG.format(latest=latest, running=running,
                                          url=INSTALLER_URL))
        return stale
    st.error(STALE_BLOCK_MSG.format(latest=latest, running=running))
    if getattr(sys, 'frozen', False):
        if st.button('⬇ Update & Restart Now', key=f'{key}_stale_restart',
                     type='primary'):
            if _relaunch_and_exit():
                _render_restart_watchdog()
            else:
                st.error(f'Couldn\'t start the restart. Close {_pn} '
                         'completely and open it again to pick up the update.')
    else:
        st.caption('Restart the app to apply. Updates install at launch.')
    return stale


def _restart_marker_path():
    """Where the restart helper records 'the old instance never let go' — the
    app dir the launcher already owns (log + engine.meta.json live there)."""
    # OTDR_SUITE_APP_DIR: the launcher's own folder when it is an edition
    # installed beside OTDR Suite.
    d = (os.environ.get('OTDR_SUITE_APP_DIR')
         or os.path.join(os.path.expanduser('~'), '.otdrSuite'))
    return os.path.join(d, 'update_restart_blocked')


def _restart_spawn_args(exe, pid, environ, os_name=None):
    """(argv, Popen kwargs) for the exe that takes over after an update click.

    The new instance is THIS SAME EXE, started with RESTART_ENV = our pid.
    The launcher reads that variable before anything else and waits for our
    server to stop answering (launcher._drain_old_instance) BEFORE its
    already-serving guard runs — so it can never re-attach to the dying
    instance and skip the update.  It is started detached / in its own
    session so our exit a moment later cannot take it down with us.

    This replaces a detached PowerShell (Windows) or sh+curl (POSIX) helper
    that did the waiting and then started the exe.  The Windows helper never
    ran on a Windows box before it shipped, and the boss reported that
    Update did not restart the app; a hidden, console-less PowerShell has
    several ways to die without a trace.  The exe has none of them.

    `os_name` defaults to this machine's os.name; the tests pass it so BOTH
    shapes are asserted wherever CI runs."""
    env = {k: v for k, v in dict(environ).items()
           if k not in _LAUNCHER_DERIVED_ENV}
    env[RESTART_ENV] = str(pid)
    kw = {'env': env, 'close_fds': True}
    if (os_name or os.name) == 'nt':
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: no console, no shared
        # Ctrl-C group, and it outlives us.
        kw['creationflags'] = 0x00000008 | 0x00000200
    else:
        kw['start_new_session'] = True
    return [exe], kw


def _relaunch_and_exit():
    """Restart the frozen app so the launcher's boot path applies the update.

    Sequencing matters: the launcher's already-running guard runs BEFORE its
    signed-update path, so the OLD instance must be gone before the NEW one
    health-checks — otherwise it re-attaches to the dying server and the
    update never lands.  The new exe does that waiting itself (see
    _restart_spawn_args + launcher._drain_old_instance); we start it and exit
    a moment later.

    Streamlit's client reconnects on its own, but it does not re-render: the
    page comes back showing the OLD engine's output (measured — see
    _restart_watchdog_html).  Every caller therefore renders
    _render_restart_watchdog(), which reloads once the new server answers."""
    import subprocess as _sp
    import threading as _th
    marker = _restart_marker_path()
    try:
        os.remove(marker)                # stale marker from an earlier attempt
    except OSError:
        pass
    argv, kw = _restart_spawn_args(sys.executable, os.getpid(), os.environ)
    try:
        _sp.Popen(argv, **kw)
    except Exception as exc:
        report_error('update restart', exc)
        return False
    _th.Timer(0.7, lambda: os._exit(0)).start()
    return True


# How long the watchdog waits for the new instance before handing the tech a
# manual way out.  A restart is a whole launcher boot — the old server drains,
# then the signed manifest is fetched, hashes verified and files swapped, and
# only then does Streamlit bind the port.  Half a minute is typical; a slow
# link or a large swap is several times that, so the deadline is generous.  It
# exists to stop the spinner lying forever, not to bound the restart.
RESTART_RECONNECT_TIMEOUT_S = 240
RESTART_HEALTH_PATH = '/_stcore/health'   # what the launcher + helper poll too


def _restart_watchdog_html(timeout_s=RESTART_RECONNECT_TIMEOUT_S):
    """HTML for the post-restart reconnect watchdog.  Split from the render
    call so it is testable as a pure string.

    WHY A RELOAD AND NOT JUST A RECONNECT.  Streamlit's client does reconnect
    on its own — measured against a plain 1.50 app with no watchdog, it sat on
    "Connection error: Streamlit server is not responding" for the whole
    outage and then recovered by itself from both a 45 s and a >4 min kill.
    The modal is what it shows WHILE retrying; it is not a give-up state.

    What it does NOT do is re-render.  On both recoveries the page came back
    still showing the render from BEFORE the outage, served by a brand-new
    process that has never heard of that session.  After "Update & restart
    now" that is the dangerous case: the new process is running a NEW ENGINE,
    and the tech is looking at the old engine's output on a page that looks
    perfectly live.  Different engines print different numbers for the same
    traces — that is the whole reason the restart exists — so a page that
    silently keeps the old ones is worse than one that plainly looks dead.

    So the watchdog waits for a new server and RELOADS, which is the only way
    to get a fresh session rendered by the engine that is actually running.
    It polls from inside a components iframe that is ALREADY LOADED in the
    browser, so it outlives the server that served it and keeps working while
    the websocket is down.

    Two guards, because a reload starts a FRESH session and would throw away
    the tech's loaded span:
      • it is armed only by an actual restart click, never on every page, and
      • it reloads only after the old server has been SEEN to go away, so a
        blip that leaves the process alive can never trigger it.

    WHY IT PAINTS OVER THE PAGE.  The caption alone was not enough, and the
    reason is measurable: roughly six seconds after the old process exits,
    Streamlit's own client puts up a "Connection error" modal with a dimmed
    full-page backdrop, and OUR line — 13 px of grey text in the sidebar — is
    underneath it.  Worse, the launcher opens the hub on 127.0.0.1, and
    Streamlit picks its wording by `hostname === "localhost"`, so what the
    tech reads is "Streamlit server is not responding. Are you connected to
    the internet?" — a question about their WiFi, during an update that is
    working perfectly.  Both the boss and a tech closed the app rather than
    wait out a restart that would have finished on its own.

    So the watchdog claims the screen the moment it is armed: a full-viewport
    panel in the PARENT document (a srcdoc iframe is same-origin, so it may
    reach out), above the modal's z-index, saying what is happening and that
    the app must be left open.  Painting it immediately is safe — arming and
    the 0.7 s exit are the same click, so the page IS going down.  The
    down-then-up guard above is about the RELOAD, and is untouched by this.
    If the parent is ever unreachable, `say` still writes the in-iframe
    caption, which is what the strip is for.
    """
    _pn = globals().get('PRODUCT_NAME', 'OTDR Suite')   # alone in a test: the default
    return """
<div id="wd" style="font-family:sans-serif;font-size:13px;color:#000000"></div>
<script>
(function(){
  var HEALTH   = "__HEALTH__";
  var POLL_MS  = 1500;
  var DEADLINE = Date.now() + __TIMEOUT_MS__;
  var sawDown  = false;
  var note     = document.getElementById("wd");
  var msg      = null;          // the overlay's line in the PARENT document
  var esc      = null;          // its manual way out, revealed only on give-up
  var spin     = null;
  var hint     = null;
  // srcdoc iframes inherit the parent's base URL, so a relative probe already
  // hits the hub — but resolve the origin explicitly when we're allowed to,
  // so a future non-srcdoc component host doesn't silently probe itself.
  function healthUrl(){
    try { return window.parent.location.origin + HEALTH; } catch(e){ return HEALTH; }
  }
  function reloadHub(){
    try { window.parent.location.reload(); return true; } catch(e){ return false; }
  }
  function pdoc(){ try { return window.parent.document; } catch(e){ return null; } }
  // Cover the hub before Streamlit's own "Connection error" modal can, and in
  // the theme the tech is actually running — the panel reads its colours off
  // the live app, so a dark theme doesn't get a white flash.
  function paint(){
    var d = pdoc();
    if (!d || !d.body || d.getElementById("otdr-restart-overlay")) return;
    var bg = "#ffffff", fg = "#31333f";
    try {
      var host = d.querySelector(".stApp") || d.body;
      var cs   = window.parent.getComputedStyle(host);
      if (cs && cs.backgroundColor &&
          cs.backgroundColor.replace(/ /g, "") !== "rgba(0,0,0,0)") bg = cs.backgroundColor;
      if (cs && cs.color) fg = cs.color;
    } catch(e){}
    var el = d.createElement("div");
    el.id = "otdr-restart-overlay";
    el.style.cssText =
      "position:fixed;top:0;left:0;right:0;bottom:0;z-index:2147483647;"
      + "background:" + bg + ";color:" + fg + ";font-family:inherit;"
      + "display:flex;flex-direction:column;align-items:center;"
      + "justify-content:center;text-align:center;padding:24px";
    el.innerHTML =
        '<style>@keyframes otdrspin{to{transform:rotate(360deg)}}</style>'
      + '<div id="otdr-restart-spin" style="width:26px;height:26px;'
      + 'margin-bottom:20px;border:3px solid currentColor;'
      + 'border-top-color:transparent;border-radius:50%;opacity:.35;'
      + 'animation:otdrspin 900ms linear infinite"></div>'
      + '<div style="font-size:22px;font-weight:600">Updating OTDR Suite\u2026</div>'
      + '<div id="otdr-restart-msg" style="font-size:15px;margin-top:12px;'
      + 'max-width:32em;line-height:1.6;opacity:.75"></div>'
      + '<div id="otdr-restart-hint" style="font-size:13px;margin-top:20px;'
      + 'opacity:.55">Leave this window open \u2014 closing OTDR Suite now '
      + 'just means starting the update over.</div>'
      + '<button id="otdr-restart-esc" style="display:none;margin-top:22px;'
      + 'padding:8px 18px;font-size:14px;cursor:pointer">Reload This Page</button>';
    d.body.appendChild(el);
    msg  = d.getElementById("otdr-restart-msg");
    spin = d.getElementById("otdr-restart-spin");
    hint = d.getElementById("otdr-restart-hint");
    esc  = d.getElementById("otdr-restart-esc");
    if (esc) esc.onclick = function(){ reloadHub(); };
  }
  function say(t){
    if (note) note.textContent = t;      // fallback: parent unreachable
    if (msg)  msg.textContent  = t;
  }
  // Give-up state: stop pretending to work, and swap the "leave it open" hint
  // for the one instruction that still helps — it contradicts the button we
  // are about to reveal.
  function bail(t){
    say(t);
    if (spin) spin.style.display = "none";
    if (hint) hint.textContent =
      "If it doesn't come back, close OTDR Suite completely and open it again.";
    if (esc)  esc.style.display  = "inline-block";
  }
  function alive(){
    return fetch(healthUrl(), {cache:"no-store"})
      .then(function(r){ return r.ok; })
      .catch(function(){ return false; });
  }
  function tick(){
    if (Date.now() > DEADLINE){
      bail("The restart is taking longer than expected \u2014 reload this page to continue.");
      return;
    }
    alive().then(function(up){
      if (!up){
        sawDown = true;
        say("Applying the update\u2026 this page comes back on its own.");
      } else if (sawDown){
        say("Reconnecting\u2026");
        if (!reloadHub())
          bail("The update is applied \u2014 reload this page to continue.");
        return;                       // reload replaces us; stop polling
      }
      setTimeout(tick, POLL_MS);
    });
  }
  paint();
  say("Applying the update\u2026 this page comes back on its own.");
  tick();
})();
</script>
""".replace('__HEALTH__', RESTART_HEALTH_PATH) \
   .replace('OTDR Suite', _pn) \
   .replace('__TIMEOUT_MS__', str(int(timeout_s) * 1000))


def _render_restart_watchdog(sidebar=False):
    """Render the watchdog after a restart has been kicked off."""
    if sidebar:
        with st.sidebar:                  # `st` itself is not a context manager
            st_components_html(theme_recolor(_restart_watchdog_html()), height=40)
    else:
        st_components_html(theme_recolor(_restart_watchdog_html()), height=40)


# Permanent link: CI rewrites this asset on every successful build, so it is
# always the newest signed installer.
INSTALLER_URL = ('https://github.com/lakeosoyoos/otdr-suite/releases/download/'
                 'windows-build/OTDRSuite-Setup.exe')
# An edition with its own installer (OTDR Suite App) names its link through
# OTDR_SUITE_INSTALLER_URL.  A second assignment so the literal above stays
# readable to the tests that lift it; __import__ because tests also run this
# block on its own, without the hub's imports.
INSTALLER_URL = (__import__('os').environ.get('OTDR_SUITE_INSTALLER_URL')
                 or INSTALLER_URL)
_CACHE_PINNED_ENV = 'OTDR_SUITE_CACHE_PINNED'   # set by desktop/launcher.py


def _cache_pinned():
    """The launcher's note that updates are not kept on this machine, or ''.

    Set when engine files have disappeared from the cache twice in a week
    (see launcher._cache_pin).  This session runs the bundled engine; a
    restart would land on the same one, so neither update spot may offer
    one.  What helps is installing the newest build, which also clears the
    pin."""
    return os.environ.get(_CACHE_PINNED_ENV, '') or ''


def _render_cache_pinned_notice(sidebar=False):
    _pn = globals().get('PRODUCT_NAME', 'OTDR Suite')   # alone in a test: the default
    target = st.sidebar if sidebar else st
    target.warning(
        'Updates cannot be kept on this computer. Files this app downloads '
        f'keep disappearing, so {_pn} is running the copy that came with '
        'its installer. To get the newest version, download and run the '
        f'installer again: {INSTALLER_URL}')


_NEEDS_INSTALL_ENV = 'OTDR_SUITE_NEEDS_INSTALL'   # set by desktop/launcher.py
_ENGINE_FILES_ENV = 'OTDR_SUITE_ENGINE_FILES'     # set by desktop/launcher.py


def _launcher_would_refuse(manifest_files, exe_files):
    """True when the launcher will refuse the published manifest: it names
    engine files the exe's frozen list does not (a build that added modules),
    or drops ones the exe runs.  Either list unknown → False: a restart is
    then still offered, and the launcher gives the real answer at boot."""
    if not manifest_files or not exe_files:
        return False
    return set(manifest_files) != set(exe_files)


def _needs_install():
    """Why 'Update & restart' cannot bring this session up to date, or ''.

    Two sources, either is enough.  The launcher's own verdict from this boot
    (it fetched the manifest, checked the signature and refused the file set;
    _NEEDS_INSTALL_ENV carries its reason).  Or this session's display-only
    look at the same manifest against the file list the launcher published at
    boot: a mismatch means the launcher WILL refuse it at the next boot, so
    the banner can say 'install' now instead of sending the tech through a
    restart that changes nothing — which is what build 476 did all day under
    manifest 658 and its 13 new files."""
    reason = os.environ.get(_NEEDS_INSTALL_ENV, '') or ''
    if reason:
        return reason
    try:
        exe_files = json.loads(os.environ.get(_ENGINE_FILES_ENV) or '[]')
    except Exception:
        exe_files = []
    if _launcher_would_refuse(st.session_state.get('upd_manifest_files'),
                              exe_files):
        return 'the published update adds files this copy cannot download'
    return ''


def _render_install_notice(latest, running, sidebar=False):
    _pn = globals().get('PRODUCT_NAME', 'OTDR Suite')   # alone in a test: the default
    target = st.sidebar if sidebar else st
    target.warning(
        f'Update {latest} needs a fresh install (running {running}). It adds '
        f'files this copy of {_pn} cannot download on its own, so Update '
        f'& restart will not apply it. Close {_pn} completely, then '
        f'download and run the installer: {INSTALLER_URL}')


def _render_update_nudge():
    """Sidebar banner, above the page radio, when the published engine is newer
    than the one this session runs — plus the same one-click restart the footer
    offers, so an always-on machine can't sit on an old build unnoticed.

    The staleness answer comes from _update_state — the same TTL-cached check
    the report block uses, so the banner and the block can never disagree and
    only ONE manifest fetch happens per recheck window (3 s cap).  Every
    failure is swallowed by _nudge_check: equal, older or unreachable renders
    nothing at all."""
    _pn = globals().get('PRODUCT_NAME', 'OTDR Suite')   # alone in a test: the default
    if 'upd_restart_blocked' not in st.session_state:
        blocked = os.path.exists(_restart_marker_path())
        if blocked:
            try:
                os.remove(_restart_marker_path())   # once per failed attempt
            except OSError:
                pass
        st.session_state['upd_restart_blocked'] = blocked
    if st.session_state['upd_restart_blocked']:
        st.error(f'The update didn\'t start: the previous {_pn} is still '
                 'running. Close it completely (or reboot), then start '
                 f'{_pn} again.')

    if _cache_pinned():
        _render_cache_pinned_notice()
        return

    nudge = _update_state()
    if not nudge:
        return
    latest, running = nudge
    try:
        note = _deadline_note(running)
    except Exception:
        note = ''
    if _needs_install():
        _render_install_notice(latest, running)
        if note:
            st.caption(note.strip())
        return
    st.warning(f'Update {latest} is available (running {running}).{note}')
    if getattr(sys, 'frozen', False):
        if st.button('⬇ Update & Restart Now', key='upd_nudge_restart',
                     type='primary', use_container_width=True):
            if _relaunch_and_exit():
                _render_restart_watchdog()
            else:
                st.error(f'Couldn\'t start the restart. Close {_pn} and '
                         'open it again to pick up the update.')
    else:
        st.caption('Restart the app to apply. Updates install at launch.')


# The viewer's engine lives in viewer/ — put it first so `import trace_server`
# resolves its sor_reader copy (NOT Secret Sauce's).  Secret Sauce is never
# imported in this process; it runs as a subprocess with its own path.
if VIEWER_DIR not in sys.path:
    sys.path.insert(0, VIEWER_DIR)

def _repair_marker_path():
    """The file the launcher reads at its next boot to throw the cached engine
    away and download a clean one.  See launcher._honour_repair_request."""
    d = (os.environ.get('OTDR_SUITE_APP_DIR')          # see _restart_marker_path
         or os.path.join(os.path.expanduser('~'), '.otdrSuite'))
    return os.path.join(d, 'repair_requested')


def _request_repair():
    """Ask for a clean engine on the next launch.  Best effort: a repair we
    could not schedule must still leave the tech with the restart button."""
    try:
        marker = _repair_marker_path()
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, 'w', encoding='utf-8') as fh:
            fh.write('repair')
        return True
    except OSError:
        return False


def _blocked_by_policy(text):
    """True when Windows REFUSED TO LOAD a file we shipped, rather than the
    file having gone missing.

    A tech hit `ImportError: DLL load failed while importing indexers: An
    Application Control policy has blocked this file.` — Application Control
    blocked pandas' compiled indexers.pyd inside our bundle, and the same block
    then surfaced as `module 'pandas' has no attribute 'DataFrame'`, because a
    pandas import that dies partway leaves a half-built module behind.

    This has to be told apart from a missing engine file, because the remedies
    are opposites.  A missing engine file is repaired by re-downloading the
    engine.  A blocked DLL cannot be: the DLLs ship inside the installed exe,
    which no update we publish ever rewrites.  Offering Repair here would send
    a tech round a loop that cannot end.  A plain "DLL load failed" (a .pyd
    that was quarantined or corrupted) lands here too, and reinstalling is the
    right answer for that one as well."""
    t = str(text or '').lower()
    return ('application control policy' in t
            or 'blocked this file' in t
            or 'dll load failed' in t)


_POLICY_HEADLINE = 'Windows blocked a file that came with this app.'
_POLICY_BODY = (
    'A security policy on this computer stopped OTDR Suite from loading one '
    'of its own files. This is set by the computer, not by the app, so '
    'repairing or re-downloading will not clear it.')
_POLICY_STEPS = (
    'Install the newest version of OTDR Suite first: it is digitally signed, '
    'and older versions were not, which is the usual reason a policy stops '
    'one. If it still happens after that, this has to be allowed by whoever '
    'manages security settings on your computers.')
_POLICY_FOR_IT = (
    'Windows blocked a file it was asked to load. The block is recorded in '
    'Event Viewer under\n\n'
    '  Applications and Services Logs\n'
    '    Microsoft > Windows > CodeIntegrity > Operational\n\n'
    'That entry names the exact file and the policy that stopped it. Please '
    'allow OTDR Suite, published by Robert Colbert, to run.')
# The product's name (the App: "OTDR App"); globals().get, because a test
# runs these _POLICY lines on their own.
_POLICY_BODY, _POLICY_STEPS, _POLICY_FOR_IT = (
    m.replace('OTDR Suite', globals().get('PRODUCT_NAME', 'OTDR Suite'))
    for m in (_POLICY_BODY, _POLICY_STEPS, _POLICY_FOR_IT))


def _policy_block_caption(exc):
    """One line under a panel that failed open, when Windows blocked the file.

    These panels are guarded so a component failure cannot take the page down,
    which is right — but "unavailable, details sent to support" is all the tech
    reads, and support is us reading it hours later.  Naming the cause on the
    screen is what lets a tech act the same day."""
    if _blocked_by_policy(exc):
        st.caption(f'{_POLICY_HEADLINE} {_POLICY_STEPS}')


def _engine_policy_block_page(exc):
    """The boot-time version: Windows blocked a file, so say that, and do NOT
    offer the repair — it rewrites engine files, and the blocked one is not."""
    _pn = globals().get('PRODUCT_NAME', 'OTDR Suite')   # alone in a test: the default
    st.set_page_config(page_title=_pn, layout='centered')
    st.title('Windows Blocked Part of This App')
    st.error(_POLICY_HEADLINE)
    st.write(_POLICY_BODY)
    st.write(_POLICY_STEPS)
    with st.expander('For Your IT Department'):
        st.code(_POLICY_FOR_IT)
    with st.expander('Details'):
        st.code(f'{type(exc).__name__}: {exc}')
    st.stop()


def _engine_file_missing_page(exc):
    """What a tech sees when a file the app needs is gone from this computer.

    A tech hit `ModuleNotFoundError: No module named 'sor_reader324802a'` as a
    red Streamlit traceback with a Copy button and links to Google and ChatGPT.
    Nothing on that screen said what to do, and the answer — delete a hidden
    folder in his user profile — was not something to ask a tech to do down a
    phone line.  So: say what happened in words, and put the repair on one
    button.  The button schedules the repair and restarts; the launcher does
    the work at boot, where nothing is holding the files open.
    """
    _pn = globals().get('PRODUCT_NAME', 'OTDR Suite')   # alone in a test: the default
    st.set_page_config(page_title=_pn, layout='centered')
    st.title(f'{_pn} Needs to Repair Itself')
    st.error('A file this app needs is missing from this computer.')
    st.write(
        'The app checks its own files at every start, and one of them is no '
        'longer there. Security software removing a file after the app '
        'downloaded it is the usual reason. Nothing you have saved is '
        'affected, and no reports are lost.')
    st.write(
        'Click the button below. The app will download a fresh copy of its '
        'files and start again. It takes about a minute.')
    # Report when the page is SHOWN, not when the button is clicked.  The
    # click hands off to _relaunch_and_exit, which hard-exits the process
    # 0.7 s later, and the report goes out on a background thread that a slow
    # link cannot finish in that time.  The one line naming the missing
    # module was lost exactly when it was needed (sscot, 2026-09-02).  The
    # hourly in-process dedup keeps reruns of this page from repeating it.
    report_error('engine file missing', exc,
                 {'engine': HERE, 'source': os.environ.get('OTDR_SUITE_SOURCE', '?')})
    if st.button('Repair and Restart', type='primary'):
        _request_repair()
        if _relaunch_and_exit():
            _render_restart_watchdog()
        else:
            st.error(f'Close {_pn} and open it again to finish the repair.')
    with st.expander('Details'):
        st.code(f'{type(exc).__name__}: {exc}\n\nengine: {HERE}')
    st.stop()


def _engine_damaged_notice(stderr, key):
    """Render the repair offer when an engine subprocess died on a missing
    file, and say whether it did.

    The boot check cannot catch this one: the files are verified at launch, and
    a quarantine that happens while the tech is working takes the engine out
    from under a run that had already started.  What the tech would otherwise
    read is "Secret Sauce did not return a result" over a Python traceback in
    an expander, which tells them nothing they can act on."""
    _pn = globals().get('PRODUCT_NAME', 'OTDR Suite')   # alone in a test: the default
    text = stderr or ''
    if 'ModuleNotFoundError' not in text and 'ImportError' not in text:
        return False
    if _blocked_by_policy(text):
        # Windows refused to LOAD a file, which a repair cannot fix: the file
        # is inside the installed program, and the repair rewrites the engine.
        st.error(_POLICY_HEADLINE)
        st.write(_POLICY_BODY)
        st.write(_POLICY_STEPS)
        with st.expander('For Your IT Department'):
            st.code(_POLICY_FOR_IT)
        with st.expander('Details'):
            st.code(text[-4000:] or '(no output)')
        return True
    st.error('A file this app needs is missing from this computer.')
    st.write(
        'Security software removing a file after the app downloaded it is the '
        'usual reason. Repair and restart, then run this again. Nothing you '
        'have saved is affected.')
    if st.button('Repair and Restart', type='primary', key=f'repair_{key}'):
        _request_repair()
        if _relaunch_and_exit():
            _render_restart_watchdog()
        else:
            st.error(f'Close {_pn} and open it again to finish the repair.')
    with st.expander('Details'):
        st.code(text[-4000:] or '(no output)')
    return True


try:
    import trace_server  # noqa: E402  (after sys.path setup)
except ImportError as _engine_exc:
    # Neither of these is a crash to show a tech a traceback for — and they
    # take opposite remedies, so they must not share a page.
    if _blocked_by_policy(_engine_exc):
        _engine_policy_block_page(_engine_exc)
    _engine_file_missing_page(_engine_exc)

TRACE_PORT_BASE = 8771

st.set_page_config(page_title=PRODUCT_NAME, layout='wide',
                   initial_sidebar_state='expanded')
# Light / Dark: every new session starts Dark, and the session's choice is
# applied before anything draws.  Streamlit sends the theme at the START of a
# run, so when this run changed it the page on screen still has the old one:
# rerun once to paint the right one.  Fail-safe (boss, 2026-10-01): at most
# one such rerun per theme change, so a Streamlit that does not keep the
# setting draws the page in whatever theme it has instead of rerunning for
# ever; and a theme error leaves Streamlit's own look rather than no page.
if 'ui_theme' not in st.session_state:
    st.session_state['ui_theme'] = THEME_DEFAULT
try:
    _theme_changed = apply_streamlit_theme(st.session_state['ui_theme'])
except Exception:
    _theme_changed = False
if _theme_changed and st.session_state.get('_theme_rerun_for') != st.session_state['ui_theme']:
    st.session_state['_theme_rerun_for'] = st.session_state['ui_theme']
    st.rerun()
try:
    st.markdown(theme_css_vars(), unsafe_allow_html=True)
except Exception:
    pass
try:
    trace_server.CONFIG['theme'] = st.session_state['ui_theme']
except Exception:
    pass
# No Streamlit chrome, top right, on any screen (Robert, 2026-09-27): the
# Deploy button, the ⋮ menu and the running / "File change · Rerun" status.
# The sidebar's own open/close arrow, top left, stays.
# (The toolbar itself stays: the sidebar's open arrow lives inside it.)
st.markdown('<style>[data-testid="stToolbarActions"],'
            '[data-testid="stAppDeployButton"],[data-testid="stMainMenu"],#MainMenu,'
            '[data-testid="stStatusWidget"],[data-testid="stDecoration"]'
            '{display:none!important}'
            # The open-sidebar arrow, a little bigger and in the app's blue so
            # it is easy to find (Robert, 2026-09-27).
            '[data-testid="stExpandSidebarButton"]{transform:scale(1.7);'
            'transform-origin:left top}'
            '[data-testid="stExpandSidebarButton"] *{color:var(--otdr-accent)!important}'
            '</style>', unsafe_allow_html=True)

# Streamlit's own theme pick (the ⋮ menu's Settings) beats the hub's Theme
# switch: once a browser has chosen Light or Dark there, Streamlit keeps it
# and ignores the theme the hub sends, so the switch does nothing.  ("Use
# system setting" removes Streamlit's entry instead, so it never blocks.)  The menu is hidden above; this clears a pick already made, once,
# and reloads so the hub's theme takes.  Only that pick is removed: an
# entry {"name": "Light"} or {"name": "Dark"}.  Everything else is left
# alone, above all what Streamlit writes by itself on every page load
# ("Custom Theme" up to 1.50, a bare "System"/"Light"/"Dark" from 1.6x on,
# which the hub's theme beats anyway).  Removing that one reloaded the page
# for ever on the 1.64 build (boss, 2026-10-01, run 1416), so the reload
# also happens at most once per window.
THEME_PICK_CLEAR_JS = """
<script>
(function () {
  var w; try { w = window.parent; void w.document; } catch (e) { return; }
  try {
    var ss = null; try { ss = w.sessionStorage; } catch (e) {}
    if (ss && ss.getItem('otdrThemePickCleared')) return;
    var ls = w.localStorage, gone = false;
    for (var i = ls.length - 1; i >= 0; i--) {
      var k = ls.key(i);
      if (!k || k.indexOf('stActiveTheme') !== 0) continue;
      var v = null; try { v = JSON.parse(ls.getItem(k)); } catch (e) {}
      if (v && typeof v === 'object' && (v.name === 'Light' || v.name === 'Dark')) {
        ls.removeItem(k); gone = true;
      }
    }
    if (gone && ss) { ss.setItem('otdrThemePickCleared', '1'); w.location.reload(); }
  } catch (e) { /* no storage: nothing was picked */ }
})();
</script>
"""


def _install_theme_pick_clear():
    """Render the script above out of the page's flow: a zero-height frame
    still takes a gap between elements, which moved every page down."""
    try:
        box = st.container(key='theme_pick_clear')
        box.markdown('<style>[data-testid="stLayoutWrapper"]:has(> .st-key-theme_pick_clear)'
                     '{position:absolute;width:0;height:0;overflow:hidden}</style>',
                     unsafe_allow_html=True)
        with box:
            st_components_html(THEME_PICK_CLEAR_JS, height=0)
    except Exception:
        pass


_install_theme_pick_clear()


# ─── Sidebar drag-to-widen must not close the sidebar ────────────────────
# Robert, 2026-09-17: "when I go to click and drag it closes it instead".
# Streamlit (1.50) closes the sidebar on any mouse press OUTSIDE its content
# box whenever the window is narrower than its tablet breakpoint -- and the
# drag handle sits on the sidebar's edge, outside that content box.  So in a
# narrow hub window, pressing the handle closed the panel before the drag
# could start.  CSS cannot fix it (the check is on the DOM tree, not the
# pixels).  This script stops a press on the handle at the app root: React's
# own handler (which starts the resize) runs first at the root, and the
# press never bubbles up to the document, where Streamlit's close-on-outside
# listener waits.  Installed once per browser tab; a no-op when anything it
# expects is missing, so a Streamlit upgrade can only make it do nothing.
SIDEBAR_DRAG_FIX_JS = """
<script>
(function(){
  var w; try { w = window.parent; void w.document; } catch (e) { return; }
  if (!w || w.__otdrSidebarDragFix) return;
  var root = w.document.getElementById('root');
  if (!root) return;
  w.__otdrSidebarDragFix = true;
  function onHandle(t){
    if (!t || !t.closest) return false;
    var sb = t.closest('[data-testid="stSidebar"]');
    // Inside the sidebar but NOT inside its content box = the resize handle.
    return !!sb && !t.closest('[data-testid="stSidebarContent"]');
  }
  root.addEventListener('mousedown', function(ev){
    if (onHandle(ev.target)) ev.stopPropagation();
  }, false);
})();
</script>
"""


def _install_sidebar_drag_fix():
    """Render the zero-height script above (best effort, never fatal)."""
    try:
        st_components_html(SIDEBAR_DRAG_FIX_JS, height=0)
    except Exception:
        pass


# Trace files dropped on the hub page itself, not on the Viewer.  Nothing on
# the hub took a dropped file, so Chrome did what it does with one: it saved it
# to Downloads.  The boss (2026-09-30, on a 432-fiber job): the A folder loaded on the
# Viewer and the B folder "goes straight to downloads and isn't populating".
# The Viewer frame is a box in the middle of the hub page; a second folder let
# go of a little off it (the sidebar's B box, the settings box above, the
# header) was a download.  So the hub page catches every file drop and refuses
# it: the cursor says no and nothing loads or downloads.  Only the Viewer's
# FILES panel takes a drop, where it lights up (Robert 2026-10-01); on the
# Viewer page a drag over the hub tells the Viewer, which shows the panel as
# the place to drop.  A file box of a page (st.file_uploader) still takes its
# own drops.
#
# The page's frames are covered too: the settings box and the other boxes
# drawn by the hub are frames of the hub's own address, and a drop on one of
# them was a download in the same way.  The Viewer frame is another address
# and takes its own drops.
#
# The code runs in the hub page itself, put there as a <script>: a listener
# left behind by this zero-height frame would stop working once Streamlit
# removed the frame.
HUB_DROP_CATCH_JS = r"""
<script>
(function(){
  var w; try { w = window.parent; void w.document; } catch (e) { return; }
  if (!w || w.__otdrDropCatch) return;
  w.__otdrDropCatch = true;
  var s = w.document.createElement('script');
  s.textContent = '(' + function(){
    function isFiles(ev) {
      var t = ev.dataTransfer && ev.dataTransfer.types;
      return !!t && Array.prototype.indexOf.call(t, 'Files') >= 0;
    }
    function viewerFrame() {
      var fs = document.querySelectorAll('iframe');
      for (var i = 0; i < fs.length; i++) {
        var src = fs[i].getAttribute('src') || '';
        if (/^https?:\/\/(127\.0\.0\.1|localhost):\d+\/\?(.*&)?b=\d+/.test(src)) return fs[i];
      }
      return null;
    }
    var lastHint = 0;
    function onOver(ev) {
      if (ev.defaultPrevented || !isFiles(ev)) return;   // a file box's own
      ev.preventDefault();
      ev.dataTransfer.dropEffect = 'none';
      // Show the Viewer's FILES panel as the place to drop.
      var fr = viewerFrame(), now = Date.now();
      if (fr && fr.contentWindow && now - lastHint > 200) {
        lastHint = now;
        fr.contentWindow.postMessage({ type: 'otdr-drag' }, new URL(fr.getAttribute('src')).origin);
      }
    }
    function onDrop(ev) {
      if (ev.defaultPrevented || !isFiles(ev)) return;
      ev.preventDefault();
    }
    function hook(win) {
      try {
        var doc = win.document;
        if (!doc || doc.__otdrDropHooked) return;
        doc.__otdrDropHooked = true;
        win.addEventListener('dragover', onOver);
        win.addEventListener('drop', onDrop);
      } catch (e) { /* another address: the Viewer, which takes its own */ }
    }
    hook(window);
    // Frames come and go with every rerun, so look again now and then.
    setInterval(function(){
      var fs = document.querySelectorAll('iframe');
      for (var i = 0; i < fs.length; i++) if (fs[i].contentWindow) hook(fs[i].contentWindow);
    }, 1000);
  } + ')();';
  w.document.head.appendChild(s);
})();
</script>
"""


def _install_hub_drop_catch():
    """Render the zero-height script above (best effort, never fatal)."""
    try:
        st_components_html(HUB_DROP_CATCH_JS, height=0)
    except Exception:
        pass


# ─── Background trace server (started once) ──────────────────────────────
def ensure_trace_server():
    if 'trace_port' not in st.session_state:
        st.session_state['trace_port'] = trace_server.start_in_thread(TRACE_PORT_BASE)
    # The pop-out Viewer runs on the trace server's own port, so it cannot know
    # the hub's.  Hand it over so its "← Back to report" button can find us.
    try:
        trace_server.CONFIG['hub_port'] = int(st.get_option('server.port'))
    except Exception:
        pass
    # The Viewer judges by the same analysis mode as the reports.
    trace_server.CONFIG['analysis_mode'] = analysis_mode()
    # In FastReporter mode the Viewer's table is FR's, built by the Splice
    # Report engine in its own process (the engines never share one): hand
    # the Viewer the same runner argv the reports use.
    trace_server.CONFIG['engine_argv'] = (
        [sys.executable, '--run-splicereport'] if FROZEN
        else [sys.executable, os.path.join(SPLICEREPORT_DIR, 'run_splicereport.py')])
    # FEC mode's gates follow the customer profile (and any FEC Settings
    # edits on the Splice Report FEC page), so the Viewer and the tool agree.
    try:
        trace_server.set_fec_gates(_fec_active_gates())
    except Exception as exc:                            # noqa: BLE001
        report_error('viewer — FEC gates', exc)
    return st.session_state['trace_port']


# ─── Back-from-Viewer caches live in the app's own state dir ─────────────────
# A report cell click into the Viewer is a URL navigation that wipes Streamlit's
# session state; each report page keeps its last manifest on disk so "← Back"
# re-shows it without re-running a multi-minute engine.  Those files used to be
# written INTO the traces folder (.uni_result_cache.json, .sr_grid_cache.json,
# SecretSauce_reports/pairs_cache.json) -- the boss asked for the trace folders
# to stay untouched, and the engines carried special code to skip them (a cache
# counted as an acquisition once aborted a run with "Mixed file types").  They
# now live under ~/.otdrSuite/cache, keyed by the folder(s) they describe.
def _hub_cache_path(name, *folders):
    import hashlib
    key = hashlib.sha1('|'.join(os.path.normcase(os.path.abspath(f))
                                for f in folders if f).encode('utf-8')).hexdigest()[:16]
    d = os.environ.get('OTDR_CACHE_DIR') or os.path.join(
        os.environ.get('OTDR_SUITE_APP_DIR')             # see _restart_marker_path
        or os.path.join(os.path.expanduser('~'), '.otdrSuite'), 'cache')
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f'{key}_{name.lstrip(".")}')


# Secret Sauce's report has to survive a round trip the tech makes constantly:
# click a mating pair -> the Viewer -> back.  That navigation goes through a
# URL query param, which starts a FRESH Streamlit session and drops every
# session_state entry, so disk is the ONLY way back to the report.  The pairs
# mode has always cached; the Excel/PDF mode never did, and the pair links
# render in EVERY output mode (see _render_mating_top) — so an Excel run could
# be clicked through and came back to an empty page and a re-run.
SS_CACHE_NAMES = ('pairs_cache.json', 'ss_result_cache.json')


def _ss_cache_write(name, folder, manifest):
    """Persist a Secret Sauce manifest under its folder key, and drop the other
    mode's cache so exactly ONE report is remembered per folder: the last run.

    Without that eviction a folder that once produced an in-app pairs report
    would keep re-showing it after a later Excel run, because the pairs restore
    runs first and returns before the download result is ever rendered.

    Never raises.  A cache that cannot be written costs a re-run, not the run."""
    try:
        import json as _json
        with open(_hub_cache_path(name, folder), 'w', encoding='utf-8') as fh:
            _json.dump(manifest, fh)
    except Exception:
        return
    for other in SS_CACHE_NAMES:
        if other != name:
            try:
                os.remove(_hub_cache_path(other, folder))
            except OSError:
                pass


def _ss_cache_read(name, folder, mode=None):
    """The manifest cached for this folder, or None.  `mode` keeps a pairs
    manifest out of the download renderer and vice versa; the `_folder` check
    keeps one folder's report from being shown for another."""
    try:
        import json as _json
        cache = _hub_cache_path(name, folder)
        if not os.path.exists(cache):
            return None
        with open(cache, encoding='utf-8') as fh:
            cached = _json.load(fh)
        if not (cached.get('ok') and cached.get('_folder') == folder):
            return None
        if mode is not None and cached.get('mode') != mode:
            return None
        return cached
    except Exception:
        return None


_LEGACY_CACHE_NAMES = ('.uni_result_cache.json', '.sr_grid_cache.json',
                       '.srfr_grid_cache.json')


def _remove_legacy_caches(folder):
    """Delete the cache files earlier builds left INSIDE a traces folder --
    only our own three dotfiles and SecretSauce_reports/pairs_cache.json,
    nothing else.  Called when a page takes a folder, so the boss's folders
    come clean the next time he opens them."""
    try:
        for n in _LEGACY_CACHE_NAMES:
            p = os.path.join(folder, n)
            if os.path.isfile(p):
                os.remove(p)
        p = os.path.join(folder, 'SecretSauce_reports', 'pairs_cache.json')
        if os.path.isfile(p):
            os.remove(p)
    except OSError:
        pass


# ─── Native folder picker (works locally + in the packaged .exe) ─────────
def _tk_unsafe():
    """Dev-only guard: OTDR Suite ships for Windows, but it is developed and
    previewed on a Mac, where only the main thread may open a window.
    Streamlit runs the script on a worker thread, so tk.Tk() there aborts the
    whole dev server (NSInternalInconsistencyException, exit 134; found
    2026-09-23).  On a Mac the picker reports itself unavailable and the path
    box is used.  No effect on Windows."""
    import threading
    return sys.platform == 'darwin' and threading.current_thread() is not threading.main_thread()


def pick_folder(title='Choose a folder'):
    """Native folder picker. Returns the chosen path, '' if the user cancelled,
    or None if the picker is UNAVAILABLE — Tcl/Tk isn't bundled in the frozen
    Windows .exe, so tk.Tk() raises and the button would otherwise do nothing
    silently.  Returning None lets the caller tell the tech to paste the path."""
    if _tk_unsafe():
        return None
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.wm_attributes('-topmost', 1)
        path = filedialog.askdirectory(title=title)
        root.destroy()
        return path or ''
    except Exception:
        return None


def _project_run_path(dest, name, traces=(), taken=()):
    """In a project, every report run keeps its own file (Robert, 2026-09-26:
    Reports is "a directory where we store and access all ... reports that
    have been ran"), so a run into the project's Reports folder gets the
    time in its name.  Anywhere else the name is left as it always was.
    Either way a file already there (or in `taken`, the names this click
    already gave out) is never written over: `name (2).xlsx` ... (main's
    _unused_report_path, 2026-09-29).  Picked here, before the run is
    recorded, so the User column names the file that is actually written."""
    ss = st.session_state
    work = work_dir() if ss.get('app_mode') == 'project' else ''
    here = _unused_report_path(os.path.join(dest, name), taken)
    if not work or os.path.normcase(os.path.abspath(dest)) != os.path.normcase(
            os.path.abspath(work_sub('reports', work))):
        return here
    base, ext = os.path.splitext(name)
    stamp = time.strftime('%Y-%m-%d %H%M')
    out = _unused_report_path(os.path.join(dest, f'{base} {stamp}{ext}'), taken)
    try:
        data = events_read(work)
        # Who ran it, for the event the folder scan logs when it lands.
        data['made_by'][_rel(work, out)] = current_user()
        if traces:
            # Which traces the run is on, for the Reports tab (Robert, 2026-09-27).
            data['report_traces'][_rel(work, out)] = [
                os.path.abspath(t) for t in traces if t]
        _events_write(work, data)
    except Exception as exc:
        report_error('project: record report traces', exc, {})
    return out


# In a project, where each tool's output goes: a folder of the job, not a choice.
PROJECT_DEST_SUBS = {'sr_report_dest': 'reports', 'uni_report_dest': 'reports',
                     'ss_report_dest': 'reports', 'fqa_dest': 'fqa',
                     'fc_report_dest': 'field'}


# ─── Boxes that live on one page ─────────────────────────────────────────
# Streamlit drops a widget's state on any run that does not draw it, so a
# trip to another tool emptied every box a page draws for itself (seen
# 2026-09-29).  Such a box keeps what it shows in `{key}_saved`, a slot no
# widget owns, and a box Streamlit forgot is seeded from it before it is
# drawn.  Never value= as well: key + value on one widget is the trap in
# feedback_streamlit_widget_state.  Anything that writes the box from off
# its page must drop the slot too (_clear_traces), or the old value comes
# back once Streamlit has dropped the write.
def _seed_box(key, options=None):
    """Before the box is drawn: give it back what it showed, if Streamlit
    forgot it.  `options` is a pick list's choices today; a kept choice that
    is no longer one of them is left out."""
    saved = key + '_saved'
    if key in st.session_state or saved not in st.session_state:
        return
    if options is not None and st.session_state[saved] not in options:
        return
    st.session_state[key] = st.session_state[saved]


def _keep_box(key):
    """Right after the box is drawn: keep what it shows (see _seed_box)."""
    st.session_state[key + '_saved'] = st.session_state.get(key)


def _report_dest_row(key, default_dir):
    """The 'Save reports to' row every report page shows: a Browse button that
    opens the native folder picker, and a path box the tech can paste into.
    Returns the folder reports go to -- what the tech chose, else
    `default_dir`, which is the tech's Downloads folder on every page: the
    boss's rule for everything the suite saves, after a report written "next
    to the traces" landed beside a drag-and-drop staging copy in a temp folder.
    The default is shown as the placeholder so the tech sees where the report
    WILL land before running anything.

    In a project there is no choice (Robert, 2026-09-27: "we shouldn't have
    the choice to save report anywhere. It should say report saved to Job
    File"): each tool's output goes to its folder in the job.

    The box's text is also kept in a slot no widget owns (`{key}_saved`).
    Streamlit drops a widget's state on any run that does not draw it, so a
    trip to another tool emptied the box and the next report went to
    Downloads (2026-09-29).  A box Streamlit forgot is seeded from the slot
    before it is drawn.  Never value= as well: key + value on one widget is
    the trap in feedback_streamlit_widget_state."""
    job_sub = PROJECT_DEST_SUBS.get(key)
    if job_sub and st.session_state.get('app_mode') == 'project' \
            and st.session_state.get('project_path'):
        work = work_dir()
        dest = work_sub(job_sub, work)
        st.session_state[key] = dest
        st.markdown(f'📁 **Report saved to Job File:** {os.path.basename(work)} / '
                    f'{PROJECT_DIRS[job_sub]}' if job_sub == 'reports' else
                    f'📁 **Saved to Job File:** {os.path.basename(work)} / '
                    f'{PROJECT_DIRS[job_sub]}')
        return dest
    saved = key + '_saved'
    if key not in st.session_state:
        st.session_state[key] = st.session_state.get(saved, '')
    c1, c2 = st.columns([1, 2])
    with c1:
        if st.button('📁 Save Reports To…', use_container_width=True, key=key + '_browse'):
            p = pick_folder('Choose where to save the reports')
            if p:
                st.session_state[key] = p
            elif p is None:
                st.info('No folder picker on this machine. Paste the path instead.')
    with c2:
        st.text_input('Save Reports To', key=key, placeholder=default_dir,
                      help='Leave blank to use the folder shown.')
    st.session_state[saved] = st.session_state.get(key) or ''
    chosen = (st.session_state.get(key) or '').strip().strip('"')
    if chosen:
        parent = os.path.dirname(os.path.abspath(chosen)) or chosen
        if not os.path.isdir(chosen) and not os.path.isdir(parent):
            st.warning(f'That folder cannot be created: {chosen}. Reports will go to {default_dir}')
            return default_dir
        return os.path.abspath(chosen)
    return default_dir


def _unused_report_path(path, taken=()):
    """`path`, or `name (2).xlsx`, `name (3).xlsx`, ... when a file of that
    name is already there (or in `taken`, the names this click already gave
    out).  The report file name is built from the site names only, so a
    rerun of the same span wrote over the report before it, with nothing
    said (2026-09-29)."""
    stem, ext = os.path.splitext(path)
    cand, n = path, 1
    while os.path.exists(cand) or cand in taken:
        n += 1
        cand = f'{stem} ({n}){ext}'
    return cand


# ─── ILA / site-name auto-detection from SOR GenParams ───────────────────────
# So the report labels WHICH ILA is the A-direction and which is the B-direction
# (the boss's request) instead of a literal "A"/"B".  Standalone + engine-free:
# does NOT import any engine's sor_reader, to keep the hub's process isolation
# intact (each engine ships a divergent copy).
def _sor_locations(path):
    """Read (location_a, location_b) from a SOR file's GenParams block.
    Returns ('', '') when the block can't be read."""
    try:
        with open(path, 'rb') as f:
            raw = f.read()
    except OSError:
        return ('', '')
    marker = b'GenParams\x00'
    i = raw.find(marker, 50)
    if i < 0:
        return ('', '')
    p = i + len(marker) + 2          # skip marker + 2-byte language code

    def _cstr(buf, q):
        e = buf.find(b'\x00', q)
        if e < 0:
            e = len(buf)
        return buf[q:e].decode('latin-1', 'replace').strip(), e + 1

    # Telcordia SR-4731 field order: cable_id, fiber_id, fiber_type(2B),
    # wavelength(2B), location_a, location_b, ...
    try:
        _, p = _cstr(raw, p)          # cable_id
        _, p = _cstr(raw, p)          # fiber_id
        p += 4                        # fiber_type_code + wavelength_code (2×uint16)
        loc_a, p = _cstr(raw, p)
        loc_b, p = _cstr(raw, p)
    except (IndexError, ValueError):
        return ('', '')
    return (loc_a, loc_b)


def _derive_ila(folder):
    """Best-effort (origin, far) ILA/site names for the direction whose .sor
    (or .trc) files live in `folder`.  GenParams carries both cable endpoints; which one
    this direction was shot FROM comes from the filename prefix (SEANOR* →
    Seattle, NORSEA* → North Bend; HOWLAN* → How, LANHOW* → Lan).  Returns
    ('', '') when nothing is readable."""
    import glob
    sors = sorted(glob.glob(os.path.join(folder, '*.sor')) +
                  glob.glob(os.path.join(folder, '*.SOR')))
    if not sors:
        # A .trc stores the same two endpoints (LocationA/LocationB, in the
        # order GenParams carries them on a .sor of the same shot).
        sors = sorted(glob.glob(os.path.join(folder, '*.trc')) +
                      glob.glob(os.path.join(folder, '*.TRC')))
        if not sors:
            return ('', '')
        import folder_intake as fi
        loc_a, loc_b = fi.trc_header(sors[0], _first_chunk_only=True).get(
            'loc_stored') or ('', '')
    else:
        loc_a, loc_b = _sor_locations(sors[0])
    if loc_a and not loc_b:
        return (loc_a, '')
    if loc_b and not loc_a:
        return (loc_b, '')
    if not (loc_a or loc_b):
        return ('', '')
    # Both endpoints present — pick the origin via the filename prefix.
    pref = ''.join(ch for ch in os.path.basename(sors[0]).upper() if ch.isalpha())[:3]
    a3 = ''.join(ch for ch in loc_a.upper() if ch.isalpha())[:3]
    b3 = ''.join(ch for ch in loc_b.upper() if ch.isalpha())[:3]
    if pref and pref == b3 and pref != a3:
        return (loc_b, loc_a)
    return (loc_a, loc_b)            # default / prefix matches A-end


def _resolve_bidir_from_single(folder, zip_file):
    """One-folder / zip intake for a bidirectional tool: auto-split a single
    folder (or an uploaded .zip) that holds BOTH directions into A/B temp dirs,
    cached per source.  Returns (dir_a, dir_b), or ('', '') until a valid source
    is given.  Renders its own status / error messages."""
    import folder_intake as fi
    # The uploader is multi-file, so `zip_file` arrives as a list: the span's
    # traces (dragging a folder in hands us its files), one or more .zip, or
    # the .bdr files themselves.  Normalize to a list and drop Streamlit's
    # empty-list-means-nothing case.
    uploads = ([u for u in zip_file if u is not None]
               if isinstance(zip_file, (list, tuple))
               else ([zip_file] if zip_file is not None else []))

    def _ext(u, *exts):
        return str(getattr(u, 'name', '')).lower().endswith(exts)

    bdr_uploads = [u for u in uploads if _ext(u, '.bdr')]
    zip_uploads = [u for u in uploads if _ext(u, '.zip')]
    trace_uploads = [u for u in uploads if _ext(u, '.sor', '.json', '.trc')]
    if bdr_uploads and (zip_uploads or trace_uploads):
        st.error('Drop either the .bdr files or the .sor/.json/.trc/.zip span, '
                 'not both.')
        return ('', '')

    def _sig(tag, us):
        return tag + ':' + ':'.join(sorted(
            f"{getattr(u, 'name', '?')}/{getattr(u, 'size', 0)}" for u in us))

    if bdr_uploads:
        key = _sig('bdr', bdr_uploads)
    elif zip_uploads or trace_uploads:
        key = _sig('drop', zip_uploads + trace_uploads)
    elif folder and os.path.isdir(folder):
        key = f"dir:{os.path.abspath(folder)}"
    else:
        st.info('👆 Choose a folder that contains **both** directions, or '
                'drop it here: its traces, a .zip, or .bdr files, which '
                'carry both directions themselves.')
        return ('', '')
    dropped_dupes = []
    cache = st.session_state.setdefault('sr_intake', {})
    cached = cache.get(key)
    if not (cached and os.path.isdir(cached[0]) and os.path.isdir(cached[1])):
        work = tempfile.mkdtemp(prefix='otdr_intake_')
        try:
            if bdr_uploads:
                # Dropped .bdr files: write them to one staging folder and
                # use it for BOTH directions — each file already carries A
                # and B, so there is nothing to split.  That folder goes
                # STRAIGHT to the engine, which lists it with os.listdir, so
                # a repeated name is skipped rather than nested out of sight.
                files, dropped_dupes = fi.stage_uploads(
                    bdr_uploads, os.path.join(work, 'bdr'),
                    nest_duplicates=False)
            elif zip_uploads or trace_uploads:
                # A dragged-in span: its loose traces, its .zip(s), or both.
                # Everything lands under `work` and the A/B split runs over
                # the lot, exactly as it does for a folder.
                files = []
                for _i, _z in enumerate(zip_uploads):
                    files += fi.extract_zip(_z, os.path.join(work, 'unzipped_%d' % _i),
                                            exts=fi.OTDR_EXTS_WITH_BDR)
                if trace_uploads:
                    _staged, dropped_dupes = fi.stage_uploads(
                        trace_uploads, os.path.join(work, 'dropped'))
                    files += fi.find_otdr_files(os.path.join(work, 'dropped'),
                                                fi.OTDR_EXTS_WITH_BDR)
                files = sorted(files)
            else:
                files = fi.find_otdr_files(folder, fi.OTDR_EXTS_WITH_BDR)
            if not files:
                st.error('No .sor / .json / .trc / .bdr files found in that folder/zip.')
                return ('', '')
            info_dupes = list(dropped_dupes)
            if fi.is_bdr_set(files):
                # A .bdr already IS both directions, so there is nothing to
                # split: the A/B prefix rule would see one filename group and
                # refuse the job ("not exactly two directions").  Hand the
                # engine the one folder for both sides — load_all reads it
                # once and fills A and B from each file.
                da = db = os.path.dirname(files[0])
                info = {'a_prefix': 'A (from .bdr)', 'a_count': len(files),
                        'b_prefix': 'B (from .bdr)', 'b_count': len(files),
                        'bdr': True, 'foreign': []}
            else:
                files, _foreign = fi.audit_foreign_files(files)
                da, db, info = fi.materialize_two_directions(files, work)
                info['foreign'] = _foreign
            info['dupes'] = info_dupes
        except ValueError as exc:                      # not exactly two directions
            st.error(str(exc))
            return ('', '')
        except Exception as exc:                       # bad zip, IO, …
            st.error(f'Could not read that folder/zip: {exc}')
            report_error('splice report — folder/zip intake', exc, {'key': key})
            return ('', '')
        cached = (da, db, info)
        cache[key] = cached
    da, db, info = cached
    if info.get('dupes'):
        st.warning('⚠ ' + fi.duplicate_names_message(info['dupes']))
    if info.get('bdr'):
        st.caption(f"**{info['a_count']} FastReporter .bdr file(s)**: each "
                   f"carries BOTH directions, so there is nothing to split.")
        return (da, db)
    _by = {'location': ' by the SOR location pair',
           'sitecode': ' by site code'}.get(info.get('split_by'), '')
    msg = (f"Auto-split by direction{_by} → **A:** {info['a_prefix']} "
           f"({info['a_count']} files)  ·  **B:** {info['b_prefix']} ({info['b_count']} files)")
    if info.get('dropped'):
        msg += f"  ·  ⚠ ignored extra group(s): {', '.join(info['dropped'])}"
    st.caption(msg)
    if info.get('foreign'):
        st.warning('⚠ ' + fi.foreign_files_message(info['foreign']))
    return (da, db)


def _load_span(folder, zip_file, out=None, dirs=None):
    """Load ONE span (a folder or a .zip holding BOTH directions) into ALL three
    tools at once: split into A/B (Viewer + Splice Report) and a combined folder
    (Secret Sauce), then populate the shared input slots every page reads.
    Returns True on success; renders its own message on failure, in `out`
    (the sidebar unless the caller says; the Quick Analysis Load screen shows
    them on the page).  `dirs` = (A folder, B folder) loads two folders."""
    out = out or st.sidebar
    import folder_intake as fi
    # zip_file may be a single uploaded file, a LIST of them (multi-upload —
    # per-direction zips like SITEA.zip + SITEB.zip, loose .sor/.json/.trc
    # traces, a dropped folder's contents, or any mix), or None.
    uploads = ((list(zip_file) if isinstance(zip_file, (list, tuple)) else [zip_file])
               if zip_file else [])
    zips = [u for u in uploads if u.name.lower().endswith('.zip')]
    loose = [u for u in uploads if not u.name.lower().endswith('.zip')]
    if uploads:
        src_label = (', '.join(getattr(z, 'name', 'uploaded.zip') for z in zips)
                     or f'{len(loose)} dropped trace file(s)')
    elif dirs and all(d and os.path.isdir(d) for d in dirs):
        src_label = ' + '.join(os.path.basename(d.rstrip('/\\')) or d for d in dirs)
    elif folder and os.path.isdir(folder):
        src_label = os.path.basename(folder.rstrip('/\\')) or folder
    else:
        out.warning('Pick a folder with both directions, or drop its '
                           '.zip(s) / trace files, first.')
        return False
    work = tempfile.mkdtemp(prefix='otdr_span_')
    dupes = []
    try:
        if uploads:
            # Uploaded zips extract into their own subdirs; loose dropped
            # traces (browsers give bytes, never paths) are written into a
            # staging subdir.  Everything combines before the A/B split.
            files = []
            for _i, _z in enumerate(zips):
                files += fi.extract_zip(_z, os.path.join(work, 'unzipped_%d' % _i))
            if loose:
                _ld = os.path.join(work, 'loose')
                _staged, dupes = fi.stage_uploads(loose, _ld)
                files += fi.find_otdr_files(_ld)
            files = sorted(files)
        elif dirs:
            # Two folders, one a direction: together they are the span.
            files = []
            for _i, _d in enumerate(dirs):
                files += fi.find_otdr_files_with_zips(_d, os.path.join(work, 'zips%d' % _i))
            files = sorted(files)
        else:
            # A folder — which may itself CONTAIN the per-direction zips (spans
            # are often delivered that way), so descend into any zips found.
            files = fi.find_otdr_files_with_zips(folder, os.path.join(work, 'zips'))
        if not files:
            out.error('No .sor / .json / .trc files found in that folder/zip '
                             '(if the span is split into per-direction zips, '
                             'select the folder that holds them, or upload them).')
            return False
        # Files shot on another job (different location pair AND a different
        # pulse/range) are excluded here, before the direction split, so they
        # neither spawn a junk direction group nor reach Secret Sauce.
        files, foreign = fi.audit_foreign_files(files)
        dir_a, dir_b, info = fi.materialize_two_directions(files, work)
        # Secret Sauce must compare the SAME two directions the Viewer + Splice
        # Report use — not every group. On a >2-group span (a span's two
        # direction codes plus its short-shot codes) feeding ALL files
        # here made Secret Sauce mix full + short traces and disagree with the
        # other tools about which fibers exist.
        chosen = list(info['a_files']) + list(info['b_files'])
        combined = fi.materialize_all(chosen, os.path.join(work, 'all'))
    except ValueError as exc:                          # not exactly two directions
        out.error(str(exc))
        return False
    except Exception as exc:                           # bad zip, IO, …
        out.error(f'Could not load that folder/zip: {exc}')
        report_error('unified span loader', exc, {'src': src_label})
        return False
    # Folder-derived names only, here.  This runs from the sidebar at module
    # level, BEFORE the profile tables and _site_names_for exist (#177 called
    # it here and every span load raised NameError).  The Splice Report page
    # re-derives the names when the folder pair changes -- see the
    # sr_site_src block there -- and that is where the identifier-based names
    # land, so the pair is deliberately NOT pinned below.
    ila_a, _ = _derive_ila(dir_a)
    ila_b, _ = _derive_ila(dir_b)
    # Fill the shared slots every page already reads.
    if _PANEL_BOXES_DRAWN:
        # Called from under the left panel's A and B boxes (From SharePoint):
        # Streamlit refuses a write to a drawn box, so the panel takes these
        # at the top of the next run (_view_drop_pending); the caller reruns.
        st.session_state['_view_drop_pending'] = (dir_a, dir_b)
    else:
        st.session_state['view_dir_a_input'] = dir_a   # Viewer + Splice Report (A)
        st.session_state['view_dir_b_input'] = dir_b   # Viewer + Splice Report (B)
    st.session_state['ss_folder_input'] = combined     # Secret Sauce (one folder)
    st.session_state['sr_input_mode'] = 'Two folders (A + B)'
    st.session_state['sr_site_a'] = ila_a or info['a_prefix']
    st.session_state['sr_site_b'] = ila_b or info['b_prefix']
    st.session_state.pop('sr_site_src', None)   # let the SR page name the ends
    # A new span invalidates the previous deep-link target and the previous
    # report grid — otherwise a stale click re-fires against the new folders
    # (missing fiber / wrong-place zoom) and a stale grid keeps sending old
    # fiber/km into the new span.
    st.session_state.pop('viewer_target', None)
    st.session_state.pop('sr_result', None)
    st.session_state.pop('sr_dirs', None)
    st.session_state.pop('uni_result', None)
    st.session_state['span_loaded'] = {
        'label': src_label,
        'a_prefix': info['a_prefix'], 'b_prefix': info['b_prefix'],
        'a_count': info['a_count'], 'b_count': info['b_count'],
        'ila_a': ila_a or info['a_prefix'], 'ila_b': ila_b or info['b_prefix'],
        'dropped': info.get('dropped', []),
        'foreign': foreign,
        'dupes': dupes,
        'dir_a': dir_a, 'dir_b': dir_b, 'combined': combined,
    }
    return True


# ─── Projects: save a span's setup to a file, open it after a restart ─────
# Robert, 2026-09-23: "can we use OTDR Suite to create saveable projects that
# will survive the app opening and closing?"  A project is ONE span's Splice
# Report setup: the A/B folders (or the one folder), the site names, the
# added spans, the customer profile with its threshold and connector tables,
# the cable type, the analysis mode, where reports go, and the span markers
# set in the Viewer.  It is a small JSON file the tech saves wherever they
# like -- beside the traces is the default, so the whole crew can open it.
#
# Folder paths are stored TWICE: relative to the project file (so the file
# still works when the span folder is on another machine, a different
# OneDrive path, or a Mac) and absolute (the fallback when the relative one
# is not there, e.g. a project saved to Downloads for a span on a share).
#
# Not saved: dropped uploads (.zip / loose files / the tech's workbook).  A
# browser upload has no path to come back to; the page says so on Save.
#
# The finished report is NOT in the file: opening a project seeds the A
# folder, and the Splice Report page's own disk cache brings the last grid
# back exactly as it does after a Viewer click-through.
PROJECT_EXT = '.otdrproj'
PROJECT_FORMAT = 'otdr-suite-project'
PROJECT_VERSION = 1
PROJECT_RECENT_MAX = 6
SR_MODE_TWO = 'Two folders (A + B)'
SR_MODE_ONE = 'One folder / zip (both directions)'


def _sr_span_keys(span):
    """Every session_state key one Splice Report span's inputs live under.
    Span 1 keeps the keys it has always had (the A/B slots are shared with
    the Viewer); span n uses its own sr<n>_* keys.  One map, read by the page
    AND the project file, so the two can never disagree on a key."""
    if span == 1:
        k = dict(mode='sr_input_mode', a='view_dir_a_input', b='view_dir_b_input',
                 browse_a='sr_browse_a', browse_b='sr_browse_b',
                 browse_one='sr_browse_one', one='sr_one_folder', zip='sr_zip',
                 tech='sr_tech_xlsx')
        pre = 'sr'
    else:
        pre = f'sr{span}'
        k = dict(mode=f'{pre}_input_mode', a=f'{pre}_dir_a', b=f'{pre}_dir_b',
                 browse_a=f'{pre}_browse_a', browse_b=f'{pre}_browse_b',
                 browse_one=f'{pre}_browse_one', one=f'{pre}_one_folder',
                 zip=f'{pre}_zip', tech=f'{pre}_tech_xlsx')
    k.update(site_a=f'{pre}_site_a', site_b=f'{pre}_site_b',
             site_src=f'{pre}_site_src', site_saved=f'{pre}_site_saved')
    return k


# _sr_site_inputs re-derives the site names whenever the folder pair
# changes.  A project's saved names must survive that first render, so apply
# leaves this marker in the site_src slot and the page adopts the pair as-is.
PROJECT_SITE_MARK = ('project',)


def _clean_path(v):
    return (v or '').strip().strip('"') if isinstance(v, str) else ''


def _project_snapshot(ss, base=None):
    """What the project file would hold right now, with ABSOLUTE paths.

    `ss` is session_state (or any mapping).  A widget key Streamlit has
    dropped because its page is not on screen falls back to `base` (the
    project as last saved/opened), so standing on the Viewer page does not
    read as "the site names were erased"."""
    base = base or {}
    bspans = base.get('spans') or []

    def pick(key, fallback):
        return ss[key] if key in ss else fallback

    n = pick('sr_n_spans', len(bspans) or 1)
    try:
        n = max(1, min(int(n), SR_MAX_SPANS_CAP))
    except (TypeError, ValueError):
        n = 1
    spans = []
    for i in range(1, n + 1):
        k = _sr_span_keys(i)
        b = bspans[i - 1] if i <= len(bspans) else {}
        mode = pick(k['mode'], None)
        mode = (b.get('mode', 'two') if mode is None
                else ('one' if mode == SR_MODE_ONE else 'two'))
        spans.append({
            'mode': mode,
            'dir_a': _clean_path(pick(k['a'], b.get('dir_a', ''))),
            'dir_b': _clean_path(pick(k['b'], b.get('dir_b', ''))),
            'folder': _clean_path(pick(k['one'], b.get('folder', ''))),
            'site_a': str(pick(k['site_a'], b.get('site_a', '')) or ''),
            'site_b': str(pick(k['site_b'], b.get('site_b', '')) or ''),
        })

    def _dict(key):
        v = pick(key, base.get(key))
        return dict(v) if isinstance(v, dict) else None

    return {
        'analysis_mode': pick('analysis_mode', base.get('analysis_mode')),
        'profile': pick('otdr_profile', base.get('profile')),
        'otdr_settings': _dict('otdr_settings'),
        'conn_settings': _dict('conn_settings'),
        'cable_type': pick('cable_type', base.get('cable_type')),
        'report_dest': _clean_path(pick('sr_report_dest', base.get('report_dest', ''))),
        'spans': spans,
        'fqa_job': _dict('fqa_job'),
        'manual': dict(pick('project_manual', base.get('manual')) or {}),
        'job_id': pick('project_job_id', base.get('job_id')),
        'shoots': dict(pick('project_shoots', base.get('shoots')) or {}),
        'final_shoot': pick('project_final_shoot', base.get('final_shoot')),
        'gps': dict(pick('project_gps', base.get('gps')) or {}),
        'owner': dict(pick('project_owner', base.get('owner')) or {}),
        'sharepoint': dict(pick('project_sp', base.get('sharepoint')) or {}),
    }


# The page's own cap lives further down (SR_MAX_SPANS); the sidebar runs
# before it is defined, so the project code carries the same number.
SR_MAX_SPANS_CAP = 8


def _path_ref(path, project_dir):
    """{'rel', 'abs'} for one folder.  rel uses '/' so a project written on
    Windows opens on a Mac; it is None across drives (no relative path)."""
    if not path:
        return None
    ab = os.path.abspath(path)
    try:
        rel = os.path.relpath(ab, project_dir).replace(os.sep, '/')
    except ValueError:
        rel = None
    return {'rel': rel, 'abs': ab}


def _path_resolve(ref, project_dir):
    """The folder a stored ref points at on THIS machine: relative-to-the-
    project first, then the absolute path, then (neither exists) the relative
    one so the tech sees a sensible 'not found' path."""
    if isinstance(ref, str):
        ref = {'rel': None, 'abs': ref}
    if not isinstance(ref, dict):
        return ''
    rel, ab = ref.get('rel'), ref.get('abs') or ''
    cand = (os.path.normpath(os.path.join(project_dir, *rel.split('/')))
            if rel else '')
    if cand and os.path.exists(cand):
        # Same folder as the stored absolute path: keep that exact string, so
        # the report cache (keyed on the path as typed) still matches.
        if ab and os.path.normcase(os.path.normpath(ab)) == os.path.normcase(cand):
            return ab
        return cand
    if ab and os.path.exists(ab):
        return ab
    return cand or ab


def project_to_file_data(snap, project_path, markers=None):
    """The JSON the project file holds, from a snapshot."""
    pdir = os.path.dirname(os.path.abspath(project_path))
    spans = []
    for i, s in enumerate(snap.get('spans') or []):
        row = {'mode': s.get('mode', 'two'),
               'site_a': s.get('site_a', ''), 'site_b': s.get('site_b', ''),
               'dir_a': _path_ref(s.get('dir_a'), pdir),
               'dir_b': _path_ref(s.get('dir_b'), pdir),
               'folder': _path_ref(s.get('folder'), pdir)}
        if i == 0 and markers and (markers.get('a') or markers.get('b')):
            row['span_markers'] = {'a': markers.get('a'), 'b': markers.get('b')}
        spans.append(row)
    return {
        'format': PROJECT_FORMAT, 'version': PROJECT_VERSION,
        'saved': time.strftime('%Y-%m-%d %H:%M:%S'),
        'app': _app_version(),
        'analysis_mode': snap.get('analysis_mode'),
        'profile': snap.get('profile'),
        'otdr_settings': snap.get('otdr_settings'),
        'conn_settings': snap.get('conn_settings'),
        'cable_type': snap.get('cable_type'),
        'report_dest': _path_ref(snap.get('report_dest'), pdir),
        'spans': spans,
        'fqa_job': snap.get('fqa_job'),
        'manual': snap.get('manual') or {},
        'job_id': snap.get('job_id'),
        'shoots': snap.get('shoots') or {},
        'final_shoot': snap.get('final_shoot'),
        'gps': snap.get('gps') or {},
        'owner': snap.get('owner') or {},
        # The SharePoint folder the project works from (link + the folder
        # opened inside it); the sign-in itself stays per PC.
        'sharepoint': snap.get('sharepoint') or {},
    }


def project_from_file_data(data, project_path):
    """(snapshot, span-1 markers) from a project file's JSON.  Raises
    ValueError on something that is not an OTDR Suite project."""
    if not isinstance(data, dict) or data.get('format') != PROJECT_FORMAT:
        raise ValueError(f'not an {PRODUCT_NAME} project file')
    if int(data.get('version') or 0) > PROJECT_VERSION:
        raise ValueError(f'this project was saved by a newer {PRODUCT_NAME} -- '
                         'update the app to open it')
    pdir = os.path.dirname(os.path.abspath(project_path))
    spans, markers = [], None
    for i, s in enumerate((data.get('spans') or [])[:SR_MAX_SPANS_CAP]):
        if not isinstance(s, dict):
            continue
        spans.append({'mode': 'one' if s.get('mode') == 'one' else 'two',
                      'dir_a': _path_resolve(s.get('dir_a'), pdir),
                      'dir_b': _path_resolve(s.get('dir_b'), pdir),
                      'folder': _path_resolve(s.get('folder'), pdir),
                      'site_a': str(s.get('site_a') or ''),
                      'site_b': str(s.get('site_b') or '')})
        if i == 0 and isinstance(s.get('span_markers'), dict):
            markers = s['span_markers']
    if not spans:
        spans = [{'mode': 'two', 'dir_a': '', 'dir_b': '', 'folder': '',
                  'site_a': '', 'site_b': ''}]
    snap = {
        'analysis_mode': (data.get('analysis_mode')
                          if data.get('analysis_mode') in ANALYSIS_MODES else None),
        'profile': data.get('profile') if isinstance(data.get('profile'), str) else None,
        'otdr_settings': data.get('otdr_settings') if isinstance(data.get('otdr_settings'), dict) else None,
        'conn_settings': data.get('conn_settings') if isinstance(data.get('conn_settings'), dict) else None,
        'cable_type': data.get('cable_type') if isinstance(data.get('cable_type'), str) else None,
        'report_dest': _path_resolve(data.get('report_dest'), pdir) if data.get('report_dest') else '',
        'spans': spans,
        'fqa_job': data.get('fqa_job') if isinstance(data.get('fqa_job'), dict) else None,
        'manual': data.get('manual') if isinstance(data.get('manual'), dict) else {},
        'job_id': data.get('job_id') if isinstance(data.get('job_id'), str) else None,
        'shoots': data.get('shoots') if isinstance(data.get('shoots'), dict) else {},
        'final_shoot': data.get('final_shoot') if isinstance(data.get('final_shoot'), str) else None,
        'gps': ({str(k): str(v) for k, v in data['gps'].items() if v}
                if isinstance(data.get('gps'), dict) else {}),
        'owner': ({k: str(data['owner'].get(k) or '') for k in ('name', 'email')}
                  if isinstance(data.get('owner'), dict) else {}),
        'sharepoint': ({k: str(data['sharepoint'].get(k) or '') for k in ('link', 'path', 'save')
                        if data['sharepoint'].get(k)}
                       if isinstance(data.get('sharepoint'), dict) else {}),
    }
    return snap, markers


def project_write(project_path, data):
    """Write atomically: a killed save must not leave half a project."""
    project_path = os.path.abspath(project_path)
    os.makedirs(os.path.dirname(project_path), exist_ok=True)
    tmp = project_path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, project_path)


def project_read(project_path):
    with open(project_path, encoding='utf-8') as fh:
        data = json.load(fh)
    return project_from_file_data(data, project_path)


def project_apply(snap, ss, only_missing=False):
    """Put a project's values into session_state, ahead of every widget.

    only_missing=True is the quiet re-attach after a Viewer click-through
    (a fresh session): the deep link already seeded the folders, so only
    what the wipe lost (profile, tables, site names) is filled back in."""
    def put(key, val):
        if only_missing and key in ss:
            return
        ss[key] = val

    if snap.get('analysis_mode') in ANALYSIS_MODES and not (
            only_missing and 'analysis_mode' in ss):
        ss['analysis_mode'] = snap['analysis_mode']
        ss.pop('analysis_switch', None)       # the switch re-reads the mode
        save_analysis_mode(snap['analysis_mode'])
        try:
            trace_server.CONFIG['analysis_mode'] = snap['analysis_mode']
        except Exception:
            pass
    if snap.get('profile') and not (only_missing and 'otdr_profile' in ss):
        ss['otdr_profile'] = snap['profile']
        ss.pop('otdr_profile_select', None)   # the picker re-reads index=
        # A missing table re-derives from the profile on the next render.
        for key in ('otdr_settings', 'conn_settings'):
            if isinstance(snap.get(key), dict):
                ss[key] = dict(snap[key])
            else:
                ss.pop(key, None)
    if snap.get('cable_type') and not (only_missing and 'cable_type' in ss):
        ss['cable_type'] = snap['cable_type']
        ss.pop('cable_type_select', None)
    put('sr_report_dest', snap.get('report_dest') or '')

    spans = snap.get('spans') or []
    old_n = ss.get('sr_n_spans', 1)
    put('sr_n_spans', max(1, len(spans)))
    for i, s in enumerate(spans, 1):
        k = _sr_span_keys(i)
        put(k['mode'], SR_MODE_ONE if s.get('mode') == 'one' else SR_MODE_TWO)
        put(k['a'], s.get('dir_a', ''))
        put(k['b'], s.get('dir_b', ''))
        put(k['one'], s.get('folder', ''))
        if s.get('site_a') or s.get('site_b'):
            if not (only_missing and k['site_a'] in ss):
                ss[k['site_a']] = s.get('site_a') or 'A'
                ss[k['site_b']] = s.get('site_b') or 'B'
                ss[k['site_src']] = PROJECT_SITE_MARK
    if only_missing:
        return
    # A different span: nothing from the previous one may stay on screen.
    for i in range(len(spans) + 1, max(int(old_n or 1), SR_MAX_SPANS_CAP) + 1):
        for key in _sr_span_keys(i).values():
            ss.pop(key, None)
    for key in ('viewer_target', 'span_loaded', 'uni_result', 'sr_queue'):
        ss.pop(key, None)
    for i in range(1, SR_MAX_SPANS_CAP + 1):
        sfx = '' if i == 1 else str(i)
        ss.pop(f'sr_result{sfx}', None)
        ss.pop(f'sr_dirs{sfx}', None)
    # Another project's job form must not leak into this one.
    for key in ('fqa_job', 'fqa_derived_for', 'fqa_prod', 'fqa_dest'):
        ss.pop(key, None)
    if isinstance(snap.get('fqa_job'), dict):
        ss['fqa_job'] = dict(snap['fqa_job'])
    ss['project_manual'] = dict(snap.get('manual') or {})
    if snap.get('job_id'):
        ss['project_job_id'] = snap['job_id']
    else:
        ss.pop('project_job_id', None)
    ss['project_shoots'] = dict(snap.get('shoots') or {})
    ss['project_gps'] = dict(snap.get('gps') or {})
    ss['project_owner'] = dict(snap.get('owner') or {})
    ss['project_sp'] = dict(snap.get('sharepoint') or {})
    # Browsing starts in the project's folder, not the last one looked at.
    ss.pop('sp_edit', None)
    for key in ('sp_path', '_sp_cache', '_sp_confirm', 'spx_path'):
        ss.pop(key, None)
    if ss['project_sp'].get('path'):
        ss['sp_path'] = ss['project_sp']['path']
    if snap.get('final_shoot') is not None:
        ss['project_final_shoot'] = snap['final_shoot']
    else:
        ss.pop('project_final_shoot', None)


# Recent projects + the one last used live in the same settings.json as the
# analysis mode (OTDR_SETTINGS_DIR overrides it for tests).
def _settings_read():
    try:
        with open(_analysis_settings_path(), encoding='utf-8') as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _settings_update(**kv):
    """Merge keys into settings.json; never fatal."""
    path = _analysis_settings_path()
    data = _settings_read()
    data.update(kv)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except OSError:
        pass


def recent_projects():
    """Recent project paths, newest first, that still exist on this machine."""
    rows = _settings_read().get('recent_projects') or []
    return [p for p in rows if isinstance(p, str) and os.path.isfile(p)]


def _remember_project(path):
    path = os.path.abspath(path)
    rows = [p for p in (_settings_read().get('recent_projects') or [])
            if isinstance(p, str)
            and os.path.normcase(os.path.abspath(p)) != os.path.normcase(path)]
    _settings_update(recent_projects=[path] + rows[:PROJECT_RECENT_MAX - 1],
                     last_project=path)


def _span_markers_for(snap):
    """Span 1's Viewer markers (spans.json), to carry inside the file."""
    s = (snap.get('spans') or [{}])[0]
    if s.get('mode') != 'two' or not (s.get('dir_a') and s.get('dir_b')):
        return None
    try:
        return trace_server.span_decl(s['dir_a'], s['dir_b'])
    except Exception:
        return None


def _restore_span_markers(snap, markers):
    """Write the file's markers into this machine's spans.json -- only when
    this machine has none for the pair, so a tech's newer local markers win."""
    s = (snap.get('spans') or [{}])[0]
    if not markers or s.get('mode') != 'two' or not (
            s.get('dir_a') and s.get('dir_b')
            and os.path.isdir(s['dir_a']) and os.path.isdir(s['dir_b'])):
        return
    try:
        have = trace_server.span_decl(s['dir_a'], s['dir_b'])
        if have.get('a') or have.get('b'):
            return
        for d in ('a', 'b'):
            for edge in ('start', 'end'):
                km = (markers.get(d) or {}).get(edge + '_km')
                if isinstance(km, (int, float)):
                    trace_server.span_decl_set(d, edge, km, s['dir_a'], s['dir_b'])
    except Exception:
        pass


def project_open(path):
    """Read `path` and put it into session_state.  Called before any input
    widget of this run is drawn -- the Project box sits above them all, and
    a key cannot be set once its widget exists.  Raises OSError / ValueError
    on a file that cannot be read."""
    ss = st.session_state
    path = os.path.abspath(path)
    snap, markers = project_read(path)
    project_apply(snap, ss)
    _restore_span_markers(snap, markers)
    ss['project_path'] = path
    ss['project_saved'] = _project_snapshot(ss, snap)
    ss['_project_rebase'] = True
    _remember_project(path)
    here = os.path.normcase(os.path.dirname(path)) + os.sep
    missing = [p for s in snap['spans']
               for p in ((s['folder'],) if s['mode'] == 'one' else (s['dir_a'], s['dir_b']))
               if p and not os.path.isdir(p)
               and not os.path.normcase(os.path.abspath(p)).startswith(here)]
    if missing:
        ss['_project_missing'] = missing


def _project_reattach():
    """Top of a fresh session: a Viewer click-through is a URL nav that wipes
    session_state.  When the A folder the link seeded is the last project's,
    that project is open again, and what the wipe lost (profile, tables,
    site names) is filled back in from it."""
    ss = st.session_state
    if 'project_path' in ss or ss.get('_project_reattach_done'):
        return
    ss['_project_reattach_done'] = True
    a = _clean_path(ss.get('view_dir_a_input'))
    last = _settings_read().get('last_project')
    if not (a and isinstance(last, str) and os.path.isfile(last)):
        return
    try:
        snap, _m = project_read(last)
    except Exception:
        return
    first = snap['spans'][0]
    if first['mode'] != 'two' or not first['dir_a'] or (
            os.path.normcase(os.path.abspath(first['dir_a']))
            != os.path.normcase(os.path.abspath(a))):
        return
    project_apply(snap, ss, only_missing=True)
    ss['project_path'] = last
    ss['project_saved'] = _project_snapshot(ss, snap)
    ss['_project_rebase'] = True
    ss['_project_reattached'] = True


# ─── Work folders, the home screen, project mode ─────────────────────────
# Robert, 2026-09-23: the suite opens on a home screen with two choices.
# "Run Traces" is OTDR Suite exactly as it was: the same sidebar and tools,
# nothing added but a Home button at the foot of the sidebar.  "Start
# Project" asks for the project's WORK FOLDER and opens the Project status
# page, laid out on the four sections of the customer's Submittal Checklist.
#
# The work folder IS the project.  Its project file sits inside it (saved
# on every change -- there is no Save button), and everything the package
# needs is copied into fixed subfolders, so the folder can be moved or put
# on SharePoint and still open:
PROJECT_DIRS = {
    'traces': 'Traces',          # Traces/A, Traces/B -- section 4 copies them in
    'production': 'Production',  # the production sheet (the project workbook)
    'field': 'Field',            # what the phone sends: FQA .xlsm, capture sheet
    'fqa': 'FQA',                # the FQA Builder's package
    'reports': 'Reports',        # Splice Report / Unidirectional output
    'power': 'Power Meter',      # checklist 4.02
    'splice_logs': 'Splice Logs',  # checklist 4.04 / 4.05
    'pictures': 'Pictures',      # photos added by hand: Pictures/A end, Z end, Other
}
# First-use help: what a project folder holds, shown on the home screen and
# the New Project screen.
PROJECT_LAYOUT_HELP = (
    'A project is one folder: Traces (A and B), Production (the production sheet), '
    'Field (what the phone sends), FQA (the FQA package), Reports, Pictures, Power '
    'Meter and Splice Logs. The project saves itself as you work; there is no Save button.')
# Run Traces is the trace tools only; the FQA Builder and Field Capture are
# project work (Robert, 2026-09-24).
TOOLS_TRACES = ['Viewer', 'Splice Report', 'Splice Report FEC', 'Viewer FEC',
                'Unidirectional', 'Secret Sauce']   # FEC: Robert 2026-10-02 (main #525)
TOOLS_PROJECT = ['Project Status'] + TOOLS_TRACES + ['FQA Builder', 'Field Capture']
def work_dir(project_path=None):
    p = project_path or st.session_state.get('project_path')
    return os.path.dirname(os.path.abspath(p)) if p else ''


def work_sub(name, work=None):
    return os.path.join(work or work_dir(), PROJECT_DIRS[name])


# ── Shoots: several dated sets of traces for one span ────────────────────
# Robert, 2026-09-24: "assign a date to traces and upload multiple shoots of
# the same span ... select which traces we use as our final".  Each shoot is
# its own folder, Traces/<date[ label]>/A and /B, so adding one never touches
# another.  A project made before shoots existed has its traces straight in
# Traces/A and Traces/B; that set is listed as a shoot too (id '').  Which
# shoot is final, and each shoot's date and label, live in the project file;
# the final shoot is what every tool, the section 4 checks and the phone job
# use.  With no choice made, the newest shoot is final.
LEGACY_SHOOT = ''


def list_shoots(work=None):
    """[{'id', 'dir', 'a', 'b'}] for every shoot in the work folder."""
    t = work_sub('traces', work)
    out = []
    if os.path.isdir(os.path.join(t, 'A')) or os.path.isdir(os.path.join(t, 'B')):
        out.append({'id': LEGACY_SHOOT, 'dir': t})
    try:
        names = sorted(os.listdir(t))
    except OSError:
        names = []
    for n in names:
        d = os.path.join(t, n)
        if n in ('A', 'B') or n.startswith('.') or not os.path.isdir(d):
            continue
        if os.path.isdir(os.path.join(d, 'A')) or os.path.isdir(os.path.join(d, 'B')):
            out.append({'id': n, 'dir': d})
    for sh in out:
        sh['a'], sh['b'] = os.path.join(sh['dir'], 'A'), os.path.join(sh['dir'], 'B')
    return out


@st.cache_data(show_spinner=False, max_entries=64)
def _sor_date_cached(folder, _mtime):
    import datetime as _dt
    try:
        import sor_reader324802a as _sr          # the Viewer's copy, as the hub uses
    except Exception:
        return ''
    import calendar as _cal
    best = None
    try:
        names = sorted(n for n in os.listdir(folder)
                       if n.lower().rstrip().endswith(('.sor', '.trc')))[:3]
    except OSError:
        return ''
    for n in names:
        try:
            with open(os.path.join(folder, n), 'rb') as fh:
                data = fh.read()
            if n.lower().rstrip().endswith('.trc'):
                # A .trc stores its shot time as an ISO string (UTC), not
                # FxdParams' epoch; folder_intake reads it with no reader copy.
                import folder_intake as _fi
                iso = _fi.trc_header(os.path.join(folder, n)).get('date_utc')
                ts = _cal.timegm(_dt.datetime.strptime(iso, '%Y-%m-%dT%H:%M:%S')
                                 .timetuple()) if iso else 0
            else:
                ts = _sr._parse_fxd_params(data, _sr._parse_block_directory(data)).get('date_time') or 0
            if ts > 0:
                best = min(best, ts) if best else ts
        except Exception:
            continue
    return _dt.datetime.fromtimestamp(best).strftime('%Y-%m-%d') if best else ''


def sor_shot_date(folder):
    """'YYYY-MM-DD' the traces in `folder` were shot (the .sor header's
    date, or a .trc's), or '' when there is no .sor or .trc to read."""
    try:
        return _sor_date_cached(os.path.abspath(folder), os.path.getmtime(folder))
    except OSError:
        return ''


def shoot_info(sh):
    """A shoot's date and label: the project's record, else the files."""
    meta = (st.session_state.get('project_shoots') or {}).get(sh['id']) or {}
    date = meta.get('date') or sor_shot_date(sh['a']) or sor_shot_date(sh['b'])
    if not date and sh['id'][:10].count('-') == 2:
        date = sh['id'][:10]
    label = meta.get('label')
    if label is None:
        label = sh['id'][10:].strip() if sh['id'][:10].count('-') == 2 else sh['id']
    return date or '', label or ''


def final_shoot(work=None):
    shoots = list_shoots(work)
    if not shoots:
        return None
    fid = st.session_state.get('project_final_shoot')
    for sh in shoots:
        if sh['id'] == fid:
            return sh
    return max(shoots, key=lambda sh: (shoot_info(sh)[0], sh['id']))


def work_trace_dirs(work=None):
    """The FINAL shoot's A and B folders (Traces/A, Traces/B before any)."""
    sh = final_shoot(work)
    if sh:
        return sh['a'], sh['b']
    t = work_sub('traces', work)
    return os.path.join(t, 'A'), os.path.join(t, 'B')


def new_shoot_folder(work, date, label=''):
    """Traces/<date[ label]>, made unique with ' (2)' ..."""
    base = (str(date) + (' ' + _safe_label(label) if label else '')).strip() or 'Shoot'
    t = work_sub('traces', work)
    name, k = base, 2
    while os.path.exists(os.path.join(t, name)):
        name = f'{base} ({k})'
        k += 1
    return name


def _safe_label(v):
    return ''.join(c if (c.isalnum() or c in ' -_.()&') else '_' for c in str(v)).strip(' .')[:40]


def project_file_for_folder(folder):
    """(the folder's project file, whether it exists yet).  An existing one
    is used whatever it is called; a new one is named after the folder."""
    try:
        have = sorted(f for f in os.listdir(folder)
                      if f.lower().endswith(PROJECT_EXT) and not f.startswith('.'))
    except OSError:
        have = []
    name = have[0] if have else (os.path.basename(os.path.normpath(folder)) or 'Project') + PROJECT_EXT
    return os.path.join(folder, name), bool(have)


def project_open_folder(folder):
    """Open the work folder as the project (creating its project file the
    first time) and land on Project status.  Called before anything is
    drawn.  Raises ValueError / OSError."""
    ss = st.session_state
    folder = os.path.abspath(_clean_path(folder))
    if not os.path.isdir(folder):
        raise ValueError(f'there is no folder at {folder}')
    path, exists = project_file_for_folder(folder)
    if not exists:
        t = work_sub('traces', folder)
        ta, tb = os.path.join(t, 'A'), os.path.join(t, 'B')
        snap = {'report_dest': work_sub('reports', folder), 'manual': {},
                'spans': [{'mode': 'two', 'dir_a': ta, 'dir_b': tb, 'folder': '',
                           'site_a': '', 'site_b': ''}]}
        project_write(path, project_to_file_data(snap, path))
    # Another project's (or Quick Analysis') folders must not stay in the
    # tools: they are seeded again from this project.
    for key in ('view_dir_a_input', 'view_dir_b_input', 'uni_folder_input', 'ss_folder_input'):
        ss.pop(key, None)
    project_open(path)
    _fresh_tool_chain()
    ss['app_mode'] = 'project'
    ss['nav_radio'] = 'Project Status'
    return path


def _project_seed_tools():
    """Project mode, every run, before anything is drawn: point the tools at
    the work folder.  setdefault only -- a tech who types another folder into
    a tool keeps it -- but Streamlit drops a widget's key while its page is
    off screen, so the seeding has to run every time, not once."""
    ss = st.session_state
    work = work_dir()
    if not work:
        return
    saved = ss.get('project_saved') or {}
    s1 = (saved.get('spans') or [{}])[0]
    ta, tb = work_trace_dirs(work)
    # The file's span-1 folders when they exist; a project made before
    # dated shoots points at Traces/A, which the first shoot moved.
    pa = s1.get('dir_a') if os.path.isdir(s1.get('dir_a') or '') else ta
    pb = s1.get('dir_b') if os.path.isdir(s1.get('dir_b') or '') else tb
    ss.setdefault('view_dir_a_input', pa)
    ss.setdefault('view_dir_b_input', pb)
    ss.setdefault('uni_folder_input', pa)
    _fs = final_shoot(work)
    ss.setdefault('ss_folder_input', _fs['dir'] if _fs else work_sub('traces', work))
    for key in ('sr_report_dest', 'uni_report_dest', 'ss_report_dest'):
        if not ss.get(key):
            ss[key] = work_sub('reports', work)
    if not ss.get('fqa_dest'):
        ss['fqa_dest'] = work_sub('fqa', work)
    if not ss.get('fc_report_dest'):
        ss['fc_report_dest'] = work_sub('field', work)
    prod = project_production_sheet(work)
    if prod and not ss.get('fqa_prod'):
        ss['fqa_prod'] = prod
    # Streamlit sends a box the value code gave it ONLY when that value was
    # assigned in the same run that draws the box (SessionState.
    # is_new_state_value).  The project fills these keys while Project status
    # is on screen and the tool draws them on a later run, so without this the
    # server held Traces/A while the browser showed an empty box -- and the
    # next keystroke would have sent the empty box back (and autosaved it).
    # Re-assigning each key to itself, every run, before anything is drawn,
    # keeps the browser in step.  Found 2026-09-23 in the browser; AppTest
    # reads the server's value and cannot see it.
    for key in _project_widget_keys():
        if key in ss:
            ss[key] = ss[key]
    if prod and 'fqa_job' not in ss and isinstance(saved.get('fqa_job'), dict):
        ss['fqa_job'] = dict(saved['fqa_job'])
    if prod and ss.get('fqa_derived_for') != prod:
        # Fill the job form from the production sheet WITHOUT losing what is
        # already there (a traces-first project's site names and fiber count,
        # anything typed): derive() never overwrites a value it is handed.
        # Marking the sheet as derived stops the FQA Builder re-deriving over
        # the result.
        try:
            from fqa.job_facts import JobFacts, derive
            merged = derive(_read_prod(prod), JobFacts.from_dict(ss.get('fqa_job') or {}))
            ss['fqa_job'] = json.loads(merged.to_json())
            ss['fqa_derived_for'] = prod
        except Exception:
            pass


def _project_widget_keys():
    """Every widget key a project fills in (see _project_seed_tools)."""
    keys = ['uni_folder_input', 'ss_folder_input', 'sr_report_dest',
            'uni_report_dest', 'ss_report_dest', 'fc_report_dest', 'fqa_dest', 'fqa_prod']
    for i in range(1, SR_MAX_SPANS_CAP + 1):
        k = _sr_span_keys(i)
        keys += [k['mode'], k['a'], k['b'], k['one'], k['site_a'], k['site_b']]
    return keys


def project_production_sheet(work=None):
    """The production sheet in the work folder, newest first; '' if none."""
    d = work_sub('production', work)
    try:
        rows = [os.path.join(d, n) for n in os.listdir(d)
                if n.lower().endswith(('.xlsx', '.xlsm')) and not n.startswith(('~$', '.'))]
    except OSError:
        return ''
    return max(rows, key=os.path.getmtime) if rows else ''


# Streamlit re-runs this whole script on every click in a FRESH namespace, so
# a module-level dict cache is empty again on the next click.  A 70-250 MB
# production sheet must be read once per file version, not once per click:
# st.cache_resource lives in the server process, keyed on size and mtime.
@st.cache_resource(show_spinner=False, max_entries=4)
def _read_prod_cached(path, _size, _mtime):
    from fqa.production_sheet import read_production_sheet
    return read_production_sheet(path)


def _read_prod(path):
    st_ = os.stat(path)
    return _read_prod_cached(os.path.abspath(path), st_.st_size, st_.st_mtime)


def _fresh_tool_chain():
    """Leaving for Home, entering Quick Analysis or opening a project starts
    a new chain of tools: the first tool there has no "previous tool", so the
    Thresholds Carried Over pop-up waits for a real change of tool (Robert,
    2026-09-30: "This was my first entry into quick analysis, I shouldn't
    have got this message yet")."""
    for k in ('_last_tool', '_last_settings_tool', '_carry_popup'):
        st.session_state.pop(k, None)


def _mode_actions():
    """Home-screen, Home-button and Project-status navigation clicks, read
    from session_state at the top of the run, before anything is drawn (the
    rerun trap: see _project_actions' history).  Returns (kind, message)
    for the home screen, or None."""
    ss = st.session_state
    if ss.get('go_home') or ss.get('setup_back'):
        ss.pop('app_mode', None)
        ss.pop('qa_stage', None)
        _fresh_tool_chain()
        return None
    for key, kind in (('home_new', 'new'), ('home_open_recent', 'open')):
        if ss.get(key):
            ss['app_mode'] = 'setup'
            ss['setup_kind'] = kind
            return None
    # Create project (on the setup screen) wrote the work folder and its
    # project file, then asked for this run: the open happens here, before
    # anything is drawn, like every other open.
    pending = ss.pop('_setup_open', None)
    if pending:
        try:
            project_open_folder(pending)
        except (OSError, ValueError) as exc:
            ss['app_mode'] = 'setup'
            ss['_setup_msg'] = ('error', f'Could not open the new project: {exc}')
            return None
        return None
    if ss.get('home_run_demo'):
        ss['_start_tour'] = True
    if ss.get('home_demo') or ss.get('home_run_demo'):
        # What makes the demo is defined further down this script: the
        # setup screen (drawn after it) makes it, then opens it like Create.
        ss['app_mode'] = 'setup'
        ss['setup_kind'] = 'demo'
        return None
    if ss.get('home_traces'):
        # OTDR Suite as it was: no project behind the tools, and a Viewer
        # click-through must not bring one back.
        for k in ('project_path', 'project_saved'):
            ss.pop(k, None)
        _settings_update(last_project=None)
        _fresh_tool_chain()
        # Quick Analysis starts on the Default profile and its tables, not
        # on the last project's customer (Robert, 2026-09-30: "we should
        # start fresh going into quick analysis").  Each slot re-derives from
        # the Default profile when its picker or table next draws.
        # The tables' own widget slots go too, or an earlier edit would be
        # committed back into the fresh table on its first draw.
        for k in list(ss.keys()):
            if (k in _CARRIED_SETTINGS
                    or k in ('otdr_profile_select', 'cable_type_select',
                             'uni_settings_component')
                    or k.startswith(('otdr_component::', 'conn_settings_component::'))):
                ss.pop(k, None)
        ss['app_mode'] = 'traces'
        # Robert, 2026-09-30: Quick Analysis opens straight on the Suite
        # screen (Trace Folders and the tool list in the left panel), with no
        # stop that asks for traces first.
        if ss.get('nav_radio') not in TOOLS_TRACES:
            ss['nav_radio'] = 'Viewer'
        return None

    def _open(folder):
        try:
            project_open_folder(folder)
        except (OSError, ValueError) as exc:
            if ss.get('app_mode') == 'setup':
                ss['_setup_msg'] = ('error', f'Could not open that work folder: {exc}')
                return None
            return ('error', f'Could not open that work folder: {exc}')
        return None

    if ss.get('home_project'):
        p = pick_folder('Choose the work folder for this project')
        if p:
            return _open(p)
        if p is None:
            ss['_home_need_path'] = True
        return None
    if ss.get('home_open_path'):
        typed = _clean_path(ss.get('home_folder'))
        return _open(typed) if typed else ('info', 'Paste the work folder\'s path first.')
    for i, p in enumerate(recent_projects()[:PROJECT_RECENT_MAX]):
        if ss.get(f'home_recent_{i}'):
            return _open(os.path.dirname(p))
    if ss.get('go_project'):
        ss['nav_radio'] = 'Project Status'
    # The top bar's Audit Project, from any tool: to Project status, audit on.
    if ss.get('bar_audit') or ss.get('ps_audit_start'):
        ss['audit_on'] = True
        ss['audit_skipped'] = []
        ss['nav_radio'] = 'Project Status'
    # Audit buttons that hand over to the status page (the audit is left
    # on pause; its button picks it up again from the top).
    if ss.get('aud_go_status') or ss.get('aud_go_phone'):
        ss['audit_on'] = False
    # Run Traces In (Traces and Reports tabs): the ticked shoots, else the
    # final one (Robert, 2026-09-27: "click a check box next to any of the
    # traces ... rather than being stuck with just the final").
    if ss.get('app_mode') == 'project' and ss.get('project_path'):
        for sh in list_shoots(work_dir()):
            for page in RUN_IN_TOOLS:
                if ss.get(run_in_key(sh, page)):
                    return _run_shoot_in(page, sh)
    # Project status buttons that switch tools.
    for key, page in (('ps_go_fqa', 'FQA Builder'), ('ps_go_fqa2', 'FQA Builder'),
                      ('ps_go_fqa3', 'FQA Builder'), ('ps_go_fc', 'Field Capture'),
                      ('aud_go_fqa', 'FQA Builder')):
        if ss.get(key):
            ss['nav_radio'] = page
    return None


# Each shoot's row on the Traces tab has its own Run In… button (Robert,
# 2026-09-27: "instead of selecting them and clicking buttons at the bottom
# we can just click a button in the trace's row").  The FEC tools start there
# too (Robert, 2026-10-02): a project opens them from a shoot's Run In….
RUN_IN_TOOLS = ('Viewer', 'Splice Report', 'Splice Report FEC', 'Viewer FEC',
                'Unidirectional', 'Secret Sauce')


def run_in_key(sh, page):
    return f"run_{page.replace(' ', '_').lower()}_{sh['id'] or '(first shoot)'}"


def _run_shoot_in(page, sh):
    """Point `page` at one shoot and open it.  Before anything is drawn,
    like every tool switch."""
    ss = st.session_state
    for k in list(ss.keys()):
        if str(k).startswith(('sr_result', 'sr_dirs', 'uni_result', 'ss_result',
                              'viewer_target')):
            ss.pop(k, None)
    if page in ('Viewer', 'Splice Report', 'Viewer FEC'):
        ss['view_dir_a_input'], ss['view_dir_b_input'] = sh['a'], sh['b']
    if page == 'Splice Report FEC':
        # Its own A End / B End FEC boxes (the page shows what is in them).
        ss['fec_dir_a'], ss['fec_dir_b'] = sh['a'], sh['b']
    if page == 'Splice Report':
        ss[_sr_span_keys(1)['mode']] = SR_MODE_TWO
        ss['sr_n_spans'] = 1
    if page == 'Unidirectional':
        ss['uni_folder_input'] = sh['a']
    if page == 'Secret Sauce':
        ss['ss_folder_input'] = sh['dir']
    ss['project_run_shoot'] = sh['id']
    ss['nav_radio'] = page
    return None


# Home -> View Sample Span (Robert, 2026-09-27): demo/ ships a made-up
# span (demo/build_demo_assets.py).  The first click makes it a project in the
# projects folder; later clicks open it as it was left.
DEMO_DIR = os.path.join(HERE, 'demo')
DEMO_NAME = 'Sample Span'
DEMO_JOB_ID = 'demo0001'


# The sample job's history (Robert, 2026-09-27: "multiple traces, multiple
# events, multiple reports ... 10 events"): three shoots, four reports on
# different shoots, Field Capture sent and back, a fix typed by hand, the
# FQA package built.  Every entry is dated, and each file carries its date.
DEMO_SHOOTS = [('2026-04-22', 'first shoot', '2026-04-22 16:30'),
               ('2026-05-01', 'reshoot after repair', '2026-05-01 14:00'),
               ('2026-05-06', 'final', '2026-05-06 11:00')]


def _demo_when(text):
    return time.mktime(time.strptime(text, '%Y-%m-%d %H:%M'))


def _demo_capture(dest):
    """The sample Field Capture package.  With A-1/A-2/Z-1/Z-2.jpg in
    demo/private_photos or the app folder's sample_photos (real job photos,
    kept on this PC only, never in the repository), those stand in for the
    drawn ones."""
    import shutil
    import folder_intake
    src = os.path.join(DEMO_DIR, 'Demo Field Capture.zfc')
    swap = {'photos/A-1-1.jpg': 'A-1.jpg', 'photos/A-1-2.jpg': 'A-2.jpg',
            'photos/Z-1-1.jpg': 'Z-1.jpg', 'photos/Z-1-2.jpg': 'Z-2.jpg'}
    # Real photos stay off the (public) repository: demo/private_photos in a
    # checkout, or sample_photos in this PC's app folder for an installed
    # build (copied there by hand; 2026-09-28).
    app_dir = (os.environ.get('OTDR_SUITE_APP_DIR')
               or os.path.join(os.path.expanduser('~'), '.otdrSuite'))
    # demo/sample_photos exists only inside a private build: CI unlocks the
    # encrypted photos into it on the demo branch (2026-09-28); git ignores it.
    private = next((d for d in (os.path.join(DEMO_DIR, 'private_photos'),
                                os.path.join(DEMO_DIR, 'sample_photos'),
                                os.path.join(app_dir, 'sample_photos'))
                    if all(os.path.isfile(os.path.join(d, f)) for f in swap.values())), None)
    if not private:
        shutil.copy2(src, dest)
        return
    sf = folder_intake.share_open(src, expect='field-capture')
    files = {'capture.json': sf.read('capture.json')}
    for inner, f in swap.items():
        with open(os.path.join(private, f), 'rb') as fh:
            files[inner] = fh.read()
    folder_intake.share_write(dest, 'field-capture', files, {'job': DEMO_JOB_ID})


SAMPLE_PHOTO_NAMES = ('A-1.jpg', 'A-2.jpg', 'Z-1.jpg', 'Z-2.jpg')
SAMPLE_PHOTO_MAX_BYTES = 25 * 1024 * 1024


def install_sample_photos(zip_bytes):
    """Put the Sample Span's real photos on this PC: a .zip holding A-1, A-2,
    Z-1 and Z-2.jpg (sent privately, never in the repository or an installer;
    Robert, 2026-09-29) unpacked into the app folder's sample_photos, where
    _demo_capture looks.  Only those four names are taken, by base name, so a
    path inside the .zip can never place a file anywhere else.  Returns '' on
    success, else what is wrong with the file."""
    import io
    import shutil
    import zipfile
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except (zipfile.BadZipFile, ValueError):
        return 'That file is not a .zip.'
    found = {}
    for info in zf.infolist():
        base = os.path.basename(info.filename.replace('\\', '/'))
        if base in SAMPLE_PHOTO_NAMES and not info.is_dir():
            found.setdefault(base, info)
    missing = [n for n in SAMPLE_PHOTO_NAMES if n not in found]
    if missing:
        return 'The .zip is missing ' + ', '.join(missing) + '.'
    photos = {}
    for name in SAMPLE_PHOTO_NAMES:
        info = found[name]
        if info.file_size > SAMPLE_PHOTO_MAX_BYTES:
            return f'{name} is too large for a sample photo.'
        try:
            data = zf.read(info)
        except Exception:
            return f'{name} could not be read from the .zip.'
        if not data.startswith(b'\xff\xd8'):
            return f'{name} is not a JPEG photo.'
        photos[name] = data
    app_dir = (os.environ.get('OTDR_SUITE_APP_DIR')
               or os.path.join(os.path.expanduser('~'), '.otdrSuite'))
    dest = os.path.join(app_dir, 'sample_photos')
    tmp = dest + '.new'
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    for name, data in photos.items():
        with open(os.path.join(tmp, name), 'wb') as fh:
            fh.write(data)
    shutil.rmtree(dest, ignore_errors=True)
    os.replace(tmp, dest)
    return ''


def _demo_capture_path(root=None):
    """The Sample Span project's Field Capture package, or '' when the sample
    has not been opened on this PC.  Without a projects folder the sample is
    found through the recent projects list: the Home screen draws before the
    projects folder helpers are defined."""
    if root:
        works = [os.path.join(root, DEMO_NAME)]
    else:
        works = [os.path.dirname(p) for p in recent_projects()
                 if os.path.basename(os.path.dirname(p)) == DEMO_NAME]
    for work in works:
        zfc = os.path.join(work_sub('field', work), 'Demo Field Capture.zfc')
        if os.path.isfile(zfc):
            return zfc
    return ''


def sample_photos_real(root=None):
    """True when the Sample Span shows real photos on this PC: the four are
    where _demo_capture looks, or an existing Sample Span was made with them
    (a private demo build put them inside its package; an upgrade keeps it)."""
    import folder_intake
    app_dir = (os.environ.get('OTDR_SUITE_APP_DIR')
               or os.path.join(os.path.expanduser('~'), '.otdrSuite'))
    for d in (os.path.join(DEMO_DIR, 'private_photos'),
              os.path.join(DEMO_DIR, 'sample_photos'),
              os.path.join(app_dir, 'sample_photos')):
        if all(os.path.isfile(os.path.join(d, f)) for f in SAMPLE_PHOTO_NAMES):
            return True
    zfc = _demo_capture_path(root)
    if not zfc:
        return False
    try:
        drawn = folder_intake.share_open(os.path.join(DEMO_DIR, 'Demo Field Capture.zfc'),
                                         expect='field-capture')
        made = folder_intake.share_open(zfc, expect='field-capture')
        return made.read('photos/A-1-1.jpg') != drawn.read('photos/A-1-1.jpg')
    except Exception:
        return False


def refresh_sample_capture(root=None):
    """Rebuild an existing Sample Span's Field Capture package so it shows the
    photos installed now.  The file keeps its time, so the project history
    does not list it as newly found.  True when there was one to rebuild."""
    zfc = _demo_capture_path(root)
    if not zfc:
        return False
    before = os.stat(zfc)
    _demo_capture(zfc)
    os.utime(zfc, (before.st_atime, before.st_mtime))
    return True


def demo_project(root=None):
    """The sample project's work folder, made on first use."""
    work = os.path.join(root or _default_projects_root(), DEMO_NAME)
    if project_file_for_folder(work)[1] if os.path.isdir(work) else False:
        return work
    import shutil
    ta, tb = os.path.join(DEMO_DIR, 'traces', 'A'), os.path.join(DEMO_DIR, 'traces', 'B')
    rep_src = os.path.join(DEMO_DIR, 'reports')
    new_project(work, customer='Lumen',
                sheet=os.path.join(DEMO_DIR, 'Demo Production Sheet.xlsx'))
    data = events_read(work)
    data['events'] = []                     # the history below replaces "created now"
    known = data['known']

    def ev(when, kind, text, paths=(), how='OTDR Suite'):
        t = _demo_when(when)
        for p in paths:
            for root_, dirs, files in os.walk(p) if os.path.isdir(p) else [('', [], [])]:
                for f in files:
                    os.utime(os.path.join(root_, f), (t, t))
            os.utime(p, (t, t))
            known[_rel(work, p)] = t
        # Made-up logins for the User column: the tech saved the files found
        # in the folder, the office did the rest.
        data['events'].append({'when': t, 'kind': kind, 'text': text, 'how': how,
                               'user': 'tech.demo' if how == 'Found in folder'
                               else 'office.demo',
                               'files': [_rel(work, p) for p in paths]})

    prod = project_production_sheet(work)
    ev('2026-04-20 09:00', 'Project', 'Project created from the production sheet · '
       'customer Lumen')
    ev('2026-04-20 09:05', 'Production Sheet',
       f'Production sheet added: {os.path.basename(prod)}', [prod])
    shoots, report_traces = {}, {}
    for date, label, when in DEMO_SHOOTS:
        sid = f'{date} {label}'
        d = os.path.join(work_sub('traces', work), sid)
        na, nb = copy_traces(ta, tb, os.path.join(d, 'A'), os.path.join(d, 'B'))
        shoots[sid] = {'date': date, 'label': label}
        ev(when, 'Traces', f'Traces added: shot {date} · {label} · {na} A and {nb} B '
           'trace files', [d])
    first, reshoot, final = [f'{d} {l}' for d, l, _w in DEMO_SHOOTS]
    reports = work_sub('reports', work)
    os.makedirs(reports, exist_ok=True)

    def report(src, name, when, sid, kind):
        dest = os.path.join(reports, name)
        if os.path.isdir(src):
            shutil.copytree(src, dest)
        else:
            shutil.copy2(src, dest)
        sh = os.path.join(work_sub('traces', work), sid)
        report_traces[_rel(work, dest)] = [os.path.join(sh, 'A'), os.path.join(sh, 'B')]
        ev(when, 'Report', f'{kind} run: {name}', [dest])

    sr = 'ELMDALE_to_MILLER_SpliceReport'
    report(os.path.join(rep_src, sr + '.xlsx'), f'{sr} 2026-04-22 1710.xlsx',
           '2026-04-22 17:10', first, 'Splice Report')
    ev('2026-04-23 08:15', 'Field Capture', 'Field Capture job emailed to the tech '
       '(job demo0001: 6 splice points, both ends)')
    field = work_sub('field', work)
    os.makedirs(field, exist_ok=True)
    zfc = os.path.join(field, 'Demo Field Capture.zfc')
    _demo_capture(zfc)
    # Saved from the tech's email straight into the Field folder, as in the
    # field: the folder scan found it.
    ev('2026-04-28 15:20', 'Field Capture', 'Field Capture received: 4 photos, 7 GPS fixes '
       '(Demo Field Capture.zfc)', [zfc], how='Found in folder')
    pics = os.path.join(work_sub('pictures', work))
    for end, name in (('A end', 'Splice 3 closure.jpg'), ('Z end', 'Vault lid.jpg')):
        os.makedirs(os.path.join(pics, end), exist_ok=True)
        dest = os.path.join(pics, end, name)
        shutil.copy2(os.path.join(DEMO_DIR, 'pictures', name), dest)
        ev('2026-04-28 16:05', 'Photo', f'Photo added ({end}): {name}', [dest])
    ev('2026-04-29 10:00', 'GPS', 'GPS entered by hand: event 4')
    # Two more files someone saved into the job folder by hand.
    for sub_, name, body, when, text in (
            ('power', 'Power meter readings 2026-04-30.csv',
             'Fiber,1310 nm (dB),1550 nm (dB)\n' + ''.join(
                 f'{f},{-18.2 - f % 3 * 0.1:.2f},{-17.1 - f % 4 * 0.1:.2f}\n' for f in range(1, 25)),
             '2026-04-30 13:15', 'Power meter file: Power meter readings 2026-04-30.csv'),
            ('splice_logs', 'Splice log Splice 3.csv',
             'Tray,Fibers,Splice loss (dB)\n1,1-12,0.03\n2,13-24,0.04\n',
             '2026-05-02 09:40', 'Splice log: Splice log Splice 3.csv')):
        d = work_sub(sub_, work)
        os.makedirs(d, exist_ok=True)
        f = os.path.join(d, name)
        with open(f, 'w', encoding='utf-8') as fh:
            fh.write(body)
        ev(when, 'File', text, [f], how='Found in folder')
    report(os.path.join(rep_src, 'unidirectional_events.xlsx'),
           'unidirectional_events 2026-05-01 1440.xlsx', '2026-05-01 14:40', reshoot,
           'Unidirectional')
    ev('2026-05-06 11:05', 'Traces', 'Final traces set to 2026-05-06 · final')
    report(os.path.join(rep_src, sr + '.xlsx'), f'{sr} 2026-05-06 1130.xlsx',
           '2026-05-06 11:30', final, 'Splice Report')
    report(os.path.join(rep_src, 'Secret Sauce'), 'Secret Sauce 2026-05-06 1200',
           '2026-05-06 12:00', final, 'Secret Sauce')
    data['report_traces'] = report_traces
    _events_write(work, data)

    # The project file: the shoots, the final one, the phone job, the typed fix.
    path, _ = project_file_for_folder(work)
    with open(path, encoding='utf-8') as fh:
        pdata = json.load(fh)
    lat, lon = 39.1000, -100.2000
    pdata.update({'job_id': DEMO_JOB_ID, 'shoots': shoots, 'final_shoot': final,
                  'gps': {'4': f'{lat:.5f}, {lon:.5f}'}})
    project_write(path, pdata)
    # The final shoot's closures, as the Splice Report found them: the FQA
    # package takes its distances from the traces with no engine run.
    fs = {'a': os.path.join(work_sub('traces', work), final, 'A'),
          'b': os.path.join(work_sub('traces', work), final, 'B')}
    try:
        with open(os.path.join(DEMO_DIR, 'closures.json'), encoding='utf-8') as fh:
            manifest = json.load(fh)
        with open(os.path.join(work, TRACE_CLOSURES_FILE), 'w', encoding='utf-8') as fh:
            json.dump({'key': _closures_key(fs), 'manifest': manifest}, fh)
    except (OSError, ValueError):
        manifest = None
    # The FQA package, built from all of it.
    try:
        from fqa.event_chain import splice_distances
        prodx = _read_prod(prod)
        pkgs = [(os.path.basename(zfc), read_capture_package(zfc))]
        rows = project_gps_rows(prodx, pkgs, pdata['gps'])
        trace = (splice_distances(fs['a'], fs['b'], prodx, manifest=manifest)
                 if manifest else {'distances_m': None})
        photos = [p for p in project_photos(work, DEMO_JOB_ID) if p['end'] in ('A', 'Z')]
        m = build_project_fqa(work, prod, pdata.get('fqa_job') or {}, rows, photos, trace)
        dated = os.path.join(os.path.dirname(m['out']),
                             f'{DEMO_NAME} - FQA SITE SURVEY 2026-05-07 0900.xlsm')
        os.replace(m['out'], dated)
        m['out'] = dated
        data = events_read(work)
        known = data['known']
        ev('2026-05-07 09:00', 'FQA', f"FQA package built: {os.path.basename(m['out'])} · "
           f"{m.get('events')} events, distances from {m.get('distance_source')}",
           [m['out']])
        _events_write(work, data)
    except Exception as exc:
        report_error('sample span: FQA build', exc, {})
    return work


def _render_home(msg):
    """The two-choice start screen.  No sidebar: nothing in it applies yet."""
    _pn = globals().get('PRODUCT_NAME', 'OTDR Suite')   # alone in a test: the default
    st.markdown('<style>[data-testid="stSidebar"],[data-testid="stSidebarCollapsedControl"]'
                '{display:none}</style>', unsafe_allow_html=True)
    _render_update_nudge()
    st.markdown(f"<h2 style='text-align:center'>🔬 {_pn}</h2>", unsafe_allow_html=True)
    # One column, three choices stacked, all the same blue (Robert, 2026-09-24).
    _l, mid, _r = st.columns([1, 2, 1])
    with mid:
        st.button('🔬 Quick Analysis', key='home_traces', type='primary',
                  use_container_width=True)
        st.caption('The Viewer, Splice Report, Unidirectional and Secret Sauce, '
                   'the way you use them today.')
        st.button('📁 Start New Project', key='home_new', type='primary',
                  use_container_width=True)
        st.caption('Load what you have for a span. The project fills in everything it '
                   'can, then shows what the FQA package still needs.')
        st.caption(PROJECT_LAYOUT_HELP)
        st.button('📂 Open Recent Project', key='home_open_recent', type='primary',
                  use_container_width=True)
        # The sample and the tour sit apart, lower down, so they do not read
        # as work functions (Robert, 2026-09-28).
        # One element: the same gap above and below the line, with the label
        # on the line itself (Robert, 2026-09-28).
        st.markdown(
            # (Streamlit's own spacing is uneven around a block: measured and
            # evened out in the padding.)
            '<div style="padding:2.03rem 0 2.97rem;display:flex;align-items:center;gap:.75rem;'
            'color:var(--otdr-text-sec);font-size:.85rem;font-weight:600">'
            '<div style="flex:1;border-top:1px solid var(--otdr-rule)"></div>Try It Out'
            '<div style="flex:1;border-top:1px solid var(--otdr-rule)"></div></div>',
            unsafe_allow_html=True)
        c1, c2 = st.columns(2)
        c1.button('🧪 View Sample Span', key='home_demo', type='secondary',
                  use_container_width=True)
        c2.button('▶ Run Demo', key='home_run_demo', type='secondary',
                  use_container_width=True,
                  help='Opens the Sample Span and plays a 90-second tour of every tab. '
                       'It changes nothing.')
        st.caption('A made-up span with traces, a production sheet, photos and GPS, to '
                   'try every tab without touching a real job. Run Demo shows you around it.')
        _render_sample_photos_box()
        if msg:
            getattr(st, msg[0])(msg[1])
    # The build line lives at the foot of the sidebar only (Robert, 2026-09-27).


def _render_sample_photos_box():
    """OTDR Suite App only: until the Sample Span shows real photos on this PC,
    a closed box takes the .zip of them that was sent privately.  The App's
    installer is on a public link, so the photos never ride in it (Robert,
    2026-09-29).  Gone once they are in."""
    if os.environ.get('OTDR_SUITE_EDITION') != 'OTDR App':
        return
    try:
        if sample_photos_real():
            return
    except Exception:
        return
    with st.expander('Sample Span Photos'):
        st.caption('The Sample Span shows drawn photos. If you were sent the real ones '
                   'as a .zip, add it here. They stay on this computer.')
        up = st.file_uploader('Sample Photos (.zip)', type=['zip'],
                              key='home_sample_photos', label_visibility='collapsed')
        if up is None:
            return
        why = install_sample_photos(up.getvalue())
        if why:
            st.error(why)
            return
        try:
            refresh_sample_capture()
        except Exception as exc:
            report_error('home: sample photos', exc, {})
        st.success('Sample photos added. Open the Sample Span to see them.')


CRUMB_CSS = ('<style>.otdr-crumbs{margin:-.4rem 0 .6rem .15rem;font-size:.95rem}'
             '.otdr-crumbs .c1{padding-left:.55rem;border-left:3px solid var(--otdr-accent);'
             'font-weight:600}'
             '.otdr-crumbs .c2{margin-left:1.1rem;padding-left:.55rem;margin-top:.2rem;'
             'border-left:3px solid var(--otdr-edge-2);color:var(--otdr-accent)}</style>')
# The project screen's tabs switch in the browser, so the last line follows
# them there: it reads the selected tab whenever the page changes.
CRUMB_TAB_JS = """<script>
(function () {
  const P = window.parent, d = P.document;
  if (P.__otdrCrumbs) return;
  P.__otdrCrumbs = true;
  const sync = () => {
    const el = d.getElementById('otdr-crumb-tab');
    if (!el) return;
    const t = Array.from(d.querySelectorAll('button[role="tab"]'))
      .find((b) => b.getAttribute('aria-selected') === 'true');
    if (t && el.textContent !== t.innerText.trim()) el.textContent = t.innerText.trim();
  };
  new P.MutationObserver(sync).observe(d.body, {subtree: true, attributes: true,
    attributeFilter: ['aria-selected'], childList: true});
  sync();
})();
</script>"""


def _crumb_lines(page):
    """(where, what) for the sidebar: the project or the loaded span, then
    the tab or tool (Robert, 2026-09-27: "a better idea of where we are")."""
    ss = st.session_state
    import html as _h
    if ss.get('app_mode') == 'project' and ss.get('project_path'):
        work = work_dir()
        job = ss.get('fqa_job') or {}
        a = (job.get('site_a') or {}).get('alias')
        z = (job.get('site_z') or {}).get('alias')
        top = '📁 ' + _h.escape(os.path.basename(work)) + (
            f' · {_h.escape(a)} → {_h.escape(z)}' if a and z else '')
        if page == 'Project Status':
            return top, '<span id="otdr-crumb-tab">Events</span>'
        sh = _project_run_shoot()
        return top, _h.escape(page) + (f" · shot {_h.escape(shoot_info(sh)[0])}" if sh else '')
    if ss.get('app_mode') == 'traces':
        sp = _qa_span()
        top = '⚡ Quick Analysis' + (
            f" · {_h.escape(str(sp.get('ila_a') or 'A'))} ↔ {_h.escape(str(sp.get('ila_b') or 'B'))}"
            if sp else '')
        return top, _h.escape(page or '')
    if ss.get('app_mode') == 'setup':
        return '📁 New Project', 'Setup'
    return None, None


def _render_crumbs(slot, page):
    top, where = _crumb_lines(page)
    if not top:
        return
    slot.markdown(CRUMB_CSS + f'<div class="otdr-crumbs"><div class="c1">{top}</div>'
                  f'<div class="c2">{where}</div></div>', unsafe_allow_html=True)
    if 'otdr-crumb-tab' in where:
        with st.sidebar:
            st_components_html(CRUMB_TAB_JS, height=0)


def _render_project_sidebar():
    path = st.session_state.get('project_path') or ''
    st.markdown(f"**📁 {os.path.basename(work_dir(path)) or 'Project'}**")


# ─── The OTDR Settings ride a click into the Viewer (Robert 2026-09-29) ──
# "we can't have clicking on a cell take us to defaults".  A report cell or
# a Secret Sauce pair is a link: the click starts a NEW session, and a new
# session used to open on the Default profile.  Each session now has a carry
# id; the end of every run files the session's settings under it here, the
# links carry it (&cs=, see _panel_qs), and the session a link starts takes
# the settings back before anything draws.  Process-wide, like the trace
# server's folders; only a session that arrives with an id it can find is
# seeded, so a fresh window still opens on the Default profile.
_CARRIED_SETTINGS = ('otdr_profile', 'otdr_settings', 'conn_settings',
                     'uni_settings', 'cable_type',
                     # The report pages' own choices ride too: where cell
                     # clicks open and where reports are saved came back as
                     # the defaults after "This tab" -> "← Back" (2026-09-29).
                     'sr_click_target_saved', 'uni_click_target_saved',
                     'sr_report_dest', 'uni_report_dest', 'fec_report_dest')
_CARRY_ID_RE = re.compile(r'[0-9a-f]{12}')
_CARRY_KEPT = 50


@st.cache_resource(show_spinner=False)
def _carry_store():
    import threading
    return {'lock': threading.Lock(), 'by_id': {}}


def _carry_settings_in(qp):
    """First run of a session: take this session's carry id from the link
    that started it and seed the settings filed under it, or make a new id.
    Before any settings box draws, so the boxes open on the carried values."""
    import copy
    import uuid
    ss = st.session_state
    if '_carry_id' in ss:
        return
    cid = qp.get('cs') or ''
    snap = None
    if _CARRY_ID_RE.fullmatch(cid):
        _store = _carry_store()
        with _store['lock']:
            snap = copy.deepcopy(_store['by_id'].get(cid))
    if snap:
        for _k, _v in snap.items():
            if _k in _CARRIED_SETTINGS:
                ss.setdefault(_k, _v)
        ss['_carry_id'] = cid
    else:
        ss['_carry_id'] = uuid.uuid4().hex[:12]


def _carry_settings_out():
    """End of a run: file this session's settings under its carry id, for
    the next link it starts.  Never raises."""
    import copy
    try:
        ss = st.session_state
        cid = ss.get('_carry_id')
        if not cid:
            return
        snap = {k: copy.deepcopy(ss[k]) for k in _CARRIED_SETTINGS if k in ss}
        _store = _carry_store()
        with _store['lock']:
            _by = _store['by_id']
            _by.pop(cid, None)
            _by[cid] = snap
            while len(_by) > _CARRY_KEPT:
                _by.pop(next(iter(_by)))
        # The pop-out Viewer's "← Back" opens a new hub tab when the old one
        # is gone: it carries the id too (see /api/list hub_carry).
        trace_server.CONFIG['hub_carry'] = cid
    except Exception as exc:
        report_error('settings carry-over', exc)


# ─── Deep-link nav: a Splice Report cell click lands as ?nav=viewer&fiber=&km=
#     → switch to the Viewer page + stash the target for the iframe URL. ──────
def _handle_nav():
    qp = st.query_params
    _carry_settings_in(qp)
    if 'cs' in qp and not qp.get('nav'):
        del st.query_params['cs']
    if qp.get('nav'):
        st.session_state['_nav_arrived'] = True     # the mode gate skips Home
    if qp.get('nav') in ('viewer', 'viewerfec') and ('pa' in qp or 'pb' in qp):
        # The left panel's own folders rode the link (see _panel_qs).
        st.session_state['_panel_restore'] = (qp.get('pa') or '', qp.get('pb') or '')
    # Duplicate Check pair click: ?nav=viewer&fibers=410,418&dir=a[&ssfolder=…]
    # → overlay BOTH fibers in the Viewer.  The pair's two .sor files live in
    # the Secret Sauce folder, so point the viewer's A-direction folder there
    # (the wrinkle: the viewer resolves fibers by number from its A/B folders).
    if qp.get('nav') == 'viewer' and qp.get('fibers'):
        ssfolder = qp.get('ssfolder')
        if ssfolder and os.path.isdir(ssfolder):
            st.session_state['view_dir_a_input'] = ssfolder
            # Preserve the Duplicate Check folder so "← Back" restores the pairs
            # list (the URL nav resets session_state; the folder + cached pairs
            # are how page_duplicate_check rebuilds the report on return).
            st.session_state['ss_folder_input'] = ssfolder
            # ...and tell the sidebar the A box now holds that folder, so it
            # does not build a second one from it (see the Trace Folders block).
            st.session_state['_ss_nav_folder'] = ssfolder
        st.session_state['viewer_target'] = {
            'fibers': qp.get('fibers'),
            'dir': qp.get('dir', 'a'),
        }
        st.session_state['viewer_jump_announce'] = True   # one-shot caption
        st.session_state['came_from_dupcheck'] = True
        st.session_state['nav_radio'] = 'Viewer'   # set BEFORE the radio widget
        st.query_params.clear()
        return
    # "← Back" from the pop-out Viewer when the hub tab that opened it is gone:
    # land on the report page with the span's folders seeded, so the page
    # restores its report from the disk cache instead of asking for a re-run.
    _back_pages = {'sr': 'Splice Report', 'uni': 'Unidirectional'}
    if qp.get('nav') in _back_pages:
        _sra, _srb = qp.get('sra'), qp.get('srb')
        if _sra and os.path.isdir(_sra):
            st.session_state['view_dir_a_input'] = _sra
            if qp.get('nav') == 'uni':
                st.session_state['uni_folder_input'] = _sra
        if _srb and os.path.isdir(_srb):
            st.session_state['view_dir_b_input'] = _srb
        # A Unidirectional report run on the panel's B folder comes back on
        # the B folder: the popped Viewer holds both of the panel's folders
        # and says which side the report ran on (`pside`).
        _pside = qp.get('pside')
        if qp.get('nav') == 'uni' and _pside in ('a', 'b'):
            st.session_state['uni_panel_side'] = 'A folder' if _pside == 'a' else 'B folder'
            _run = _srb if _pside == 'b' else _sra
            if _run and os.path.isdir(_run):
                st.session_state['uni_folder_input'] = _run
        st.session_state['nav_radio'] = _back_pages[qp.get('nav')]
        st.query_params.clear()
        return

    # Splice Report FEC row click: ?nav=viewerfec&fiber=&km=&dir=a|b&fa=&fb=
    # (the run's resolved folders) &ra=&rb= (what the FEC page's boxes held).
    # Viewer FEC reads the left panel's folders, so they point at the run's;
    # the tech's own come back on leaving (pa/pb -> _panel_restore).
    if qp.get('nav') == 'viewerfec' and qp.get('fiber'):
        _fa, _fb = qp.get('fa') or '', qp.get('fb') or ''
        st.session_state['view_dir_a_input'] = _fa if os.path.isdir(_fa) else ''
        st.session_state['view_dir_b_input'] = _fb if os.path.isdir(_fb) else ''
        for _k, _v in (('fec_dir_a', qp.get('ra') or _fa), ('fec_dir_b', qp.get('rb') or _fb)):
            st.session_state[_k] = st.session_state[_k + '_saved'] = _v
        st.session_state['viewer_target'] = {
            'fiber': qp.get('fiber'), 'km': qp.get('km'),
            'dir': qp.get('dir') if qp.get('dir') in ('a', 'b') else 'a'}
        st.session_state['viewer_jump_announce'] = True
        st.session_state['came_from_fec'] = True
        st.session_state['nav_radio'] = 'Viewer FEC'   # BEFORE the radio widget
        st.query_params.clear()
        return
    if qp.get('nav') == 'viewer' and qp.get('fiber'):
        # Splice Report / Unidirectional cell click: the link carries the
        # run's own dirs (incl. one-folder/zip staging) — seed the viewer
        # slots so the fresh session resolves the SAME span the grid was
        # built from, instead of whatever stale folders the process-global
        # server config held.
        _sra, _srb = qp.get('sra'), qp.get('srb')
        _src = qp.get('src')
        _dir = qp.get('dir', 'both')
        _same = lambda a, b: bool(a and b) and (
            os.path.normcase(os.path.abspath(a))
            == os.path.normcase(os.path.abspath(b)))
        # A Unidirectional report run on the left panel's A or B folder
        # (`pside`, or the folder itself when an older link has no pside):
        # the Viewer keeps BOTH of the panel's folders and opens the fibre on
        # the side the report ran on.  Pointing the A box at the report's
        # folder left the B box empty while in the Viewer (2026-09-29).
        _pside = qp.get('pside')
        if _pside not in ('a', 'b') and _src == 'uni':
            _pside = ('b' if _same(_sra, qp.get('pb')) and not _same(_sra, qp.get('pa'))
                      else 'a' if _same(_sra, qp.get('pa')) else None)
        if _src == 'uni' and _pside in ('a', 'b') and ('pa' in qp or 'pb' in qp):
            st.session_state['view_dir_a_input'] = qp.get('pa') or ''
            st.session_state['view_dir_b_input'] = qp.get('pb') or ''
            _dir = _pside
        elif _src == 'uni':
            # Run on the page's own folder or upload: the link puts it in the
            # slot of the side its files are (`sra` for A, `srb` for B), and
            # the other slot stays empty.  Every such link said `sra`, so a
            # B run's files went in the A slot, listed as A->B (2026-10-01).
            for _k, _d in (('view_dir_a_input', _sra), ('view_dir_b_input', _srb)):
                st.session_state[_k] = _d if _d and os.path.isdir(_d) else ''
        else:
            if _sra and os.path.isdir(_sra):
                st.session_state['view_dir_a_input'] = _sra
            if _srb and os.path.isdir(_srb):
                st.session_state['view_dir_b_input'] = _srb
        st.session_state['viewer_target'] = {
            'fiber': qp.get('fiber'),
            'km': qp.get('km'),
            'dir': _dir,
            # The report the click came from: the embedded Viewer judges by
            # that report's gate (the Uni report's, not the Splice Report's
            # one-direction gate), as the pop-out window already did.
            'src': _src if _src in ('sr', 'uni') else None,
        }
        # `src` names the report the click came from, so the Viewer can offer
        # the right "← Back" AND the origin page can restore its report from
        # the disk cache after this nav wiped session_state.
        if _src == 'sr':
            st.session_state['came_from_splicereport'] = True
        elif _src == 'uni':
            st.session_state['came_from_uni'] = True
            # The folder the report ran on: `srb` on a link of a B run on the
            # page's own folder.
            _ran = next((_d for _d in (_sra, _srb) if _d and os.path.isdir(_d)), None)
            if _ran:
                st.session_state['uni_folder_input'] = _ran
            # With the left panel loaded the page runs on its A or its B
            # folder: the way back lands on the one the report ran on.
            if _pside == 'b':
                st.session_state['uni_panel_side'] = 'B folder'
        st.session_state['viewer_jump_announce'] = True   # one-shot caption
        st.session_state['nav_radio'] = 'Viewer'   # set BEFORE the radio widget
        st.query_params.clear()

# The software opened (or the page loaded afresh): the Viewer starts empty
# (Robert 2026-10-02).  Its chart is kept on the trace server for a trip to
# another tool (viewer_state), and the server outlives the page, so a new
# session took the last one's traces.  A report link (?nav=) is a page load
# too, and the chart stays for it: going to a report cell is a trip.
if '_viewer_fresh' not in st.session_state:
    st.session_state['_viewer_fresh'] = True
    if not st.query_params.get('nav'):
        trace_server.reset_viewer_state()
_handle_nav()


# ─── Double-clicked .zfc/.zdb/.otdrproject: the launcher leaves the path in
#     <app folder>/open_request.json (never in the URL).  Claim it by rename so
#     only one session opens it; stale requests (>10 min) are dropped.  The app
#     folder is the launcher's: OTDR_SUITE_APP_DIR when it names one. ────────
def _consume_open_request(req=None, now=None):
    req = req or os.path.join(
        os.environ.get('OTDR_SUITE_APP_DIR')
        or os.path.join(os.path.expanduser('~'), '.otdrSuite'),
        'open_request.json')
    if not os.path.exists(req):
        return None
    claimed = req + '.%d.claimed' % os.getpid()
    try:
        os.replace(req, claimed)
    except OSError:
        return None                          # another session got it first
    try:
        with open(claimed, encoding='utf-8') as fh:
            data = json.load(fh)
    except Exception:
        data = {}
    finally:
        try:
            os.remove(claimed)
        except OSError:
            pass
    path = str(data.get('path') or '')
    if not path or (now or time.time()) - float(data.get('ts') or 0) > 600:
        return None
    return path


_open_req = _consume_open_request()
if _open_req:
    # open_share_file and everything it needs are defined further down this
    # script, so the path waits in session state and is opened there (see
    # "Double-click handoff" after open_share_file), then a rerun shows it.
    st.session_state['_share_open_pending'] = _open_req
_share_msg = st.session_state.pop('_share_open_msg', None)
if _share_msg:
    getattr(st, _share_msg[0])(_share_msg[1])
try:
    _project_reattach()
except Exception as _exc:
    report_error('project: reattach after click-through', _exc)

# ─── Home screen / mode gate ──────────────────────────────────────────────
_home_msg = _mode_actions()
if st.session_state.get('app_mode') not in ('traces', 'project', 'setup'):
    if st.session_state.get('project_path') and st.session_state.get('_project_reattached'):
        st.session_state['app_mode'] = 'project'
    elif st.session_state.get('_nav_arrived'):
        st.session_state['app_mode'] = 'traces'
if st.session_state.get('app_mode') not in ('traces', 'project', 'setup'):
    _render_home(_home_msg)
    # OTDR Suite App: a file dropped on the home screen is refused, not saved
    # to Downloads (Robert, 2026-10-01).  There is no Viewer frame here, so
    # the hub's drop catcher only refuses it; Quick Analysis and a project
    # install it again below (it runs once per page).
    _install_hub_drop_catch()
    try:
        maybe_report_update()
    except Exception:
        pass
    st.stop()
_PROJECT_MODE = (st.session_state.get('app_mode') == 'project'
                 and bool(st.session_state.get('project_path')))
if _PROJECT_MODE:
    _project_seed_tools()
_install_sidebar_drag_fix()
_install_hub_drop_catch()

# No "Deploy" button in the header (Robert, 2026-09-29): it is Streamlit's
# developer menu and means nothing to a tech.  New builds turn the whole
# developer toolbar off (client.toolbarMode = viewer, see desktop/launcher.py
# and .streamlit/config.toml); this hides the button on builds already out
# in the field, which pick up app.py on update but keep their old launcher.
# Nor the ⋮ menu (Robert, 2026-10-01): its Settings has a theme picker of its
# own that overrides the hub's Theme switch (see THEME_PICK_CLEAR_JS).
st.markdown('<style>[data-testid="stAppDeployButton"],[data-testid="stMainMenu"],'
            '#MainMenu{display:none}</style>',
            unsafe_allow_html=True)


# ─── Clear Traces / Clear Report (Robert 2026-09-28) ─────────────────────
# Two ways back to a clean page, both behind a pop-up that says what will go:
#   Clear Traces  (sidebar)            every tool's traces and reports.
#   Clear Report  (on a report page)   that page's report, or everything.
# A cleared report is cleared for good: its saved copy under ~/.otdrSuite/cache
# goes with it, so the same folder needs a fresh run.  Without that the page
# would bring the report straight back from the copy.  The traces themselves
# and the report files the tech saved to a folder are never touched.
def _panel_shown():
    """OTDR Suite App: the left panel's Trace Folders are shown in Quick
    Analysis, as in the regular Suite.  Never in a project or on the
    setup screens: there the traces come from the project (a shoot's Run
    In...)."""
    ss = st.session_state
    return ss.get('app_mode') == 'traces'


def _panel_boxes():
    """The folders the left panel's two Trace Folders boxes stand for: what
    was typed, or, for a box showing a Viewer drop's name, the folder the drop
    was staged in (see _label_drop_boxes)."""
    out = []
    for side in ('a', 'b'):
        v = (st.session_state.get(f'view_dir_{side}_input') or '').strip().strip('"')
        shown = st.session_state.get(f'_drop_box_{side}')
        if shown and v == shown[0]:
            v = shown[1]
        out.append(v)
    return tuple(out)


def _drop_box_label(path):
    """What a Trace Folders box shows for a folder a drop on the Viewer
    staged: the folder or file the tech dropped, never the staging folder
    (/var/folders/.../T/otdr_viewer_drop_jnwvux7o/A, demo list #31).  The
    same for the folder an upload on the Unidirectional page was staged in
    (_upload_label): a cell click puts it in a box.  None for any other
    folder."""
    try:
        name = trace_server.drop_name(path)
    except Exception:
        name = None
    if not name:
        return _upload_label(path)
    return name if name.startswith('Dropped files') else f'{name} (dropped)'


# The Unidirectional page's upload is staged in a temporary folder, and a
# cell click and the way back carry that folder: the page's folder box and
# the left panel's boxes showed its path (/var/folders/.../T/
# otdr_panel_split_.../B, 2026-10-01).  What was uploaded is kept here by
# the folder it was staged in, process-wide (a click starts a new session),
# for the boxes to show instead (_upload_label).
@st.cache_resource(show_spinner=False)
def _upload_label_store():
    return {'labels': {}, 'sides': {}, 'typed': {}}


def _staged_key(path):
    return os.path.normcase(os.path.normpath(os.path.abspath(str(path))))


def _upload_label(path):
    """What a box shows for the folder an upload was staged in, for example
    'Uploaded Files: B Direction (12 files)', or None for any other folder."""
    if not path:
        return None
    try:
        return _upload_label_store()['labels'].get(_staged_key(path))
    except Exception:
        return None


def _remember_upload(folder, name, side, what=None):
    """Name the staged upload `folder` for the boxes (_upload_label): the
    .zip it came from (`name`) or 'Uploaded Files', the direction its files
    are (`side`, 'a' | 'b' | None) and how many there are.  `what` names it
    instead, for one side of a typed folder holding both directions."""
    key = _staged_key(folder)
    labels = _upload_label_store()['labels']
    if key in labels:
        return labels[key]
    try:
        n = len([f for f in os.listdir(folder)
                 if f.lower().endswith(trace_server.DROP_EXTS) and not f.startswith('.')])
    except OSError:
        return None
    what = what or (f'{name} (Uploaded)' if name else 'Uploaded Files')
    label = (f"{what}: {side.upper()} Direction ({_count(n, 'file')})" if side
             else f"{what} ({_count(n, 'file')})")
    _remember(labels, key, label)
    return label


def _remember_typed_split(side_folder, typed, picked_from, side):
    """A folder typed into the Unidirectional page's box that holds both
    directions runs on one side's split folder, and a cell click and the way
    back carry that folder.  Kept here, process-wide: the box shows the
    typed folder again and Run On the side (_typed_split_origin), and the
    left panel's box names it (_upload_label) instead of its temporary path."""
    _remember(_upload_label_store()['typed'], _staged_key(side_folder),
              (typed, picked_from, side))
    name = os.path.basename(str(typed).rstrip('/\\')) or str(typed)
    _remember_upload(side_folder, '', side, what=name)


def _typed_split_origin(path):
    """(typed, picked_from, side) for one side's split folder of a typed
    folder holding both directions, or None (see _remember_typed_split)."""
    if not path:
        return None
    try:
        return _upload_label_store()['typed'].get(_staged_key(path))
    except Exception:
        return None


def _stamped_side(folder):
    """'a' | 'b': the direction a one-direction folder's own files stamp
    (LocationsDirection), read the way a drop on the Viewer reads one
    direction (trace_server._declared_direction: a unanimous sample of the
    .sor files), so it lands in the Viewer slot of that side.  None when the
    files do not say.  Kept per folder and its signature: the report page
    asks on every rerun."""
    sig = trace_server._folder_sig(folder)
    if sig is None:
        return None
    key = _staged_key(folder)
    sides = _upload_label_store()['sides']
    hit = sides.get(key)
    if hit and hit[0] == sig:
        return hit[1]
    try:
        files = sorted(os.path.join(folder, f) for f in os.listdir(folder)
                       if f.lower().endswith('.sor') and not f.startswith('.'))
        side = trace_server._declared_direction(files)
    except Exception:
        side = None
    _remember(sides, key, (sig, side))
    return side


def _uni_box_folder():
    """The folder the Unidirectional page's folder box stands for: what was
    typed, or, for a box showing an upload's name, the folder the upload was
    staged in (see page_unidirectional)."""
    v = (st.session_state.get('uni_folder_input') or '').strip().strip('"')
    shown = st.session_state.get('_uni_box_upload')
    if shown and v == shown[0]:
        return shown[1]
    return v


def _label_drop_boxes():
    """Put the drop's name in each box that holds a staged drop folder, and
    remember which folder that name stands for (_panel_boxes reads it back).
    Run before the boxes are drawn, on every run: a drop, a link back from the
    Viewer and a new session seeded from the trace server all bring the
    staged path in."""
    for side in ('a', 'b'):
        key = f'view_dir_{side}_input'
        v = (st.session_state.get(key) or '').strip().strip('"')
        label = _drop_box_label(v) if v else None
        if label:
            st.session_state[f'_drop_box_{side}'] = (label, v)
            st.session_state[key] = label


def _panel_traces():
    """The traces loaded in the left panel, as (dir_a, dir_b): each the folder
    its box resolves to when that folder exists, else ''.  With either one
    loaded a report page draws no loader of its own and runs on these (Robert
    2026-09-28): one place to load traces, not one per tool.

    Resolved the way the Viewer resolves them (_panel_dirs): a .zip is read
    from its extracted copy, and a folder holding both directions is split.
    The report pages used to take the box text as a folder, so a .zip in each
    box loaded 24 + 24 fibers in the Viewer while the Splice Report asked for
    both folders and the Unidirectional page ignored them (2026-09-29)."""
    if not _panel_shown():
        # OTDR Suite App: no left panel here (a project), so nothing is loaded in it.
        return ('', '')
    dir_a, dir_b, _notes = _panel_dirs()
    return tuple(_d if _d and os.path.isdir(_d) else '' for _d in (dir_a, dir_b))


def _show_panel_notes():
    """What _panel_dirs had to say about the left panel's boxes (a .zip read,
    a folder split into its two directions, a box it could not use), on a
    report page that runs on them.  None in a project: it has no left panel."""
    if not _panel_shown():
        return
    for _kind, _text in _panel_dirs()[2]:
        (st.warning if _kind == 'warning' else st.caption)(_text)


def _panel_qs():
    """The left panel's own folders, for a link into the Viewer tab.  Such a
    click starts a new session and points the A box at the folder the Viewer
    must read (Secret Sauce's one folder, the Unidirectional folder); these
    two bring the tech's A and B back when the tech leaves the Viewer.  The
    carry id brings the OTDR Settings along (_carry_settings_in)."""
    from urllib.parse import quote
    _a, _b = _panel_boxes()
    return (f"&pa={quote(_a, safe='')}&pb={quote(_b, safe='')}"
            f"&cs={st.session_state.get('_carry_id', '')}")


def _files_sig(paths):
    """What a staged copy was built from: every file's path, size, mtime and
    inode.  A count plus the newest mtime missed a file swapped for an older
    one (an Explorer zip extraction keeps the archive's timestamps)."""
    out = []
    for f in paths:
        st_ = os.stat(f)
        out.append((f, st_.st_size, st_.st_mtime_ns, st_.st_ino))
    return tuple(out)


def _panel_ss_folder(dir_a, dir_b):
    """The ONE folder Secret Sauce reads for the left panel's A and B folders:
    every trace of both, flat.  Returns (folder, renamed): `renamed` is
    [(name, new_name)] for the B files that went in under a name of their
    own because an A file has their name (folder_intake.combined_names).

    The folder is named after exactly what it holds: the two folders and
    every trace's path, size, time and inode (_files_sig).  The same traces
    always give the SAME folder, because a report is saved under the folder
    it ran on.  A new session is started by every click into the Viewer tab;
    a folder built fresh each time would cost the report on the way back, and
    a copy of the whole span where the traces cannot be hard-linked.  Any
    change gives a NEW folder, built whole, and the report saved for the old
    traces stays with the old one.  The name used to come from the file count
    and the newest time, and a name already in the folder was never placed
    again: a trace swapped for another of the same size with an older time
    (as an Explorer zip extraction leaves it) kept Secret Sauce on the old
    trace, in every session."""
    import hashlib
    import folder_intake as fi
    files_a, files_b = fi.find_otdr_files(dir_a), fi.find_otdr_files(dir_b)
    placed, renamed = fi.combined_names(files_a, files_b)
    sig = repr((os.path.normcase(os.path.abspath(dir_a)),
                os.path.normcase(os.path.abspath(dir_b)),
                _files_sig(files_a), _files_sig(files_b),
                [_n for _f, _n in placed]))
    dest = os.path.join(
        tempfile.gettempdir(),
        'otdr_span_all_' + hashlib.sha1(sig.encode('utf-8')).hexdigest()[:16])
    return fi.materialize_combined(placed, dest), renamed


def _run_folder(folder):
    """The folder a report runs on: `folder`, or a copy of it without the
    files the tech removed in the Viewer (Robert 2026-10-01: a Viewer Remove
    takes them out of the Splice Report, Unidirectional and Duplicate Check
    too).  Never raises: a copy that cannot be made runs the folder as is."""
    if not folder:
        return folder
    try:
        return trace_server.without_removed(folder)
    except Exception as exc:
        report_error('report folder without removed files', exc, {'folder': folder})
        return folder


def _viewer_removed_note(*folders):
    """Say on a report page how many files removed in the Viewer it leaves out."""
    try:
        n = sum(len(trace_server.removed_names(f)) for f in folders if f)
    except Exception:
        return
    if n:
        st.caption(f"{n} file{'s' if n != 1 else ''} removed in the Viewer "
                   f"{'are' if n != 1 else 'is'} left out of this report. "
                   'Put them back from the Viewer’s Files list (right-click).')


def _take_panel_ss_folder(dir_a, dir_b):
    """Build (or find) the left panel's Secret Sauce folder and put it where
    the page, Clear Report and Clear Traces look for it.  Returns what
    _panel_ss_folder returns."""
    folder, renamed = _panel_ss_folder(dir_a, dir_b)
    ss = st.session_state
    ss['_ss_from_ab'] = (dir_a, dir_b)
    ss['ss_folder_input'] = folder
    # ...and in a slot no widget owns, for the page to read when it draws no
    # folder box (the left panel is loaded).
    ss['_ss_panel_folder'] = folder
    return folder, renamed


def _renamed_note(renamed, limit=3):
    """One line for the page: which B files went in under a new name."""
    n = len(renamed)
    shown = [f'{_old} as {_new}' for _old, _new in renamed[:limit]]
    more = f' and {n - limit} more' if n > limit else ''
    if n == 1:
        return (f'1 B-direction file has the same name as an A-direction '
                f'file. It goes in as {renamed[0][1]}, so both directions '
                f'are checked.')
    return (f'{n} B-direction files have the same name as an A-direction '
            f'file. Each goes in under a new name, so both directions are '
            f'checked: {", ".join(shown)}{more}.')


_SAVED_REPORTS = {'sr': ('.sr_grid_cache.json',),
                  'uni': ('uni_result_cache.json',),
                  'ss': SS_CACHE_NAMES}


def _report_folders(which):
    """Every folder a saved copy of the `which` report can sit under: the
    folder the report on screen ran on (its own record of it, which is the
    cleaned copy when files from another job were set aside), the folder in
    the page's box, and the left panel's folders the page runs on when it
    draws no box."""
    ss = st.session_state
    if which == 'sr':
        cands = [(ss.get('sr_dirs') or (None,))[0], _panel_boxes()[0]]
    elif which == 'uni':
        cands = [(ss.get('uni_result') or {}).get('_folder'),
                 _uni_box_folder(), *_panel_boxes()]
    else:
        cands = [(ss.get('ss_result') or {}).get('_folder'),
                 (ss.get('ss_pairs_result') or {}).get('_folder'),
                 ss.get('ss_folder_input'), ss.get('_ss_panel_folder'),
                 ss.get('_ss_nav_folder'), *_panel_boxes()]
    out = []
    for _c in cands:
        _c = (_c or '').strip().strip('"') if isinstance(_c, str) else ''
        if not _c:
            continue
        if _c not in out:
            out.append(_c)
    return out


# The table a Splice Report run writes for the Viewer's OTDR Suite mode (its
# columns and its numbers for every fibre).  One per pair of folders, a few
# megabytes on a big cable, so only the newest few are kept.
VIEWER_TABLE_NAME = 'sr_viewer_table.json'
VIEWER_TABLES_KEPT = 8


def _viewer_table_path(dir_a, dir_b):
    return _hub_cache_path(VIEWER_TABLE_NAME, dir_a, dir_b)


def _is_viewer_table(path):
    return (isinstance(path, str)
            and os.path.basename(path).endswith('_' + VIEWER_TABLE_NAME))


def _prune_viewer_tables(keep=VIEWER_TABLES_KEPT):
    """Delete all but the `keep` newest Viewer tables.  Never raises."""
    try:
        d = os.path.dirname(_viewer_table_path('a', 'b'))
        mine = [os.path.join(d, n) for n in os.listdir(d) if _is_viewer_table(n)]
        mine.sort(key=os.path.getmtime, reverse=True)
        for p in mine[max(0, int(keep)):]:
            os.remove(p)
    except OSError:
        pass


def _forget_saved_reports(which):
    """Delete the saved copies of the `which` report.  Never raises: a copy
    that cannot be deleted costs a stale report, not the page."""
    for _f in _report_folders(which):
        for _n in _SAVED_REPORTS[which]:
            try:
                os.remove(_hub_cache_path(_n, _f))
            except OSError:
                pass


def _drop_report(which):
    """Take the `which` report off the screen and forget its saved copy."""
    _forget_saved_reports(which)
    ss = st.session_state
    if which == 'sr':
        # The table each report wrote for the Viewer goes with it.
        for _k in [str(k) for k in ss.keys()]:
            _vt = (ss.get(_k) or {}).get('viewer_table') if _k.startswith('sr_result') else None
            if _is_viewer_table(_vt):
                try:
                    os.remove(_vt)
                except OSError:
                    pass
        # Span 1 and the added spans (sr_result2, sr_dirs2, sr2_techcmp ...).
        # Found by name: the span ceiling is defined further down.
        for _k in [str(k) for k in ss.keys()]:
            if _k.startswith(('sr_result', 'sr_dirs')) or (
                    _k.startswith('sr') and _k.endswith('_techcmp')):
                ss.pop(_k, None)
        ss.pop('came_from_splicereport', None)
    elif which == 'uni':
        ss.pop('uni_result', None)
        ss.pop('came_from_uni', None)
    else:
        ss.pop('ss_result', None)
        ss.pop('ss_pairs_result', None)
        ss.pop('came_from_dupcheck', None)
    if which in ('sr', 'uni'):
        # The Viewer judges by the gates of the report that opened it; with
        # the report gone it goes back to its own.
        ss.pop('viewer_target', None)
        trace_server.set_thresholds(None)
        trace_server.set_end_refl(None)
        trace_server.set_panel_span(None)
        trace_server.set_suite_table(None)


def _clear_traces():
    """Back to an empty Suite: every report, every tool's folder box and the
    Viewer's folders.  The pages' own drop zones keep their files: Streamlit
    does not let code empty an uploader, each has its own ✕.  Writes the
    sidebar's folder boxes, so it runs at the top of the sidebar, before they
    are drawn."""
    for _which in ('sr', 'uni', 'ss'):
        _drop_report(_which)
    # The Splice Report's site names were read out of the cleared traces.
    st.session_state.pop('sr_site_src', None)
    st.session_state.pop('sr_site_saved', None)
    st.session_state['sr_site_a'], st.session_state['sr_site_b'] = 'A', 'B'
    st.session_state.pop('_ss_from_ab', None)
    st.session_state.pop('_ss_nav_folder', None)
    st.session_state.pop('_panel_restore', None)
    st.session_state.pop('_ss_panel_folder', None)
    st.session_state['sr_input_mode'] = 'Two folders (A + B)'
    st.session_state['sr_n_spans'] = 1
    for _k in ('view_dir_a_input', 'view_dir_b_input', 'ss_folder_input',
               'uni_folder_input', 'sr_one_folder'):
        st.session_state[_k] = ''
    st.session_state.pop('_drop_box_a', None)
    st.session_state.pop('_drop_box_b', None)
    # Unidirectional's own upload is kept in a slot, not in its uploader.
    st.session_state.pop('uni_upload', None)
    st.session_state.pop('uni_both_side', None)
    st.session_state.pop('_uni_box_upload', None)
    # What the pages' own boxes kept goes too (_seed_box): Streamlit drops
    # the writes above on a page that is not drawn, and the old folders
    # would come back from the kept copy.  Unidirectional's landmarks and
    # direction go as well, they were for the cleared traces, and so does
    # everything the added spans kept: the page is back to span 1.
    for _k in ('sr_input_mode', 'sr_one_folder', 'uni_folder_input',
               'uni_landmarks_text', 'uni_dir_pick'):
        st.session_state.pop(_k + '_saved', None)
    for _k in [k for k in st.session_state if re.fullmatch(r'sr\d+_\w+_saved', k)]:
        st.session_state.pop(_k, None)
    # The trace server's folders are process-wide: left set, the next
    # session would seed the boxes from them and the span would be back.
    trace_server.set_dirs(None, None)
    # OTDR Suite App: what a SharePoint folder loaded goes too.
    st.session_state.pop('span_loaded', None)
    # ...and so does the Viewer's chart: kept on the server for a trip to
    # another tool (viewer_state), it came back when the same folders were
    # put back after a Clear Traces.
    trace_server.reset_viewer_state()


# The pop-ups' buttons act in click callbacks, which run whether or not the
# pop-up is drawn again on the run the click starts.
def _allow_clear_traces():
    st.session_state['_clear_traces_go'] = True


# Cancel, the ✕ and a click outside the box all leave everything as it was.
@st.dialog('Clear Traces')
def _confirm_clear_traces():
    st.write('This will clear the traces and the reports from every tool. '
             'The same folders will need a fresh run.')
    st.caption('Report files already saved to a folder are not deleted.')
    _c1, _c2 = st.columns(2)
    if _c1.button('Cancel', key='clear_traces_cancel', use_container_width=True):
        st.rerun()
    # Named for what it does (Robert 2026-10-02); it read 'Allow'.
    if _c2.button('Clear Traces', key='clear_traces_allow', type='primary',
                  use_container_width=True, on_click=_allow_clear_traces):
        st.rerun()


@st.dialog('Clear Report')
def _confirm_clear_report(which):
    st.markdown('**Clear Report Only** removes this report. The traces stay '
                'loaded and the same folder will need a fresh run.')
    if not _PROJECT_MODE:
        st.markdown('**Clear Report and Traces** also clears the traces from the '
                    'left panel and from every tool, with their reports.')
    st.caption('Report files already saved to a folder are not deleted.')
    if st.button('Skip', key='clear_report_skip', use_container_width=True):
        st.rerun()
    if st.button('Clear Report Only', key='clear_report_only',
                 use_container_width=True, on_click=_drop_report, args=(which,)):
        st.rerun()
    # In a project the traces belong to the project (a shoot's Run In…).
    if not _PROJECT_MODE and st.button('Clear Report and Traces',
                                       key='clear_report_and_traces',
                                       use_container_width=True,
                                       on_click=_allow_clear_traces):
        st.rerun()


def _clear_report_button(which):
    """The Clear Report button a report page draws above its report."""
    if st.button('Clear Report', key=f'{which}_clear_report'):
        _confirm_clear_report(which)


# ─── Thresholds Carried Over pop-up (Robert 2026-09-29) ──────────────────
# "when we change tools I want a pop up that says Thresholds Carried Over
# from Previous Tool and they have to click Edit Settings or OK".  Shown on
# landing on a tool that draws the OTDR Settings, from any other tool, by
# the Select Tool list or a "← Back" button.  Not on a report-cell or pair
# click into the Viewer (Robert: no pop-up on cell jumps): that click starts
# a new session, which has no previous tool.  No ✕, Esc or click-outside:
# the tech answers it.  OK is dark and takes Return / Enter; Edit Settings
# opens the Settings box and scrolls the page to it.  A report running on
# the page the tech lands on holds it until the run ends (_page_run_going).
SETTINGS_TOOLS = ('Viewer', 'Splice Report', 'Unidirectional')
SETTINGS_BOX_KEY = 'otdr_settings_box'
CARRY_OK_KEY = 'carry_ok'

# Return / Enter presses OK, wherever the focus sits in the pop-up, unless
# the tech has tabbed to another button (Edit Settings), which then takes it
# as any button does.  Each pop-up puts in its own listener (see the script);
# it does nothing while OK is not on screen.  OK also takes the focus when
# the pop-up opens.
_CARRY_ENTER_JS = """
<script>
(function(){
  var w; try { w = window.parent; void w.document; } catch (e) { return; }
  if (!w) return;
  var d = w.document;
  function okBtn(){ return d.querySelector('.st-key-__OK__ button'); }
  w.__otdrCarryTabbed = false;
  // The listener belongs to THIS frame, which goes when the pop-up closes,
  // and a browser runs no listener of a frame that is gone.  So each pop-up
  // puts in its own and takes out the one before (a one-time install worked
  // for the first pop-up of a page only).
  if (w.__otdrCarryKeys) {
    try { d.removeEventListener('keydown', w.__otdrCarryKeys, true); } catch (e) {}
  }
  w.__otdrCarryKeys = function(ev){
    var ok = okBtn();
    if (!ok) return;
    if (ev.key === 'Tab') { w.__otdrCarryTabbed = true; return; }
    if (ev.key !== 'Enter' || ev.isComposing) return;
    var a = d.activeElement;
    if (w.__otdrCarryTabbed && a && a !== ok && a.tagName === 'BUTTON') return;
    ev.preventDefault();
    ev.stopPropagation();
    if (!ev.repeat) ok.click();
  };
  d.addEventListener('keydown', w.__otdrCarryKeys, true);
  var tries = 0;
  (function focusOk(){
    var ok = okBtn();
    if (ok) { try { ok.focus({preventScroll: true}); } catch (e) {} return; }
    if (++tries < 40) setTimeout(focusOk, 50);
  })();
})();
</script>
""".replace('__OK__', CARRY_OK_KEY)

# Edit Settings: once the pop-up has gone (it locks the page's scroll) and
# the box has drawn, open the box if it is shut (a click on its title, so
# Streamlit knows it is open), then scroll it to the top of the page, below
# Streamlit's header.  The nonce makes each press a new script.
_OPEN_SETTINGS_JS = """
<script>
(function(){
  var w; try { w = window.parent; void w.document; } catch (e) { return; }
  if (!w) return;
  var d = w.document, tries = 0;
  (function go(){
    var box = d.querySelector('.st-key-__BOX__');
    if (!box || d.querySelector('.st-key-__OK__')) {
      if (++tries < 100) setTimeout(go, 50);
      return;
    }
    var det = box.querySelector('details'), wait = 0;
    if (det && !det.open) {
      var sum = det.querySelector('summary');
      if (sum) { sum.click(); wait = 650; }
    }
    // After the box has opened (Streamlit grows it over 0.5 s): scrolled
    // first, the page may still be too short to bring the box up.  A hidden
    // window runs no smooth scroll.
    setTimeout(function(){
      box.style.scrollMarginTop = '4.5rem';
      box.scrollIntoView({block: 'start',
        behavior: d.visibilityState === 'visible' ? 'smooth' : 'auto'});
    }, wait);
  })();
})();
</script>
<!-- __NONCE__ -->
""".replace('__BOX__', SETTINGS_BOX_KEY).replace('__OK__', CARRY_OK_KEY)

_CARRY_OK_CSS = (
    '<style>'
    f'.st-key-{CARRY_OK_KEY} button{{background-color:var(--otdr-accent-2);'
    'border-color:var(--otdr-accent-2);color:var(--otdr-on-accent);}'
    f'.st-key-{CARRY_OK_KEY} button:hover,'
    f'.st-key-{CARRY_OK_KEY} button:focus:not(:active){{'
    'background-color:var(--otdr-accent-3);border-color:var(--otdr-accent-3);color:var(--otdr-on-accent);}'
    '</style>')


# The two built-in profiles' names in Title Case, for the screen only: the
# stored names are the keys of CUSTOMER_PROFILES (saved projects carry them).
_PROFILE_SHOWN = {'Default (engine baseline)': 'Default (Engine Baseline)',
                  'Custom (edit table below)': 'Custom (Edit Table Below)'}


def _profile_label(name):
    return _PROFILE_SHOWN.get(name, name)


_CARRY_PROFILE_BOX = (
    '<div style="background:var(--otdr-ok-bg-2);border:1px solid var(--otdr-ok-edge);'
    'border-radius:6px;padding:8px 12px;font-size:1rem;color:var(--otdr-ok-text)">'
    'Customer Profile: <span style="font-weight:600;font-size:1.15rem">'
    '{name}</span></div>')


def _carry_ok():
    st.session_state.pop('_carry_popup', None)


def _carry_edit_settings():
    st.session_state.pop('_carry_popup', None)
    st.session_state['_carry_open_settings'] = time.time_ns()


@st.dialog('Thresholds Carried Over from Previous Tool', width='medium',
           dismissible=False)
def _thresholds_carried_dialog(info):
    _from = info.get('from')
    if _from and _from != info.get('to'):
        st.markdown(f"**{info.get('to')}** is using the same thresholds and "
                    f"connector settings as **{_from}**.")
    else:
        st.markdown(f"**{info.get('to')}** is using the thresholds and "
                    'connector settings already set.')
    # The whole line in a green box, to catch the eye (Robert 2026-09-29,
    # option C of the mock-ups).  Escaped: a profile name can hold an '&'.
    import html
    _prof = st.session_state.get('otdr_profile') or next(iter(CUSTOMER_PROFILES))
    st.markdown(_CARRY_PROFILE_BOX.format(name=html.escape(_profile_label(_prof))),
                unsafe_allow_html=True)
    st.markdown(_CARRY_OK_CSS, unsafe_allow_html=True)
    _c1, _c2 = st.columns(2)
    if _c1.button('Edit Settings', key='carry_edit', use_container_width=True,
                  on_click=_carry_edit_settings):
        st.rerun()
    if _c2.button('OK', key=CARRY_OK_KEY, type='primary',
                  use_container_width=True, on_click=_carry_ok):
        st.rerun()
    st_components_html(_CARRY_ENTER_JS, height=0)


def _note_tool_change(page):
    """Before the page draws: a change of tool onto a Settings tool asks
    for the pop-up, any other change of tool drops a pop-up still waiting."""
    ss = st.session_state
    _prev = ss.get('_last_tool')
    ss['_last_tool'] = page
    if _prev is None or _prev == page:
        return
    # Only when the settings actually come over from ANOTHER Settings tool
    # (Robert 2026-10-01: "thresholds carried over on every tool switch if we
    # are actually carrying them over").  Back on the tool they were last set
    # on (Viewer -> Secret Sauce -> Viewer), or with no Settings tool before
    # it, nothing is carried and the pop-up stays away.
    _from = ss.get('_last_settings_tool')
    if page in SETTINGS_TOOLS and _from and _from != page:
        ss['_carry_popup'] = {'to': page, 'from': _from}
    else:
        ss.pop('_carry_popup', None)


# The report each Settings tool runs in this session (run_engine_live's
# prefix).  The Viewer has none: its own background run lives in the trace
# server.
_PAGE_RUN_PREFIX = {'Splice Report': 'sr', 'Unidirectional': 'uni'}


def _page_run_going(page):
    """True while the page's report is running or queued for the next pass.
    The pop-up waits for it (Robert 2026-09-29, "do A"): during a run the
    tech cannot change the settings and the run's were fixed at Generate, so
    the counter and Cancel stay clear and the pop-up opens on the first pass
    after the run finishes or is cancelled.  The queued command counts
    because the next span of a Splice Report queue is started that way (the
    page reruns straight into it), so a queue of spans holds the pop-up until
    the last one is done.  A cancel drops the rest of the queue.

    A job counts only while its engine is still running: a page that returns
    before its run block (Clear Traces during a run empties the folders)
    leaves the job in session_state with nothing collecting it, and the
    pop-up must not wait on that for good."""
    _p = _PAGE_RUN_PREFIX.get(page)
    if not _p:
        return False
    if f'{_p}_pending_cmd' in st.session_state:
        return True
    job = st.session_state.get(f'{_p}_job')
    try:
        return job is not None and job['proc'].poll() is None
    except Exception:
        return False


def _after_page(page):
    """After the page draws: the pop-up while it waits for an answer, the
    Edit Settings scroll, and the settings filed for the next link.  Never
    raises: none of it may take the page down."""
    try:
        if page in SETTINGS_TOOLS:
            st.session_state['_last_settings_tool'] = page
        if st.session_state.get('_carry_popup') and not _page_run_going(page):
            # Streamlit opens one pop-up per run.  When the page opened its
            # own (Clear Report), this one waits for the next run.
            from streamlit.runtime.scriptrunner import get_script_run_ctx
            _ctx = get_script_run_ctx()
            if not (_ctx and _ctx.has_dialog_opened):
                _thresholds_carried_dialog(st.session_state['_carry_popup'])
        _nonce = st.session_state.pop('_carry_open_settings', None)
        if _nonce and page in SETTINGS_TOOLS:
            st_components_html(_OPEN_SETTINGS_JS.replace('__NONCE__', str(_nonce)),
                               height=0)
    except Exception as exc:
        report_error('thresholds carried over pop-up', exc)
    _carry_settings_out()


# ─── Sidebar nav ─────────────────────────────────────────────────────────
# In a project the page is plain state, and a key a widget owned on the run
# before (the setup screen draws the tool list) is dropped once no widget
# draws it -- that sent the Sample Span to the Viewer on its first click
# (2026-09-27).  The project's own copy puts it back.
_PANEL_BOXES_DRAWN = False       # set once the left panel's A/B boxes are drawn
_sp_section = None               # the left panel's From SharePoint section
st.session_state.setdefault('nav_radio', (st.session_state.get('_project_page') or 'Project Status')
                            if _PROJECT_MODE else
'Viewer')
with st.sidebar:
    # Home at the very top of the sidebar, in a project and in Run Traces.
    st.button('🏠 Home', key='go_home', use_container_width=True)
    st.markdown(f'## 🔬 {PRODUCT_NAME}')
    # Where we are, filled in once the page is known (see _render_crumbs).
    _crumb_slot = st.empty()

    # Update nudge FIRST — above the tools, so a stale always-on machine sees
    # it before it starts working (the footer's manual check is still there).
    _render_update_nudge()


    # ── OTDR Suite App: where the traces are chosen ─────────────────────────
    # A project chooses them on its own screen (a shoot's Run In…).  Quick
    # Analysis is the regular Suite screen (Robert, 2026-09-30: no stop that
    # asks for traces first): the left panel's Trace Folders -- the A and B
    # folder boxes, Clear Traces, and one SharePoint folder under the boxes --
    # and the tool list.  Not in a project.
    _PANEL_DRAWN = _panel_shown()
    _ask_clear_traces = False
    if _PANEL_DRAWN:
        # Values code wrote on an earlier run reach the browser only when
        # re-assigned in the run that draws the box.
        for _k in ('view_dir_a_input', 'view_dir_b_input'):
            if _k in st.session_state:
                st.session_state[_k] = st.session_state[_k]
        # ── Trace folders: the A and B directions, for every tool ───────────────
        # Robert 2026-09-26: the "Load Span (Both Directions)" box and its "Load
        # into all tools" button are gone; an A-direction and a B-direction folder
        # loader sits in their place.  These two boxes ARE the shared A/B slots
        # the Viewer and the Splice Report read (view_dir_a_input /
        # view_dir_b_input), so there is nothing to push: picking a folder is
        # loading it.  Drawn on every page, so the choice also survives a trip
        # between tools (a widget Streamlit does not draw loses its state).
        # Keyed widgets, no value= (key + value on a written widget is the
        # Streamlit footgun); a Browse writes the slot BEFORE its box is drawn.
        st.markdown('##### Trace Folders')
        st.session_state.setdefault('view_dir_a_input', trace_server.CONFIG.get('dir_a') or '')
        st.session_state.setdefault('view_dir_b_input', trace_server.CONFIG.get('dir_b') or '')
        # OTDR Suite App: a From SharePoint load under the boxes (_load_span)
        # lands here on the next run, before the boxes are drawn.  (Main
        # dropped this hand-off when Viewer drops moved to the dropped_at
        # check below; SharePoint still needs it.)
        _pend = st.session_state.pop('_view_drop_pending', None)
        if _pend:
            st.session_state['view_dir_a_input'], st.session_state['view_dir_b_input'] = _pend

        def _trace_folders_changed():
            # A new span invalidates the previous deep-link target and report
            # grids, exactly as the old span loader did: a stale click would
            # re-fire against the new folders.
            for _k in ('viewer_target', 'sr_result', 'sr_dirs', 'uni_result',
                       'sr_site_src'):
                st.session_state.pop(_k, None)
            st.session_state['sr_input_mode'] = 'Two folders (A + B)'
            st.session_state.pop('sr_input_mode_saved', None)     # _seed_box

        # The tech pressed Clear Traces, or Clear Report and Traces, in a pop-up (see
        # _clear_traces above the sidebar).  Done HERE, on the run that follows,
        # because the boxes must be emptied in the same run that draws them and
        # before they are drawn: a value written in an earlier run reaches the
        # server and never the browser.
        if st.session_state.pop('_clear_traces_go', False):
            _clear_traces()
        # Files dropped on the Viewer's FILES panel point the trace server at a
        # staged folder from inside the page (trace_server.drop_end stamps
        # CONFIG['dropped_at']).  Checked HERE, on every page and before the boxes
        # are drawn, so the next run of ANY tool picks up the drop: a tech who
        # drops files and then clicks Splice Report ran the report on the old
        # span while the Viewer showed the new one (click-through audit
        # 2026-09-29), because only the Viewer page looked.  A hub rerun must
        # not put the old paths back, so the drop's folders become the boxes'.
        # A new span, as a Browse is: the old report grids go, and so does a
        # pending "back from the Viewer" restore, which would put the old span
        # back on the way out.
        _drop_at = trace_server.CONFIG.get('dropped_at') or 0
        if _drop_at > st.session_state.get('view_drop_seen', 0):
            st.session_state['view_drop_seen'] = _drop_at
            st.session_state['view_dir_a_input'] = trace_server.CONFIG.get('dir_a') or ''
            st.session_state['view_dir_b_input'] = trace_server.CONFIG.get('dir_b') or ''
            # The Viewer that took the drop already shows these folders: its
            # frame must not reload for them (see page_viewer).
            st.session_state['_viewer_drop_dirs'] = (
                trace_server.CONFIG.get('dir_a') or '', trace_server.CONFIG.get('dir_b') or '')
            # Nor for the report link it was opened on, which goes below (see
            # page_viewer): the frame keeps the whole address it had.
            if '_viewer_q' in st.session_state:
                st.session_state['_viewer_drop_q'] = st.session_state['_viewer_q']
            st.session_state.pop('_panel_restore', None)
            st.session_state.pop('_ss_nav_folder', None)
            _trace_folders_changed()
        # Back from the Viewer tab after a click that pointed the A box at the
        # folder the Viewer had to read: the tech's own A and B come back.  No
        # report is dropped, it is the same span.
        if ('_panel_restore' in st.session_state
                and st.session_state.get('nav_radio') not in ('Viewer', 'Viewer FEC')):
            (st.session_state['view_dir_a_input'],
             st.session_state['view_dir_b_input']) = st.session_state.pop('_panel_restore')
            st.session_state.pop('_ss_nav_folder', None)
        # A drop's staged folder shows as what was dropped (demo list #31).
        _label_drop_boxes()

        for _side, _lbl in (('a', 'A'), ('b', 'B')):
            _key = f'view_dir_{_side}_input'
            if st.button(f'📁 {_lbl}-Direction Folder', use_container_width=True,
                         key=f'side_browse_{_side}'):
                _p = pick_folder(f'Choose the {_lbl}-direction folder')
                if _p:
                    st.session_state[_key] = _p
                    _trace_folders_changed()
                elif _p is None:
                    st.session_state['_picker_unavailable'] = True
            st.text_input(f'{_lbl} Folder', key=_key, label_visibility='collapsed',
                          placeholder=f'{_lbl}-Direction Folder Path',
                          on_change=_trace_folders_changed)
        if st.session_state.get('_picker_unavailable'):
            st.caption('⚠ The folder picker isn\'t available in this build. '
                       'Paste the folder paths instead.')
        # OTDR Suite App: one SharePoint folder, under the A and B boxes
        # (Robert, 2026-09-30).  Its load fills the boxes on the next run
        # (_load_span), since they are drawn above it.
        _PANEL_BOXES_DRAWN = True
        # Filled just before the page is drawn (see the route): the SharePoint
        # code is defined further down this script.
        _sp_section = st.expander('☁️ From SharePoint', expanded=False)

        # Asks first (the pop-up is drawn below the sidebar): a click here clears
        # nothing until the tech presses Clear Traces in the pop-up.
        _ask_clear_traces = st.button('Clear Traces', key='side_clear_traces',
                                      use_container_width=True)
        _follow_viewer_folders()          # the Viewer's own folder changes reach the boxes

        # Secret Sauce takes ONE folder holding both directions: build it from the
        # A and B folders whenever that pair changes, as the span loader did.
        _pa, _pb = _panel_boxes()
        if _pa and _pa == st.session_state.get('_ss_nav_folder'):
            # A pair click put the Secret Sauce folder itself in the A box (the
            # Viewer reads the pair from it; see _handle_nav).  It IS the folder
            # the report on the way back was run on: building another one from it
            # and B would move Secret Sauce off its own report.
            pass
        elif (_pa and _pb and os.path.isdir(_pa) and os.path.isdir(_pb)
                and st.session_state.get('_ss_from_ab') != (_pa, _pb)):
            st.session_state['_ss_from_ab'] = (_pa, _pb)
            try:
                _take_panel_ss_folder(_pa, _pb)
            except Exception as _exc:
                report_error('sidebar trace folders: Secret Sauce folder', _exc)
        st.divider()

    if _PROJECT_MODE:
        # Robert, 2026-09-27: "In Project Mode we don't need to have select
        # tool on the left or analysis mode."  The project screen's tabs open
        # the tools (Traces, Reports, Audit FQA); a tool page has a way back.
        # nav_radio is plain state here (no widget), re-assigned every run so
        # it is kept while no widget owns it.
        page = st.session_state.get('nav_radio')
        if page not in TOOLS_PROJECT:
            page = 'Project Status'
        st.session_state['nav_radio'] = st.session_state['_project_page'] = page
        if page != 'Project Status':
            st.button('← Back to Project', key='go_project', type='primary',
                      use_container_width=True)
            st.divider()
    else:
        st.markdown('##### Select Tool')
        page = st.radio('Tool', TOOLS_TRACES, key='nav_radio',
                        label_visibility='collapsed')
        st.divider()

        # The Analysis switch sits right under the Tool list, on every page.
        # Below rather than above so the Tool radio stays the sidebar's first
        # radio -- six tests (and any tech's muscle memory) address it that way.
        _render_analysis_mode_control()
        st.divider()


# A report can be minutes of engine time, so Clear Traces asks before it acts.
if _ask_clear_traces:
    _confirm_clear_traces()


# ═════════════════════════════════════════════════════════════════════════
#  PAGE: Viewer
# ═════════════════════════════════════════════════════════════════════════
@st.cache_resource(show_spinner=False)
def _rerun_caches():
    """Dicts that must outlive a rerun.  Streamlit runs this script in a fresh
    module on every rerun, so a plain module-level {} was empty again on the
    next pass: every click re-read every trace header of a one-folder tool
    (165 MB on a 1,728-file folder), re-copied a folder holding foreign files,
    and re-unzipped a zipped Viewer input (which also reloaded the Viewer).
    Process-wide, so every browser tab shares them: each key and signature
    names exactly what its entry was built from."""
    return {'viewer_dir': {}, 'foreign': {}, 'drop': {}}


_RERUN_CACHE_KEPT = 50


def _remember(cache, key, value):
    """Store an entry and keep only the newest _RERUN_CACHE_KEPT, so a hub
    left open for days does not collect one entry per folder ever opened.
    A dropped entry only costs a re-read the next time that input is used;
    its temp copy is left where it is (an engine may be reading it)."""
    cache.pop(key, None)                  # re-insert as the newest
    cache[key] = value
    for old in list(cache)[:-_RERUN_CACHE_KEPT]:
        cache.pop(old, None)


# A Viewer folder input that is a .zip (or a folder holding zips) is extracted
# ONCE to a temp dir, keyed on the source path and a signature of the zip(s),
# so the Viewer doesn't re-unzip on every Streamlit rerun.
_VIEWER_DIR_CACHE = _rerun_caches()['viewer_dir']


_FOREIGN_STAGE_CACHE = _rerun_caches()['foreign']


def _stable_dir(prefix, source, *version):
    """<temp>/<prefix><hash>/all for one input and one version of it, so the
    same input always stages to the same folder (in every session and after
    a restart) and a changed input gets a new one.  '' when the version is
    unknown: then nothing may be reused."""
    import hashlib
    if any(v is None for v in version):
        return ''
    tag = '|'.join([os.path.normcase(os.path.abspath(source))]
                   + [repr(v) for v in version])
    return os.path.join(tempfile.gettempdir(), prefix + hashlib.sha1(
        tag.encode('utf-8')).hexdigest()[:16], 'all')


def _settle(built, final):
    """Move a freshly built folder to its stable name, whole or not at all
    (a rename).  Keeps the built folder where it is when there is no stable
    name or the name is taken (another session got there first, or an older
    copy that failed its check): a folder we did not just build is never
    handed out from here."""
    if not final:
        return built
    try:
        os.makedirs(os.path.dirname(final), exist_ok=True)
        os.rename(built, final)
        return final
    except OSError:
        return built


def _exclude_foreign_files(folder, exts=None):
    """Run the foreign-file audit on a one-folder tool's input.  When files
    from another job are found, stage the remaining files into a temp folder
    and return (staged_folder, foreign); otherwise (folder, []).  Renders the
    tech-facing warning itself.  Cached per (folder, file-list signature) so a
    rerun neither re-reads 800 headers nor re-copies the span.  Never raises —
    any failure returns the folder untouched."""
    import folder_intake as fi
    try:
        exts = tuple(exts or fi.OTDR_EXTS)
        files = fi.find_otdr_files(folder, exts)
        sig = _files_sig(files)
        # Keyed on the file types too: Secret Sauce also reads .trc, so the
        # same folder is a different input there than in Unidirectional.
        key = (folder, exts)
        cached = _FOREIGN_STAGE_CACHE.get(key)
        if cached and cached[0] == sig and (cached[1] == folder or os.path.isdir(cached[1])):
            staged, foreign = cached[1], cached[2]
        else:
            kept, foreign = fi.audit_foreign_files(files)
            staged = folder
            if foreign:
                # A stable name for this folder, these file types and these
                # files: the Uni report and its saved copy are keyed on the
                # folder a run used, so a restarted hub must find it again.
                final = _stable_dir('otdr_clean_', folder, exts, sig)
                if (final and os.path.isdir(final)
                        and len(fi.find_otdr_files(final, exts)) == len(kept)):
                    staged = final
                else:
                    staged = _settle(fi.materialize_all(
                        kept, os.path.join(tempfile.mkdtemp(prefix='otdr_clean_'), 'all')),
                        final)
            _remember(_FOREIGN_STAGE_CACHE, key, (sig, staged, foreign))
    except Exception as exc:
        report_error('foreign-file audit', exc, {'folder': folder})
        return folder, []
    if foreign:
        st.warning('⚠ ' + fi.foreign_files_message(foreign))
        # Visible to tests and to the run-audit: what was excluded, and where
        # the cleaned input actually lives.
        st.session_state['foreign_excluded'] = {
            'src': folder, 'staged': staged, 'foreign': foreign}
    return staged, foreign


def _resolve_viewer_dir(raw_path):
    """Resolve a Viewer 'A/B folder' input to a directory the trace server can
    list.  Accepts a plain folder, a `.zip`, or a folder CONTAINING zip(s) —
    extracting and flattening as needed — so a zipped SOR span (even a single
    direction) can be viewed WITHOUT the bidirectional 'Load span' flow.
    Returns (usable_dir, note_or_None).  Never raises."""
    import folder_intake as fi
    p = (raw_path or '').strip().strip('"')
    if not p:
        return '', None
    # Fast path: a folder that already lists trace files → use it as-is.
    if os.path.isdir(p) and trace_server.list_fibers(p):
        return p, None
    is_zip = os.path.isfile(p) and p.lower().endswith('.zip')
    try:
        has_inner_zip = os.path.isdir(p) and any(
            f.lower().endswith('.zip') for f in os.listdir(p))
    except OSError:
        has_inner_zip = False
    if not (is_zip or has_inner_zip):
        return p, None            # nothing to extract; page_viewer validates/warns
    # What the extraction was built from: the zip itself, or, for a folder,
    # every zip and trace file in it, each by path, size, mtime and inode, so
    # a zip replaced or overwritten in place is extracted again.
    try:
        if is_zip:
            _zsig = _files_sig([p])
        else:
            _zsig = _files_sig(fi.zip_paths(p) + fi.find_otdr_files(p))
    except OSError:
        _zsig = None
    cached_sig, cached_dir = _VIEWER_DIR_CACHE.get(p) or (None, None)
    if (_zsig is not None and cached_sig == _zsig
            and cached_dir and os.path.isdir(cached_dir)
            and trace_server.list_fibers(cached_dir)):
        return cached_dir, 'viewing from .zip'
    # One folder per zip (and per version of it), named after both, so every
    # tool, every session and a restarted hub get the same folder for the same
    # zip: the report pages read these boxes too (_panel_dirs), and a report
    # is saved under the folders it ran on.
    final = _stable_dir('viewer_zip_', p, _zsig)
    if final and os.path.isdir(final) and trace_server.list_fibers(final):
        _remember(_VIEWER_DIR_CACHE, p, (_zsig, final))
        return final, 'viewing from .zip'
    try:
        dest = tempfile.mkdtemp(prefix='viewer_zip_')
        files = (fi.extract_zip(p, os.path.join(dest, 'unzipped')) if is_zip
                 else fi.find_otdr_files_with_zips(p, os.path.join(dest, 'zips')))
        if not files:
            return p, None        # nothing extractable; fall through to the folder
        # Flatten everything discoverable into one dir the trace server can list
        # (extract_zip / find_otdr_files_with_zips may leave files in subfolders).
        flat = _settle(fi.materialize_all(files, os.path.join(dest, 'all')), final)
        _remember(_VIEWER_DIR_CACHE, p, (_zsig, flat))
        return flat, 'viewing from .zip'
    except Exception as exc:                           # bad zip / IO
        return '', f'could not read that .zip ({exc})'


def _project_run_shoot():
    """The shoot a tool was opened on from the project's Traces tab (Run In…),
    or None.  Such a page shows no trace picking, profile or site names --
    only its settings, Show/Hide and Run (Robert, 2026-09-27)."""
    ss = st.session_state
    if not _PROJECT_MODE or ss.get('project_run_shoot') is None:
        return None
    return next((sh for sh in list_shoots(work_dir())
                 if sh['id'] == ss['project_run_shoot']), None)


def _render_run_shoot_line(sh):
    d, lab = shoot_info(sh)
    st.markdown(f"**Traces:** ✅ shot {d or '(no date)'}{' · ' + lab if lab else ''} · "
                f"A {len(_trace_fibers(sh['a']))} / B {len(_trace_fibers(sh['b']))} fibers")
    st.caption(f"{_rel(work_dir(), sh['dir'])} · chosen on the project's Traces tab")


def _same_dir(x, y):
    return bool(x and y) and (os.path.normcase(os.path.abspath(x))
                              == os.path.normcase(os.path.abspath(y)))


def _qa_span():
    """Quick Analysis' loaded traces, or None.  The left panel's A and B
    boxes hold them (Trace Folders, brought into the App 2026-09-28), so a
    direction the tech changes there is the span from then on, and one box
    left empty is no span.  A Splice Report click-through into the Viewer is
    a fresh session: its folders are the trace server's."""
    sp = st.session_state.get('span_loaded') or {}
    a, b = _panel_boxes()
    if a or b:
        a, b = (a if os.path.isdir(a) else ''), (b if os.path.isdir(b) else '')
        if not (a and b):
            return None
        if _same_dir(sp.get('dir_a'), a) and _same_dir(sp.get('dir_b'), b):
            return sp
        _ss = st.session_state
        return {'dir_a': a, 'dir_b': b,
                'combined': _ss.get('_ss_panel_folder') if _ss.get('_ss_from_ab') == (a, b) else None,
                'ila_a': _derive_ila(a)[0] or 'A', 'ila_b': _derive_ila(b)[0] or 'B',
                'label': os.path.basename(os.path.dirname(os.path.abspath(a))),
                'a_count': len(trace_server.list_fibers(a)),
                'b_count': len(trace_server.list_fibers(b))}
    if sp.get('dir_a') and os.path.isdir(sp['dir_a']) and os.path.isdir(sp.get('dir_b') or ''):
        return sp
    a, b = trace_server.CONFIG.get('dir_a'), trace_server.CONFIG.get('dir_b')
    if a and b and os.path.isdir(a) and os.path.isdir(b):
        return {'dir_a': a, 'dir_b': b, 'combined': None,
                'ila_a': _derive_ila(a)[0] or 'A', 'ila_b': _derive_ila(b)[0] or 'B',
                'label': os.path.basename(os.path.dirname(os.path.abspath(a))),
                'a_count': len(trace_server.list_fibers(a)),
                'b_count': len(trace_server.list_fibers(b))}
    return None


def _chosen_traces():
    """The traces a tool runs on when they were chosen before it opened: a
    project's Run In… (one shoot).  (Quick Analysis runs on the left panel's
    Trace Folders instead, see _panel_traces.)  Such a
    tool shows no trace picking (Robert, 2026-09-27: "we shouldn't have to
    reselect the traces ... we can choose settings and then generate").
    {'a', 'b', 'dir' (both directions, or None), 'name'} or None."""
    sh = _project_run_shoot()
    if sh:
        d, lab = shoot_info(sh)
        return {'a': sh['a'], 'b': sh['b'], 'dir': sh['dir'],
                'name': f"shot {d or '(no date)'}{' · ' + lab if lab else ''}", 'shoot': sh}
    return None


def _render_chosen_line(ct):
    _render_run_shoot_line(ct['shoot'])


# A folder in one of the left panel's boxes that holds BOTH directions:
# split the way a drop on the Viewer splits it (trace_server.split_directions),
# into a folder per direction.  Keyed on the folder and its _folder_sig, so a
# rerun lists the folder and nothing else.  Process-wide (cache_resource): a
# plain dict here starts empty on every rerun.
@st.cache_resource(show_spinner=False)
def _panel_split_store():
    return {}


def _split_panel_folder(folder):
    """The two direction folders of a folder that holds both directions, as
    {'a', 'b', 'a_key', 'a_count', 'b_key', 'b_count', 'ignored'}, or None
    when it holds one direction (or cannot be read).

    Pasted into the A box, a span's two directions side by side
    listed as fibers 1, 1, 2, 2, ...: the Viewer showed every B file as A->B
    under the A file's key and opened the A file for either, and the Splice
    Report named both directions after one site (2026-09-29).  The drop
    already split such a folder; this is the same rule.  The two folders are
    hard links (a copy where a link cannot be made), named after the folder
    and its signature, so the same folder always gives the same two: a
    report is saved under the folders it ran on."""
    import hashlib
    import folder_intake as fi
    p = os.path.abspath(folder)
    sig = trace_server._folder_sig(p)
    if sig is None:
        return None
    hit = _panel_split_store().get(p)
    if hit and hit[0] == sig and (hit[1] is None or (
            os.path.isdir(hit[1]['a']) and os.path.isdir(hit[1]['b']))):
        return hit[1]
    res = None
    try:
        files = sorted(os.path.join(p, f) for f in os.listdir(p)
                       if f.lower().endswith(trace_server.DROP_EXTS)
                       and not f.startswith('.') and os.path.isfile(os.path.join(p, f)))
        split = trace_server.split_directions(files) if len(files) >= 2 else None
        if split and len(split['keep']) == 2:
            dest = os.path.join(
                tempfile.gettempdir(), 'otdr_panel_split_' + hashlib.sha1(
                    f'{os.path.normcase(p)}|{sig}'.encode('utf-8')).hexdigest()[:16])
            res = {'ignored': list(split['ignored'])}
            for side, (key, fs) in zip(split['sides'], split['keep']):
                s_ = side.lower()
                res[s_] = fi.materialize_all(fs, os.path.join(dest, side))
                res[s_ + '_key'], res[s_ + '_count'] = key, len(fs)
    except Exception as exc:                           # IO: leave the folder as is
        report_error('left panel: split a both-direction folder', exc, {'folder': p})
        res = None
    _panel_split_store()[p] = (sig, res)
    return res


def _panel_dirs():
    """The left panel's two boxes, resolved: (dir_a, dir_b, notes).

    Each box may hold a folder, a .zip or a folder of zips (_resolve_viewer_dir).
    A box whose folder holds both directions is split into A and B when the
    other box is empty or names the same folder; with another folder in the
    other box nothing is split and a note says why.  `notes` is a list of
    (kind, text), kind 'warning' or 'caption', for the page to show.  A path
    that does not exist comes back as typed, for the caller to judge."""
    raw_a, raw_b = _panel_boxes()
    out, notes = {}, []
    for side, raw in (('A', raw_a), ('B', raw_b)):
        d, note = _resolve_viewer_dir(raw)
        if note and note.startswith('could not'):
            notes.append(('warning', f'{side}: {note}'))
            d = ''
        elif note:
            notes.append(('caption', f'{side}: {note}'))
        out[side] = d
    same = bool(raw_a and raw_b) and (os.path.normcase(os.path.abspath(raw_a))
                                      == os.path.normcase(os.path.abspath(raw_b)))
    # A pair click points the A box at Secret Sauce's own folder, which holds
    # both directions on purpose (see _handle_nav): the pair is read from it.
    ss_nav = st.session_state.get('_ss_nav_folder')
    for side, other in (('A', 'B'), ('B', 'A')):
        d = out[side]
        if not d or not os.path.isdir(d) or (side == 'A' and raw_a and raw_a == ss_nav):
            continue
        split = _split_panel_folder(d)
        if not split:
            continue
        what = (f"**A:** {split['a_key'] or '?'} ({split['a_count']} files) · "
                f"**B:** {split['b_key'] or '?'} ({split['b_count']} files)")
        if not out[other] or same:
            out['A'], out['B'] = split['a'], split['b']
            notes.append(('caption', f'The {side} folder holds both directions, '
                                     f'split like a drop on the Viewer: {what}'
                          + (f" · ignored: {', '.join(split['ignored'])}"
                             if split['ignored'] else '')))
            break
        notes.append(('warning', f'The {side} folder holds both directions '
                                 f'({what}), and the {other} box has a folder of '
                                 f'its own. Empty the {other} box to split it '
                                 f'into A and B, or give {side} one direction.'))
    return out['A'], out['B'], notes


def page_viewer(fec=False):
    """The Trace Viewer.  fec=True is the Viewer FEC tool (Robert 2026-10-01):
    the same Viewer, opened in FEC mode (?fec=1), which it cannot leave."""
    port = ensure_trace_server()

    with st.sidebar:
        # The A/B folder boxes are the sidebar's Trace Folders loader, drawn
        # on every page above the tool list; the Viewer reads the same slots.
        # Files dropped on the Viewer's own FILES panel: in Quick Analysis the
        # left panel's Trace Folders block has already taken them (it checks
        # CONFIG['dropped_at'] on every page, before its boxes).  A project
        # draws no left panel, so the Viewer takes a drop here, before its
        # own boxes below.  Never while the panel is drawn: its boxes exist
        # by now, and Streamlit refuses a write to a drawn box.
        if not _PANEL_DRAWN:
            _drop_at = trace_server.CONFIG.get('dropped_at') or 0
            if _drop_at > st.session_state.get('view_drop_seen', 0):
                st.session_state['view_drop_seen'] = _drop_at
                st.session_state['view_dir_a_input'] = trace_server.CONFIG['dir_a'] or ''
                st.session_state['view_dir_b_input'] = trace_server.CONFIG['dir_b'] or ''
                # The Viewer that took the drop already shows these folders: its
                # frame must not reload for them (the b= below).
                st.session_state['_viewer_drop_dirs'] = (
                    trace_server.CONFIG.get('dir_a') or '', trace_server.CONFIG.get('dir_b') or '')
                # Nor for the report link it was opened on: the frame below keeps
                # the whole address it had.
                if '_viewer_q' in st.session_state:
                    st.session_state['_viewer_drop_q'] = st.session_state['_viewer_q']

        if not _PANEL_DRAWN:
            # In a project there is no left-panel Trace Folders: the Viewer
            # shows the traces a shoot's Run In… chose, or boxes of its own.
            _ct = _chosen_traces()
            if _ct:
                # The traces are chosen (the Quick Analysis Load screen, or a
                # project's Run In…): nothing to pick here.
                st.session_state['view_dir_a_input'] = _ct['a']
                st.session_state['view_dir_b_input'] = _ct['b']
                st.caption('✅ ' + _ct['name'])
            else:
                if st.button('📁 A-Direction Folder', use_container_width=True):
                    p = pick_folder('Choose the A-direction folder')
                    if p:
                        st.session_state['view_dir_a_input'] = p
                st.text_input('A Folder', key='view_dir_a_input',
                              label_visibility='collapsed', placeholder='A-Direction Folder Path')

                if st.button('📁 B-Direction Folder', use_container_width=True):
                    p = pick_folder('Choose the B-direction folder')
                    if p:
                        st.session_state['view_dir_b_input'] = p
                st.text_input('B Folder', key='view_dir_b_input',
                              label_visibility='collapsed', placeholder='B-Direction Folder Path')

        # Resolve each input (a folder, a .zip, or a folder holding zip(s)) to a
        # directory the trace server can list — so a zipped SOR span views
        # without the bidirectional 'Load span' flow.
        # A folder holding both directions is split as a drop splits it.
        dir_a, dir_b, _notes = _panel_dirs()

        # Validate + push into the trace server's shared config.
        warn = [t for k, t in _notes if k == 'warning']
        if dir_a and not os.path.isdir(dir_a):
            warn.append('A folder not found')
            dir_a = ''
        if dir_b and not os.path.isdir(dir_b):
            warn.append('B folder not found')
            dir_b = ''
        for _d, _lbl in ((dir_a, 'A'), (dir_b, 'B')):
            if _d:
                try:
                    os.listdir(_d)
                except OSError:
                    warn.append(f'{_lbl} folder is not readable (check permissions)')
        if dir_a and dir_b and os.path.abspath(dir_a) == os.path.abspath(dir_b):
            warn.append('A and B are the same folder')
        trace_server.set_dirs(dir_a or None, dir_b or None)
        for w in warn:
            st.warning(w)

        na = len(trace_server.list_fibers(dir_a)) if dir_a else 0
        nb = len(trace_server.list_fibers(dir_b)) if dir_b else 0
        st.caption(f'A: {na} fibers · B: {nb} fibers')
        for _k, _t in _notes:
            if _k == 'caption' and 'both directions' in _t:
                st.caption(_t)

    # If the tech arrived here by clicking a Duplicate Check pair, offer a
    # one-click route back to the report (the sidebar radio also works, but an
    # explicit back button makes flipping pair⇄list a single click).
    if st.session_state.get('came_from_dupcheck'):
        # Set the nav state in an on_click CALLBACK — callbacks run before the
        # sidebar radio is re-instantiated, so writing nav_radio here is allowed
        # (writing it inline, after the widget exists, raises StreamlitAPIException).
        def _back_to_dupcheck():
            st.session_state['came_from_dupcheck'] = False
            st.session_state['nav_radio'] = 'Secret Sauce'
        st.button('← Back to Secret Sauce', key='view_back_dupcheck',
                  on_click=_back_to_dupcheck)
    # Same one-click return for the other two report surfaces.  Each origin
    # page restores its report from a disk cache on render (the anchor nav
    # wiped session_state), so Back never forces an engine re-run.
    if st.session_state.get('came_from_splicereport'):
        def _back_to_sr():
            st.session_state['came_from_splicereport'] = False
            st.session_state['nav_radio'] = 'Splice Report'
        st.button('← Back to Splice Report', key='view_back_sr',
                  on_click=_back_to_sr)
    if fec and st.session_state.get('came_from_fec'):
        def _back_to_fec():
            st.session_state['came_from_fec'] = False
            st.session_state['nav_radio'] = 'Splice Report FEC'
        st.button('← Back to Splice Report FEC', key='view_back_fec',
                  on_click=_back_to_fec)
    if st.session_state.get('came_from_uni'):
        def _back_to_uni():
            st.session_state['came_from_uni'] = False
            st.session_state['nav_radio'] = 'Unidirectional'
        st.button('← Back to Unidirectional', key='view_back_uni',
                  on_click=_back_to_uni)

    # No "Trace Viewer" heading: the sidebar already says where you are
    # (Robert 2026-10-01), and it pushed the Viewer further down the page.
    # The Viewer FEC page keeps its heading, at half the old size (Robert
    # 2026-10-01: 0.75rem, the #### was 1.5rem), and its line on FEC mode.
    if fec:
        st.markdown('<p style="font-size:0.75rem;font-weight:600;margin:0">'
                    'Viewer FEC</p>', unsafe_allow_html=True)
        st.caption('Facility entrance (FEC) shots: the short traces from '
                   'each end. A and B are drawn as shot and never paired; '
                   'each trace’s panel connector is graded at the customer '
                   'profile’s FEC gates, as Splice Report FEC does.')
    # Pop the Viewer into its own window from HERE too — a tech who came to
    # the Viewer page first (rather than clicking a report cell) had no way
    # to detach it.  Same window NAME as the report grids' button, so the two
    # entry points share ONE window: opening from here and then clicking
    # report cells drives this same window instead of spawning a second.
    # From Viewer FEC it opens in FEC mode (?fec=1).
    _pop_doc = """
<style>body{margin:0;padding:3px 0}</style>
<button id="vpop2" style="padding:4px 10px;border:1px solid #c9d5e1;border-radius:4px;
    background:#eef3f8;cursor:pointer;font-weight:600;color:#000000;white-space:nowrap;
    font-family:sans-serif;font-size:13px"
    title="Keeps this page free for the report. Report cell clicks drive the same window."
    >&#8862; Open Viewer in Its Own Window</button>
<script>
try { window.top.name = "otdr_hub"; } catch (e) {}
document.getElementById("vpop2").addEventListener("click", function(){
  var w = window.open("__ORIGIN__/__POPQ__", "otdr_viewer", "width=1400,height=900");
  if (w) w.focus();
});
</script>
""".replace('__ORIGIN__', f'http://127.0.0.1:{port}').replace('__POPQ__', '?fec=1' if fec else '')
    # The OTDR Settings, same box as the report pages and sharing their
    # values (Robert 2026-09-28).  With no report behind it the Viewer judges
    # pass/fail at these and runs its own report with them; a report on
    # screen still sets the Viewer's gates, so say so when these differ.
    # All of it in ONE slot.  Streamlit places the Viewer frame by its
    # position on the page, so the caption below appearing on a setting
    # change (or a warning in the box) moved the frame down a place, and
    # Streamlit rebuilt it: the Viewer reloaded and the tech lost every
    # trace they had loaded and highlighted (Robert 2026-09-29: "keep traces
    # highlighted if changing setting as long as you don't leave viewer").
    # One row for the profile dropdown and the pop-out button, and the line
    # that explained the button is its tooltip now (window-size audit
    # 2026-10-01): the heading, the dropdown, the button and its line took
    # 432 px above the Viewer, so a 1366 x 768 laptop showed only the
    # toolbar and the top of the chart; and under ~1030 px the line wrapped
    # and its second half was cut off in the button's 42 px frame.
    with st.container():
        with st.container(horizontal=True, vertical_alignment='center', gap='medium'):
            _render_profile_picker_box('viewer', compact=True)
            st_components_html(theme_recolor(_pop_doc), height=36, width=270)
        _viewer_box_exc = _render_settings_box('viewer')
        if _viewer_box_exc is None and trace_server.settings_differ_from_report():
            st.caption('Pass/fail in the Viewer follows the Splice Report on '
                       'screen, at the settings it ran with. Generate the report '
                       'again to judge by the settings above.')
    # The note above the frame keeps ONE slot whether it shows or not:
    # Streamlit places the frame by its position on the page, so a note
    # going away after the first drop moved the frame up a place and rebuilt
    # it, and the Viewer lost the traces it had just loaded.  The blue "Pick
    # an A and/or B folder" box that sat here is gone (Robert 2026-10-01): the
    # Viewer's own Files panel says where to drop them.  The slot stays for
    # the one-shot jump captions below.
    _note = st.empty()
    # Embed the canvas viewer.  Cache-bust on folder change so the iframe
    # re-reads /api/list.  A deep-link target is appended so the viewer
    # auto-loads:  a single fiber + km (Splice Report cell), OR a pair of
    # fibers overlaid (Duplicate Check "Stay in app").
    from urllib.parse import urlencode
    # Not after a drop on the Viewer, though: the Viewer pointed the server at
    # the dropped folders itself and shows them, and a reload on the next
    # rerun threw away every trace it had loaded, while a second folder
    # dropped as the frame came back went to Chrome's Downloads (the boss,
    # 2026-09-30: A loaded, then B went to Downloads).
    _key = ((dir_a or ''), (dir_b or ''))
    if (_key == st.session_state.get('_viewer_drop_dirs')
            and '_viewer_b' in st.session_state):
        _b = st.session_state['_viewer_b']
    else:
        _b = abs(hash(_key)) % 100000
    st.session_state['_viewer_b'] = _b
    q = {'b': _b}
    if fec:
        q['fec'] = 1
    # PERSISTENT deep-link target (read, NOT consumed).  Keeping the last
    # clicked/loaded fiber in the iframe URL makes the src STABLE across
    # Streamlit reruns.  Consuming it with .pop made the very next rerun rebuild
    # the URL WITHOUT the fiber, which reloaded the iframe back to its hardcoded
    # default (F64) and wiped any fibers the tech had typed in — the "viewer only
    # shows F64" bug.  The target changes only when the user clicks a new
    # cell/pair (_handle_nav overwrites it).  The caption is one-shot (announced
    # once per fresh jump, not on every rerun).
    tgt = st.session_state.get('viewer_target')
    announce = st.session_state.pop('viewer_jump_announce', False)
    if tgt and tgt.get('fibers'):
        q['fibers'] = tgt['fibers']
        q['dir'] = tgt.get('dir', 'a')
        if announce:
            _note.caption(f"Overlaying duplicate-pair fibers {tgt['fibers']} "
                       f"(direction {q['dir'].upper()})")
    elif tgt and tgt.get('fiber'):
        q['fiber'] = tgt['fiber']
        if tgt.get('km'):
            q['km'] = tgt['km']
        q['dir'] = tgt.get('dir', 'both')
        if tgt.get('src'):
            q['src'] = tgt['src']
        if announce:
            _note.caption(f"Jumped to fiber {tgt['fiber']}"
                       + (f" @ {tgt['km']} km" if tgt.get('km') else ''))
    # A drop on a Viewer opened from a report cell: the drop is a new span,
    # so the cell's link goes (_trace_folders_changed), and the address
    # without it reloaded the frame.  The hub reruns by itself just after a
    # drop (_follow_viewer_folders), so the Viewer lost the A set it had just
    # loaded, and the B set dropped while it came back went to Chrome's
    # Downloads (the boss, 2026-10-01, after #466).  Until the next cell
    # click the frame keeps the address it had.
    _frozen = st.session_state.get('_viewer_drop_q')
    if _key == st.session_state.get('_viewer_drop_dirs') and _frozen and not tgt:
        q = dict(_frozen)
    st.session_state['_viewer_q'] = q
    # Use the whole window (Robert, 2026-09-29: blank space at every edge).
    # Streamlit's wide layout keeps ~5rem each side and 6rem / 10rem above and
    # below the page, and the Viewer was a fixed 760 px tall, so a big screen
    # showed a strip of white all round it.  On this page only: the margins
    # go down to a few px, and the Viewer is as tall as the window below
    # Streamlit's header (3.75rem), so scrolled down to it the Viewer fills the
    # screen and the plot takes the extra height.  The iframe is 100% of the
    # box Streamlit wraps it in, and the box carries the 760 px (as its height
    # and its flex size), so both go on the box.  Never below 560 px, so a small laptop window keeps a
    # usable plot.  760 stays as the height if a browser ignores :has().
    st.markdown(
        '<style>'
        '[data-testid="stMainBlockContainer"]'
        '{padding:3.75rem 0.75rem 0.75rem 0.75rem;max-width:none}'
        '[data-testid="stElementContainer"]:has(> iframe[src^="'
        f'http://127.0.0.1:{port}/"])'
        '{height:max(560px, calc(100vh - 4.5rem)) !important;'
        'flex:0 0 max(560px, calc(100vh - 4.5rem)) !important}'
        '</style>', unsafe_allow_html=True)
    st_iframe(f'http://127.0.0.1:{port}/?{urlencode(q)}', height=760, scrolling=False)


# ═════════════════════════════════════════════════════════════════════════
#  PAGE: Duplicate Check (Secret Sauce)
# ═════════════════════════════════════════════════════════════════════════
def page_duplicate_check():
    st.markdown('#### Secret Sauce')
    # The line that tells the tech to pick a folder is for the page's own
    # loader, which is not drawn when the left panel holds the traces.
    st.caption(('' if any(_panel_traces()) else
                'Pick a folder of `.sor` / `.trc` / `.json` files. ')
               + 'Reports are saved to the folder you choose below (Downloads '
               'by default) and offered for download.')

    st.session_state.setdefault('ss_folder_input', '')
    _shoot = _chosen_traces()
    _pa, _pb = _panel_traces()
    _dropped = None
    if _shoot and _shoot['dir']:
        _render_chosen_line(_shoot)
        folder = _shoot['dir']
    elif _pa or _pb:
        # Traces loaded in the left panel: the page draws no loader of its
        # own and runs on those (Robert 2026-09-28).  Both directions go in
        # as the one folder the sidebar built from them; after a pair click
        # the A box IS that folder (see _handle_nav).
        _renamed = []
        if _pa == st.session_state.get('_ss_nav_folder'):
            folder = _pa
        elif _pa and _pb:
            # Signed again on every pass, not only when the pair changes: a
            # trace swapped on disk since the sidebar built the folder gets a
            # new folder now (see _panel_ss_folder).
            try:
                folder, _renamed = _take_panel_ss_folder(_run_folder(_pa),
                                                         _run_folder(_pb))
            except Exception as _exc:
                report_error('secret sauce: A and B folder', _exc,
                             {'dir_a': _pa, 'dir_b': _pb})
                st.warning('The A and B folders could not be read just now '
                           f'({type(_exc).__name__}: {_exc}). If files are '
                           'still being copied in, try again when that is done.')
                return
        else:
            folder = _run_folder(_pa or _pb)
        _viewer_removed_note(_pa, _pb)
        st.caption('Traces: ' + ('the A and B folders' if _pa and _pb
                                 and folder not in (_pa, _pb)
                                 else f"the {'A' if _pa else 'B'} folder")
                   + ' loaded in the left panel.')
        if _renamed:
            st.caption(_renamed_note(_renamed))
    else:
        c1, c2 = st.columns([1, 2])
        with c1:
            if st.button('📁 Browse for Folder', type='primary', use_container_width=True):
                p = pick_folder('Choose a folder of OTDR files')
                if p:
                    st.session_state['ss_folder_input'] = p
        with c2:
            st.text_input('…or Paste a Folder Path',
                          key='ss_folder_input',
                          placeholder=r'C:\Users\you\Desktop\fiber files')

        folder = (st.session_state.get('ss_folder_input') or '').strip().strip('"')
        _dropped = st.file_uploader(
            '…or Drag & Drop the Files Here (.sor / .trc / .json, a Whole '
            'Folder, or a .zip)',
            type=['sor', 'trc', 'json', 'zip'], accept_multiple_files=True,
            key='ss_drop')
    if _dropped:
        _sdir, _sn, _sdupes = _stage_dropped(_dropped)
        if _sn:
            st.caption(f'📥 {_sn} file(s) staged from the drop, used as the '
                       'input folder.')
            folder = _sdir
        else:
            st.warning('The drop contained no readable OTDR files.')
        if _sdupes:
            import folder_intake as _fi_d
            st.warning('⚠ ' + _fi_d.duplicate_names_message(_sdupes, kept=False))
    if not folder or not os.path.isdir(folder):
        st.info('👆 Choose the folder that holds your `.sor` / `.trc` / `.json` '
                'files, or drag & drop them above.')
        return
    # Normalize to an absolute path (as the Viewer and Splice Report pages do)
    # before we build the output dir inside it — a relative/CWD-dependent folder
    # would put SecretSauce_reports somewhere the engine can't reliably write.
    folder = os.path.abspath(folder)
    _remove_legacy_caches(folder)
    # Reports still land next to the ORIGINAL folder; only the engine's input
    # moves to the cleaned copy when foreign files are excluded.
    src_folder = folder
    import folder_intake as _fi
    folder, _foreign = _exclude_foreign_files(folder, _fi.OTDR_EXTS_WITH_TRC)

    out_format = st.radio('Output', ['Excel (xlsx)', 'PDF', 'Stay in App'],
                          horizontal=True)
    fmt = {'Excel (xlsx)': 'xlsx', 'PDF': 'pdf'}.get(out_format, 'pairs')

    st.caption("⏳ Large folders can take several minutes. After you click you'll see "
               "live progress here. **Leave this window open and don't refresh.**")
    # Downloads/SecretSauce_reports by default (the engine writes several
    # files, so they get their own folder there); the tech can point it.
    import folder_intake as _fi_dest
    _ss_dest = _report_dest_row(
        'ss_report_dest', os.path.join(_fi_dest.default_report_dir(), 'SecretSauce_reports'))
    _stale = _report_gate('ss')
    if st.button('Run Analysis', type='primary', disabled=bool(_stale)):
        out_dir = _ss_dest
        if _project_run_path(out_dir, 'x') != os.path.join(out_dir, 'x'):
            out_dir = _project_run_path(out_dir, 'Secret Sauce', traces=(src_folder,))
        st.session_state['ss_pending_cmd'] = secretsauce_cmd(folder, out_dir, fmt)
        st.session_state['ss_out_dir'] = out_dir
        # The report is saved under the folder it RAN on: the page may build
        # a new one from the panel's folders while the run is going.
        st.session_state['ss_run_folder'] = folder
        st.session_state.pop('ss_result', None)        # clear any prior result
        st.session_state.pop('ss_pairs_result', None)
        st.rerun()

    # Background run with a live progress panel + Cancel; the engine runs as a
    # concurrent subprocess so the page never freezes.
    if 'ss_pending_cmd' in st.session_state or 'ss_job' in st.session_state:
        out_dir = st.session_state.get('ss_out_dir',
                                       os.path.join(folder, 'SecretSauce_reports'))
        try:
            proc = run_engine_live('ss', running_title='Running Secret Sauce')
        except subprocess.TimeoutExpired as _to:
            # The engine narrates its phases on stdout; run_engine_live now
            # carries the tail on the exception.  Surfacing it is what makes a
            # timeout diagnosable — five have been reported and not one said
            # where it died.  No "Loaded N ... files" line means it never got
            # past STAGING, i.e. copying the folder, which is the slow part
            # when the source is a network or Parallels share.
            _phase = (_to.output or '').strip()
            st.error(f'Secret Sauce timed out after {ENGINE_TIMEOUT_S}s '
                     'and was stopped. Try a smaller folder, or check for a '
                     'wedged engine.')
            if _phase:
                with st.expander('How Far It Got'):
                    st.code(_phase)
            _nf, _nb = _count_input_files(folder)
            report_error("secret sauce — timeout",
                         RuntimeError(f"engine exceeded {ENGINE_TIMEOUT_S}s"),
                         {"folder": folder, "format": fmt,
                          "n_files": _nf,
                          "input_mb": (None if _nb is None
                                       else round(_nb / 1_048_576.0, 1)),
                          "reached": (_phase.splitlines() or ['(no output: '
                                      'died before the first phase)'])[-1]},
                         log=_phase or None)
            return
        if proc is None:
            return                                     # cancelled — clean slate
        manifest = _parse_manifest(proc.stdout)
        if manifest is None:
            if not _engine_damaged_notice(proc.stderr, 'ss'):
                st.error('Secret Sauce did not return a result.')
                with st.expander('Engine Log'):
                    st.code(proc.stderr[-4000:] or '(no output)')
            report_error("secret sauce — no manifest",
                         RuntimeError("runner returned no JSON manifest"),
                         {"returncode": proc.returncode},
                         log=proc.stderr)
            return
        if not manifest.get('ok'):
            st.error(manifest.get('error', 'Analysis failed.'))
            if manifest.get('counts'):
                st.caption(f"Inventory: {manifest['counts']}")
            with st.expander('Engine Log'):
                st.code(proc.stderr[-4000:] or '(no output)')
            report_error("secret sauce — engine returned not-ok",
                         RuntimeError(manifest.get('error', 'analysis failed')),
                         {"counts": manifest.get('counts'), "format": fmt},
                         log=proc.stderr)
            return

        # Stash the folder so the in-app pair links can point the viewer at it.
        manifest['_folder'] = st.session_state.pop('ss_run_folder', None) or folder
        if manifest.get('mode') == 'pairs':
            st.session_state['ss_pairs_result'] = manifest
            # Cache to disk so "← Back" from the Viewer (which reset session_state
            # via the URL nav) re-shows the pairs list instantly — no re-run.
            _ss_cache_write('pairs_cache.json', folder, manifest)
        else:
            st.session_state['ss_result'] = manifest
            # Same round trip, same loss — the Excel/PDF result needs it too.
            _ss_cache_write('ss_result_cache.json', folder, manifest)

    # ── In-app duplicate report (persists across reruns; restore from the
    #    on-disk cache after a pair-click round trip cleared session_state) ──
    pres = st.session_state.get('ss_pairs_result')
    if not (pres and pres.get('mode') == 'pairs'):
        cached = _ss_cache_read('pairs_cache.json', folder, mode='pairs')
        if cached:
            pres = cached
            st.session_state['ss_pairs_result'] = cached
    if pres and pres.get('ok') and pres.get('mode') == 'pairs':
        _clear_report_button('ss')
        _render_pairs_report(pres)
        return

    # ── Excel / PDF download result (persists across reruns, and across the
    #    pair-click round trip that resets session_state) ──
    res = st.session_state.get('ss_result')
    if not (res and res.get('ok')):
        cached = _ss_cache_read('ss_result_cache.json', folder)
        if cached:
            res = cached
            st.session_state['ss_result'] = cached
    if res and res.get('ok'):
        _clear_report_button('ss')
        c = res.get('counts', {})
        st.success(f"Done: {c.get('sor',0)} SOR · {c.get('trc',0)} TRC · "
                   f"{c.get('json',0)} JSON found.")
        _render_competence_banner(res)
        _render_confidence_caption(res)
        _render_near_splice(res)
        _render_mating_top(res)
        _render_fill_ins(res)
        # The engine excludes suspected-broken traces from the comparison and
        # says so in the manifest; until now nothing rendered it, so a folder
        # could report on fewer fibers than it found with no explanation on
        # screen.  DURANC 1-144: 144 found, 141 compared, 3 excluded.
        _short = res.get('short_traces') or []
        _excl = [e for e in _short if e.get('excluded')]
        if _excl:
            st.warning(
                f"{len(_excl)} trace(s) excluded from the comparison as "
                f"suspected breaks. The report covers the rest.")
            with st.expander(f'Excluded Traces ({len(_excl)})'):
                for e in _excl:
                    st.write(f"**{e.get('file','?')}**: {e.get('note','')}")
        for _w in res.get('window_warnings') or []:
            st.warning(_w)
        for w in res.get('written', []):
            p = w['path']
            if not os.path.exists(p):
                continue
            with open(p, 'rb') as fh:
                data = fh.read()
            label = (f"⬇ {os.path.basename(p)}  "
                     f"({w.get('key','')} · {w.get('n_files','?')} files · "
                     f"{w.get('n_pairs','?')} pairs)")
            st.download_button(label, data=data, file_name=os.path.basename(p),
                               key='dl_' + p)
        st.caption(f'Saved to: {os.path.join(folder, "SecretSauce_reports")}')


# Likelihood-tier colors for the in-app duplicate-pair report.
_DUP_COLOR = {'CONFIRMED duplicate': '#c0392b', 'Likely duplicate': '#e67e22',
              'Possible duplicate': '#b97000', 'Unique': '#7f8c8d'}


# When a NOT MEASURED folder carries a mating ranking, the ranking IS the
# result for the tech (a port-log check): the notice leads with it and is a
# warning, not an error.  A red box on a tie panel read as "the tool failed"
# while the ranking sat unseen on the workbook's last sheet.
_MATING_LEAD = ('**The fiber fingerprint cannot be measured here; the mating '
                'ranking below is the result to check against the port log.** ')


def _render_competence_banner(res):
    """Say, on screen, when the duplicate detector could not measure this
    folder.  The engine decides from the folder's own noise and spread (see
    report_sor._speckle_competence); a zero on such a folder means NOT
    MEASURED, and until now that sentence lived only on the workbook's
    summary sheet.  Renders nothing when every lineage reported OK."""
    for c in (res.get('competence') or []):
        if not isinstance(c, dict) or c.get('status') in (None, 'OK'):
            continue
        status = c.get('status')
        show = st.error if status == 'NOT MEASURED' else st.warning
        lead = ''
        if status == 'NOT MEASURED' and res.get('mating_top'):
            show, lead = st.warning, _MATING_LEAD   # the ranking is the result
        show(f"{lead}**Duplicate detection {status}.** {c.get('message', '')}")
        if c.get('what_it_takes'):
            st.caption(c['what_it_takes'])


# ── Near splice: the splice behind the panel ──────────────────────────────
# The engine measures, on every fibre, the loss of the splice a few tens of
# metres behind the panel (FEC spans: 43-86 m past the port).  It is glass, so
# an unplug and re-plug cannot change it: two shots of one fibre read it within
# a small wobble, and two files that read it far apart are different fibres.
# A match proves nothing, because many fibres have similar splices.  The hub
# answers a two-fibre check from the manifest, no re-run.
_NEAR_SPLICE_CLEAR_SD_DEFAULT = 4.0


def _near_splice_pct(ns, sd):
    """Percent of same-fibre shot pairs that differ by at least `sd` two-shot
    sd, read off the engine's empirical tail (linear between 0.25 sd steps)."""
    tail = (ns or {}).get('tail') or []
    if not tail:
        return None
    if sd <= tail[0][0]:
        return float(tail[0][1])
    for (k0, p0), (k1, p1) in zip(tail[:-1], tail[1:]):
        if sd <= k1:
            return float(p0 + (sd - k0) / (k1 - k0) * (p1 - p0))
    return float(tail[-1][1])


def _near_splice_lookup(ns, token):
    """What the tech typed -> (file, splice loss) or (None, reason).  Accepts a
    fibre number (uses the runner's unique fibre-number map) or a file name."""
    token = str(token or '').strip()
    loss = (ns or {}).get('loss') or {}
    if not token:
        return None, None
    if token in loss:
        return token, loss[token]
    if token.isdigit():
        stem = ((ns or {}).get('fibres') or {}).get(str(int(token)))
        if stem and stem in loss:
            return stem, loss[stem]
        return None, f'Fiber {int(token)} has no splice reading in this folder.'
    hits = [n for n in loss if token.lower() in n.lower()]
    if len(hits) == 1:
        return hits[0], loss[hits[0]]
    return None, f'"{token}" matches {len(hits)} files; type the fiber number or the full name.'


def _pct_text(pct):
    if pct is None:
        return ''
    return 'under 0.01%' if pct < 0.01 else f'{pct:.2f}%'


def _near_splice_check(ns, a, b):
    """Plain-language answer for two fibres: {'ok', 'cleared', 'sd', 'text'}."""
    fa, la = _near_splice_lookup(ns, a)
    fb, lb = _near_splice_lookup(ns, b)
    if fa is None or fb is None:
        why = (la if fa is None else lb) or 'Enter two fibers.'
        return {'ok': False, 'cleared': False, 'sd': None, 'text': why}
    if fa == fb:
        return {'ok': False, 'cleared': False, 'sd': None,
                'text': 'That is the same file twice.'}
    d = abs(la - lb)
    sd = d / ns['sd_pair_db']
    clear = ns.get('clear_sd') or _NEAR_SPLICE_CLEAR_SD_DEFAULT
    pct = _near_splice_pct(ns, sd)
    head = (f'{fa} reads {la:+.3f} dB and {fb} reads {lb:+.3f} dB, a difference of '
            f'{d:.3f} dB, {sd:.1f}x the wobble')
    if sd > clear:
        return {'ok': True, 'cleared': True, 'sd': sd,
                'text': (f'**Different fibers.** {head}. Two shots of one fiber differ '
                         f'this much {_pct_text(pct)} of the time.')}
    # Always give the rate.  At 3.9x "the splices match" is not what the number
    # says: shots of one fibre differ that much about 1 time in 400 on Goodland.
    return {'ok': True, 'cleared': False, 'sd': sd,
            'text': (f'**Not cleared.** {head}. Two shots of one fiber differ this much '
                     f'{_pct_text(pct)} of the time; the line for calling them different '
                     f'fibers is {clear:g}x. A close reading would not make them '
                     f'duplicates either: many different fibers have similar splices.')}


def _render_near_splice(res):
    """One line about the splice, and the two-fibre check.  Nothing when the
    engine abstained (no manifest key)."""
    for ns in res.get('near_splice') or []:
        if not isinstance(ns, dict) or not ns.get('loss'):
            continue
        clear = ns.get('clear_sd') or _NEAR_SPLICE_CLEAR_SD_DEFAULT
        st.info(f"This span has a splice {ns['offset_m']:.0f} m behind the panel. It is "
                f"glass, so unplugging and re-plugging cannot change it: two shots of one "
                f"fiber read it within {ns['sd_pair_db']:.3f} dB. Two files that read it "
                f"more than {clear:g}x that far apart are different fibers.")
        st.markdown('**Check Two Fibers**')
        key = f"ns_check_{ns.get('group', 'report')}"
        c1, c2 = st.columns(2)
        a = c1.text_input('Fiber', key=key + '_a', placeholder='e.g. 350')
        b = c2.text_input('Other Fiber', key=key + '_b', placeholder='e.g. 351')
        if a and b:
            r = _near_splice_check(ns, a, b)
            if not r['ok']:
                st.warning(r['text'])
            elif r['cleared']:
                st.success(r['text'])
            else:
                st.markdown(r['text'])


def _render_fill_ins(res):
    """Fibres skipped in the run and shot later: where the port had to be found
    again.  A place to check the port log, not a duplicate finding."""
    runs = [r for r in (res.get('fill_ins') or []) if isinstance(r, dict)]
    if not runs:
        return
    from datetime import datetime, timezone
    n = sum(len(r.get('names') or []) for r in runs)

    def _t(x):
        return datetime.fromtimestamp(float(x), timezone.utc).strftime('%m-%d %H:%M')
    with st.expander(f'Shot Out of Order: {n} Fiber(s) Skipped and Shot Later'):
        st.caption('Each was shot long after both neighboring fibers, which were shot '
                   'back to back, so its port had to be found again. Worth checking '
                   'against the port log. Not a duplicate finding.')
        for r in runs:
            st.write(f"**{', '.join(r.get('names') or [])}**: shot {_t(r['shot_at'])}, "
                     f"{r.get('minutes_later', 0) / 60:.1f} h after {r.get('before')} "
                     f"({_t(r['before_at'])}) and {r.get('after')} ({_t(r['after_at'])})")


def _splice_cell(p, clear_sd):
    """The mating table's Splice column for one pair."""
    sd = p.get('splice_sd')
    style = 'padding:4px 10px;border:1px solid var(--otdr-line-soft);text-align:right'
    if sd is None:
        return f"<td style='{style}'></td>"
    if sd > clear_sd:
        return (f"<td style='{style};color:#1e7b34;font-weight:600'>"
                f"different fibers ({sd:.1f}x)</td>")
    return f"<td style='{style};color:var(--otdr-text)'>{sd:.1f}x</td>"


def _render_mating_top(res):
    """The top mating pairs in-page, in EVERY output mode.  Until now the
    ranking reached the tech only in the in-app pairs table or on the
    workbook's last sheet.  Each row deep-links both fibers into the Viewer.
    Renders nothing when the manifest carries no ranking (folder abstained)."""
    from urllib.parse import quote
    top = res.get('mating_top') or []
    if not top:
        return
    folder = res.get('folder') or res.get('_folder') or ''
    ssq = quote(folder, safe='')
    ns_list = [n for n in (res.get('near_splice') or []) if isinstance(n, dict)]
    clear_sd = (ns_list[0].get('clear_sd') if ns_list else None) or _NEAR_SPLICE_CLEAR_SD_DEFAULT
    has_splice = any(p.get('splice_sd') is not None for p in top)
    st.markdown(f"**Mating Likelihood: Top {len(top)} Pairs** "
                "(connector-mating similarity: a ranking to check against the "
                "port log, not a verdict)")
    rows = ['<div style="overflow:auto;max-height:50vh;border:1px solid var(--otdr-edge);'
            'border-radius:4px;color:var(--otdr-text);background:var(--otdr-bg)">',
            '<table style="border-collapse:collapse;font-size:12px;'
            'font-family:Consolas,monospace;width:100%">',
            '<thead><tr>'
            "<th style='padding:5px 10px;border:1px solid var(--otdr-line);background:var(--otdr-panel)'>Rank</th>"
            "<th style='padding:5px 10px;border:1px solid var(--otdr-line);background:var(--otdr-panel);text-align:left'>Pair</th>"
            "<th style='padding:5px 10px;border:1px solid var(--otdr-line);background:var(--otdr-panel)'>Mating Likelihood</th>"
            "<th style='padding:5px 10px;border:1px solid var(--otdr-line);background:var(--otdr-panel)'>Ratio</th>"
            + ("<th style='padding:5px 10px;border:1px solid var(--otdr-line);background:var(--otdr-panel)' "
               "title='How far apart the two files read the splice behind the panel, in "
               "multiples of the two-shot wobble'>Splice</th>" if has_splice else '')
            + '</tr></thead><tbody>']
    for i, p in enumerate(top, 1):
        fa, fb = p.get('fiberA'), p.get('fiberB')
        label = (f"F{fa} ↔ F{fb}" if fa is not None and fb is not None
                 else f"{p.get('fileA')} ↔ {p.get('fileB')}")
        if p.get('viewable') and fa is not None and fb is not None:
            href = f"?nav=viewer&fibers={fa},{fb}&dir=a&ssfolder={ssq}{_panel_qs()}"
            cell = (f"<a href='{href}' target='_self' "
                    f"title='Overlay {p.get('fileA')} + {p.get('fileB')}' "
                    f"style='color:#1a5fb4;text-decoration:none;font-weight:600'>{label}</a>")
        else:
            cell = f"<span title='not viewable: {p.get('reason','')}' style='color:#888'>{label}</span>"
        rows.append(
            "<tr>"
            f"<td style='padding:4px 10px;border:1px solid var(--otdr-line-soft);text-align:center'>{i}</td>"
            f"<td style='padding:4px 10px;border:1px solid var(--otdr-line-soft)'>{cell}</td>"
            f"<td style='padding:4px 10px;border:1px solid var(--otdr-line-soft);text-align:right'>{p['mating_p']*100:.1f}%</td>"
            f"<td style='padding:4px 10px;border:1px solid var(--otdr-line-soft);text-align:right'>{p['mating_lr']:.0f}x</td>"
            + (_splice_cell(p, clear_sd) if has_splice else '')
            + "</tr>")
    rows.append('</tbody></table></div>')
    st.markdown(''.join(rows), unsafe_allow_html=True)


def _render_confidence_caption(res):
    """One line under the result: how much to trust the likelihoods here."""
    for c in (res.get('confidence') or []):
        if isinstance(c, dict) and c.get('note'):
            st.caption(c['note'])


def _render_pairs_report(res):
    """Render the Secret Sauce pair list IN the page — one row per suspected-
    duplicate pair (worst-first), each a link that overlays BOTH fibers in the
    Viewer (?nav=viewer&fibers=A,B&dir=a&ssfolder=…)."""
    from urllib.parse import quote
    folder = res.get('folder') or res.get('_folder') or ''
    pairs = res.get('pairs', [])
    # Defensive cap: the runner now ships only the worst-first top rows, but a
    # cached / older manifest could still carry the full N²/2 list (372k+ on a
    # combined bidirectional folder), which builds a browser-freezing HTML
    # table.  Render at most the top rows; keep the true total in the summary.
    _RENDER_CAP = 500
    n_pairs_total = res.get('n_pairs', len(pairs))
    if len(pairs) > _RENDER_CAP:
        pairs = pairs[:_RENDER_CAP]
    st.success(f"{res.get('n_files','?')} files · {n_pairs_total} pairs · "
               f"{res.get('n_flagged',0)} at ≥50% likelihood.")
    _render_competence_banner(res)
    _render_confidence_caption(res)
    _render_near_splice(res)
    _render_fill_ins(res)
    if res.get('pairs_truncated') or n_pairs_total > len(pairs):
        st.caption(f"Showing the top {len(pairs)} most-likely-duplicate pairs "
                   f"of {n_pairs_total:,} (worst-first); the rest are "
                   f"low-likelihood non-duplicates.")
    st.markdown('###### Click a Pair → Overlay BOTH Fibers in the Viewer')
    if not pairs:
        st.info('No comparable pairs were produced for this folder.')
        return

    ssq = quote(folder, safe='')
    rows = ['<div style="overflow:auto;max-height:62vh;border:1px solid var(--otdr-edge);'
            'border-radius:4px;color:var(--otdr-text);background:var(--otdr-bg)">',
            '<table style="border-collapse:collapse;font-size:12px;'
            'font-family:Consolas,monospace;width:100%">',
            '<thead><tr>'
            "<th style='padding:5px 10px;border:1px solid var(--otdr-line);background:var(--otdr-panel);text-align:left'>Pair</th>"
            "<th style='padding:5px 10px;border:1px solid var(--otdr-line);background:var(--otdr-panel)'>Likelihood</th>"
            "<th style='padding:5px 10px;border:1px solid var(--otdr-line);background:var(--otdr-panel)'>Score σ</th>"
            "<th style='padding:5px 10px;border:1px solid var(--otdr-line);background:var(--otdr-panel)'>Shape r</th>"
            "<th style='padding:5px 10px;border:1px solid var(--otdr-line);background:var(--otdr-panel)' title='Connector-mating similarity: a ranking to check against the port log, not a verdict'>Mating</th>"
            "<th style='padding:5px 10px;border:1px solid var(--otdr-line);background:var(--otdr-panel);text-align:left'>Verdict</th>"
            '</tr></thead><tbody>']
    for p in pairs:
        color = _DUP_COLOR.get(p['verdict'], '#000000')
        fa, fb = p.get('fiberA'), p.get('fiberB')
        label = f"F{fa} ↔ F{fb}"
        if p.get('viewable') and fa is not None and fb is not None:
            href = (f"?nav=viewer&fibers={fa},{fb}&dir=a&ssfolder={ssq}"
                    f"{_panel_qs()}")
            pair_cell = (f"<a href='{href}' target='_self' "
                         f"title='Overlay {p['fileA']} + {p['fileB']}' "
                         f"style='color:#1a5fb4;text-decoration:none;font-weight:600'>"
                         f"{label}</a>")
        else:
            pair_cell = (f"<span title='not viewable: {p.get('reason','')}' "
                         f"style='color:#888'>{label} ⚠</span>")
        pct = f"{p['p_dup']*100:.0f}%"
        r_txt = '-' if p.get('shape_r') is None else f"{p['shape_r']:.3f}"
        m_txt = ('-' if p.get('mating_p') is None
                 else f"{p['mating_p']*100:.1f}% ({p.get('mating_lr', 0):.0f}x)")
        rows.append(
            "<tr>"
            f"<td style='padding:4px 10px;border:1px solid var(--otdr-line-soft)'>{pair_cell}</td>"
            f"<td style='padding:4px 10px;border:1px solid var(--otdr-line-soft);text-align:center;"
            f"font-weight:600;color:{color}'>{pct}</td>"
            f"<td style='padding:4px 10px;border:1px solid var(--otdr-line-soft);text-align:right'>{p['score']:.4f}</td>"
            f"<td style='padding:4px 10px;border:1px solid var(--otdr-line-soft);text-align:right'>{r_txt}</td>"
            f"<td style='padding:4px 10px;border:1px solid var(--otdr-line-soft);text-align:right'>{m_txt}</td>"
            f"<td style='padding:4px 10px;border:1px solid var(--otdr-line-soft);color:{color}'>{p['verdict']}</td>"
            "</tr>")
    rows.append('</tbody></table></div>')
    st.markdown(''.join(rows), unsafe_allow_html=True)
    st.caption('⚠ = both files share a fiber number in this folder (e.g. two '
               'directions), so the Viewer can\'t tell them apart by number.')


def _parse_manifest(stdout):
    """The runner prints exactly one JSON line; take the last JSON-looking line."""
    for line in reversed((stdout or '').strip().splitlines()):
        line = line.strip()
        if line.startswith('{') and line.endswith('}'):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


# ═════════════════════════════════════════════════════════════════════════
#  PAGE: Splice Report (bidirectional)  — grid drives the Viewer
# ═════════════════════════════════════════════════════════════════════════
#  OTDR settings panel — pixel-perfect EXFO threshold table (custom HTML
#  component) + customer-profile dropdown.  Ported verbatim from the
#  standalone Splice Report app.  Only the rows the engine wires through
#  (supported=True) do anything when their Apply checkbox is ticked.
#
#  Unlike the standalone (which mutates the engine module IN-PROCESS), the
#  OTDR Suite runs the splice engine as a SUBPROCESS, so the panel values
#  travel to the engine as a JSON `--overrides` arg (see
#  _overrides_from_settings + splicereport_cmd + run_splicereport.py).
OTDR_ROWS = [
    # (key,                       label,                       fail_default,  unit,    supported)
    ("unidir_splice_loss",        "Unidir. Splice Loss",        0.200,        "dB",    True),
    ("bidir_splice_loss",         "Bidir Splice Loss",          0.160,        "dB",    True),
    ("unidir_connector_loss",     "Connector Loss (1 Direction)", 0.649,      "dB",    True),
    ("bidir_connector_loss",      "Bidir Connector Loss",       0.500,        "dB",    True),
    ("splitter_loss",             "Splitter Loss",              4.500,        "dB",    False),
    ("reflectance",               "Reflectance",                -50.0,        "dB",    True),
    ("reflectance_ceiling",       "Reflectance Ceiling",        0.0,          "dB",    True),
    ("midspan_reflectance",       "Mid-Span Reflectance Band",  -50.0,        "dB",    True),
    # Optional BAND ceiling for the row above: tick it to flag ONLY the
    # band [warn floor, ceiling] — e.g. -80..-40 isolates faint fusion
    # glints while connector-grade reflections stay with the connector
    # rules.  Unticked (default) = no ceiling, shipped behavior.
    ("midspan_refl_ceiling",      "Mid-Span Refl Ceiling",      -40.0,        "dB",    True),
    # NOTE: the launch-connector loss gates used to live here as two rows.
    # They moved to the 'Connector & launch' knobs panel below, which carries
    # per-knob help text and holds the REST of the connector path beside them
    # (re-measure tolerance, search windows, tailbox outlier margin).  One
    # control per engine global — see _CONN_ROWS.
    # Per-FIBER span attenuation: EXFO's stored span loss (the number FR
    # prints as Span Loss) over the stored span length, both directions
    # averaged.  Off by default (0 = off in the engine); the contract profile sets 0.250.
    ("fiber_section_atten",       "Fiber Attenuation",          0.400,        "dB/km", True),
    ("span_loss",                 "Span Loss",                  20.000,       "dB",    False),
    ("span_length",               "Span Length",                0.0000,       "km",    False),
    # ORL FLOOR: the OTDR's own total ORL per direction, from the file; a
    # reading below the value fails.  Not the OLTS ORL a contract names, and
    # the sheet says so.  Off by default; the contract profile sets 30.
    ("span_orl",                  "Span ORL (Floor)",           15.00,        "dB",    True),
    # Bend/damage clusters within this distance of a validated splice column
    # stay IN that splice column (cells keep their bend labels); farther out
    # they get their own "Bends @ X km" column.  Unchecking reverts to the
    # legacy 75 m gate (Platteville-Cheyenne: short-lay fibers put splice
    # events 107-128 m before the column and grew phantom bend columns).
    ("bend_fold_distance",        "Bend Fold Distance",         0.200,        "km",    True),
    # Per-FIBER average splice loss, FastReporter's "Avg. Splice Loss": the
    # signed mean of (A->B + B->A)/2 over every splice either direction
    # recorded.  A per-span statistic, not a per-cell gate, so it grades on
    # its own sheet and never colours the grid.  Off by default (0 = off in
    # the engine); the contract sets it at 0.08 dB.
    ("avg_splice_loss",           "Avg. Splice Loss (per Fiber)", 0.080,      "dB",    True),
]
# Pre-checked rows (match what the splice report flags out of the box):
OTDR_DEFAULT_APPLY = {"unidir_splice_loss", "bidir_splice_loss",
                       "unidir_connector_loss", "bidir_connector_loss", "reflectance",
                       "reflectance_ceiling",
                       "midspan_reflectance", "bend_fold_distance"}

# Rows whose Warning threshold differs from Fail (most rows use a single
# threshold, warning == fail).  Mid-span reflectance is a BAND: Fail at the
# strong end (-50 dB), Warning floor at the weak end (-80 dB).
_OTDR_WARN_DEFAULT = {"midspan_reflectance": -80.0}

# Rows that are really a BAND rather than a fail/warning pair, and the label
# each end carries in the panel.  The values stay in their semantic columns
# — the strong end IS the fail threshold, the weak end IS the warning floor
# — so nothing about the profiles, the key->global maps or the override path
# changes.  What changes is that the panel now SAYS it is a band, which is
# how the engine has described it since the row was introduced (see the
# comment above) and how the unidirectional panel renders its own bands.
#   ("weak end label", "strong end label")
_OTDR_BAND_ROWS = {
    "midspan_reflectance": ("Band Low", "Band High"),
    # Launch/tailbox reflectance reads as a band for the same reason: a
    # connector has an acceptable WINDOW, not a single edge.  -49.9 was
    # calibrated for a fusion-spliced launch pigtail (Tulsa measures -51.8
    # median, 0 of 60 flagged).  A mechanical connector legitimately reflects
    # near -45 — two polished ferrules always leave an index step — so a
    # tie-panel job reads -44.9 across every fiber (Reubensville: 60 fibers
    # inside 0.15 dB) and every one of them trips a fusion-splice threshold.
    # With a band the panel job sets the low end to -40 and only genuinely bad
    # mates flag; FTH's -39.2 outliers still stand out at 12x the floor.
    "reflectance": ("Band Low", "Band High"),
}

# ── Customer threshold profiles ──────────────────────────────────────
# Each entry is a named preset that overrides the per-row 'fail' values
# and 'apply' flags above.  Pick one from the dropdown to switch.  To add
# a new customer, append a dict here — the dropdown picks it up.
CUSTOMER_PROFILES = {
    "Default (engine baseline)": {
        "apply":      set(OTDR_DEFAULT_APPLY),
        "thresholds": {},
    },
    # ── FastReporter3 customer templates (Sep 2026) ────────────────────
    # Source: the customer .prj templates the prime contractor runs FastReporter3 with,
    # forwarded 16 Sep 2026 (FW: FastReporter3 Customer Templates).  Every
    # template applies ONE threshold set to all 16 wavelengths, so each
    # customer is the handful of numbers below.  The mapping is the one the
    # the contract profile established:
    #
    #   FR Splice Loss           -> unidir_splice_loss
    #   FR Bidir Splice Loss     -> bidir_splice_loss
    #   FR Connector Loss        -> conn LAUNCH_CONN_UNI_MIN_DB (either side)
    #   FR Bidir Connector Loss  -> bidir_connector_loss AND
    #                               conn LAUNCH_CONN_AVG_MIN_DB ((A+B)/2)
    #   FR Reflectance           -> reflectance (signed; less negative fails)
    #   FR Span ORL              -> span_orl (floor; every template applies it)
    #   FR Fiber Section Atten.  -> off (Apply=False in every template)
    #   FR Span Loss / Length    -> nothing (rows unsupported); Splitter off
    #
    # The FAIL value is what grades: the grid is flag-or-blank, with no
    # review tier, so a template's separate Warning value is recorded in the
    # comment and not wired.  Two template switches have no engine control
    # and are noted per customer only: IncludeSpanEnd=False (the far-end
    # event is left out of FR's table) and the Macrobend tolerance pairs
    # (1310/1550, 1310/1490, 1490/1550 at 0.5 dB in every template).
    #
    # customer L and customer Z existed before these templates arrived; their previous
    # hand-set values are kept in the comment so the change is visible.
    "Lumen": {
        # FR: splice warn 0.15 / fail 0.25, bidir splice 0.15, connector
        # 0.5, bidir connector 0.5, reflectance -50, ORL 30, span end kept.
        # Before 2026-09-16: bidir 0.120, unidir 0.200, bidir conn 0.400.
        # Bidir splice 0.160, not the template's 0.15 (Robert 2026-09-26).
        "apply":      {"unidir_splice_loss", "bidir_splice_loss",
                        "bidir_connector_loss", "reflectance",
                        "reflectance_ceiling",
                        "midspan_reflectance", "bend_fold_distance",
                        "span_orl"},
        "thresholds": {
            "unidir_splice_loss":    0.250,
            "bidir_splice_loss":     0.160,
            "bidir_connector_loss":  0.500,
            "reflectance":          -50.0,
            "span_orl":             30.0,
        },
        "conn": {"LAUNCH_CONN_UNI_MIN_DB": 0.50,
                 "LAUNCH_CONN_AVG_MIN_DB": 0.50},
    },
    "Zayo": {
        # FR: splice 0.3, bidir splice 0.1, connector 0.5, bidir connector
        # 0.5, reflectance -50, ORL 30, IncludeSpanEnd=False.
        # Before 2026-09-16: bidir 0.200, bidir conn 0.600, unidir and
        # reflectance rows unticked.
        "apply":      {"unidir_splice_loss", "bidir_splice_loss",
                        "bidir_connector_loss", "reflectance",
                        "reflectance_ceiling",
                        "midspan_reflectance", "bend_fold_distance",
                        "span_orl"},
        "thresholds": {
            "unidir_splice_loss":    0.300,
            "bidir_splice_loss":     0.100,
            "bidir_connector_loss":  0.500,
            "reflectance":          -50.0,
            "span_orl":             30.0,
        },
        "conn": {"LAUNCH_CONN_UNI_MIN_DB": 0.50,
                 "LAUNCH_CONN_AVG_MIN_DB": 0.50},
    },
    "AT&T": {
        # FR: splice warn 0.5 / fail 0.75, bidir splice 0.3, connector 0.5,
        # bidir connector 0.5, reflectance -40, ORL 30, span end kept.
        "apply":      {"unidir_splice_loss", "bidir_splice_loss",
                        "bidir_connector_loss", "reflectance",
                        "reflectance_ceiling",
                        "midspan_reflectance", "bend_fold_distance",
                        "span_orl"},
        "thresholds": {
            "unidir_splice_loss":    0.750,
            "bidir_splice_loss":     0.300,
            "bidir_connector_loss":  0.500,
            "reflectance":          -40.0,
            "span_orl":             30.0,
        },
        "conn": {"LAUNCH_CONN_UNI_MIN_DB": 0.50,
                 "LAUNCH_CONN_AVG_MIN_DB": 0.50},
    },
    "AT&T (Fusion)": {
        # FR: splice 0.3, bidir splice 0.3, connector warn 0.5 / fail 0.75,
        # bidir connector 0.5, reflectance -40, ORL 29, IncludeSpanEnd=False.
        # Only template with a 4th macrobend pair: 1550/1625 at 0.3 dB.
        "apply":      {"unidir_splice_loss", "bidir_splice_loss",
                        "bidir_connector_loss", "reflectance",
                        "reflectance_ceiling",
                        "midspan_reflectance", "bend_fold_distance",
                        "span_orl"},
        "thresholds": {
            "unidir_splice_loss":    0.300,
            "bidir_splice_loss":     0.300,
            "bidir_connector_loss":  0.500,
            "reflectance":          -40.0,
            "span_orl":             29.0,
        },
        "conn": {"LAUNCH_CONN_UNI_MIN_DB": 0.75,
                 "LAUNCH_CONN_AVG_MIN_DB": 0.50},
    },
    "AT&T (Rotary)": {
        # FR: splice 0.3, bidir splice 0.5, connector 1.0, bidir connector
        # 0.75, reflectance -27, ORL 27, IncludeSpanEnd=False.  The loosest
        # template of the set (rotary/mechanical splicing).
        "apply":      {"unidir_splice_loss", "bidir_splice_loss",
                        "bidir_connector_loss", "reflectance",
                        "reflectance_ceiling",
                        "midspan_reflectance", "bend_fold_distance",
                        "span_orl"},
        "thresholds": {
            "unidir_splice_loss":    0.300,
            "bidir_splice_loss":     0.500,
            "bidir_connector_loss":  0.750,
            "reflectance":          -27.0,
            "span_orl":             27.0,
        },
        "conn": {"LAUNCH_CONN_UNI_MIN_DB": 1.00,
                 "LAUNCH_CONN_AVG_MIN_DB": 0.75},
    },
    "Blackfoot": {
        # FR: splice 0.3, bidir splice 0.3, connector 0.5, bidir connector
        # 0.5, reflectance -50, ORL 30, span end kept.
        "apply":      {"unidir_splice_loss", "bidir_splice_loss",
                        "bidir_connector_loss", "reflectance",
                        "reflectance_ceiling",
                        "midspan_reflectance", "bend_fold_distance",
                        "span_orl"},
        "thresholds": {
            "unidir_splice_loss":    0.300,
            "bidir_splice_loss":     0.300,
            "bidir_connector_loss":  0.500,
            "reflectance":          -50.0,
            "span_orl":             30.0,
        },
        "conn": {"LAUNCH_CONN_UNI_MIN_DB": 0.50,
                 "LAUNCH_CONN_AVG_MIN_DB": 0.50},
    },
    "BrightSpeed": {
        # FR: identical to customer L's template (splice warn 0.15 / fail 0.25,
        # bidir splice 0.15, connectors 0.5, reflectance -50, ORL 30).
        "apply":      {"unidir_splice_loss", "bidir_splice_loss",
                        "bidir_connector_loss", "reflectance",
                        "reflectance_ceiling",
                        "midspan_reflectance", "bend_fold_distance",
                        "span_orl"},
        "thresholds": {
            "unidir_splice_loss":    0.250,
            "bidir_splice_loss":     0.150,
            "bidir_connector_loss":  0.500,
            "reflectance":          -50.0,
            "span_orl":             30.0,
        },
        "conn": {"LAUNCH_CONN_UNI_MIN_DB": 0.50,
                 "LAUNCH_CONN_AVG_MIN_DB": 0.50},
    },
    "Ciena (RAMAN)": {
        # FR: splice warn 0.25 / fail 0.8, bidir splice warn 0.3 / fail 0.8,
        # connector warn 0.5 / fail 0.8, bidir connector 0.5, reflectance
        # warn -50 / fail -33, ORL warn 29 / fail 27, IncludeSpanEnd=False.
        # Widest warn-to-fail gaps of the set; the fail values grade here.
        "apply":      {"unidir_splice_loss", "bidir_splice_loss",
                        "bidir_connector_loss", "reflectance",
                        "reflectance_ceiling",
                        "midspan_reflectance", "bend_fold_distance",
                        "span_orl"},
        "thresholds": {
            "unidir_splice_loss":    0.800,
            "bidir_splice_loss":     0.800,
            "bidir_connector_loss":  0.500,
            "reflectance":          -33.0,
            "span_orl":             27.0,
        },
        "conn": {"LAUNCH_CONN_UNI_MIN_DB": 0.80,
                 "LAUNCH_CONN_AVG_MIN_DB": 0.50},
    },
    "Intermountain (FR template)": {
        # FR: splice 0.2, bidir splice 0.08, connector 0.3, bidir connector
        # 0.3, reflectance -55, ORL 30, span end kept.
        # NOT the same numbers as the contract profile below: the template
        # grades every bidir splice at 0.08 and connectors at 0.30 (the RFP
        # figures), while the contract profile follows the prime contractor's 24 Aug 2026
        # reconciliation (0.20 per splice, 0.08 as the per-fiber AVERAGE,
        # 0.50 connectors per the executed SOW).  Pick the contract profile
        # for MT.1085 deliverables; this one reproduces the FR template.
        "apply":      {"unidir_splice_loss", "bidir_splice_loss",
                        "bidir_connector_loss", "reflectance",
                        "reflectance_ceiling",
                        "midspan_reflectance", "bend_fold_distance",
                        "span_orl"},
        "thresholds": {
            "unidir_splice_loss":    0.200,
            "bidir_splice_loss":     0.080,
            "bidir_connector_loss":  0.300,
            "reflectance":          -55.0,
            "span_orl":             30.0,
        },
        "conn": {"LAUNCH_CONN_UNI_MIN_DB": 0.30,
                 "LAUNCH_CONN_AVG_MIN_DB": 0.30},
    },
    "Meta": {
        # FR: splice 0.25, bidir splice 0.25, connectors 0.5, reflectance
        # -50, ORL 30, span end kept.
        "apply":      {"unidir_splice_loss", "bidir_splice_loss",
                        "bidir_connector_loss", "reflectance",
                        "reflectance_ceiling",
                        "midspan_reflectance", "bend_fold_distance",
                        "span_orl"},
        "thresholds": {
            "unidir_splice_loss":    0.250,
            "bidir_splice_loss":     0.250,
            "bidir_connector_loss":  0.500,
            "reflectance":          -50.0,
            "span_orl":             30.0,
        },
        "conn": {"LAUNCH_CONN_UNI_MIN_DB": 0.50,
                 "LAUNCH_CONN_AVG_MIN_DB": 0.50},
    },
    "Microsoft": {
        # FR: splice 0.25, bidir splice 0.25, connectors 0.5, reflectance
        # -50, ORL 29, IncludeSpanEnd=False.
        "apply":      {"unidir_splice_loss", "bidir_splice_loss",
                        "bidir_connector_loss", "reflectance",
                        "reflectance_ceiling",
                        "midspan_reflectance", "bend_fold_distance",
                        "span_orl"},
        "thresholds": {
            "unidir_splice_loss":    0.250,
            "bidir_splice_loss":     0.250,
            "bidir_connector_loss":  0.500,
            "reflectance":          -50.0,
            "span_orl":             29.0,
        },
        "conn": {"LAUNCH_CONN_UNI_MIN_DB": 0.50,
                 "LAUNCH_CONN_AVG_MIN_DB": 0.50},
    },
    # ── Contract customer profile ────────
    # Sources: RFP-FOT-2025-001 (issued 09 Jul 2026) and the Zero DB SOW
    # (DocuSigned 06 Aug 2026), as reconciled by the prime contractor on
    # 24 Aug 2026.  Only the three rows below are contract thresholds the
    # engine can grade on:
    #
    #   Bidir splice loss     <= 0.20 dB  — both documents agree.  NOTE this
    #     is LOOSER than the engine baseline (0.160), so this profile flags
    #     FEWER splice cells than Default, by contract.
    #   Bidir connector loss  <= 0.50 dB  — the executed SOW governs.  The
    #     RFP says 0.30, but the SOW incorporates it nowhere and the prime contractor's
    #     counterparty is Zero DB.  On Span 29 the choice is 3 failures at
    #     0.50 against 152 at 0.30, which is why the Legend sheet prints the
    #     value actually applied.  0.500 is also today's engine baseline, so
    #     the row is a no-op now — stated anyway so the profile still means
    #     0.50 if that baseline ever moves.
    #   Connector reflectance <= -55 dB   — both documents agree.  SIGNED: a
    #     LESS negative reading fails (-49 fails, -70 passes).  Inert on this
    #     job (observed population runs -58 to -90) but correct by contract.
    #
    # Unidir. splice loss stays ON at the engine default: the contract sets
    # no single-direction threshold, and unticking the row would HIDE events
    # rather than grade them differently.
    #
    #   Average splice loss   <= 0.08 dB  — per FIBER, FastReporter's own
    #     "Avg. Splice Loss" definition (union of both directions' splices,
    #     signed mean of the per-splice averages).  Graded on its own sheet,
    #     never on a grid cell.  Validated against FR's exports on 1152
    #     fibers: unbiased, median 1 mdB.
    #
    #   Fiber attenuation     <= 0.250 dB/km — per fiber, EXFO's stored span
    #     loss (FR's "Span Loss", exact on 1152 fibers) over the stored span
    #     length, both directions averaged.  Own sheet.
    #   ORL                   >= 30 dB     — the OTDR's own total ORL per
    #     direction, graded as a FLOOR and labelled as OTDR ORL, because the
    #     contract's figure is the OLTS measurement.  Same sheet.
    #
    # Deliberately absent because no engine global grades them: span loss,
    # span length and splitter loss (rows exist in OTDR_ROWS but are
    # supported=False and reach nothing), and OLTS, PMD
    # and CD, which are not OTDR measurements at all.  (G.652 also specifies
    # PMD statistically, so a per-fiber PMD pass/fail column would
    # misrepresent the spec.)
    "AWS / IIG MT.1085": {
        "apply":      {"unidir_splice_loss", "bidir_splice_loss",
                        "bidir_connector_loss", "reflectance",
                        "reflectance_ceiling",
                        "midspan_reflectance", "bend_fold_distance",
                        "avg_splice_loss", "fiber_section_atten", "span_orl"},
        "thresholds": {
            "bidir_splice_loss":     0.200,
            "bidir_connector_loss":  0.500,
            "reflectance":          -55.0,
            "avg_splice_loss":       0.080,
            "fiber_section_atten":   0.250,
            "span_orl":             30.0,
        },
        # Connector & launch knobs (see _CONN_ROWS).  The ONE-SIDED connector
        # gate is off for this customer.  Every span shows a large one-sided
        # offset at the panel — Span 29 Musselshell -0.503 dB, Span 17
        # Frenchtown -0.428, Span 27 Lavina -0.554 — where the far-end tech
        # reads the connection high and the near-end tech reads a GAINER.
        # Neither contamination nor a bad connector can produce a gainer;
        # what remains is a backscatter / mode-field mismatch between the
        # 200 um span fiber and the launch reel, which is what the RFP's own
        # 200 um launch-cable clause is about.  One-sided against
        # bidirectional counts run 26->2, 154->7 and 18->2 on those spans.
        #
        # This is a real trade, not a free filter: the gate exists for
        # genuine one-sided failures (Defuniak F34 B=1.090, F98 B=1.108,
        # where the min and average gates flagged 0 of 144).  The
        # BIDIRECTIONAL connector gate keeps running, so a connector both
        # directions see as bad still flags — and the tech can type 0.65 back
        # into 'Connector loss (1 direction)' to restore it for a run.
        # The contract's connector gate is the BIDIRECTIONAL AVERAGE at 0.50
        # dB (Span 29 F97 near 0.480 / far 0.589 = 0.535, F108 0.503 / 0.755
        # = 0.629, both failures in the prime contractor's own review).  The min gate at 0.62
        # misses both, so the average gate runs beside it at the contract
        # value.
        "conn": {"LAUNCH_CONN_UNI_MIN_DB": 0.0,
                 "LAUNCH_CONN_AVG_MIN_DB": 0.50},
        # The contract's own figures for the cable (RFP §1: Bend Bright
        # XS-200, group index 1.467, backscatter -81.4 dB, both wavelengths
        # acquired).  Checked by the acquisition audit and REPORTED, never
        # used to change a measurement: FastReporter reads the file's own
        # IOR, and matching FR is the north star.  An OTDR configured to
        # 1.4700 on this 1.467 glass puts every event ~148 m long at
        # 72.6 km, which is what the audit row is there to catch.
        "contract": {"name": "AWS / IIG MT.1085", "ior": 1.467,
                     "backscatter_db": -81.4,
                     "wavelengths_nm": [1550.0, 1625.0],
                     "graded_nm": 1550.0,
                     # Span lengths on this job (spec sheet §1).  A trace
                     # outside this range is the wrong span, a trace that
                     # ends early, or an IOR that stretched the distance.
                     "span_km_range": [64.8, 72.6]},
        # Engine settings the threshold table has no row for.  Grade at
        # 1550 nm -- the prime contractor's ruling of 22 Aug 2026: splice loss falls with
        # wavelength, so 1550 is always the worse wavelength for a real
        # splice, and an event worse at 1625 is carrying bend loss rather
        # than splice loss.  Both wavelengths are still delivered; only
        # which one is GRADED changes.  Whitelisted in _PROFILE_ENGINE_KEYS;
        # anything else written here is ignored.
        # The cable is 18 buffer tubes of 24 fibers (spec sheet §1), so the
        # grid groups fibers by 24 and its tube letters match the cable.  The
        # engine default of 12 would show 36 half-tubes.
        # This customer's traces are iOLM exports shot straight from the
        # panel (Span 29, Ingomar-Musselshell): a broken fiber carries no
        # end-of-fiber marker, the panel connector is the 0 km event, and the
        # iOLM picks its own acquisition time per fiber.  Three engine
        # switches, each off for every other profile, handle that.
        # The contract line reads "0.20 dB or less", so the splice gate is a
        # strict > on the unrounded loss: exactly 0.200 passes and 0.2005
        # fails even though it prints ".200" (the prime contractor's 2026-09-12 ruling,
        # matched against their Span 17/19/25/27 reviews).
        "engine": {"GRADE_WAVELENGTH_NM": 1550.0, "RIBBON_SIZE": 24,
                   "IOLM_END_FALLBACK": 1, "PANEL_CONN_DIRECT": 1,
                   "FQA_DURATION_TAG": 0, "SPLICE_STRICT_BOUNDARY": 1,
                   "SITE_NAMES_FROM_IDENTIFIERS": 1,
                   "BREAK_LOSS_DB": 5.0,
                   "PANEL_UNGRADEABLE_GAP_DB": 0.45,
                   "PIGTAIL_SPLICE_WINDOW_M": 50.0,
                   "ONE_SIDED_TRUST_STORED": 1},
    },
    # ── FEC tech styles (Robert 2026-10-01) ──────────────────────────
    # Two techs' FEC OOS lists on one span.  Both grade the panel
    # connector plus any event within 150 m behind it, from the event table,
    # and fail reflectance above -50.0.  They differ on a loss of exactly
    # 0.500: tech A fails it, tech C passes it.  Only the Splice Report
    # FEC tool reads the "fec" block; every other tool runs these two at the
    # engine baseline, same as Default.
    "A": {
        "apply":      set(OTDR_DEFAULT_APPLY),
        "thresholds": {},
        "fec": {"FEC_LOSS_GATE": 0.500, "FEC_LOSS_STRICT": 0,
                "FEC_REFL_GATE": -50.0, "FEC_COMBINE_M": 150.0},
    },
    "C": {
        "apply":      set(OTDR_DEFAULT_APPLY),
        "thresholds": {},
        "fec": {"FEC_LOSS_GATE": 0.500, "FEC_LOSS_STRICT": 1,
                "FEC_REFL_GATE": -50.0, "FEC_COMBINE_M": 150.0},
    },
    "Custom (edit table below)": {  # sentinel — uses session edits as-is
        "apply":      None,
        "thresholds": None,
    },
}

# Maps each supported OTDR-panel row key → the engine module global it
# overrides.  This is the standalone's _apply_overrides mapping, encoded
# as a table so it can be applied across the subprocess boundary.
_OTDR_KEY_TO_ENGINE_GLOBAL = {
    "bidir_splice_loss":    "REBURN_THRESHOLD",
    "unidir_splice_loss":   "SINGLE_DIR_THRESHOLD",
    "unidir_connector_loss": "LAUNCH_CONN_UNI_MIN_DB",
    "bidir_connector_loss": "BIDIR_CONNECTOR_LOSS",
    "reflectance":          "LAUNCH_BAD_REFL_DB",
    "reflectance_ceiling":  "LAUNCH_REFL_CEIL_DB",
    "midspan_reflectance":  "MIDSPAN_REFL_FAIL_DB",
    "midspan_refl_ceiling": "MIDSPAN_REFL_CEIL_DB",
    "bend_fold_distance":   "BEND_SPLICE_FOLD_KM",
    "avg_splice_loss":      "AVG_SPLICE_LOSS_DB",
    "fiber_section_atten":  "FIBER_ATTEN_DB_KM",
    "span_orl":             "SPAN_ORL_MIN_DB",
}
# Rows that ALSO push a separate Warning-threshold global to the engine.
_OTDR_KEY_TO_WARN_GLOBAL = {
    "midspan_reflectance":  "MIDSPAN_REFL_WARN_DB",
}
# Loss rows whose Warning colours the Viewer's event panel ONLY (Robert
# 2026-09-26): a reading at or over Warning but under Fail prints bright
# yellow there.  The report and the uni report never see these -- the engine
# has no such globals, and run_splicereport echoes them to the Viewer
# without applying them -- so the grid stays flag or blank.  Warning equal
# to Fail (every profile's default) sends nothing, and the Viewer is
# unchanged.
_OTDR_KEY_TO_VIEWER_WARN = {
    "bidir_splice_loss":     "REBURN_WARN_DB",
    "unidir_splice_loss":    "SINGLE_DIR_WARN_DB",
    "bidir_connector_loss":  "BIDIR_CONNECTOR_WARN_DB",
    "unidir_connector_loss": "LAUNCH_CONN_UNI_WARN_DB",
}

# Threshold sentinel that turns a detection OFF.  Unchecking a settings row
# sends this in place of the row's threshold; because every panel-controlled
# detection gates at `value >= threshold` (or, for mid-span reflectance, on its
# Warning floor), no real OTDR reading reaches 1e9 dB, so the category stops
# flagging.  Finite and > 0, so it clears run_splicereport's NaN/inf/<=0 guard.
_OTDR_DISABLE_SENTINEL = 1.0e9

# Per-row override for what "unchecked" sends.  Most rows are detections
# gated at `value >= threshold`, so the unreachable sentinel above turns them
# OFF.  Rows that tune a DISTANCE instead (bend fold) would be blown wide
# open by 1e9 ("fold everything") — their off-value is the legacy engine
# behavior instead (75 m = CLOSURE_MATCH_KM, the pre-panel hard-wired gate).
_OTDR_KEY_DISABLE_VALUE = {
    "bend_fold_distance": 0.075,
    # Unticked ceiling = NO ceiling (0.0 sentinel — the engine only applies
    # the band when the value is negative), NOT the 1e9 detection-off value.
    "midspan_refl_ceiling": 0.0,
    # Unticked ceiling = NO ceiling (0.0 sentinel — the engine only applies the
    # band's top when the value is negative), NOT the 1e9 detection-off value.
    "reflectance_ceiling": 0.0,
    # Unticked average-splice gate = OFF in the engine (0 = no sheet, no
    # Legend row).  The 1e9 sentinel would still compute and print a sheet
    # of all-PASS averages for every customer, which is not "off".
    "avg_splice_loss": 0.0,
    # 1-direction connector gate: 0 = off in the engine (Legend prints OFF).
    "unidir_connector_loss": 0.0,
    # Same for the two span gates: 0 = off in the engine.  The ORL row is a
    # FLOOR (below fails), so the 1e9 sentinel would fail every fiber.
    "fiber_section_atten": 0.0,
    "span_orl": 0.0,
}


# ── Connector & launch knobs ─────────────────────────────────────────
# Everything on the launch / box-connector path, in the shared EXFO-styled
# component's 'knobs' mode (same layout the Unidirectional panel uses), so
# each knob can carry help text explaining what it does and why its default
# is what it is.  The EXFO threshold table above stays the EXFO table.
#
# One control per engine global.  A row here must reach a global the engine
# READS AT RUN TIME — `desktop/tests/test_conn_settings_panel.py` pins every
# row to its global and fails if one stops being wired, because a knob that
# renders but changes nothing is worse than no knob at all.
#
# Deliberately NOT exposed, and why:
#   LAUNCH_REFL_OUTLIER_DB, LAUNCH_NO_FIRST_SPLICE_TOL_KM — dead constants.
#     Defined in the engine, read by nothing (verified by grep).  A row for
#     either would be a knob that does nothing.
#   LAUNCH_FIBER_MAX — it is not only the connector search distance; the same
#     constant also sets the mid-span dead zone, the tailbox zone and the
#     reflective-frame shift limit.  A row labelled "connector search
#     distance" that silently moves four other rules would be misleading.
#     Splitting a dedicated connector-search constant out of it is its own
#     change.
_CONN_ROWS = [
    {'key': 'conn_bidi', 'label': 'Connector Loss (Bidirectional)', 'unit': 'dB',
     'kind': 'scalar', 'globals': {'value': 'LAUNCH_CONN_LOSS_MIN_DB'},
     'defaults': {'value': 0.650}, 'min': 0.0, 'max': 5.0, 'step': 0.01,
     'int': False,
     'help': ('Flag a launch/box connector when BOTH directions measure at '
              'least this much loss on it: the gate is min(A, B). Every '
              'mated connector costs real loss, so this sits well above the '
              'population median: BKF↔DEL runs a 0.42 dB median with 405 of '
              '432 fibers over 0.3. 0.65 is the field standard for both '
              'connector gates (the adjudicated set’s bad fibers sit at 0.716 '
              '/ 0.690 / 0.645, the next fiber at 0.587). 0 turns this gate '
              'off.')},

    {'key': 'conn_avg', 'label': 'Connector Loss (Bidirectional Average)', 'unit': 'dB',
     'kind': 'scalar', 'globals': {'value': 'LAUNCH_CONN_AVG_MIN_DB'},
     'defaults': {'value': 0.500}, 'min': 0.0, 'max': 5.0, 'step': 0.01,
     'int': False,
     'help': ('Flag on the connector’s actual loss, (A + B) / 2: the number '
              'the report prints, the number FastReporter reports, and the '
              'number a reviewer hand-types. It runs beside the two gates '
              'above rather than replacing them, so their calibration does not '
              'move. Sacramento↔Suisun F1013 is why it exists: near 0.318 / '
              'far 1.088 averages 0.703, exactly the value the field sheet '
              'carries, but min = 0.318 never reached 0.62. On at 0.50, the '
              'Bidir Connector Loss value, so a connector is flagged when '
              'either direction or its average is over its limit. Cells that '
              'fire only here print the average, without the side marker. '
              '0 turns this gate off.')},

    {'key': 'conn_confirm', 'label': 'Connector Re-measure Tolerance', 'unit': 'dB',
     'kind': 'scalar', 'globals': {'value': 'LAUNCH_CONN_CONFIRM_TOL_DB'},
     'defaults': {'value': 0.050}, 'min': 0.001, 'max': 1.0, 'step': 0.005,
     'int': False,
     'help': ('Before a connector flag is believed, its stored loss is '
              're-derived from the fiber’s own glass and the two must agree '
              'this closely on BOTH sides. Stored-table values have been wrong '
              'often enough to need it (BKF↔DEL’s targets agree to 0.003 dB). '
              'Widen it to trust the table more, tighten it to demand the '
              'trace back every flag. Where the trace cannot be measured at '
              'all the flag stands. A defect is never hidden because the '
              'check could not run.')},

    {'key': 'tailbox_outlier', 'label': 'Tailbox Reflectance Outlier Margin', 'unit': 'dB',
     'kind': 'scalar', 'globals': {'value': 'TAILBOX_OUTLIER_DB'},
     'defaults': {'value': 7.5}, 'min': 0.0, 'max': 30.0, 'step': 0.5,
     'int': False,
     'help': ('On top of clearing the Reflectance threshold, a tailbox has to '
              'read at least this much WORSE than its own direction’s median '
              'before it counts as a defect. The population test is there for '
              'spans shot with no receive jumper, where every fiber shows the '
              'same bare-glass end and would otherwise flag. It was 10.0, '
              'which was too strict to catch anything real: on '
              'Sacramento↔Suisun the only three fibers of 1152 clearing the '
              '-49.9 floor sit +7.95 / +9.20 / +8.40 dB out and were all '
              'dropped, while the field sheet carries every one. 7.5 sits just '
              'under the tightest of the three, and costs nothing: no other '
              'fiber on that span reaches the absolute threshold at all. '
              '0 drops the population test and judges on the threshold alone.')},

    {'key': 'conn_far_window', 'label': 'Far-End Connector Search Window', 'unit': 'km',
     'kind': 'scalar', 'globals': {'value': 'LAUNCH_CONN_FAR_WINDOW_KM'},
     'defaults': {'value': 2.0}, 'min': 0.1, 'max': 10.0, 'step': 0.1,
     'int': False,
     'help': ('How far back from a direction’s own end-of-fiber to hunt for '
              'the OTHER end’s connector, which is what makes the reading '
              'bidirectional. Also sets the window the tailbox reflectance '
              'baseline is drawn from. One launch reel plus slack; widening '
              'it starts pulling real plant near the tail into a connector '
              'rule.')},

    {'key': 'conn_reel_slack', 'label': 'Reel-Length Match Slack', 'unit': 'km',
     'kind': 'scalar', 'globals': {'value': 'LAUNCH_CONN_REEL_SLACK_KM'},
     'defaults': {'value': 0.3}, 'min': 0.01, 'max': 2.0, 'step': 0.01,
     'int': False,
     'help': ('How far the far view’s distance may sit from the measured '
              'launch-reel length and still be judged the SAME connector. The '
              'two directions derive distance with their own IOR, so the two '
              'views never agree exactly. Too tight and the pair is never '
              'formed, so nothing is bidirectional; too loose and a nearby '
              'splice can be mistaken for the far view of the connector.')},

    {'key': 'launch_step_guard', 'label': 'Launch Step Guard', 'unit': 'km',
     'kind': 'scalar', 'globals': {'value': 'LAUNCH_STEP_GUARD_KM'},
     'defaults': {'value': 0.150}, 'min': 0.0, 'max': 2.0, 'step': 0.005,
     'int': False,
     'help': ('An event closer than this to a trace’s own start is treated as '
              'launch-connector skirt rather than plant, because the first '
              'samples after a connector have not settled. It is genuinely '
              'load-bearing: on Sacramento↔Suisun the far ILA sits 0.09 km '
              'into the B frame, so all 329 B-side views of it are suppressed '
              'here. Widening it is NOT the way to recover cells like those: '
              'that is an input problem (the short shots resolve that event; '
              'the long ones merge it into the connector), and loosening the '
              'guard buys the missing cells at the price of connector skirt '
              'reported as plant.')},

    {'key': 'launch_high_loss', 'label': 'Launch Event Loss Rule', 'unit': 'dB',
     'kind': 'scalar', 'globals': {'value': 'LAUNCH_HIGH_LOSS_DB'},
     'defaults': {'value': 0.0}, 'min': 0.0, 'max': 5.0, 'step': 0.01,
     'int': False,
     'help': ('Flag the launch event itself when its own stored loss exceeds '
              'this. Ships OFF (0), by tech direction: the launch end is '
              'judged on reflectance and on the connector gates above, not on '
              'a bare loss reading, because a launch event’s stored loss '
              'includes the backscatter step between two different fibers and '
              'reads high on healthy launches. Set a value only if you want '
              'the old HIGH_LAUNCH_LOSS behavior back.')},
]

_CONN_DEFAULTS = {g: row['defaults'][slot]
                  for row in _CONN_ROWS
                  for slot, g in row['globals'].items()}


def _conn_settings_from_profile(profile_name):
    """Return a fresh conn_settings dict, {engine_global: value}, for the
    named profile: the engine defaults with that profile's connector
    overrides applied on top.

    Profiles carry connector knobs because some customer rules ARE connector
    rules — the contract profile turns the one-sided connector gate off, and a
    profile that could only reach the threshold table would silently keep
    firing it.  Only a profile that declares a "conn" block differs from
    _CONN_DEFAULTS, so every profile that predates this (Default / customer L /
    customer Z) keeps byte-identical connector behavior.

    A global the panel does not render is ignored rather than set, so a typo
    in a profile can never invent a knob or push an unwired constant at the
    engine.
    """
    prof = CUSTOMER_PROFILES.get(profile_name) or {}
    out = dict(_CONN_DEFAULTS)
    for g, v in (prof.get("conn") or {}).items():
        if g not in _CONN_DEFAULTS:
            continue
        try:
            out[g] = float(v)
        except (TypeError, ValueError):
            continue
    return out


# Engine globals a profile's "engine" block may set.  A closed list, not
# "any attribute the engine has": the override channel will setattr whatever
# it is handed, so this is the only thing standing between a typo in a profile
# and a silently changed engine constant.
_PROFILE_ENGINE_KEYS = {"GRADE_WAVELENGTH_NM", "RIBBON_SIZE",
                        # iOLM-export handling, all inert unless a profile
                        # sets them (see the engine's IOLM_END_FALLBACK block).
                        "IOLM_END_FALLBACK", "PANEL_CONN_DIRECT",
                        "FQA_DURATION_TAG",
                        # splice-gate boundary rule (see the engine's
                        # _clears_splice_threshold)
                        "SPLICE_STRICT_BOUNDARY",
                        # name the two ends from the measurements themselves
                        "SITE_NAMES_FROM_IDENTIFIERS",
                        # a loss this big is a break on its own
                        "BREAK_LOSS_DB",
                        # an end whose far readings are the recovery reel
                        "PANEL_UNGRADEABLE_GAP_DB",
                        # the pigtail splice a few metres behind the panel
                        "PIGTAIL_SPLICE_WINDOW_M",
                        # grade a fiber whose other direction was never delivered
                        "ONE_SIDED_TRUST_STORED"}


def _contract_from_profile(profile_name):
    """The active profile's contract figures for the acquisition audit, or
    None.  Reported by the engine, never used to change a measurement."""
    prof = CUSTOMER_PROFILES.get(profile_name) or {}
    con = prof.get("contract")
    return dict(con) if isinstance(con, dict) and con else None


def _engine_extras_from_profile(profile_name):
    """Engine-global overrides a profile sets OUTSIDE the two settings
    panels, e.g. the grading wavelength.  Whitelisted (see
    _PROFILE_ENGINE_KEYS) and numeric only."""
    prof = CUSTOMER_PROFILES.get(profile_name) or {}
    out = {}
    for g, v in (prof.get("engine") or {}).items():
        if g not in _PROFILE_ENGINE_KEYS:
            continue
        try:
            out[g] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def _splicereport_json_reader():
    """The splice-report engine's json_reader, loaded BY PATH.

    `from json_reader import ...` is not safe in this process: the Viewer
    puts its own directory on sys.path and its own json_reader.py (a trace
    parser with no span_site_names) is already in sys.modules by the time
    the Splice Report page runs.  The by-name import then raises, the
    except below swallowed it, and every contract span showed "A" / "B" in the
    site boxes -- the feature shipped in #177 never once ran in the hub.
    Same class of fault as the sor_reader shadowing the tests guard against."""
    import importlib.util
    path = os.path.join(str(SPLICEREPORT_DIR), 'json_reader.py')
    spec = importlib.util.spec_from_file_location('_splicereport_json_reader', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _site_names_for(dir_a, dir_b, profile_name=None):
    """The two end names to put in the site boxes for this folder pair.

    When the active profile turns SITE_NAMES_FROM_IDENTIFIERS on, the
    measurements name their own ends — code and town, in the order the job
    config declares — and the tech types nothing.  The reader stays silent
    unless the files agree with each other, so any doubt falls through to
    the folder-derived ILA names this has always used, and the tech can
    still type over whatever lands in the box.

    The FOLDER NAME is never the source: the contract job's span 27 sits in
    a folder whose two ends are the wrong way round (the prime contractor, 2026-09-12)."""
    if profile_name is None:
        profile_name = st.session_state.get('otdr_profile')
    if _engine_extras_from_profile(profile_name).get(
            'SITE_NAMES_FROM_IDENTIFIERS'):
        try:
            names = _splicereport_json_reader().span_site_names(dir_a, dir_b)
        except Exception as exc:
            names = None            # never block a report on a sidecar read
            report_error('site names from identifiers', exc, {})
            try:
                st.caption("Couldn't read the site names from the measurements "
                           f'({type(exc).__name__}); using the folder names.')
            except Exception:
                pass
        if names:
            return names
    return (_derive_ila(dir_a)[0] or '', _derive_ila(dir_b)[0] or '')


def _conn_settings_state():
    """The committed connector/launch settings, {global: number}."""
    cur = st.session_state.get('conn_settings')
    if not isinstance(cur, dict):
        cur = _conn_settings_from_profile(st.session_state.get('otdr_profile'))
        st.session_state.conn_settings = cur
    # Heal a stored dict from an older build that lacks a newer knob.
    for g, d in _CONN_DEFAULTS.items():
        cur.setdefault(g, d)
    return cur


def _otdr_settings_from_profile(profile_name):
    """Return a fresh otdr_settings dict for the named profile."""
    prof = CUSTOMER_PROFILES.get(profile_name) or {}
    apply_set = prof.get("apply")
    overrides = prof.get("thresholds") or {}
    out = {}
    for key, _, fail_default, _, _ in OTDR_ROWS:
        fail = float(overrides.get(key, fail_default))
        warn = float(_OTDR_WARN_DEFAULT.get(key, fail))
        applied = ((apply_set is not None and key in apply_set)
                   if apply_set is not None
                   else (key in OTDR_DEFAULT_APPLY))
        out[key] = {"apply": applied, "fail": fail, "warning": warn}
    # The 1-direction connector gate moved here from the Connector & Launch
    # panel.  Profiles still declare it in their "conn" block (0 = off), so
    # read it from there: a profile that turned it off keeps it off.
    _uni = (prof.get("conn") or {}).get("LAUNCH_CONN_UNI_MIN_DB")
    if _uni is not None:
        _uni = float(_uni)
        _row = out["unidir_connector_loss"]
        _row["apply"] = _uni > 0
        if _uni > 0:
            _row["fail"] = _row["warning"] = _uni
    return out


def _overrides_from_settings(otdr_settings):
    """Translate the OTDR panel's per-row settings into the engine-global
    overrides dict that crosses the subprocess boundary.

    The Apply checkbox is a real ON/OFF switch for the detection:

      * TICKED  → send the row's Fail (and, for a band row, Warning) threshold,
        so the detection runs at the tech's value.
      * UNTICKED → DISABLE that detection entirely.  We send a sentinel
        threshold (`_OTDR_DISABLE_SENTINEL`) that no real OTDR reading can
        reach, so the engine stops flagging that category.  (Before this, an
        unticked row was simply omitted, which reverted the engine to its
        BUILT-IN default threshold — the detection still fired.  That was the
        boss's bug: unchecking 'Unidir. splice loss' still reported.)

    Every panel-controlled detection gates at `value >= threshold` at its
    reporting point (mid-span reflectance gates on its Warning FLOOR, which we
    also sentinel), so a huge finite threshold cleanly disables each one WITHOUT
    touching the engine.  The sentinel is finite and > 0, so it clears
    run_splicereport's NaN/inf/<=0 override guard (incl. REBURN_THRESHOLD's
    positive check).

    Byte-identical baseline: the Default profile ticks all five mapped rows at
    their engine-default values, so its overrides are the engine defaults and
    the report is unchanged.  Only an explicitly UNticked mapped row differs
    from today (it now disables instead of reverting to default — e.g. the customer Z
    profile leaves unidir splice loss + launch reflectance off).
    """
    # No table at all (the panel failed to draw and its slot was dropped) is
    # NOT "every row unticked": that sent the off-sentinels, and the report
    # flagged nothing while the page said "default thresholds".  The Splice
    # Report no longer runs without the table; this keeps any other caller
    # from switching every gate off by accident.
    if not isinstance(otdr_settings, dict):
        return {}
    out = {}
    settings = otdr_settings
    for row_key, engine_global in _OTDR_KEY_TO_ENGINE_GLOBAL.items():
        row = settings.get(row_key) or {}
        # Rows with a distinct Warning threshold (e.g. mid-span reflectance's
        # -80 floor) drive a second engine global alongside Fail.
        warn_global = _OTDR_KEY_TO_WARN_GLOBAL.get(row_key)
        if row.get("apply"):
            if row.get("fail") is not None:
                out[engine_global] = float(row["fail"])
            if warn_global and row.get("warning") is not None:
                out[warn_global] = float(row["warning"])
            # Viewer-only Warning: sent only when it opens a real band
            # below Fail, so an untouched row adds nothing to the run.
            viewer_warn = _OTDR_KEY_TO_VIEWER_WARN.get(row_key)
            try:
                _w, _f = float(row.get("warning")), float(row.get("fail"))
            except (TypeError, ValueError):
                _w = _f = None
            if viewer_warn and _w is not None and 0 < _w < _f:
                out[viewer_warn] = _w
        else:
            # OFF → sentinel the gate global(s) so the detection never fires.
            # Distance-tuning rows (see _OTDR_KEY_DISABLE_VALUE) send their
            # legacy-behavior value instead — 1e9 would invert their meaning.
            off_val = _OTDR_KEY_DISABLE_VALUE.get(row_key, _OTDR_DISABLE_SENTINEL)
            out[engine_global] = off_val
            if warn_global:
                out[warn_global] = off_val
    return out


# Profiles that are not a customer: no check mark, no greyed Settings name.
_NOT_CUSTOMERS_PROFILES = ('Default (engine baseline)', 'Custom (edit table below)')


def _profile_settings_changed():
    """True when the threshold table or the connector knobs differ from the
    chosen customer's own values (the tech edited them)."""
    ss = st.session_state
    prof = ss.get('otdr_profile')
    cur = ss.get('otdr_settings')
    if not prof or not isinstance(cur, dict):
        return False

    def diff(a, b):
        try:
            return abs(float(a) - float(b)) > 1e-9
        except (TypeError, ValueError):
            return a != b
    for key, base in _otdr_settings_from_profile(prof).items():
        c = cur.get(key)
        if not isinstance(c, dict):
            continue
        if bool(c.get('apply')) != bool(base['apply']) or diff(c.get('fail'), base['fail']) \
                or diff(c.get('warning'), base['warning']):
            return True
    cc = ss.get('conn_settings')
    if isinstance(cc, dict):
        for g, d in _conn_settings_from_profile(prof).items():
            if g in cc and diff(cc[g], d):
                return True
    return False


def _render_customer_profile_picker(compact=False):
    """The Customer profile dropdown, on its own above the A/B boxes.

    Robert, 2026-09-16: a tech chooses default or customer settings BEFORE
    selecting A and B.  It used to sit inside the OTDR settings expander
    (which stays where it is, below); the dropdown alone moved up.  Same
    state, same reload-on-change: session_state.otdr_profile drives the
    settings table and the connector knobs exactly as before.

    `compact` (the Viewer page, window-size audit 2026-10-01): the heading
    becomes a bold label beside the dropdown, in the row the caller set up,
    so the Viewer frame starts higher up the page.
    """
    # Initialise persisted settings + active profile on first run.
    if 'otdr_profile' not in st.session_state:
        st.session_state.otdr_profile = next(iter(CUSTOMER_PROFILES))
    if 'otdr_settings' not in st.session_state:
        st.session_state.otdr_settings = _otdr_settings_from_profile(
            st.session_state.otdr_profile)
    # ── Customer profile dropdown ─────────────────────────────────
    # Robert, 2026-09-24: more prominent -- larger letters, and only as wide
    # as the longest name instead of the full page width.  The CSS is
    # scoped to this one widget by its key class.
    _big = ('<style>.st-key-otdr_profile_select div[data-baseweb="select"] '
            '{font-size:1.2rem;font-weight:600;}</style>')
    if compact:
        # One element: a style of its own in the Viewer's row took a gap there.
        st.markdown('**Customer profile**' + _big, unsafe_allow_html=True,
                    width='content')
    else:
        st.markdown('#### Select Customer Profile')
        st.markdown(_big, unsafe_allow_html=True)
    _profile_names = list(CUSTOMER_PROFILES.keys())
    # ~11 px a character at 1.2rem semibold, plus the arrow and padding.
    _profile_w = min(700, 11 * max(len(n) for n in _profile_names) + 70)

    # Defensive cleanup: a stale stored profile name (e.g. from a prior
    # deploy whose profile was renamed) would make st.selectbox raise
    # because the saved value isn't in the options list.  Reset to the
    # first profile when the stored name is unknown.
    if st.session_state.get('otdr_profile') not in _profile_names:
        st.session_state.otdr_profile = _profile_names[0]
    if st.session_state.get('otdr_profile_select') not in _profile_names:
        st.session_state.pop('otdr_profile_select', None)

    _cur = st.session_state['otdr_profile']
    # Beside the dropdown (Robert, 2026-09-27): a green check while a customer
    # is chosen and its settings are untouched; an orange X once the tech has
    # changed them.  Default and Custom are not customers: no mark.
    # The dropdown's column is as wide as the dropdown, so the mark sits
    # right beside it.
    _c_sel, _c_mark = st.columns([_profile_w + 16, max(200, 1100 - _profile_w)],
                                 vertical_alignment='center', gap='small')
    _is_customer = _cur not in _NOT_CUSTOMERS_PROFILES
    if _is_customer and _profile_settings_changed():
        _c_mark.markdown(':orange[**✖ Settings changed**] from '
                         f'{_cur.split(" (")[0]}\'s', help='Pick the customer again to '
                         'put their settings back.')
    elif _is_customer:
        _c_mark.markdown(':green[**✅**]', help=f"{_cur}'s settings, unchanged.")
    _picked = _c_sel.selectbox(
        'Customer', _profile_names,
        index=_profile_names.index(_cur),
        format_func=_profile_label,
        label_visibility='collapsed',
        key='otdr_profile_select',
        width=_profile_w,
        help=("Default engine thresholds, or a customer's bundle of Apply / "
              "Fail values for the OTDR settings table further down.  Pick "
              "'Custom' to keep your own manual edits."),
    )
    # If the user just changed the profile, reload the table from that
    # profile's preset (unless they picked 'Custom').
    if _picked != _cur:
        st.session_state.otdr_profile = _picked
        if 'Custom' not in _picked:
            st.session_state.otdr_settings = _otdr_settings_from_profile(_picked)
            # The connector & launch knobs travel with the profile as
            # well — a customer rule that lives on that panel (the contract profile's
            # one-sided connector gate) has to actually arrive when the
            # tech picks the customer.  'Custom' keeps the tech's own
            # edits, exactly as it does for the threshold table above.
            st.session_state.conn_settings = _conn_settings_from_profile(_picked)
        st.rerun()


def _render_otdr_settings_panel(in_expander=True):
    """Render the customer-profile dropdown + the pixel-perfect EXFO OTDR
    settings table (custom HTML component).  Returns the active
    otdr_settings dict (also stored on st.session_state.otdr_settings).

    Iframe-state footgun (carried over from the standalone, see bug #1 in
    components/otdr_settings/index.html): an older build sent the panel's
    values to Python ONLY when the tech clicked 'Apply settings', so a tech
    who typed a Fail value and clicked Generate would silently run with the
    OLD threshold.  The shipped component auto-commits on every checkbox /
    field change, but we DON'T trust that alone — we read the component's
    return value into session_state.otdr_settings here, and the run reads
    the SAME session_state slot (never the raw component return), so the
    values the panel shows are exactly the values that reach the engine.
    """
    # Initialise persisted settings + active profile on first run.
    if 'otdr_profile' not in st.session_state:
        st.session_state.otdr_profile = next(iter(CUSTOMER_PROFILES))
    if 'otdr_settings' not in st.session_state:
        st.session_state.otdr_settings = _otdr_settings_from_profile(
            st.session_state.otdr_profile)

    from components.otdr_settings import otdr_settings as otdr_settings_component

    # One Splice Report settings box (Robert 2026-09-26): the call site
    # opens a single expander around this table and the connector knobs.
    with (st.expander('OTDR Settings (Thresholds)', expanded=False)
          if in_expander else contextlib.nullcontext()):
        # Build the rows definition for the component.  Each row's initial
        # values come from session_state (the user's last-committed
        # settings); supported tells the component to grey 'not yet wired'.
        _rows = [
            {
                'key':       key,
                'label':     label,
                'unit':      unit,
                'supported': supported,
                'initial':   st.session_state.otdr_settings[key],
                # ('low label', 'high label') on a band row, absent otherwise
                'band':      _OTDR_BAND_ROWS.get(key),
                # Greying is driven by the ACTUAL maps, not a hand-kept flag,
                # so a row can never look live while reaching nothing:
                #   wired    — the engine reads this row's Fail at all
                #   warnUsed — the engine reads its Warning, or the
                #              Viewer colours cells between Warning and Fail
                'wired':     key in _OTDR_KEY_TO_ENGINE_GLOBAL,
                'warnUsed':  (key in _OTDR_KEY_TO_WARN_GLOBAL
                              or key in _OTDR_KEY_TO_VIEWER_WARN),
                # an untouched Warning (equal to Fail) moves when Fail does
                'warnFollowsFail': key in _OTDR_KEY_TO_VIEWER_WARN,
            }
            for key, label, _fail, unit, supported in OTDR_ROWS
        ]
        # Rows you can adjust first, greyed 'not wired' rows at the bottom
        # (stable, so each group keeps its own order).
        _rows.sort(key=lambda r: not (r['supported'] and r['wired']))
        # The component key encodes the active profile so switching customers
        # forces a re-mount with the new initial values.
        _commit = otdr_settings_component(
            _rows, default=None,
            key=f"otdr_component::{st.session_state.otdr_profile}",
        )
        if _commit:
            # Component reported its state (auto-commit on edit, or Apply
            # click) — persist to session_state for the run to read.
            import math

            def _finite_or(v, fallback):
                # An Infinity keystroke crosses the JSON bridge as null and a
                # blank field as None; float(None) used to raise HERE → the outer
                # except popped the whole otdr_settings dict → a deliberate
                # customer REBURN_THRESHOLD override SILENTLY reverted to 0.160.
                # Keep the previous committed value on any bad input instead.
                try:
                    f = float(v)
                except (TypeError, ValueError):
                    return fallback
                return f if math.isfinite(f) else fallback

            for key, vals in _commit.items():
                _prev = st.session_state.otdr_settings.get(key, {})
                st.session_state.otdr_settings[key] = {
                    'apply':   bool(vals.get('apply')),
                    'fail':    _finite_or(vals.get('fail'), _prev.get('fail', 0.0)),
                    'warning': _finite_or(vals.get('warning'), _prev.get('warning', 0.0)),
                }

        # Show which thresholds will actually be pushed onto the engine.
        _ov = _overrides_from_settings(st.session_state.otdr_settings)
        # Profile-level engine settings with no row of their own (the grading
        # wavelength) are listed here too, so the caption names everything
        # that will reach the engine, not just what the table can show.
        _ov.update(_engine_extras_from_profile(st.session_state.otdr_profile))
        if _ov:
            st.caption('Active overrides → ' + ', '.join(
                f'{k} = {v:g}' for k, v in sorted(_ov.items())))
        else:
            st.caption('No overrides active. Engine defaults in effect.')

        # ── Cable type → helix factor (manual fallback) ───────────────
        # The helix-calibration tool needs to know the cable construction to
        # pick the AEN-142 sanity band.  On most spans the SOR GenParams
        # cable_code is empty (true on HOWESPAN→LANCASTER), so cable type
        # cannot be auto-detected and must be chosen here.  This is a pure
        # Streamlit selectbox — it does NOT touch the custom HTML component —
        # and persists to st.session_state.cable_type (read by the helix tool;
        # never mixed with the component's auto-commit, per the iframe footgun
        # note above).
        _render_cable_type_select()

    return st.session_state.otdr_settings

def _render_conn_settings_panel(in_expander=True):
    """Connector & launch knobs, in the shared component's 'knobs' mode.
    Returns {engine_global: number} for splicereport_cmd's --overrides.

    Same iframe-state discipline as the panel above: the component
    auto-commits on every edit, but the return value is read into
    session_state HERE and the run reads the SAME slot, so what the panel
    shows is what reaches the engine.
    """
    import math          # module-local, matching _render_otdr_settings_panel
    from components.otdr_settings import otdr_settings as otdr_settings_component

    cur = _conn_settings_state()

    with (st.expander('Connector & Launch Settings', expanded=False)
          if in_expander else contextlib.nullcontext()):
        if not in_expander:
            st.markdown('**Connector & Launch**')
        rows = []
        for row in _CONN_ROWS:
            rows.append({
                'key':       row['key'],
                'label':     row['label'],
                'unit':      row['unit'],
                'supported': True,
                'kind':      row['kind'],
                'initial':   {slot: cur[g] for slot, g in row['globals'].items()},
                'defaults':  dict(row['defaults']),
                'min':       row['min'],
                'max':       row['max'],
                'step':      row['step'],
                'help':      row['help'],
            })
        # The key encodes the active profile for the same reason the
        # threshold table's does: switching customers must re-mount the
        # component with the new initial values, or the iframe keeps showing
        # (and re-committing) the previous customer's knobs.
        commit = otdr_settings_component(
            rows, default=None, mode='knobs',
            key=f"conn_settings_component::{st.session_state.get('otdr_profile', '')}")
        if commit:
            for row in _CONN_ROWS:
                got = commit.get(row['key']) or {}
                for slot, g in row['globals'].items():
                    v = got.get(slot)
                    if v is None:
                        continue          # blank / non-finite: keep committed
                    try:
                        v = int(round(float(v))) if row['int'] else float(v)
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if not (isinstance(v, int) or math.isfinite(v)):
                        continue
                    cur[g] = v
            st.session_state.conn_settings = cur

        changed = {g: v for g, v in cur.items() if v != _CONN_DEFAULTS[g]}
        if changed:
            st.caption('Active overrides: '
                       + ', '.join(f'`{g}` = {v}' for g, v in sorted(changed.items())))
        else:
            st.caption('All connector settings at their defaults.')

    return dict(cur)


# ── The OTDR Settings on every tool page ─────────────────────────────
# Robert, 2026-09-28: "we need to have our OTDR settings available in all of
# our tools except Secret Sauce".  The Customer profile dropdown and the
# Settings box are drawn on the Viewer, Splice Report and Unidirectional
# pages; Secret Sauce draws neither.  All three pages read and write the
# SAME session slots (otdr_profile, otdr_settings, conn_settings), so a
# profile picked on one tool is the profile on all of them.

def _report_overrides():
    """Every engine override the OTDR Settings hold right now, as one
    {engine_global: value} dict: the threshold table, the Connector & Launch
    knobs, and the active profile's engine settings that have no row of
    their own.  The Splice Report's run sends it, and so does the Viewer's
    own background run, so the two judge by the same numbers.

    Read out of the committed session_state slots, never the components'
    return values: see the iframe-state note in _render_otdr_settings_panel."""
    overrides = _overrides_from_settings(st.session_state.get('otdr_settings'))
    # Connector/launch knobs ride the SAME --overrides channel.  Absent slot =
    # engine defaults, which is exactly what the panel shows.
    _conn = st.session_state.get('conn_settings')
    if isinstance(_conn, dict):
        overrides.update({g: v for g, v in _conn.items()
                          if g in _CONN_DEFAULTS})
    # Profile-level engine settings with no panel row (grading wavelength).
    # Most profiles declare none, so their runs are byte-identical to before.
    overrides.update(_engine_extras_from_profile(st.session_state.get('otdr_profile')))
    return overrides


# OTDR Settings rows that mean the same thing on a one-direction shot, and
# the Unidirectional engine global each one drives (Robert 2026-09-28):
#   row key -> (which value of the row, Uni engine global)
# An unticked row sends 0, the Uni engine's own "off" for all three: no
# connector flag, no reflectance band, no ceiling.  The Default profile lands
# exactly on the Uni engine's defaults (0.649, -80, 0), so a default Uni run
# is unchanged; a customer profile's one-direction connector gate now reaches
# Uni too.  These three globals left the Unidirectional box, so each still
# has one control.
_OTDR_KEY_TO_UNI_GLOBAL = {
    'unidir_connector_loss': ('fail',    'UNI_CONN_LOSS_DB'),
    # The band's weak end (its Warning column) is the floor the
    # bidirectional report flags from (MIDSPAN_REFL_WARN_DB), and the Uni
    # rule reads the same floor.
    'midspan_reflectance':   ('warning', 'UNI_REFL_FLOOR_DB'),
    'midspan_refl_ceiling':  ('fail',    'UNI_REFL_CEIL_DB'),
}


def _uni_overrides_from_settings(otdr_settings):
    """The Uni engine overrides the OTDR Settings imply, {global: number}.
    A row missing from the dict adds nothing, so the Uni engine keeps its
    own default for it."""
    import math          # module-local, matching _render_otdr_settings_panel
    out = {}
    for key, (slot, g) in _OTDR_KEY_TO_UNI_GLOBAL.items():
        row = (otdr_settings or {}).get(key)
        if not isinstance(row, dict):
            continue
        if not row.get('apply'):
            out[g] = 0.0
            continue
        try:
            v = float(row.get(slot))
        except (TypeError, ValueError):
            continue
        if math.isfinite(v):
            out[g] = v
    return out


def _render_profile_picker_box(where, compact=False):
    """The Customer profile dropdown, guarded: a failure here must not take
    the page down, and the tool runs with the default profile."""
    try:
        _render_customer_profile_picker(compact)
    except Exception as _exc:
        st.warning('Customer profile picker unavailable, running with the '
                   'default profile. (Details sent to support.)')
        report_error(f'{where} — profile picker render', _exc)


def _render_settings_box(where, blocks_report=False):
    """The Settings box: the OTDR threshold table and the Connector & Launch
    knobs in one expander (Robert 2026-09-26).  Each part is guarded on its
    own, and a component failure (path quirk, Streamlit version, an App
    Control block) never takes the page down.  The first failure of either
    part is RETURNED (None when the whole box drew).  A page that
    `blocks_report` (the Splice Report and Unidirectional, Robert 2026-09-28:
    "block it if any part fails", "block uni too") turns its run button off
    on it.  The Viewer, which runs no report, flags nothing instead and says
    so; its traces, events and values still show (Robert 2026-09-28: "Viewer
    shouldn't show any flags if the settings box or connector launch knobs
    fail but it can still show events and values").  A failure on ANY page
    turns the Viewer's flags off, the pop-out's too.

    Rendered BEFORE any folder guard (2026-07-31, Robert's ask): the panel
    needs nothing from the span, and a tech should be able to set customer
    thresholds first and then load data.  Also hands the settings to the
    Viewer (_share_settings_with_viewer).  The keyed container is how
    Edit Settings on the carry-over pop-up finds the box (_open_settings_box)."""
    settings_exc = None
    with st.container(key=SETTINGS_BOX_KEY), st.expander(
            'Settings (Thresholds, Connector & Launch)', expanded=False):
        try:
            _render_otdr_settings_panel(in_expander=False)
        except Exception as _exc:
            if blocks_report:
                st.error('OTDR settings table could not load. The report is '
                         'turned off until it does. (Details sent to support.)')
            else:
                st.warning('OTDR settings table could not load. Until it '
                           'does, the Viewer flags only breaks (a fiber that '
                           'stops short of the span); every other event and '
                           'value still shows, unflagged. (Details sent to '
                           'support.)')
            _policy_block_caption(_exc)
            report_error(f'{where} — settings panel render', _exc)
            st.session_state.pop('otdr_settings', None)
            settings_exc = _exc
        # Connector/launch knobs, same guard, and on the Viewer the same
        # no-flags answer.
        try:
            _render_conn_settings_panel(in_expander=False)
        except Exception as _exc:
            if blocks_report:
                st.error('Connector & Launch settings could not load. The '
                         'report is turned off until they do. (Details sent '
                         'to support.)')
            else:
                st.warning('Connector & Launch settings could not load. '
                           'Until they do, the Viewer flags only breaks (a '
                           'fiber that stops short of the span); every other '
                           'event and value still shows, unflagged. (Details '
                           'sent to support.)')
            _policy_block_caption(_exc)
            report_error(f'{where} — connector settings panel render', _exc)
            st.session_state.pop('conn_settings', None)
            settings_exc = settings_exc or _exc
    _share_settings_with_viewer(failed=settings_exc is not None)
    return settings_exc


def _settings_block_notice(exc, button):
    """Why the page's run button is off, under a settings box that failed
    to draw, and what to do about it: the Windows policy steps when a file
    was blocked, otherwise a restart (a half-loaded module stays broken
    until the process restarts).  `button` names the control: 'Generate'
    on the Splice Report, 'Run' on Unidirectional."""
    st.error(f'The settings did not load completely, so {button} is turned '
             'off. A report without them could use thresholds you did not '
             'choose. (Details sent to support.)')
    if _blocked_by_policy(exc):
        _policy_block_caption(exc)
    else:
        st.caption(f'Close {PRODUCT_NAME} completely and open it again.')


def _share_settings_with_viewer(failed=False):
    """Point the Viewer at the settings on screen (trace_server.set_settings).
    A Viewer with no report behind it judges pass/fail at them and runs its
    own report with them.  A report on screen still wins: the Viewer judges
    by the gates that report ran at, so it agrees with the grid the tech
    clicked from.  `failed` (either part of the box did not draw) turns the
    Viewer's flags off, report or not, all but a sure break's.

    Sent whole even when the table did not draw: the profile's engine
    settings live outside the box, and some of them move a break call (an
    iOLM export's missing end marker, IOLM_END_FALLBACK), so the Viewer's own
    run must keep them for its breaks to read as the report's would.  The
    missing table adds nothing (_overrides_from_settings(None) is {})."""
    try:
        trace_server.set_settings(_report_overrides(), failed=failed)
    except Exception as exc:
        report_error('OTDR settings → Viewer', exc)


def _render_cable_type_select():
    """Cable-type → helix-factor manual picker for the helix-calibration tool.

    Renders inside the OTDR-settings expander.  Reads the extensible cable
    database (``helixcal.cable_db``) for the option list + expected band, so
    adding a cable family there makes it appear here automatically.  Stores the
    chosen ``cable_type`` key on ``st.session_state.cable_type``.  Degrades
    gracefully (renders nothing) if the helixcal package is unavailable.
    """
    try:
        from helixcal import cable_db
    except Exception:
        return  # helix tool not installed in this build; skip the control

    options = cable_db.all_types()
    if not options:
        return
    entries = {e.key: e for e in cable_db.entries()}

    if st.session_state.get('cable_type') not in options:
        st.session_state.cable_type = cable_db.DEFAULT_CABLE_TYPE

    st.markdown('**Cable Type (Helix Factor)**')

    def _fmt(key):
        e = entries.get(key)
        if not e:
            return key
        return (f"{e.label}, m {e.m_low:.3f}–{e.m_high:.3f} "
                f"(EFL {e.efl_low:.1f}–{e.efl_high:.1f}%)")

    _cur = st.session_state['cable_type']
    _picked = st.selectbox(
        'Cable Type', options,
        index=options.index(_cur),
        format_func=_fmt,
        label_visibility='collapsed',
        key='cable_type_select',
        help=("Cable construction sets the expected helix / EFL band the "
              "helix-calibration tool sanity-checks the fitted factor "
              "against.  Auto-detected from the SOR GenParams when a cable "
              "code is present; pick it here when it is not (most spans)."),
    )
    if _picked != _cur:
        st.session_state.cable_type = _picked
    st.caption(
        f'Helix sanity band: {_fmt(st.session_state.cable_type)} '
        f'(Corning AEN-142). Used by the helix-calibration report.')


_CAT_COLOR = {
    'reburn': '#e74c3c', 'break': '#c0392b', 'broke': '#922b21',
    'bend': '#e67e22', 'ref': '#d35400', 'gainer': '#27ae60',
    'bfill': '#2980b9', 'a_only': '#8e44ad', 'b_only': '#16a085',
    'deadzone': '#7f8c8d', 'event': '#000000',
}

def _viewer_click_target(page_key):
    """Do report-grid cell clicks open the separate Viewer WINDOW (default —
    what the pop-out shipped as) or load the in-app Viewer TAB (the
    pre-pop-out behavior, kept for techs who prefer a single window)?
    Returns True when the pop-out window should be used."""
    k = f'{page_key}_click_target'
    # Also kept in a slot no widget owns: Streamlit drops a widget's state on
    # a run that does not draw it, so a trip to the Viewer and back put the
    # choice back to 'Separate window'.  The slot rides a cell click too
    # (_CARRIED_SETTINGS), which starts a new session.
    saved = k + '_saved'
    if k not in st.session_state:
        st.session_state[k] = st.session_state.get(saved, 'Separate window')
    choice = st.radio(
        'Cell Clicks Open In', ['Separate window', 'This tab (Viewer page)'],
        key=k, horizontal=True,
        format_func={'Separate window': 'Separate Window',
                     'This tab (Viewer page)': 'This Tab (Viewer Page)'}.get,
        help='Separate Window: one Viewer window stays open beside the report '
             'and re-plots as you click cells (shift-click adds a fiber). '
             'This Tab: cells load the in-app Viewer page with a Back button.')
    st.session_state[saved] = choice
    return choice == 'Separate window'


def _cell_markup(popout, fiber, km, direction, color, label, text, href):
    """One flagged-cell's markup, in whichever click mode is active — so the
    Splice Report / FR / Uni grids stay identical to each other."""
    if popout:
        return (f"<span class='vc' data-fiber='{fiber}' data-km='{km}' "
                f"data-dir='{direction}' title='{label}' "
                f"style='color:{color};font-weight:600'>{text}</span>")
    return (f"<a href='{href}' target='_self' title='{label}' "
            f"style='color:{color};text-decoration:none;font-weight:600'>"
            f"{text}</a>")


def _render_clickable_grid(table_html, port, height=560, src=''):
    """Render a report's ribbon grid with client-side cells that drive ONE
    persistent pop-out Viewer window instead of the in-app tab.

    The whole grid + JS lives in a single component iframe, so:
      • clicks are handled entirely client-side (no Streamlit rerun), and
      • the opened-window reference survives across the session.
    First click opens the Viewer in its own window (named 'otdr_viewer', so it
    is reused, never duplicated); every later click LIVE-UPDATES that same
    window in place via postMessage — no reload, no closing/reopening.  The
    trace server is pointed at this report's span (one span at a time), so the
    popped window shows the right fibers.  Cells carry data-fiber/-km/-dir.
    """
    origin = f"http://127.0.0.1:{port}"
    doc = """
<div style="font-family:Consolas,monospace">
  <button id="vpop" style="margin:0 0 6px;padding:4px 10px;border:1px solid #c9d5e1;
      border-radius:4px;background:#eef3f8;cursor:pointer;font-weight:600;color:#000000">
      &#8862; Open / Focus Viewer Window</button>
  <span style="margin-left:8px;font-size:11px;color:#000000">click any cell &rarr;
      it plots in the Viewer window (stays open, updates in place) &middot;
      <b>shift-click</b> to add a fiber instead of replacing</span>
  __TABLE__
</div>
<script>
(function(){
  var ORIGIN = "__ORIGIN__";
  // Which report this grid belongs to.  The Viewer seeds its verdict gate
  // from it, so a cell flagged here is flagged there — the in-tab href has
  // carried &src= all along and the pop-out path was the one missing it.
  var SRC = "__SRC__";
  var vw = null;
  // Name the hub tab so the Viewer window's "← Back" can bring THIS tab
  // forward (report untouched) instead of opening a second hub.
  try { window.top.name = "otdr_hub"; } catch (e) {}
  function ensure(url){
    if (!vw || vw.closed) {
      vw = window.open(url || (ORIGIN + "/"), "otdr_viewer", "width=1400,height=900");
    }
    return vw;
  }
  // stack = the tech shift-clicked: keep what's plotted and ADD this fiber
  // (compare two cells).  A plain click REPLACES, so clicking through many
  // cells shows one fiber at a time instead of piling up traces.
  function jump(el, stack){
    var f = el.getAttribute("data-fiber");
    var km = el.getAttribute("data-km");
    var dir = el.getAttribute("data-dir") || "both";
    if (!vw || vw.closed) {
      var u = ORIGIN + "/?dir=" + dir + "&fiber=" + f + (km ? "&km=" + km : "")
              + (SRC ? "&src=" + SRC : "");
      ensure(u);
    } else {
      vw.focus();
      vw.postMessage({type:"otdr-jump", fiber:f, km:km, dir:dir,
                      src: SRC, replace: !stack}, ORIGIN);
    }
  }
  var cells = document.querySelectorAll(".vc");
  for (var i=0;i<cells.length;i++){
    cells[i].style.cursor = "pointer";
    (function(el){ el.addEventListener("click", function(ev){
      jump(el, ev.shiftKey);
    }); })(cells[i]);
  }
  var pop = document.getElementById("vpop");
  if (pop) pop.addEventListener("click", function(){ var w = ensure(); if (w) w.focus(); });
})();
</script>
"""
    # SRC sits inside a "..." literal in the script: json escapes quotes and
    # backslashes, and every "<" becomes its unicode escape so the value can
    # never close the <script>.  Left unreplaced, every pop-out click sent
    # src=__SRC__, which the Viewer reads as the Splice Report, so a
    # Unidirectional cell got the Splice Report's gate and a "Back to Splice
    # Report" button.  The table goes in last so nothing in the report's own
    # text is ever taken for a placeholder.
    src_js = json.dumps(str(src or ''))[1:-1].replace('<', '\\u003c')
    doc = (theme_recolor(doc).replace("__ORIGIN__", origin).replace("__SRC__", src_js)
              .replace("__TABLE__", table_html))
    st_components_html(doc, height=height, scrolling=True)


# ═══════════════════════════════════════════════════════════════════════════
# ─── tech_compare: begin ───  (folded into app.py on purpose: a NEW shipped
#     file would change the ENGINE_FILES set and freeze fleet hot-updates
#     until every tech ran a fresh installer.  Engine-free: openpyxl only.)
# Tech comparison — compare OUR Splice Report workbook against the tech's own
# splice report and write a third workbook that highlights every difference.
#
# Hub-side helper, like folder_intake.py: openpyxl only, no engine imports
# (each engine ships its own sor_reader copy and the hub must never import one).
#
# What it does
# ------------
# * Reads the ribbon x splice grid out of both workbooks.  Ours is the
#   "Splice Report" sheet write_xlsx() produces (two distance rows, a header
#   row, one row per ribbon, one Excel column per splice).  The
#   tech's is whatever they hand-build: one "Distance:" row, a "Ribbon /
#   ILA / Splice N" header row and one row per ribbon.  Both layouts are
#   auto-detected from the "Ribbon" header cell, so the row offsets do not
#   have to match.
# * Lines the columns up BY DISTANCE, not by index or by name: techs number
#   splices from either end and add their own "bends" / "damage" / "HH"
#   columns.  Both of our distance frames (A->B and B->A) are tried and the
#   one that lines up more columns wins, so a tech who counts from the far
#   end still gets a like-for-like comparison.
# * Parses each cell into per-fiber entries ("49,50,60 .369" -> three fibers
#   at 0.369 dB; "1-8 brok" -> eight broken fibers; "all" -> the whole
#   ribbon) and compares fiber by fiber.
# * Writes <site_a>_to_<site_b>_SpliceReport_vs_Tech.xlsx with three sheets:
#   the grid with only the differing cells filled (colour = kind of
#   difference), a flat list of every fiber-level difference, and a summary
#   with the column line-up.
#
# Differences reported (per fiber, per column)
# --------------------------------------------
#   Tech only      the tech flagged the fiber here and our report did not
#   Ours only      our report flagged the fiber here and the tech did not
#   Value differs  both flagged it with a loss and the losses differ by more
#                  than the tolerance (0.010 dB — techs round to 2 decimals)
#   Type differs   both flagged it but one has a loss and the other a
#                  word (broke / bend / DZ ...), or the words differ


from dataclasses import dataclass, field


TC_LOSS_TOL_DB = 0.010        # |ours - tech| above this = "Value differs"
TC_COLUMN_MATCH_KM = 0.25     # a tech column within this of ours = the same column

TC_KIND_TECH_ONLY = 'Tech only'
TC_KIND_OURS_ONLY = 'Ours only'
TC_KIND_VALUE = 'Value differs'
TC_KIND_TYPE = 'Type differs'
TC_KIND_ORDER = (TC_KIND_TECH_ONLY, TC_KIND_OURS_ONLY, TC_KIND_TYPE, TC_KIND_VALUE)

_TC_FILLS = {
    TC_KIND_TECH_ONLY: 'FFC7CE',   # pink-red: they saw something we did not print
    TC_KIND_OURS_ONLY: 'BDD7EE',   # light blue: we printed something they did not
    TC_KIND_VALUE:     'FFEB9C',   # yellow: same fiber, different number
    TC_KIND_TYPE:      'F8CBAD',   # orange: number on one side, a word on the other
}
_TC_UNMATCHED_HDR = 'BFBFBF'


# ──────────────────────────────────────────────────────────────────────────
#  TcGrid model
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class TcColumn:
    label: str
    km: float | None            # distance in the sheet's own frame
    km_alt: float | None = None  # our B->A reading (ours only)
    excel_col: int = 0
    kind: str = 'splice'        # splice | bend | damage | ref | ila_left | ila_right | other


@dataclass
class TcGrid:
    path: str
    columns: list = field(default_factory=list)
    ribbons: list = field(default_factory=list)    # [(idx, label, lo, hi)]
    cells: dict = field(default_factory=dict)      # (ribbon_idx, col_idx) -> text
    ribbon_size: int = 12
    site_left: str = ''
    site_right: str = ''


_TC_KM_RE = re.compile(r'(-?\d+(?:\.\d+)?)\s*(?:km|k\b)', re.I)


def _tc_km_from(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(',', '')
    m = _TC_KM_RE.search(s)
    if m:
        return float(m.group(1))
    m = re.match(r'^-?\d+(?:\.\d+)?$', s)
    return float(m.group(0)) if m else None


def _tc_col_kind(label: str) -> str:
    l = (label or '').strip().lower()
    if 'ila' in l:
        return 'ila'
    if l.startswith('splice') or l == 'entry' or l.startswith('hh'):
        return 'splice'
    if 'bend' in l:
        return 'bend'
    if 'damag' in l or 'brok' in l:
        return 'damage'
    if 'ref' in l or 'connector' in l:
        return 'ref'
    return 'other'


def _tc_ribbon_bounds(label: str, order: int, ribbon_size: int):
    """(idx, lo, hi) from 'Fiber 13-24 (2) (A2)' / '793-804 (67)' / 'Ribbon 3'.
    Falls back to the row order when nothing parses."""
    s = str(label)
    m_rng = re.search(r'(\d+)\s*-\s*(\d+)', s)
    m_idx = re.search(r'\((\d+)\)', s)
    if m_idx:
        idx = int(m_idx.group(1)) - 1
    elif m_rng:
        idx = (int(m_rng.group(1)) - 1) // ribbon_size
    else:
        m_n = re.search(r'(\d+)', s)
        idx = int(m_n.group(1)) - 1 if m_n else order
    if m_rng:
        lo, hi = int(m_rng.group(1)), int(m_rng.group(2))
    else:
        lo, hi = idx * ribbon_size + 1, (idx + 1) * ribbon_size
    return idx, lo, hi


def tc_read_grid(path: str, sheet: str | None = None) -> TcGrid:
    """Parse a splice-report grid (ours or the tech's) into a TcGrid."""
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True, read_only=False)
    if sheet and sheet in wb.sheetnames:
        ws = wb[sheet]
    elif 'Splice Report' in wb.sheetnames:
        ws = wb['Splice Report']
    else:
        ws = None
        for cand in wb.worksheets:
            if _tc_find_header_row(cand) is not None:
                ws = cand
                break
        if ws is None:
            raise ValueError('No sheet with a "Ribbon" header row found in '
                             + os.path.basename(path))
    hdr_row = _tc_find_header_row(ws)
    if hdr_row is None:
        raise ValueError(f'No "Ribbon" header row in {os.path.basename(path)} '
                         f'sheet {ws.title!r}')
    g = TcGrid(path=path)

    # Distance rows: every row above the header holding km-ish values.  Ours
    # labels them "B→A:" / "A→B:" in column 2; the tech's says "Distance:".
    dist_rows = []
    for r in range(1, hdr_row):
        lab = str(ws.cell(r, 2).value or '').strip().lower()
        kms = [_tc_km_from(ws.cell(r, c).value) for c in range(3, ws.max_column + 1)]
        if any(k is not None for k in kms):
            dist_rows.append((r, lab))
    row_ab = next((r for r, lab in dist_rows if 'a→b' in lab or 'a->b' in lab
                   or 'a-b' in lab), None)
    row_ba = next((r for r, lab in dist_rows if 'b→a' in lab or 'b->a' in lab
                   or 'b-a' in lab), None)
    if row_ab is None:
        row_ab = dist_rows[-1][0] if dist_rows else None

    # Columns: every header-row cell with a label from column 2 on.  Reports
    # built before 2026-09-28 spread each splice over a merged pair of
    # columns; the right half has no header, so it is skipped naturally.
    for c in range(2, ws.max_column + 1):
        v = ws.cell(hdr_row, c).value
        if v is None or not str(v).strip():
            continue
        label = str(v).strip()
        kind = _tc_col_kind(label)
        km = _tc_km_from(ws.cell(row_ab, c).value) if row_ab else None
        km_alt = _tc_km_from(ws.cell(row_ba, c).value) if row_ba else None
        g.columns.append(TcColumn(label=label, km=km, km_alt=km_alt,
                                excel_col=c, kind=kind))
    ilas = [i for i, col in enumerate(g.columns) if col.kind == 'ila']
    if ilas:
        g.columns[ilas[0]].kind = 'ila_left'
        g.site_left = re.sub(r'^.*?ila\s*:?\s*', '', g.columns[ilas[0]].label,
                             flags=re.I).strip()
        if len(ilas) > 1:
            g.columns[ilas[-1]].kind = 'ila_right'
            g.site_right = re.sub(r'^.*?ila\s*:?\s*', '', g.columns[ilas[-1]].label,
                                  flags=re.I).strip()

    # Ribbon size from the first fiber range we can read.
    for r in range(hdr_row + 1, ws.max_row + 1):
        m = re.search(r'(\d+)\s*-\s*(\d+)', str(ws.cell(r, 1).value or ''))
        if m and int(m.group(2)) >= int(m.group(1)):
            g.ribbon_size = int(m.group(2)) - int(m.group(1)) + 1
            break

    order = 0
    for r in range(hdr_row + 1, ws.max_row + 1):
        lab = ws.cell(r, 1).value
        if lab is None or not str(lab).strip():
            continue
        if not re.search(r'\d', str(lab)):
            continue
        idx, lo, hi = _tc_ribbon_bounds(str(lab), order, g.ribbon_size)
        order += 1
        g.ribbons.append((idx, str(lab).strip(), lo, hi))
        for ci, col in enumerate(g.columns):
            v = ws.cell(r, col.excel_col).value
            if v is None or not str(v).strip():
                continue
            g.cells[(idx, ci)] = str(v).strip()
    return g


def _tc_find_header_row(ws) -> int | None:
    for r in range(1, min(ws.max_row, 30) + 1):
        v = ws.cell(r, 1).value
        if v is not None and str(v).strip().lower().startswith('ribbon'):
            return r
    return None


# ──────────────────────────────────────────────────────────────────────────
#  Cell parsing  → {fiber: TcEntry}
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class TcEntry:
    loss: float | None = None
    tag: str = ''            # broke | break | bend | ref | dz | launch | damage | flag
    raw: str = ''


_TC_FIBER_SPEC = re.compile(r'^(all|\d{1,4}(?:-\d{1,4})?(?:,\d{1,4}(?:-\d{1,4})?)*)$')
_TC_NUM = re.compile(r'^[-+~]?(?:\d+\.\d+|\.\d+|\d+\.)(?:bd)?$')
_TC_TAG_WORDS = (
    ('broke', 'broke'), ('brok', 'broke'), ('break', 'break'),
    ('bend', 'bend'), ('damag', 'damage'), ('refl', 'ref'), ('ref', 'ref'),
    ('dz', 'dz'), ('launch', 'launch'), ('bad_launch', 'launch'),
    ('bad_tailbox', 'launch'), ('no_events', 'launch'),
    ('high_launch', 'launch'), ('duration_mismatch', 'launch'),
    ('gain', 'gainer'),
)


def _tc_norm_text(t: str) -> str:
    t = str(t)
    t = t.replace('→', ' ').replace('|', ' ')
    t = re.sub(r'(\d)-\s+(\d)', r'\1-\2', t)         # '179- 180' -> '179-180'
    t = re.sub(r'(\d)\s+-\s+(\d)', r'\1-\2', t)       # '179 - 180' (but not '176 -0.162')
    t = re.sub(r'(?<=\d)\s*,\s*(?=\d)', ',', t)        # '49, 50' -> '49,50'
    t = re.sub(r'\s*,\s+|\s+,\s*', ' ', t)           # a stray ' , ' between entries
    t = re.sub(r'(?<=\d)\(', ' (', t)                 # '.171(B-fill)' -> '.171 (B-fill)'
    t = re.sub(r'\bF(?=\d)', '', t)                   # 'F12' -> '12'
    return t


def _tc_expand_fibers(spec: str, lo: int, hi: int) -> list:
    if spec == 'all':
        return list(range(lo, hi + 1))
    out = []
    for part in spec.split(','):
        if '-' in part:
            a, b = part.split('-', 1)
            a, b = int(a), int(b)
            if b < a:
                a, b = b, a
            if b - a > 999:
                continue
            out.extend(range(a, b + 1))
        else:
            out.append(int(part))
    return out


def tc_parse_cell(text: str, lo: int = 1, hi: int = 12) -> dict:
    """'49,50,60 .369'  -> {49: TcEntry(.369), 50: ..., 60: ...}
    '1-8 brok'         -> eight TcEntry(tag='broke')
    '10 BEND .883 bidi 11 bend .127 bidi' -> {10: TcEntry(.883,'bend'), 11: ...}
    'all 145 .35'      -> whole ribbon flagged, F145 at .35 dB
    """
    if text is None:
        return {}
    tokens = _tc_norm_text(text).split()
    segments = []          # [(fiberspec, [tokens...])]
    cur = None
    for tok in tokens:
        if _TC_FIBER_SPEC.match(tok.lower()):
            cur = (tok.lower(), [])
            segments.append(cur)
        elif cur is not None:
            cur[1].append(tok)
    out = {}
    for spec, rest in segments:
        e = TcEntry(raw=(spec + ' ' + ' '.join(rest)).strip())
        for tok in rest:
            tl = tok.lower()
            if tl.startswith('(') or tl.endswith(')'):
                continue                       # '(B-fill)', '(refl', '-67dB)' — notes
            if e.loss is None and _TC_NUM.match(tl) and not tl.startswith('~'):
                try:
                    e.loss = float(tl.rstrip('bd').lstrip('+'))
                except ValueError:
                    pass
                continue
            if not e.tag:
                for prefix, tag in _TC_TAG_WORDS:
                    if tl.startswith(prefix):
                        e.tag = tag
                        break
        if e.loss is None and not e.tag:
            e.tag = 'flag'
        try:
            fibers = _tc_expand_fibers(spec, lo, hi)
        except ValueError:
            continue
        for f in fibers:
            out[f] = TcEntry(loss=e.loss, tag=e.tag, raw=e.raw)
    return out


# ──────────────────────────────────────────────────────────────────────────
#  TcColumn line-up
# ──────────────────────────────────────────────────────────────────────────
def _tc_match_columns(ours: TcGrid, tech: TcGrid, use_alt: bool):
    """Greedy nearest-distance one-to-one pairing.  Returns
    {our_col_idx: tech_col_idx}."""
    pairs = []
    for oi, oc in enumerate(ours.columns):
        okm = oc.km_alt if use_alt else oc.km
        if okm is None or oc.kind in ('ila_left', 'ila_right'):
            continue
        for ti, tc in enumerate(tech.columns):
            if tc.km is None or tc.kind in ('ila_left', 'ila_right'):
                continue
            d = abs(okm - tc.km)
            if d <= TC_COLUMN_MATCH_KM:
                pairs.append((d, oi, ti))
    pairs.sort()
    used_o, used_t, out = set(), set(), {}
    for d, oi, ti in pairs:
        if oi in used_o or ti in used_t:
            continue
        used_o.add(oi); used_t.add(ti); out[oi] = ti
    # The ILA end columns pair by physical end.  In the alt frame the tech's
    # left end is our right end.
    o_l = next((i for i, c in enumerate(ours.columns) if c.kind == 'ila_left'), None)
    o_r = next((i for i, c in enumerate(ours.columns) if c.kind == 'ila_right'), None)
    t_l = next((i for i, c in enumerate(tech.columns) if c.kind == 'ila_left'), None)
    t_r = next((i for i, c in enumerate(tech.columns) if c.kind == 'ila_right'), None)
    if use_alt:
        t_l, t_r = t_r, t_l
    if o_l is not None and t_l is not None:
        out[o_l] = t_l
    if o_r is not None and t_r is not None:
        out[o_r] = t_r
    return out


def tc_line_up_columns(ours: TcGrid, tech: TcGrid):
    """Pick the frame (A->B or B->A) that lines up more columns."""
    m_ab = _tc_match_columns(ours, tech, use_alt=False)
    n_ab = sum(1 for oi in m_ab if ours.columns[oi].kind not in ('ila_left', 'ila_right'))
    if any(c.km_alt is not None for c in ours.columns):
        m_ba = _tc_match_columns(ours, tech, use_alt=True)
        n_ba = sum(1 for oi in m_ba
                   if ours.columns[oi].kind not in ('ila_left', 'ila_right'))
        if n_ba > n_ab:
            return m_ba, 'B→A'
    return m_ab, 'A→B'


# ──────────────────────────────────────────────────────────────────────────
#  Compare
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class TcDiff:
    ribbon_idx: int
    ribbon_label: str
    our_col: int | None
    tech_col: int | None
    fiber: int
    ours: str
    tech: str
    kind: str


def _tc_fmt(e: TcEntry | None) -> str:
    if e is None:
        return ''
    if e.loss is not None and e.tag and e.tag != 'flag':
        return f'{e.tag} {e.loss:.3f}'
    if e.loss is not None:
        return f'{e.loss:.3f}'
    return e.tag


def tc_compare(ours: TcGrid, tech: TcGrid, tol_db: float = TC_LOSS_TOL_DB):
    """Returns (diffs, colmap, frame).  colmap = {our_col_idx: tech_col_idx}."""
    colmap, frame = tc_line_up_columns(ours, tech)
    rev = {t: o for o, t in colmap.items()}
    our_rib = {idx: (lab, lo, hi) for idx, lab, lo, hi in ours.ribbons}
    tech_rib = {idx: (lab, lo, hi) for idx, lab, lo, hi in tech.ribbons}
    diffs = []

    def bounds(ri):
        if ri in our_rib:
            return our_rib[ri][1:]
        if ri in tech_rib:
            return tech_rib[ri][1:]
        return ri * ours.ribbon_size + 1, (ri + 1) * ours.ribbon_size

    def label(ri):
        return (our_rib.get(ri) or tech_rib.get(ri) or (f'Ribbon {ri + 1}',))[0]

    keys = set()
    for (ri, oi) in ours.cells:
        keys.add((ri, oi, colmap.get(oi)))
    for (ri, ti) in tech.cells:
        keys.add((ri, rev.get(ti), ti))
    for ri, oi, ti in sorted(keys, key=lambda k: (k[0], k[1] if k[1] is not None else 10 ** 6, k[2] or 0)):
        lo, hi = bounds(ri)
        o_ent = tc_parse_cell(ours.cells.get((ri, oi)), lo, hi) if oi is not None else {}
        t_ent = tc_parse_cell(tech.cells.get((ri, ti)), lo, hi) if ti is not None else {}
        for f in sorted(set(o_ent) | set(t_ent)):
            o, t = o_ent.get(f), t_ent.get(f)
            kind = None
            if o is None:
                kind = TC_KIND_TECH_ONLY
            elif t is None:
                kind = TC_KIND_OURS_ONLY
            elif o.loss is not None and t.loss is not None:
                if abs(o.loss - t.loss) > tol_db + 1e-9:
                    kind = TC_KIND_VALUE
            elif o.loss is None and t.loss is None:
                if o.tag != t.tag and 'flag' not in (o.tag, t.tag):
                    kind = TC_KIND_TYPE
            else:
                kind = TC_KIND_TYPE
            if kind:
                diffs.append(TcDiff(ri, label(ri), oi, ti, f, _tc_fmt(o), _tc_fmt(t), kind))
    return diffs, colmap, frame


# ──────────────────────────────────────────────────────────────────────────
#  Workbook
# ──────────────────────────────────────────────────────────────────────────
def _tc_col_title(c: TcColumn | None) -> str:
    if c is None:
        return '-'
    if c.km is None or '@' in c.label:
        return c.label
    return f'{c.label} @ {c.km:.2f} km'


def tc_write_comparison(ours: TcGrid, tech: TcGrid, diffs, colmap, frame, out_path: str,
                     tol_db: float = TC_LOSS_TOL_DB) -> str:
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Differences'
    thin = Side(style='thin', color='CCCCCC')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    hdr_font = Font(name='Calibri', bold=True, size=12, color='FFFFFF')
    hdr_fill = PatternFill(start_color='1F4E79', end_color='1F4E79', fill_type='solid')
    body = Font(name='Calibri', size=11)
    wrap = Alignment(wrap_text=True, vertical='top')

    # TcColumn order: ours left→right (with its tech partner), then any tech
    # columns that never lined up, in the tech's distance order.
    rev = {t: o for o, t in colmap.items()}
    order = [(oi, colmap.get(oi)) for oi in range(len(ours.columns))]
    order += [(None, ti) for ti in range(len(tech.columns)) if ti not in rev]

    ws.cell(1, 1, 'Ribbon').font = hdr_font
    ws.cell(1, 1).fill = hdr_fill
    ws.cell(2, 1, '').fill = hdr_fill
    for j, (oi, ti) in enumerate(order):
        c = j + 2
        oc = ours.columns[oi] if oi is not None else None
        tc = tech.columns[ti] if ti is not None else None
        top = ws.cell(1, c, 'Ours: ' + _tc_col_title(oc))
        bot = ws.cell(2, c, 'Tech: ' + _tc_col_title(tc))
        for cell in (top, bot):
            cell.font = hdr_font
            cell.alignment = Alignment(wrap_text=True, horizontal='center', vertical='center')
            cell.fill = hdr_fill
        if oc is None or tc is None:
            fill = PatternFill(start_color=_TC_UNMATCHED_HDR, end_color=_TC_UNMATCHED_HDR,
                               fill_type='solid')
            top.fill = bot.fill = fill
            top.font = bot.font = Font(name='Calibri', bold=True, size=12, color='000000')
        ws.column_dimensions[openpyxl.utils.get_column_letter(c)].width = 26
    ws.column_dimensions['A'].width = 24
    ws.row_dimensions[1].height = 32
    ws.row_dimensions[2].height = 32

    by_cell = {}
    for d in diffs:
        by_cell.setdefault((d.ribbon_idx, d.our_col, d.tech_col), []).append(d)
    ribbon_ids = sorted({idx for idx, *_ in ours.ribbons} | {idx for idx, *_ in tech.ribbons})
    our_lab = {idx: lab for idx, lab, *_ in ours.ribbons}
    tech_lab = {idx: lab for idx, lab, *_ in tech.ribbons}
    for i, ri in enumerate(ribbon_ids):
        r = i + 3
        ws.cell(r, 1, our_lab.get(ri) or tech_lab.get(ri)).font = body
        ws.cell(r, 1).border = border
        for j, (oi, ti) in enumerate(order):
            c = j + 2
            cell = ws.cell(r, c)
            cell.border = border
            cell.alignment = wrap
            cell.font = body
            ds = by_cell.get((ri, oi, ti))
            if not ds:
                continue
            o_txt = ours.cells.get((ri, oi), '') if oi is not None else ''
            t_txt = tech.cells.get((ri, ti), '') if ti is not None else ''
            kinds = {d.kind for d in ds}
            worst = next(k for k in TC_KIND_ORDER if k in kinds)
            fibers = ', '.join(f'F{d.fiber}' for d in ds[:12]) + (' …' if len(ds) > 12 else '')
            cell.value = (f'Ours: {o_txt or "(blank)"}\nTech: {t_txt or "(blank)"}\n'
                          f'{" / ".join(k for k in TC_KIND_ORDER if k in kinds)}: {fibers}')
            cell.fill = PatternFill(start_color=_TC_FILLS[worst], end_color=_TC_FILLS[worst],
                                    fill_type='solid')
    ws.freeze_panes = 'B3'

    # ── Difference list ──
    wl = wb.create_sheet('Difference list')
    heads = ['Ribbon', 'Fiber', 'Our Column', 'Tech Column', 'Ours', 'Tech', 'Difference']
    for c, h in enumerate(heads, 1):
        cell = wl.cell(1, c, h)
        cell.font = hdr_font; cell.fill = hdr_fill
    for r, d in enumerate(diffs, 2):
        oc = ours.columns[d.our_col] if d.our_col is not None else None
        tc = tech.columns[d.tech_col] if d.tech_col is not None else None
        vals = [d.ribbon_label, d.fiber, _tc_col_title(oc), _tc_col_title(tc),
                d.ours or '(blank)', d.tech or '(blank)', d.kind]
        for c, v in enumerate(vals, 1):
            cell = wl.cell(r, c, v)
            cell.font = body
            cell.fill = PatternFill(start_color=_TC_FILLS[d.kind], end_color=_TC_FILLS[d.kind],
                                    fill_type='solid')
    for col, w in zip('ABCDEFG', (24, 8, 26, 26, 22, 22, 16)):
        wl.column_dimensions[col].width = w
    wl.freeze_panes = 'A2'
    if diffs:
        wl.auto_filter.ref = f'A1:G{len(diffs) + 1}'

    # ── Summary ──
    wsum = wb.create_sheet('Summary', 0)
    wsum.column_dimensions['A'].width = 34
    wsum.column_dimensions['B'].width = 60
    wsum.column_dimensions['C'].width = 34
    rows = [
        ('Our report', os.path.basename(ours.path)),
        ('Tech report', os.path.basename(tech.path)),
        ('Distance frame used', f'{frame} (the frame that lined up more columns)'),
        ('Loss tolerance', f'{tol_db:.3f} dB'),
        ('Ribbons (ours / tech)', f'{len(ours.ribbons)} / {len(tech.ribbons)}'),
        ('Columns lined up', f'{sum(1 for oi in colmap if ours.columns[oi].kind not in ("ila_left", "ila_right"))} of '
                             f'{sum(1 for c in ours.columns if c.kind not in ("ila_left", "ila_right"))} ours, '
                             f'{sum(1 for c in tech.columns if c.kind not in ("ila_left", "ila_right"))} tech'),
        ('Fiber-level differences', len(diffs)),
    ]
    for k in TC_KIND_ORDER:
        rows.append((f'   {k}', sum(1 for d in diffs if d.kind == k)))
    r = 1
    wsum.cell(r, 1, 'Splice report vs tech report').font = Font(bold=True, size=14)
    r += 2
    for k, v in rows:
        wsum.cell(r, 1, k).font = Font(bold=True)
        wsum.cell(r, 2, v)
        r += 1
    r += 1
    wsum.cell(r, 1, 'Color key').font = Font(bold=True)
    r += 1
    for k in TC_KIND_ORDER:
        c = wsum.cell(r, 1, k)
        c.fill = PatternFill(start_color=_TC_FILLS[k], end_color=_TC_FILLS[k], fill_type='solid')
        wsum.cell(r, 2, {
            TC_KIND_TECH_ONLY: 'the tech flagged this fiber here; our report did not',
            TC_KIND_OURS_ONLY: 'our report flagged this fiber here; the tech did not',
            TC_KIND_VALUE: f'both flagged it; losses differ by more than {tol_db:.3f} dB',
            TC_KIND_TYPE: 'both flagged it; a loss on one side and a word (broke, bend, DZ …) on the other',
        }[k])
        r += 1
    c = wsum.cell(r, 1, 'Gray column header')
    c.fill = PatternFill(start_color=_TC_UNMATCHED_HDR, end_color=_TC_UNMATCHED_HDR, fill_type='solid')
    wsum.cell(r, 2, f'a column only one report has (no column within {TC_COLUMN_MATCH_KM * 1000:.0f} m in the other)')
    r += 2
    wsum.cell(r, 1, 'Column Line-Up').font = Font(bold=True)
    r += 1
    for c, h in enumerate(('Our Column', 'Tech Column', 'Distance Gap'), 1):
        cell = wsum.cell(r, c, h)
        cell.font = hdr_font; cell.fill = hdr_fill
    r += 1
    for oi, ti in order:
        oc = ours.columns[oi] if oi is not None else None
        tc = tech.columns[ti] if ti is not None else None
        wsum.cell(r, 1, _tc_col_title(oc))
        wsum.cell(r, 2, _tc_col_title(tc))
        if oc is not None and tc is not None and tc.km is not None:
            okm = oc.km_alt if frame == 'B→A' else oc.km
            if okm is not None:
                wsum.cell(r, 3, f'{abs(okm - tc.km) * 1000:.0f} m')
        elif oc is None or tc is None:
            for c in (1, 2):
                wsum.cell(r, c).fill = PatternFill(start_color=_TC_UNMATCHED_HDR,
                                                   end_color=_TC_UNMATCHED_HDR, fill_type='solid')
        r += 1

    wb.save(out_path)
    return out_path


def tc_compare_reports(ours_xlsx: str, tech_xlsx: str, out_path: str,
                    tol_db: float = TC_LOSS_TOL_DB) -> dict:
    """One call for the hub: read both, tc_compare, write, summarise."""
    ours = tc_read_grid(ours_xlsx, 'Splice Report')
    tech = tc_read_grid(tech_xlsx)
    diffs, colmap, frame = tc_compare(ours, tech, tol_db)
    tc_write_comparison(ours, tech, diffs, colmap, frame, out_path, tol_db)
    n_our_cols = sum(1 for c in ours.columns if c.kind not in ('ila_left', 'ila_right'))
    n_tech_cols = sum(1 for c in tech.columns if c.kind not in ('ila_left', 'ila_right'))
    n_matched = sum(1 for oi in colmap if ours.columns[oi].kind not in ('ila_left', 'ila_right'))
    return {
        'xlsx': out_path,
        'frame': frame,
        'n_diffs': len(diffs),
        'counts': {k: sum(1 for d in diffs if d.kind == k) for k in TC_KIND_ORDER},
        'columns_matched': n_matched,
        'columns_ours': n_our_cols,
        'columns_tech': n_tech_cols,
        'ribbons_ours': len(ours.ribbons),
        'ribbons_tech': len(tech.ribbons),
        'diffs': [{'Ribbon': d.ribbon_label, 'Fiber': d.fiber,
                   'Our Column': _tc_col_title(ours.columns[d.our_col]) if d.our_col is not None else '-',
                   'Tech Column': _tc_col_title(tech.columns[d.tech_col]) if d.tech_col is not None else '-',
                   'Ours': d.ours or '(blank)', 'Tech': d.tech or '(blank)',
                   'Difference': d.kind} for d in diffs],
    }
# ─── tech_compare: end ───
# ═══════════════════════════════════════════════════════════════════════════


def _render_tech_comparison(page, our_xlsx, upload, dest_dir, site_a, site_b):
    """Compare our finished report against the tech's uploaded workbook and
    offer the difference workbook.  Written to `dest_dir` — the same folder
    the splice report went to — as <A>_to_<B>_SpliceReport_vs_Tech.xlsx.
    Cached per (report file, upload) in session_state so a rerun (any widget
    click) doesn't redo the compare or rewrite the file.  Never lets a bad
    tech workbook take the page down: the report above is already saved."""
    _safe = lambda s: ''.join(c if (c.isalnum() or c in ' -_') else '_' for c in str(s)).strip() or 'site'
    try:
        _mtime = os.path.getmtime(our_xlsx)
    except OSError:
        _mtime = 0
    sig = (our_xlsx, _mtime, upload.name, upload.size,
           getattr(upload, 'file_id', None))
    slot = f'{page}_techcmp'
    cached = st.session_state.get(slot)
    if not (cached and cached.get('sig') == sig and os.path.exists(cached.get('xlsx', ''))):
        out_path = os.path.join(dest_dir,
                                f'{_safe(site_a)}_to_{_safe(site_b)}_SpliceReport_vs_Tech.xlsx')
        tmp_tech = None
        try:
            os.makedirs(dest_dir, exist_ok=True)
            fd, tmp_tech = tempfile.mkstemp(suffix='.xlsx', prefix='tech_')
            with os.fdopen(fd, 'wb') as fh:
                fh.write(upload.getvalue())
            summary = tc_compare_reports(our_xlsx, tmp_tech, out_path)
            cached = {'sig': sig, **summary}
            st.session_state[slot] = cached
        except Exception as _exc:
            st.warning(f"Couldn't compare against **{upload.name}**: {_exc}. "
                       "The tech report needs a 'Ribbon' header row with "
                       "'Splice N' columns and a distance row above it.")
            report_error('splice report — tech comparison', _exc,
                         {'tech_file': upload.name, 'our_xlsx': our_xlsx})
            return
        finally:
            if tmp_tech:
                try:
                    os.remove(tmp_tech)
                except OSError:
                    pass
    counts = cached['counts']
    st.markdown('###### Compared Against the Tech\'s Report')
    if cached['n_diffs'] == 0:
        st.success(f"**No differences**: every cell matches **{upload.name}** "
                   f"({cached['columns_matched']} columns lined up).")
    else:
        st.warning(f"**{cached['n_diffs']} differences** vs **{upload.name}**: "
                   + '  ·  '.join(f'{k}: {v}' for k, v in counts.items() if v)
                   + f"  ·  {cached['columns_matched']} of {cached['columns_ours']} "
                     f"columns lined up ({cached['frame']} frame)")
    if cached['columns_matched'] < min(cached['columns_ours'], cached['columns_tech']):
        st.caption("Columns that didn't line up (no column within 250 m in the "
                   "other report) are shown with gray headers; everything in "
                   "them counts as a difference.")
    st.caption(f"Saved to `{cached['xlsx']}`")
    try:
        with open(cached['xlsx'], 'rb') as fh:
            st.download_button('⬇ Differences vs Tech (Excel)', data=fh.read(),
                               file_name=os.path.basename(cached['xlsx']),
                               key=f'{page}_techcmp_dl')
    except OSError:
        pass
    if cached['diffs']:
        with st.expander(f"Difference List ({cached['n_diffs']})"):
            st.dataframe(cached['diffs'], use_container_width=True, hide_index=True)


# How many spans the Splice Report page will chain in one Generate click
# (span 1 + the 'Add span…' boxes).  A ceiling, not a target.
SR_MAX_SPANS = 8


def _sr_span_inputs(span):
    """The A/B input boxes for ONE span of the Splice Report page and the
    optional tech-workbook upload under them.  Span 1 keeps every widget key
    it has always had (the A/B folder slots are shared with the Viewer, the
    hub deep links seed them, tests pin them); every added span (the "Add
    span…" chain, 2026-09-16) uses its own `sr<n>_*` keys so no two spans
    share a folder, a site name or a tech upload.  Returns
    (dir_a, dir_b, tech_upload) -- dirs are '' until both are picked."""
    two, one = SR_MODE_TWO, SR_MODE_ONE
    _k = _sr_span_keys(span)
    k_mode, k_a, k_b = _k['mode'], _k['a'], _k['b']
    k_ba, k_bb, k_bone = _k['browse_a'], _k['browse_b'], _k['browse_one']
    k_one, k_zip, k_tech = _k['one'], _k['zip'], _k['tech']

    # Span 1 with traces loaded in the left panel: the page draws no loader
    # of its own and runs on those (Robert 2026-09-28).  With the panel
    # empty the page loads its own, as before.
    _panel = _panel_traces() if span == 1 else ('', '')
    if span == 1:
        _show_panel_notes()
    if any(_panel):
        dir_a, dir_b = _panel
        mode = None
        if dir_a and dir_b:
            st.caption('Traces: the A and B folders loaded in the left panel.')
            _viewer_removed_note(dir_a, dir_b)
        else:
            st.caption(f"Traces: only the {'A' if dir_a else 'B'} folder is "
                       f"loaded in the left panel. Load the "
                       f"{'B' if dir_a else 'A'} folder there too.")
    else:
        # Input mode: two A/B folders (shared with the Viewer) OR a single
        # folder / .zip that holds both directions (auto-split by direction).
        # The choice, and every box below that the page draws, keep what
        # they show across a trip to another tool (_seed_box).  A dropped
        # file does not: Streamlit does not let code fill an uploader.
        _seed_box(k_mode, [two, one])
        mode = st.radio('Select Traces', [two, one], horizontal=True, key=k_mode,
                        format_func={two: 'Two Folders (A + B)',
                                     one: 'One Folder / Zip (Both Directions)'}.get)
        _keep_box(k_mode)

    if mode is None:
        pass
    elif mode == two and span == 1 and _panel_shown():
        # Span 1's A and B are the sidebar's Trace Folders (Robert
        # 2026-09-26): one place to pick them, shared with the Viewer, so the
        # page shows what is loaded instead of a second pair of boxes.
        # (OTDR Suite App: in a project there is no left panel, so the page
        # draws its own pair, below.)
        dir_a, dir_b = _panel_boxes()
        # Plain text, not a disabled box: a keyed widget would keep its first
        # value= forever (the key + value footgun).
        c1, c2 = st.columns(2)
        for _c, _lbl, _d in ((c1, 'A Folder', dir_a), (c2, 'B Folder', dir_b)):
            with _c:
                st.markdown(f'**{_lbl}**')
                if _d:
                    st.code(_d, language=None)
                else:
                    st.caption('Pick it under **Trace Folders** in the sidebar.')
    elif mode == two:
        _seed_box(k_a)
        _seed_box(k_b)
        c1, c2 = st.columns(2)
        with c1:
            if st.button('📁 A-Direction Folder', use_container_width=True, key=k_ba):
                p = pick_folder('Choose the A-direction folder')
                if p:
                    st.session_state[k_a] = p
            st.text_input('A Folder', key=k_a, placeholder='A-Direction Folder')
        with c2:
            if st.button('📁 B-Direction Folder', use_container_width=True, key=k_bb):
                p = pick_folder('Choose the B-direction folder')
                if p:
                    st.session_state[k_b] = p
            st.text_input('B Folder', key=k_b, placeholder='B-Direction Folder')
        _keep_box(k_a)
        _keep_box(k_b)
        dir_a = _typed_trace_dir(st.session_state.get(k_a), 'A')
        dir_b = _typed_trace_dir(st.session_state.get(k_b), 'B')
    else:
        _seed_box(k_one)
        c1, c2 = st.columns(2)
        with c1:
            if st.button('📁 Folder with BOTH Directions', use_container_width=True,
                         key=k_bone):
                p = pick_folder('Choose a folder containing both directions')
                if p:
                    st.session_state[k_one] = p
            st.text_input('Folder (Both Directions)', key=k_one,
                          placeholder='One Folder with Both Directions '
                                      '(.sor / .json / .trc, or .bdr)')
            _keep_box(k_one)
        with c2:
            zf = st.file_uploader('…or Drop the Span Here: Its Traces '
                                  '(a Whole Folder Works), a .zip, or the '
                                  '.bdr Files Themselves',
                                  type=['zip', 'bdr', 'sor', 'json', 'trc'],
                                  key=k_zip, accept_multiple_files=True)
        dir_a, dir_b = _resolve_bidir_from_single(
            _typed_trace_dir(st.session_state.get(k_one), 'That'), zf)

    # The tech's own splice report (optional).  When one is here, the run
    # also writes a <A>_to_<B>_SpliceReport_vs_Tech.xlsx beside the report
    # that highlights every cell where the two disagree — the tech_compare block.
    # Sits under the A/B inputs on both input modes (the boss's placement).
    tech_xlsx = st.file_uploader(
        "Tech's Splice Report to Compare Against (.xlsx, Optional)",
        type=['xlsx', 'xlsm'], key=k_tech,
        help='Upload the splice report the tech built. After the report runs, '
             'a second workbook highlighting every difference is saved next '
             'to it.')
    return dir_a, dir_b, tech_xlsx


def _typed_trace_dir(raw, label):
    """A folder box on a report page, read the way the Viewer reads its own
    boxes: a .zip, or a folder of zips, becomes its extracted copy.  A zip
    that cannot be read says so and gives ''."""
    typed = (raw or '').strip().strip('"')
    if not typed:
        return ''
    d, note = _resolve_viewer_dir(typed)
    if note and note.startswith('could not'):
        st.warning(f'{label} folder: {note}')
        return ''
    return d


def _sr_site_inputs(span, dir_a, dir_b):
    """The A/B ILA-site boxes for one span, auto-derived from the SOR
    GenParams so the report shows WHICH ILA is the A-direction and which is
    the B-direction (instead of a literal "A"/"B").  Re-derived when the
    folder pair (or the profile) changes; the tech can still override.
    Keyed-state pattern (set session_state BEFORE the widget) — never mix
    value= and key= on a widget we write to.  Returns (site_a, site_b).

    What the boxes show is also kept in a slot no widget owns
    (`{pre}_site_saved`), with the folders it was shown for.  Streamlit drops
    a widget's state on any run that does not draw it, so a trip to another
    tool put the boxes back to "A" and "B", and the pair had not changed, so
    nothing re-derived them: the report came out as A_to_B_SpliceReport.xlsx
    (2026-09-29)."""
    _k = _sr_span_keys(span)
    k_a, k_b, k_src = _k['site_a'], _k['site_b'], _k['site_src']
    k_saved = _k['site_saved']
    # Boxes Streamlit forgot get back what they showed, while the span is
    # the one they showed it for.  Folders loaded or cleared in between get
    # their own names below, or "A" and "B".
    _saved = st.session_state.get(k_saved)
    if _saved and tuple(_saved[0]) == (dir_a, dir_b):
        for _key, _v in zip((k_a, k_b), _saved[1]):
            if _key not in st.session_state:
                st.session_state[_key] = _v
    if dir_a and dir_b and os.path.isdir(dir_a) and os.path.isdir(dir_b):
        # The profile is part of the signature: a tech who loads the span
        # and THEN picks the contract profile must still get the identifier-based
        # names, not the "A"/"B" derived under the profile that was active
        # at load time (hub click-through, 2026-09-15).
        _sig = (dir_a, dir_b, st.session_state.get('otdr_profile'))
        if st.session_state.get(k_src) == PROJECT_SITE_MARK:
            # Just opened from a project: its saved names stand for this pair.
            st.session_state[k_src] = _sig
        elif st.session_state.get(k_src) != _sig:
            _ila_a, _ila_b = _site_names_for(dir_a, dir_b)
            st.session_state[k_a] = _ila_a or 'A'
            st.session_state[k_b] = _ila_b or 'B'
            st.session_state[k_src] = _sig
    st.session_state.setdefault(k_a, 'A')
    st.session_state.setdefault(k_b, 'B')

    s1, s2 = st.columns(2)
    site_a = s1.text_input('A-Direction ILA / Site', key=k_a)
    site_b = s2.text_input('B-Direction ILA / Site', key=k_b)
    st.session_state[k_saved] = ((dir_a, dir_b), (site_a, site_b))
    if site_a and site_b and (site_a, site_b) != ('A', 'B'):
        st.caption(f"📍 **A direction:** {site_a} → {site_b}  ·  "
                   f"**B direction:** {site_b} → {site_a}")
    return site_a, site_b


def _count(n, word):
    """'1 fiber', '12 fibers': a count with its noun, singular for one."""
    return f"{n} {word}" + ('' if n == 1 else 's')


def _sr_column_count(res):
    """The summary line's column count, named the way the workbook names
    them.  A small job lays its closures out as Event columns, and the
    manifest's n_splices counts only kind 'splice', so the line read
    "0 splices" above a grid of flagged Event columns (2026-10-02).  The
    Reburn Summary says "events" for the whole job once any column is an
    event column, and counts those columns: so does this."""
    cols = res.get('columns') or []
    if any(c.get('kind') == 'event' for c in cols):
        return _count(sum(1 for c in cols if c.get('kind') in ('splice', 'event')),
                      'event')
    return _count(res['n_splices'], 'splice')


def _sr_result_slot(_p, span):
    """session_state key roots for one span's finished run: span 1 keeps the
    names every other path reads (`sr_result` / `sr_dirs` — the disk cache,
    the Viewer deep links, the tests); span n>=2 gets an `n` suffix."""
    sfx = '' if span == 1 else str(span)
    return f'{_p}_result{sfx}', f'{_p}_dirs{sfx}'


def _sr_start_next_queued(_p):
    """Hand the next queued span to run_engine_live.  Spans run back to
    back, not at once: the engine is a subprocess with its own staging copy,
    and one at a time is what the progress panel + Cancel were built for.
    Returns True when a run was started."""
    _qk = f'{_p}_queue'
    queue = st.session_state.get(_qk) or []
    if not queue or f'{_p}_pending_cmd' in st.session_state or f'{_p}_job' in st.session_state:
        return False
    run = queue.pop(0)
    st.session_state[_qk] = queue
    st.session_state[f'{_p}_pending_cmd'] = run['cmd']
    st.session_state[f'{_p}_running'] = run
    _rk, _dk = _sr_result_slot(_p, run['span'])
    # The dirs this run used — cell-click deep links carry them so the
    # Viewer (a FRESH session after the anchor nav) can find the span,
    # including one-folder/zip runs staged into temp dirs the viewer was
    # never told about (the boss's 'clicks a cell, trace never loads').
    st.session_state[_dk] = run['dirs']
    st.session_state.pop(_rk, None)                   # clear any prior result
    return True


def _render_sr_result(_p, res, *, span, n_spans, dirs, dest, tech_xlsx,
                      popout, port):
    """One finished span's summary, Excel download and tech comparison —
    plus, for span 1 only, the clickable ribbon grid.  Added spans are
    report-only (Robert, 2026-09-16: "we don't need span 2 to have a grid";
    the Viewer loads from the first span only).  With several spans on the
    page each gets its own block, in the order the tech laid them out."""
    sfx = '' if span == 1 else str(span)
    if n_spans > 1:
        st.markdown(f"##### Span {span}: {res['site_a']} → {res['site_b']}")
    # Summary + Excel download
    st.success(f"{res['site_a']} → {res['site_b']}  ·  "
               f"{_count(res['n_fibers'], 'fiber')}  ·  "
               f"{_sr_column_count(res)}  ·  span {res['span_km']} km  ·  "
               f"{_count(res['n_flagged'], 'flagged event')}")
    xp = res.get('xlsx')
    if xp and os.path.exists(xp):
        with open(xp, 'rb') as fh:
            st.download_button('⬇ Excel Report', data=fh.read(),
                               file_name=os.path.basename(xp), key=f'{_p}_dl{sfx}')
        if tech_xlsx is not None:
            _render_tech_comparison(f'{_p}{sfx}', xp, tech_xlsx, dest,
                                    res['site_a'], res['site_b'])

    if span != 1:
        return                                   # report-only: no grid
    st.markdown('###### Click a Flagged Cell → Jump to It in the Viewer')

    # Build a ribbon × splice-column grid (mirrors the Excel), flagged cells
    # link to ?nav=viewer&fiber=&km= which the hub turns into a viewer deep-link.
    cols = res['columns']
    ribbon_size = res['ribbon_size']
    # max_fiber lays the grid out; n_fibers is how many were loaded (a
    # manifest from before max_fiber carried the highest fiber there).
    n_fibers = res.get('max_fiber') or res['n_fibers']
    n_ribbons = (n_fibers + ribbon_size - 1) // ribbon_size
    # Rows from the first ribbon holding a loaded fiber to the last; an empty
    # ribbon between them keeps its row (a gap the tech should see).
    _rl = res.get('ribbons') or []
    ribbon_rows = (list(range(min(_rl), max(_rl) + 1)) if _rl
                   else list(range(n_ribbons)))
    # group flagged cells by (ribbon, column index)
    by_rc = {}
    for c in res['cells']:
        ri = (c['fiber'] - 1) // ribbon_size
        by_rc.setdefault((ri, c['splice']), []).append(c)

    def hdr(col):
        tag = f"S{col['num']}" if col['kind'] == 'splice' and col['num'] else col['kind'].title()
        return f"<div style='font-weight:600'>{tag}</div><div style='font-size:10px;color:#000000'>{col['km']:.3f} km</div>"

    html = ['<div style="overflow:auto;max-height:62vh;border:1px solid #c9d5e1;border-radius:4px;color:#000000;background:#ffffff">',
            '<table style="border-collapse:collapse;font-size:11px;font-family:Consolas,monospace">',
            '<thead><tr><th style="position:sticky;top:0;left:0;z-index:2;background:#eef3f8;padding:4px 8px;border:1px solid #dbe4ee">Ribbon</th>']
    for col in cols:
        html.append(f"<th style='position:sticky;top:0;z-index:1;padding:4px 8px;border:1px solid #dbe4ee;background:#eef3f8;white-space:nowrap'>{hdr(col)}</th>")
    html.append('</tr></thead><tbody>')
    # Viewer frame conversion (the manifest is the report on screen).
    _mani = res
    _launch_a = float(_mani.get('launch_a_km') or 0.0)
    def _vkm(km):
        return round(float(km) + _launch_a, 4)
    _sd = dirs or (None, None)
    from urllib.parse import quote as _q
    _dirs_qs = ''
    if _sd[0] and os.path.isdir(_sd[0]):
        _dirs_qs += f"&sra={_q(_sd[0])}"
    if _sd[1] and os.path.isdir(_sd[1]):
        _dirs_qs += f"&srb={_q(_sd[1])}"
    _dirs_qs += _panel_qs()
    for ri in ribbon_rows:
        f0, f1 = ri * ribbon_size + 1, min((ri + 1) * ribbon_size, n_fibers)
        html.append(f"<tr><td style='position:sticky;left:0;background:#f7fafc;padding:3px 8px;border:1px solid #e3e9f0;white-space:nowrap'>F{f0}–{f1}</td>")
        for ci, col in enumerate(cols):
            cell = by_rc.get((ri, ci), [])
            if not cell:
                html.append("<td style='padding:3px 6px;border:1px solid #eef2f6'></td>")
                continue
            links = []
            for c in sorted(cell, key=lambda x: x['fiber']):
                color = _CAT_COLOR.get(c['category'], '#000000')
                loss = '' if c['loss'] is None else f" {c['loss']:.3f}"
                links.append(_cell_markup(
                    popout, c['fiber'], _vkm(c['km']), 'both', color,
                    c['label'], f"F{c['fiber']}{loss}",
                    href=(f"?nav=viewer&fiber={c['fiber']}&km={_vkm(c['km'])}"
                          f"&dir=both{_dirs_qs}&src={_p}")))
            html.append("<td style='padding:3px 6px;border:1px solid #eef2f6;white-space:nowrap'>"
                        + "<br>".join(links) + "</td>")
        html.append('</tr>')
    html.append('</tbody></table></div>')
    if popout:
        _render_clickable_grid(''.join(html), port, src=_p)
    else:
        st.markdown(''.join(html), unsafe_allow_html=True)


# ── Show / hide in report ─────────────────────────────────────────────
# Its own box, apart from the threshold settings: these switches never
# change what the engine finds, only which findings the report prints.
# Anything switched off is listed on the workbook's Display sheet.
_SHOW_ROWS = [('loss', 'Splice Loss'), ('bend', 'Bend/Damage'),
              ('break', 'Breaks')]


def _render_show_hide_box(prefix, rows=_SHOW_ROWS):
    """One toggle per row, all on by default.  Returns the dict for --show, or
    None when everything is shown (the engine default).

    The switches are remembered in a plain session_state slot of their own
    (`{prefix}_show_saved`), not only in the toggles.  Streamlit drops a
    widget's state on any run that does not draw it, so leaving the page
    (the Viewer, another tool) and coming back put every switch back ON --
    inside a collapsed box, where nobody sees it -- and the next report
    printed everything the tech had hidden (the boss, 2026-09-26).  The
    threshold panel survives the same trip because it keeps its own slot
    too (otdr_settings)."""
    saved = st.session_state.setdefault(f'{prefix}_show_saved', {})
    with st.expander('Show/Hide in Report', expanded=False):
        st.caption('Switch a category off to leave it out of the report. '
                   'Connectors and Reflectance cover the connectors in the '
                   'end columns, each on its own; anything at a splice always '
                   'shows. The report gets a Display sheet listing what was '
                   'hidden.')
        show = {}
        for k, label in rows:
            wkey = f'{prefix}_show_{k}'
            # Seed a toggle Streamlit forgot from the saved slot.  Never
            # value= as well: key + value on one widget is the trap in
            # feedback_streamlit_widget_state.
            if wkey not in st.session_state:
                st.session_state[wkey] = saved.get(k, True)
            show[k] = saved[k] = st.toggle(label, key=wkey)
    return None if all(show.values()) else show


def page_splice_report():
    _p = 'sr'
    _cache_name = '.sr_grid_cache.json'
    st.markdown('#### Bidirectional Splice Report')
    st.caption('Generates the Excel report (saved to '
               + ("the project's **Reports** folder" if _PROJECT_MODE else 'your **Downloads**')
               + ') and a '
               'clickable grid: click any flagged cell to jump to that fiber and '
               'splice in the Viewer.'
               + ('' if (_chosen_traces() or any(_panel_traces())) else
                  ' Give it two A/B folders, or one folder / .zip holding both '
                  'directions.'))

    # Customer profile first, above the A/B boxes: default or a customer's
    # thresholds, chosen before the span is picked.  Guarded the same way as
    # the settings panel below — a failure here must not take the page down.
    _shoot = _chosen_traces()
    if _shoot:
        # The traces were chosen before the page opened (a project's Run In…,
        # Quick Analysis' Load screen): their folders and site names, nothing
        # to pick; the customer stays a choice (Robert, 2026-09-27).
        _render_chosen_line(_shoot)
        _render_profile_picker_box('splice report')
        dir_a, dir_b, tech_xlsx = _shoot['a'], _shoot['b'], None
        _k1 = _sr_span_keys(1)
        site_a = st.session_state.get(_k1['site_a']) or ''
        site_b = st.session_state.get(_k1['site_b']) or ''
        if not (site_a and site_b):
            try:
                _sa, _sb = _site_names_for(dir_a, dir_b)
            except Exception:
                _sa, _sb = '', ''
            site_a, site_b = site_a or _sa or 'A', site_b or _sb or 'B'
        st.session_state['sr_n_spans'] = 1
    else:
        _render_profile_picker_box('splice report')

        # Span 1: the A/B boxes (+ optional tech workbook) and the site names.
        dir_a, dir_b, tech_xlsx = _sr_span_inputs(1)
        site_a, site_b = _sr_site_inputs(1, dir_a, dir_b)

    # ── More spans (Robert, 2026-09-16) ──────────────────────────────────
    # A tech who shot several spans in one trip chains them on: under span 1
    # an "Add span…" button opens span 2's A/B boxes (+ its own optional tech
    # workbook); under span 2 the same button opens span 3, and so on.
    # Generate then runs every span independently, back to back, and saves
    # ALL the reports to the one 'Save reports to' folder chosen below.  Only
    # span 1 gets the clickable grid / the Viewer — the added spans are
    # report generation only.  Off by default: one span is the page everyone
    # knows.  Only the LAST span can be removed, so span numbers never shift
    # under a tech's inputs.
    st.session_state.setdefault('sr_n_spans', 1)
    n_spans = max(1, int(st.session_state['sr_n_spans']))
    extra = {}                          # span -> (dir_a, dir_b, site_a, site_b, tech)
    for _n in range(2, n_spans + 1):
        st.markdown('---')
        h1, h2 = st.columns([3, 1])
        h1.markdown(f'**Span {_n}**: its own A/B folders; runs after span {_n - 1} '
                    'and saves to the same folder.')
        if _n == n_spans and h2.button(f'✖ Remove Span {_n}', key=f'sr_del_span{_n}',
                                       use_container_width=True):
            st.session_state['sr_n_spans'] = _n - 1
            # Drop its finished result too — a report block for a span the
            # tech removed would be a stale page.  What its boxes kept goes
            # with it (_seed_box, _sr_site_inputs): a span added again
            # starts empty, its site names at "A" and "B".
            for _k in (f'{_p}_result{_n}', f'{_p}_dirs{_n}', f'{_p}{_n}_techcmp',
                       f'{_p}{_n}_site_saved', f'{_p}{_n}_input_mode_saved',
                       f'{_p}{_n}_dir_a_saved', f'{_p}{_n}_dir_b_saved',
                       f'{_p}{_n}_one_folder_saved'):
                st.session_state.pop(_k, None)
            st.rerun()
        _da, _db, _tech = _sr_span_inputs(_n)
        _sa, _sb = _sr_site_inputs(_n, _da, _db)
        extra[_n] = (_da, _db, _sa, _sb, _tech)
    if n_spans < SR_MAX_SPANS and not _shoot:
        if st.button('➕ Add Span…', key='sr_add_span',
                     help='Run another span in the same click: its own A/B '
                          'folders and its own report, saved to the same folder.'):
            st.session_state['sr_n_spans'] = n_spans + 1
            st.rerun()
    if n_spans > 1:
        st.markdown('---')

    # ── OTDR settings panel (pixel-perfect EXFO threshold table) ─────────
    # Renders the custom HTML component (the customer-profile dropdown sits
    # above the A/B boxes now — _render_customer_profile_picker).
    # The values it commits land in session_state.otdr_settings and become
    # the engine overrides forwarded to the subprocess on Generate.
    # Rendered BEFORE the folder guard (2026-07-31, Robert's ask): the panel
    # needs nothing from the span, and a tech should be able to set customer
    # thresholds first and then load data — previously an empty page showed
    # no settings at all, which reads as "there is no settings tab".
    # Guarded: a settings-panel failure (component path quirk, Streamlit
    # version) must NOT take down the core Splice Report — fall back to the
    # engine's default thresholds with a visible warning.
    # The name is grey while a customer's own settings are in use, black
    # once the tech changes them (Robert, 2026-09-27).  Styled, not renamed:
    # a new name would re-draw the box and close it mid-edit.
    _settings_grey = (st.session_state.get('otdr_profile') not in _NOT_CUSTOMERS_PROFILES
                      and not _profile_settings_changed())
    st.markdown('<style>.st-key-sr_settings_box summary p{color:'
                + ('#8a939e' if _settings_grey else 'var(--otdr-text)') + '!important}</style>',
                unsafe_allow_html=True)
    # version, an App Control block) must NOT take down the page.  But the
    # report does not run unless the WHOLE box drew, the threshold table and
    # the Connector & Launch knobs both (Robert 2026-09-28: "block the report",
    # then "block it if any part fails").  The fallback used to say "default
    # thresholds" and then send every row as UNticked, so the report flagged
    # nothing; a report at thresholds the tech never saw is worse than no
    # report.  The same box sits on the Viewer and Unidirectional pages
    # (_render_settings_box), which still fall back to the engine defaults.
    with st.container(key='sr_settings_box'):
        _settings_exc = _render_settings_box('splice report', blocks_report=True)
    if _settings_exc is not None:
        # Outside the box, which is collapsed: the tech sees why Generate
        # is off before picking folders.
        _settings_block_notice(_settings_exc, 'Generate')
    sr_show = _render_show_hide_box(
        'sr', _SHOW_ROWS + [('conn', 'Connectors'), ('refl', 'Reflectance')])

    if not (dir_a and os.path.isdir(dir_a) and dir_b and os.path.isdir(dir_b)):
        st.info('Pick **both** an A and a B folder (a bidirectional report needs both).')
        return
    _remove_legacy_caches(dir_a)
    _remove_legacy_caches(dir_b)
    _not_ready = [n for n, (a, b, *_r) in extra.items()
                  if not (a and os.path.isdir(a) and b and os.path.isdir(b))]
    if _not_ready:
        st.info('Span ' + ', '.join(str(n) for n in _not_ready) + ' needs **both** an '
                'A and a B folder too, or remove it to run without it.')
    _seen = {(dir_a, dir_b): 1}
    for _n, (_da, _db, *_r) in extra.items():
        if _n in _not_ready:
            continue
        _remove_legacy_caches(_da)
        _remove_legacy_caches(_db)
        if (_da, _db) in _seen:
            st.warning(f'Span {_n} points at the same folders as span {_seen[(_da, _db)]}. '
                       'The two reports will be identical.')
        _seen.setdefault((_da, _db), _n)

    st.caption("⏳ Large spans can take several minutes. After you click you'll see "
               "live progress here. **Leave this window open and don't refresh.**")
    # Downloads by default -- NOT the traces folder (which in one-folder/zip
    # mode is a temp dir that gets cleaned up) -- and the tech can point it.
    # ONE destination for the page: every span's report lands in it.
    import folder_intake as _fi
    _sr_dest = _report_dest_row('sr_report_dest', _fi.default_report_dir())
    _stale = _report_gate('sr')
    _gen_label = (f'Generate Splice Reports ({n_spans} spans)'
                  if n_spans > 1 else 'Generate Splice Report')
    # Checked twice: a click made while the table was up still arrives on
    # the run where it failed to draw, disabled or not.
    _no_settings = _settings_exc is not None
    if st.button(_gen_label, type='primary',
                 disabled=bool(_stale) or bool(_not_ready) or _no_settings) \
            and not _no_settings:
        _safe = lambda s: ''.join(c if (c.isalnum() or c in ' -_') else '_' for c in str(s)).strip() or 'site'
        _suffix = '_SpliceReport.xlsx'
        # Read the panel values straight out of session_state (which the
        # component's auto-commit keeps current) and translate to engine
        # globals: the threshold table, the connector/launch knobs and the
        # profile's engine settings, all in _report_overrides.  This is the
        # value the run actually uses — see the iframe-state footgun note in
        # _render_otdr_settings_panel.  The Viewer's own run reads the same.
        overrides = _report_overrides()
        # The profile's contract figures go to the audit, keyed off the
        # ACTIVE profile; most profiles declare none, so their runs are
        # byte-identical to before.
        _prof_name = st.session_state.get('otdr_profile')
        _contract = _contract_from_profile(_prof_name)
        # One queue entry per span; the same profile / thresholds / contract
        # apply to all of them (they were chosen once, above the boxes).
        spans = [(1, dir_a, dir_b, site_a, site_b)]
        for _n in sorted(extra):
            _da, _db, _sa, _sb, _t = extra[_n]
            spans.append((_n, _da, _db, _sa, _sb))
        queue, used_names = [], set()
        _prune_viewer_tables(VIEWER_TABLES_KEPT - len(spans))
        for _n, _da, _db, _sa, _sb in spans:
            _name = f'{_safe(_sa)}_to_{_safe(_sb)}{_suffix}'
            if _name in used_names:                   # same sites twice → keep both files
                _name = f'{_safe(_sa)}_to_{_safe(_sb)}_span{_n}{_suffix}'
            used_names.add(_name)
            out_xlsx = _project_run_path(_sr_dest, _name, traces=(_da, _db),
                                         taken=[q['out'] for q in queue])
            queue.append({'span': _n, 'dirs': (_da, _db), 'out': out_xlsx,
                          'cmd': splicereport_cmd(_run_folder(_da), _run_folder(_db),
                                                  out_xlsx, _sa, _sb,
                                                  contract=_contract,
                                                  overrides=overrides,
                                                  show=sr_show,
                                                  viewer_table=_viewer_table_path(_da, _db))})
        st.session_state[f'{_p}_queue'] = queue
        for _n in range(1, SR_MAX_SPANS + 1):
            _rk, _dk = _sr_result_slot(_p, _n)
            st.session_state.pop(_rk, None)           # clear any prior result
            if _n > 1:
                st.session_state.pop(_dk, None)
        _sr_start_next_queued(_p)
        st.rerun()

    # Background run with a live progress panel + Cancel; the engine runs as a
    # concurrent subprocess so the page never freezes.  Stashes sr_result on
    # done, then starts the next queued span (if any) and reruns.
    if f'{_p}_pending_cmd' in st.session_state or f'{_p}_job' in st.session_state:
        _run = st.session_state.get(f'{_p}_running') or {'span': 1, 'dirs': (dir_a, dir_b)}
        _n_total = 1 + len(st.session_state.get(f'{_p}_queue') or []) + (_run['span'] - 1)
        _which = f" (span {_run['span']} of {_n_total})" if _n_total > 1 else ''
        _rdir_a, _rdir_b = _run['dirs']
        try:
            proc = run_engine_live(_p, running_title='Generating the splice report'
                                   + _which)
        except subprocess.TimeoutExpired:
            st.error(f'Splice report{_which} timed out after {ENGINE_TIMEOUT_S}s '
                     'and was stopped. Try fewer files, or check for a '
                     'wedged engine.')
            report_error('splice report (hub) — timeout',
                         RuntimeError(f"engine exceeded {ENGINE_TIMEOUT_S}s"),
                         {'dir_a': _rdir_a, 'dir_b': _rdir_b})
            proc = None
        if proc is None and f'{_p}_job' in st.session_state:
            return                  # still running: the panel is the page
        if proc is None and f'{_p}_job' not in st.session_state:
            # Cancelled or timed out: the rest of the queue goes with it — a
            # tech who hit Cancel did not ask for span 2 to start.
            st.session_state.pop(f'{_p}_queue', None)
        if proc is not None:
            manifest = _parse_manifest(proc.stdout)
            if manifest is None or not manifest.get('ok'):
                if not _engine_damaged_notice(proc.stderr, 'sr'):
                    st.error(f"Span {_run['span']}: " * (_n_total > 1)
                             + (manifest or {}).get('error', 'Splice report failed.'))
                    with st.expander('Engine Log'):
                        st.code(proc.stderr[-4000:] or '(no output)')
                report_error('splice report (hub)',
                             RuntimeError((manifest or {}).get('error', 'no manifest')),
                             {'dir_a': _rdir_a, 'dir_b': _rdir_b},
                             log=proc.stderr)
            else:
                _rk, _dk = _sr_result_slot(_p, _run['span'])
                st.session_state[_rk] = manifest
                # Disk cache (same idea as Secret Sauce's pairs_cache.json):
                # a cell-click into the Viewer is a URL nav that WIPES
                # session_state — this file is how "← Back" re-shows the grid
                # without re-running the multi-minute engine.  Span 1 only:
                # the added spans have no grid to bring back.
                try:
                    _sd = st.session_state.get(_dk) or (None, None)
                    if _run['span'] == 1 and _sd[0] and os.path.isdir(_sd[0]):
                        with open(_hub_cache_path(_cache_name, _sd[0]),
                                  'w', encoding='utf-8') as fh:
                            json.dump({'manifest': manifest, '_dirs': list(_sd)}, fh)
                except Exception:
                    pass
            # This span done (or failed): the next queued one starts now.
            if _sr_start_next_queued(_p):
                st.rerun()

    res = st.session_state.get(f'{_p}_result')
    if not (res and res.get('ok')):
        # Back from the Viewer (or any session reset): restore the last grid
        # from the disk cache.  Candidate dirs: this page's own sr_dirs if it
        # survived, else the viewer slots the deep link seeded (sra/srb).
        for _cand in (st.session_state.get(f'{_p}_dirs'), _panel_boxes()):
            if not (_cand and _cand[0] and os.path.isdir(_cand[0])):
                continue
            try:
                with open(_hub_cache_path(_cache_name, _cand[0]),
                          encoding='utf-8') as fh:
                    _cached = json.load(fh)
                # A cache written by the retired beta page (2026-09-21)
                # carries manifest.fr = True; never show its grid here.
                if (_cached.get('manifest', {}).get('ok')
                        and not _cached.get('manifest', {}).get('fr')
                        and _cached.get('_dirs', [None])[0] == _cand[0]):
                    res = _cached['manifest']
                    st.session_state[f'{_p}_result'] = res
                    st.session_state[f'{_p}_dirs'] = tuple(_cached['_dirs'])
                    break
            except Exception:
                continue
    if not (res and res.get('ok')):
        res = None
    # Which finished spans are on screen, in order: span 1 first (grid +
    # Viewer), then every added span's report block.
    shown = []
    if res is not None:
        shown.append((1, res, st.session_state.get(f'{_p}_dirs') or (None, None), tech_xlsx))
    for _n in range(2, SR_MAX_SPANS + 1):
        _r = st.session_state.get(f'{_p}_result{_n}')
        if _r and _r.get('ok'):
            _t = extra[_n][4] if _n in extra else None
            shown.append((_n, _r, st.session_state.get(f'{_p}_dirs{_n}') or (None, None), _t))
    if not shown:
        return

    _port = ensure_trace_server()
    _popout = _viewer_click_target(_p)
    # The Viewer follows ONE span — the first one loaded, as it always has
    # (Robert, 2026-09-16: "viewer only needs to load from the first span").
    _follow = shown[0]
    res, _sd = _follow[1], _follow[2]
    if _sd[0] and os.path.isdir(_sd[0]):
        trace_server.set_dirs(_sd[0], _sd[1] if (_sd[1] and os.path.isdir(_sd[1])) else None)
    # ...and at the gates THIS report ran at, so a cell that is unflagged in
    # the grid is unflagged in the Viewer.  Without this the Viewer judged
    # every run at the engine baseline while a customer profile had moved the
    # engine (the contract profile 0.200 vs 0.160 — a 40 mdB band where the two disagreed).
    # Sourced from the manifest, which is the report on screen: it is the run's
    # own echo of what it applied, and it rides the disk cache too, so a grid
    # restored after 'Back' keeps its own gates instead of the panel's current
    # ones.  Absent (an older cached manifest) → None → baseline, as before.
    trace_server.set_thresholds(res.get('thresholds'), source='sr')
    trace_server.set_end_refl(res.get('end_refl'))
    trace_server.set_panel_span(res.get('panel_span'))
    trace_server.set_suite_table(res.get('viewer_table'))

    _clear_report_button(_p)
    for _n, _r, _d, _t in shown:
        _render_sr_result(_p, _r, span=_n, n_spans=len(shown), dirs=_d,
                          dest=_sr_dest, tech_xlsx=_t, popout=_popout, port=_port)


# ═════════════════════════════════════════════════════════════════════════
#  PAGE: Unidirectional (A-only one-shot)  — splice report engine, --uni mode
# ═════════════════════════════════════════════════════════════════════════
def uni_cmd(folder, out_xlsx, direction=None, overrides=None, landmarks=None,
            show=None, site_a=None, site_b=None):
    """Argv for the unidirectional one-shot — the splice report engine's
    --uni mode (same subprocess, same sor_reader isolation, ZK-format
    workbook out).  `site_a` / `site_b` are the names in the page's A-End /
    B-End boxes; the engine prints them in the direction of the shot."""
    common = ['--uni', '--dir-a', folder, '--out', out_xlsx,
              '--analysis', analysis_mode()]
    if site_a:
        common += ['--site-a', site_a]
    if site_b:
        common += ['--site-b', site_b]
    if direction:
        common += ['--direction', direction]
    if landmarks:
        common += ['--landmarks', json.dumps(landmarks)]
    if show:
        common += ['--show', json.dumps(show)]
    if overrides:
        common += ['--overrides', json.dumps(overrides)]
    if FROZEN:
        return [sys.executable, '--run-splicereport', *common]
    return [sys.executable, os.path.join(SPLICEREPORT_DIR, 'run_splicereport.py'), *common]


# ── Uni settings box ─────────────────────────────────────────────────────
# Most of the OTDR Settings rows are BIDIRECTIONAL thresholds the uni engine
# never reads; the three that mean the same thing in one direction drive it
# from there (_OTDR_KEY_TO_UNI_GLOBAL).  Uni's own knobs are the other UNI_*
# engine globals (plus ribbon size); this spec drives the panel below and
# crosses to the engine exactly like the SR panel does (--overrides JSON →
# the runner's setattr block, which runs BEFORE the --uni branch).
# `default` values are drift-locked to the engine by test_uni_settings.py.
#  Unidirectional settings — rendered by the SAME custom component as the
#  Splice Report / FR panels (components/otdr_settings), in 'knobs' mode.
#  It used to be a collapsed st.expander full of bare number boxes, which
#  read as "the uni page has no settings" next to the EXFO-styled table on
#  the other two report pages.
#
#  Rows are one of two kinds:
#    'range'  — a genuine low/high band, both ends real
#    'scalar' — a single knob, its input spanning both value columns
#
#  A row maps to one engine global per slot, so the band rows write two.
#  The return value is still {GLOBAL_NAME: number}, exactly what uni_cmd
#  feeds to --overrides, so nothing downstream changed.
_UNI_ROWS = [
    {'key': 'flag_threshold', 'label': 'Flag Threshold', 'unit': 'dB',
     'kind': 'scalar', 'globals': {'value': 'UNI_BEND_THRESHOLD'},
     'defaults': {'value': 0.250}, 'min': 0.005, 'max': 2.0, 'step': 0.005,
     'int': False,
     'help': 'A-side event this far off a validated closure is flagged.'},

    {'key': 'min_pop', 'label': 'Min Fibers for a Splice Column', 'unit': 'fibers',
     'kind': 'scalar', 'globals': {'value': 'UNI_MIN_POP_SPLICE'},
     'defaults': {'value': 20}, 'min': 2, 'max': 500, 'step': 1, 'int': True,
     'help': 'Population in a 1 km bin needed to call a candidate closure. '
             'A job of 50 fibers or fewer lists its events instead.'},

    {'key': 'closure_radius', 'label': 'At-Splice Radius', 'unit': 'km',
     'kind': 'scalar', 'globals': {'value': 'UNI_CLOSURE_MATCH_KM'},
     'defaults': {'value': 0.075}, 'min': 0.005, 'max': 1.0, 'step': 0.005,
     'int': False,
     'help': 'How close an event must sit to a closure to count as at it.'},

    # The mid-span reflectance band (UNI_REFL_FLOOR_DB / UNI_REFL_CEIL_DB)
    # and the one-direction connector gate (UNI_CONN_LOSS_DB) moved to the
    # OTDR Settings on 2026-09-28: the same rows there drive both reports
    # (_OTDR_KEY_TO_UNI_GLOBAL).  What they do on a Uni run is unchanged:
    # a glint flags at or above the band's floor and below its ceiling, and
    # is confirmed as a spike in the raw trace; a connector flags at or above
    # the gate in the one direction shot, an upper bound on its true loss
    # because one direction cannot separate the backscatter step between the
    # fibers it joins.

    {'key': 'break_floor', 'label': 'Break Floor: Min EOF', 'unit': 'km',
     'kind': 'scalar', 'globals': {'value': 'UNI_BREAK_MIN_KM'},
     'defaults': {'value': 0.3}, 'min': 0.05, 'max': 10.0, 'step': 0.05,
     'int': False,
     'help': 'A fiber ending below this is too short to count as a break.'},

    {'key': 'break_short_by', 'label': 'Break: EOF Short of Span By', 'unit': 'km',
     'kind': 'scalar', 'globals': {'value': 'UNI_BREAK_PREMATURE_KM'},
     'defaults': {'value': 3.0}, 'min': 0.1, 'max': 50.0, 'step': 0.1,
     'int': False,
     'help': 'A fiber ending this far short of the cable end is a break.'},

    {'key': 'end_region', 'label': 'End Exclusion, Full-Span Fibers', 'unit': 'km',
     'kind': 'scalar', 'globals': {'value': 'UNI_END_REGION_KM'},
     'defaults': {'value': 0.5}, 'min': 0.0, 'max': 10.0, 'step': 0.1,
     'int': False,
     'help': 'Tail of a fiber that reaches the far end, excluded from flags.'},

    {'key': 'zone_certify', 'label': 'Damage-Zone Certify Radius', 'unit': 'km',
     'kind': 'scalar', 'globals': {'value': 'UNI_DAMAGE_ZONE_BREAK_KM'},
     'defaults': {'value': 0.5}, 'min': 0.05, 'max': 5.0, 'step': 0.05,
     'int': False,
     'help': 'A damage anchor this close to a break column certifies the zone.'},

    {'key': 'zone_anchor', 'label': 'Damage-Zone Anchor Confirm', 'unit': 'dB',
     'kind': 'scalar', 'globals': {'value': 'UNI_PREBREAK_CONFIRM_DB'},
     'defaults': {'value': 0.03}, 'min': 0.005, 'max': 1.0, 'step': 0.005,
     'int': False,
     'help': 'Step a stored zone event must show in the trace to anchor a zone.'},

    {'key': 'zone_member', 'label': 'Zone Membership Floor (Stored / Sweep)',
     'unit': 'dB',
     'kind': 'range', 'globals': {'low': 'UNI_PREBREAK_STORED_DB',
                                  'high': 'UNI_PREBREAK_MEMBER_DB'},
     'defaults': {'low': 0.02, 'high': 0.03},
     'min': 0.001, 'max': 1.0, 'step': 0.005, 'int': False,
     'help': ('Two floors for two evidence classes. Low applies when the '
              'stored table and the trace agree; high applies to sweep-only '
              'membership, where the bar is higher because nothing '
              'corroborates it (control noise tops out near 0.026 dB).')},

    {'key': 'landmark_radius', 'label': 'Landmark Radius (Demote / Label)',
     'unit': 'km',
     'kind': 'range', 'globals': {'low': 'UNI_LANDMARK_DEMOTE_KM',
                                  'high': 'UNI_LANDMARK_MATCH_KM'},
     'defaults': {'low': 0.10, 'high': 0.15},
     'min': 0.01, 'max': 2.0, 'step': 0.01, 'int': False,
     'help': ('Nested radii. Within the high radius a landmark prints on the '
              'Handholes row; within the tighter low radius a NON-closure '
              'landmark also demotes a splice column to Bend/Damage.')},

    {'key': 'ribbon_size', 'label': 'Ribbon Size', 'unit': 'fibers',
     'kind': 'scalar', 'globals': {'value': 'RIBBON_SIZE'},
     'defaults': {'value': 12}, 'min': 1, 'max': 48, 'step': 1, 'int': True,
     'help': 'Fibers per grid row.'},
]

# Flat {global: default} view, for seeding and for the overrides return.
_UNI_DEFAULTS = {g: row['defaults'][slot]
                 for row in _UNI_ROWS
                 for slot, g in row['globals'].items()}


def _uni_settings_state():
    """The committed uni settings, {global: number}, seeded from defaults."""
    cur = st.session_state.get('uni_settings')
    if not isinstance(cur, dict):
        cur = dict(_UNI_DEFAULTS)
        st.session_state.uni_settings = cur
    # Heal a stored dict from an older build that lacks a newer knob.
    for g, d in _UNI_DEFAULTS.items():
        cur.setdefault(g, d)
    # ...and drop a knob that left this box (the reflectance band and the
    # connector gate moved to the OTDR Settings): a stale value here would
    # ride --overrides and beat the OTDR Settings row that now owns it.
    for g in [g for g in cur if g not in _UNI_DEFAULTS]:
        del cur[g]
    return cur


def _fiber_ranges(nums):
    """[313..324] → '313-324' — a tech reads ribbons, not 12 loose numbers."""
    out, run = [], []
    for n in sorted(nums):
        if run and n == run[-1] + 1:
            run.append(n)
            continue
        if run:
            out.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else f"{run[0]}")
        run = [n]
    if run:
        out.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else f"{run[0]}")
    return ', '.join(out)


def _render_uni_settings_panel():
    """Uni settings, rendered by the shared EXFO-styled component in 'knobs'
    mode.  Returns {global: number} for uni_cmd's --overrides.

    Same iframe-state discipline as the Splice Report panel: the component
    auto-commits on every edit, but we do not trust that alone — the return
    value is read into session_state here and the run reads the SAME slot,
    so what the panel shows is what reaches the engine.
    """
    import math          # module-local, matching _render_otdr_settings_panel
    from components.otdr_settings import otdr_settings as otdr_settings_component

    cur = _uni_settings_state()

    with st.expander('Unidirectional Settings (Thresholds & Bands)', expanded=False):
        rows = []
        for row in _UNI_ROWS:
            rows.append({
                'key':       row['key'],
                'label':     row['label'],
                'unit':      row['unit'],
                'supported': True,
                'kind':      row['kind'],
                'initial':   {slot: cur[g] for slot, g in row['globals'].items()},
                'defaults':  dict(row['defaults']),
                'min':       row['min'],
                'max':       row['max'],
                'step':      row['step'],
                'help':      row['help'],
            })
        commit = otdr_settings_component(rows, default=None, mode='knobs',
                                         key='uni_settings_component')
        if commit:
            for row in _UNI_ROWS:
                got = commit.get(row['key']) or {}
                for slot, g in row['globals'].items():
                    v = got.get(slot)
                    if v is None:
                        continue          # blank / non-finite: keep committed
                    try:
                        v = int(round(float(v))) if row['int'] else float(v)
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if not (isinstance(v, int) or math.isfinite(v)):
                        continue
                    cur[g] = v
            st.session_state.uni_settings = cur

        changed = {g: v for g, v in cur.items() if v != _UNI_DEFAULTS[g]}
        if changed:
            st.caption('Active overrides: '
                       + ', '.join(f'`{g}` = {v}' for g, v in sorted(changed.items())))
        else:
            st.caption('All settings at their defaults.')

    return dict(cur)


# Staging dirs for drag-and-dropped inputs, keyed on the drop's upload ids
# (name and size only when a file has no id) so Streamlit reruns reuse the
# dir instead of re-writing hundreds of files every rerun.  An upload id is
# new for every drop, so dropping different files that share names and sizes
# never gets an older drop's staging back.
_DROP_STAGE_CACHE = _rerun_caches()['drop']


def _stage_dropped(files):
    """Stage drag-and-dropped uploads into a flat working folder on disk.

    Browsers never expose a dropped file's real filesystem path — content
    arrives as bytes — so the engines (which need a FOLDER) get this staging
    dir instead.  Accepts loose trace files and/or .zip archives (extracted
    via folder_intake.extract_zip, zip-slip-guarded).  Dropping a whole
    folder works in Chromium browsers: the drop enumerates the folder's
    files — flat, with no folder path, so two subfolders that name their
    traces alike arrive as one name twice.  The second is NOT written (this
    dir goes straight to an engine, which inventories it with os.listdir and
    would never see a nested copy) and comes back in `dupes` for the page to
    warn about; it used to overwrite the first in silence.  Returns
    (staging_dir, n_trace_files, dupes)."""
    import tempfile
    import folder_intake as fi
    sig = tuple(sorted((getattr(f, 'file_id', '') or '', f.name,
                        getattr(f, 'size', 0)) for f in files))
    hit = _DROP_STAGE_CACHE.get(sig)
    if hit and os.path.isdir(hit[0]):
        return hit
    td = tempfile.mkdtemp(prefix='otdr_drop_')
    dupes = []
    for f in [x for x in files if x.name.lower().endswith('.zip')]:
        try:
            fi.extract_zip(f, td)
        except Exception as exc:
            print(f'drop staging: skipped {f.name}: {exc}')
    loose = [x for x in files if not x.name.lower().endswith('.zip')]
    try:
        # One call, so repeated names are seen as repeats.  It writes as it
        # goes, so anything staged before a failure is still counted below.
        _written, dupes = fi.stage_uploads(loose, td, nest_duplicates=False)
    except Exception as exc:
        print(f'drop staging: {exc}')
    n = len(fi.find_otdr_files(td))
    _remember(_DROP_STAGE_CACHE, sig, (td, n, dupes))
    return td, n, dupes


def _parse_landmarks_text(text):
    """Parse the uni page's landmarks box: one per line, 'km, label' or
    'km, label, splice'.  The trailing 'splice'/'closure' word marks a KNOWN
    closure (labels the column, never demotes it); anything else is a
    non-closure landmark (handhole, replaced section, vault …) which demotes
    an overlapping splice column.  Bad lines are skipped, returned for
    surfacing."""
    landmarks, bad = [], []
    for raw in (text or '').splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        parts = [p.strip() for p in line.split(',')]
        try:
            km = float(parts[0])
        except (ValueError, IndexError):
            bad.append(raw)
            continue
        closure = len(parts) > 2 and parts[-1].lower() in ('splice', 'closure')
        label_parts = parts[1:-1] if closure else parts[1:]
        label = ', '.join(p for p in label_parts if p)
        landmarks.append({'km': km, 'label': label, 'closure': closure})
    return landmarks, bad


def _uni_site_inputs(folder):
    """The Unidirectional page's two site boxes, the Splice Report's pair:
    the cable's A-end and B-end names.  Filled with the names the traces
    store (GenParams, exactly as stored) when the folder changes; a
    presenter can type over them (WEST / EAST).  The engine prints them in
    the direction of the shot, so a B folder reads "EAST → WEST".

    What the boxes show is kept in a slot no widget owns, with the folder it
    was shown for, so a trip to another tool does not put them back (the
    same keyed-state pattern as _sr_site_inputs; never value= and key= on
    one widget).  Returns (site_a, site_b)."""
    k_a, k_b, k_src, k_saved = ('uni_site_a', 'uni_site_b', 'uni_site_src',
                                'uni_site_saved')
    _saved = st.session_state.get(k_saved)
    if _saved and _saved[0] == folder:
        for _k, _v in zip((k_a, k_b), _saved[1]):
            if _k not in st.session_state:
                st.session_state[_k] = _v
    if st.session_state.get(k_src) != folder:
        import glob
        sors = sorted(glob.glob(os.path.join(folder, '*.sor')) +
                      glob.glob(os.path.join(folder, '*.SOR')))
        loc_a, loc_b = _sor_locations(sors[0]) if sors else ('', '')
        st.session_state[k_a] = loc_a
        st.session_state[k_b] = loc_b
        st.session_state[k_src] = folder
    st.session_state.setdefault(k_a, '')
    st.session_state.setdefault(k_b, '')
    s1, s2 = st.columns(2)
    site_a = s1.text_input('A-End Site', key=k_a,
                           help='The site at the A end of the cable. Prints in '
                                'the report in the direction of the shot.')
    site_b = s2.text_input('B-End Site', key=k_b,
                           help='The site at the B end of the cable.')
    st.session_state[k_saved] = (folder, (site_a, site_b))
    return site_a.strip(), site_b.strip()


def _flat_upload(sdir, n, dupes):
    """(folder, n, dupes): the staged upload with every trace at the top level.
    A zip keeps its own folders (A side/, B side/), and the engine reads
    only the folder it is given, so a zip of two direction folders ran on
    no files at all.  A name that two of the zip's folders both hold is
    placed once and listed in `dupes`, never dropped in silence."""
    import folder_intake as fi
    files = fi.find_otdr_files(sdir)
    if all(os.path.dirname(f) == sdir for f in files):
        return sdir, n, dupes
    flat = sdir.rstrip(os.sep) + '_flat'
    seen, dupes = set(), list(dupes or [])
    for f in files:
        name = os.path.basename(f).lower()
        if name in seen:
            dupes.append(os.path.basename(f))
        seen.add(name)
    fi.materialize_all(files, flat)
    return flat, len(fi.find_otdr_files(flat)), dupes


def _uni_upload_box(box_folder):
    """The Unidirectional page's drop zone.  Returns the upload in use,
    {'dir', 'n', 'dupes', 'box'}, or None.

    Each upload REPLACES the one before (Robert 2026-10-01).  A Streamlit
    uploader adds every new drop to the files it already holds, so a second
    set of traces ran together with the first.  So the upload is staged and
    kept in a slot no widget owns, and the uploader is drawn again under a
    new key, empty, for the next drop.  Typing or browsing to another folder
    forgets the upload, as does Clear upload."""
    gen = int(st.session_state.get('uni_drop_gen', 0))
    dropped = st.file_uploader(
        '…or Drag & Drop the Shots Here (.sor / .json / .trc Files, a Whole '
        'Folder, or a .zip)',
        type=['sor', 'json', 'trc', 'zip'], accept_multiple_files=True,
        key=f'uni_drop_{gen}')
    if dropped:
        sdir, n, dupes = _stage_dropped(dropped)
        sdir, n, dupes = _flat_upload(sdir, n, dupes)
        # One .zip: its name is what the boxes show for it (_upload_label).
        _zips = [f.name for f in dropped if f.name.lower().endswith('.zip')]
        st.session_state['uni_upload'] = {
            'dir': sdir, 'n': n, 'dupes': dupes, 'box': box_folder,
            'name': _zips[0] if len(dropped) == 1 and _zips else ''}
        st.session_state['uni_drop_gen'] = gen + 1
        st.rerun()
    up = st.session_state.get('uni_upload')
    if up and (up.get('box') != box_folder or not os.path.isdir(up.get('dir') or '')):
        st.session_state.pop('uni_upload', None)
        up = None
    return up


def _shot_sites(folder):
    """(origin, far) for the traces in `folder`, in the order the shot ran:
    the stored GenParams pair, turned round when the file's own direction
    stamp says B to A (the engine's uni_shot_direction, read off the first
    trace).  ('', '') when nothing is readable."""
    import folder_intake as fi
    files = fi.find_otdr_files(folder)
    sor = [p for p in files if p.lower().endswith('.sor')]
    trc = [p for p in files if p.lower().endswith('.trc')]
    if sor:
        loc_a, loc_b = _sor_locations(sor[0])
        first = sor[0]
    elif trc:
        loc_a, loc_b = fi.trc_header(trc[0], _first_chunk_only=True).get(
            'loc_stored') or ('', '')
        first = trc[0]
    else:
        return ('', '')
    try:
        with open(first, 'rb') as fh:
            stamp = trace_server.read_direction(fh.read())
    except Exception:
        stamp = None
    return (loc_b, loc_a) if stamp == 'b' else (loc_a, loc_b)


def _uni_pick_direction(folder, stamped_only=False):
    """(folder, side): the folder this report runs on, for the page's own
    folder or upload, and the side picked ('a' | 'b'), None when nothing was
    split.  A folder or upload that holds both directions (the A and B shots
    together, loose or in one zip) is split the way the left panel splits
    such a folder (_split_panel_folder: the files' own direction stamps say
    which side is A), and the tech picks the direction: A by default.  Only
    the picked direction is analysed, so the other side is not reported as
    missing.  A one-direction folder or upload comes back as it is, with the
    Direction pick it always had.

    `stamped_only` (a typed folder): split only when the two sides' own
    direction stamps say A and B.  A folder of one direction whose files
    carry two GenParams site names splits by name into two sides stamped
    alike; it keeps the Direction pick it always had."""
    split = _split_panel_folder(folder)
    if not split:
        return folder, None
    if stamped_only and (_stamped_side(split['a']), _stamped_side(split['b'])) != ('a', 'b'):
        return folder, None
    labels = {}
    for side in ('a', 'b'):
        origin, far = _shot_sites(split[side])
        sites = f'{origin} → {far}, ' if origin and far else ''
        labels[side] = (f"{side.upper()} Direction ({sites}"
                        f"{_count(split[side + '_count'], 'file')})")
    # Kept in a slot no widget owns, with the folder it was picked for.
    _was = st.session_state.get('uni_both_side')
    side = _was[1] if isinstance(_was, tuple) and _was[0] == folder else 'a'
    opts = [labels['a'], labels['b']]
    pick = st.radio('Run On', opts, horizontal=True,
                    index=0 if side == 'a' else 1)
    side = 'b' if pick == labels['b'] else 'a'
    st.session_state['uni_both_side'] = (folder, side)
    st.caption('These traces hold both directions. This report reads one '
               'direction at a time: pick the one to run.'
               + (f" Not read: {', '.join(split['ignored'])}."
                  if split.get('ignored') else ''))
    return split[side], side


def _uni_end_cell(entries, ribbon_fibers):
    """The Cable End cell of one ribbon, exactly as the workbook prints it
    (uni_format_end_cell in splicereportmatchexfo, which the hub cannot
    import: it would load an engine's reader into the hub).  `entries` are
    (fiber, end reflectance or None) for the ribbon's fibers that reach the
    end.  Every fiber of the ribbon there: the strongest reflectance
    ('REFL-45.8dB', or 'end' when none is stored).  Some broke upstream:
    the fibers that do reach it, then that tag.  test_uni_end_cells holds
    the two copies to the same text."""
    if not entries:
        return ''
    members = sorted(f for f, _ in entries)
    refls = [v for _, v in entries if v is not None]
    tag = f"REFL{max(refls):.1f}dB" if refls else "end"
    if set(members) >= set(ribbon_fibers):
        return tag
    return ','.join(f"F{f}" for f in members) + " " + tag


def page_unidirectional():
    st.markdown('#### Unidirectional')

    # The OTDR Settings, same box as the Splice Report's and sharing its
    # values (Robert 2026-09-28).  Three of its rows drive the Uni engine:
    # the one-direction connector gate and the mid-span reflectance band
    # with its ceiling (_OTDR_KEY_TO_UNI_GLOBAL).
    _render_profile_picker_box('unidirectional')
    # Blocks like the Splice Report (Robert 2026-09-28: "block uni too"): the
    # three rows below reach the Uni engine, and a report at thresholds the
    # tech never saw is worse than no report.  Uni's own settings panel,
    # further down, blocks the same way.
    _settings_exc = _render_settings_box('unidirectional', blocks_report=True)
    st.caption('Unidirectional reads three of these settings: Connector Loss '
               '(1 Direction), Mid-Span Reflectance Band and Mid-Span Refl Ceiling. '
               'The others grade the bidirectional report.')

    # The page's own boxes keep what they show across a trip to another
    # tool (_seed_box): the folder box, the Direction pick, the landmarks.
    # The folder box only on the runs that draw it (the left panel empty):
    # Clear Report forgets the saved report of whatever folder it holds.
    _shoot = _chosen_traces()
    _pa, _pb = (_shoot['a'], _shoot['b']) if _shoot else _panel_traces()
    if not _shoot:
        _show_panel_notes()
    if not (_pa or _pb):
        _seed_box('uni_folder_input')
    st.session_state.setdefault('uni_folder_input', '')
    _dropped = None
    _uni_pside = ''            # the left panel's side this page runs on, if any
    if _pa or _pb:
        if _shoot:
            _render_chosen_line(_shoot)
        # Traces loaded in the left panel: the page draws no loader of its
        # own and runs on one of those folders (Robert 2026-09-28).  One
        # direction at a time is what this tool reads, so with both loaded
        # the tech says which.  The choice is kept in a slot no widget owns:
        # a widget the page does not draw loses its state, and the way back
        # from the Viewer must land on the folder the report ran on.
        if _pa and _pb:
            _sides = ['A folder', 'B folder']
            _was = st.session_state.get('uni_panel_side', 'A folder')
            _side = st.radio('Run On', _sides, horizontal=True,
                             format_func=lambda o: o.replace('folder', 'Folder'),
                             index=_sides.index(_was) if _was in _sides else 0)
            st.session_state['uni_panel_side'] = _side
            folder = _pa if _side == 'A folder' else _pb
        else:
            folder = _pa or _pb
        if not _shoot:
            _uni_pside = 'a' if folder == _pa else 'b'
            st.caption(f"Traces: the {'A' if folder == _pa else 'B'} folder loaded "
                       'in the left panel.')
        _viewer_removed_note(folder)
    else:
        # A folder an upload was staged in shows as what was uploaded
        # ('Uploaded Files: B Direction (12 files)'), never its temporary
        # path: the way back from a cell click brings that path into the box
        # (2026-10-01).  The page still runs on the staged files
        # (_uni_box_folder).
        _boxed = (st.session_state.get('uni_folder_input') or '').strip().strip('"')
        _boxed_origin = _typed_split_origin(_boxed)
        _boxed_label = _upload_label(_boxed) if _boxed and not _boxed_origin else None
        if _boxed_origin:
            # One side of a typed folder that holds both directions: the
            # typed folder again, with Run On on that side.
            st.session_state['uni_folder_input'] = _boxed_origin[0]
            st.session_state['uni_both_side'] = (_boxed_origin[1], _boxed_origin[2])
        elif _boxed_label:
            st.session_state['_uni_box_upload'] = (_boxed_label, _boxed)
            st.session_state['uni_folder_input'] = _boxed_label
        elif (st.session_state.get('uni_folder_input')
              != st.session_state.get('uni_folder_input_saved')):
            # A value written on an earlier run reaches the server and never
            # the box: the way back from a cell click writes the folder on the
            # Viewer's run, so the box came back empty, and the next click
            # sent that empty box back and the report went.  Written again
            # on the run that draws the box, it shows.
            st.session_state['uni_folder_input'] = st.session_state.get('uni_folder_input')
        c1, c2 = st.columns([1, 2])
        with c1:
            if st.button('📁 Browse for Folder', type='primary', use_container_width=True):
                p = pick_folder('Choose a folder of OTDR files')
                if p:
                    st.session_state['uni_folder_input'] = p
        with c2:
            st.text_input('…or Paste a Folder Path',
                          key='uni_folder_input',
                          placeholder=r'C:\Users\you\Desktop\uni shots')
            _keep_box('uni_folder_input')

        folder = _uni_box_folder()
        # The same inputs the Viewer takes: a .zip, or a folder of zips, is
        # read from its extracted copy.  A pasted .zip used to leave the page
        # asking for a folder, with nothing said (2026-09-29).
        if folder:
            _typed = folder
            folder, _znote = _resolve_viewer_dir(folder)
            if _znote and _znote.startswith('could not'):
                st.warning(f'{_typed}: {_znote}')
            elif _znote:
                st.caption(f'📦 Reading the traces from the .zip: {_typed}')
            elif not os.path.exists(_typed):
                st.warning(f'Not found: {_typed}. Paste a folder of `.sor` / '
                           '`.json` / `.trc` shots, or a .zip of them.')
        _dropped = _uni_upload_box(
            (st.session_state.get('uni_folder_input') or '').strip())
    _from_upload = False
    if _dropped:
        _sdir, _sn, _sdupes = _dropped['dir'], _dropped['n'], _dropped['dupes']
        if _sn:
            _from_upload = True
            st.caption(f'📥 {_sn} trace file(s) from the upload, used as the '
                       'input. A new upload replaces them.')
            folder = _sdir
            st.button('Clear Upload', key='uni_upload_clear',
                      on_click=lambda: st.session_state.pop('uni_upload', None))
        else:
            st.warning('The drop contained no readable `.sor` / `.json` / `.trc` files.')
        if _sdupes:
            import folder_intake as _fi_d
            st.warning('⚠ ' + _fi_d.duplicate_names_message(_sdupes, kept=False))
    # The Run button draws here, just under the folder, so a tech does not
    # scroll past the settings to reach it.  It is FILLED further down,
    # once the settings, direction and landmarks it sends are known.
    _uni_run_slot = st.container()
    # ── Uni settings box (thresholds & radii → engine overrides) ──────
    # Rendered BEFORE the folder guard (2026-07-31): thresholds are
    # settable before any data is loaded, same as the SR/FR panel.
    # Guarded like the SR panel: a render failure must not take down the
    # page, and the report does not run without it.
    try:
        uni_overrides = _render_uni_settings_panel()
    except Exception as _exc:
        st.error('Unidirectional settings could not load. The report is '
                 'turned off until they do. (Details sent to support.)')
        _policy_block_caption(_exc)
        report_error('unidirectional — settings panel render', _exc)
        uni_overrides = None
        _settings_exc = _settings_exc or _exc
    if _settings_exc is not None:
        # In the Run slot, right above the button it turns off, and drawn
        # before the folder guard so the tech sees it with no folder yet.
        with _uni_run_slot:
            _settings_block_notice(_settings_exc, 'Run')
    # The OTDR Settings rows Uni reads, on top: those globals have no row in
    # the Uni box any more, so nothing here is overwritten.
    uni_overrides = {**(uni_overrides or {}),
                     **_uni_overrides_from_settings(
                         st.session_state.get('otdr_settings'))} or None
    uni_show = _render_show_hide_box(
        'uni', _SHOW_ROWS + [('conn', 'Connectors')])

    if not folder or not os.path.isdir(folder):
        st.info('👆 Choose the folder that holds the one-direction `.sor` / '
                '`.json` shots, or drag & drop them above.')
        return
    folder = os.path.abspath(folder)
    _remove_legacy_caches(folder)
    src_folder = folder
    folder, _foreign = _exclude_foreign_files(folder)
    _uni_up_side = None        # the side picked from a folder or upload of both
    if not _uni_pside:
        # Both directions in the page's own folder or upload, loose or
        # zipped: the tech picks one (Robert 2026-10-01: "yes give typed
        # folders the Run On choice").  After the foreign-file audit, so a
        # stray from another job is not taken for a second direction.
        _picked_from = folder
        folder, _uni_up_side = _uni_pick_direction(folder, stamped_only=not _from_upload)
        if _from_upload:
            # What the boxes show for the staged files (_upload_label).
            _remember_upload(folder, _dropped.get('name'),
                             _uni_up_side or _stamped_side(folder))
        elif _uni_up_side:
            _remember_typed_split(folder, _uni_box_folder(), _picked_from,
                                  _uni_up_side)

    # If a prior run reported multiple GenParams directions in this folder,
    # offer the pick list (default stays "most populous").
    dir_choice = None
    prior = st.session_state.get('uni_result')
    if prior and prior.get('_folder') == folder:
        counts = (prior.get('uni') or {}).get('direction_counts') or {}
        if len(counts) > 1:
            opts = ['(most populous)'] + [f"{sig}  ({n} fibers)"
                                          for sig, n in sorted(counts.items(),
                                                               key=lambda kv: -kv[1])]
            _seed_box('uni_dir_pick', opts)
            pick = st.selectbox('Direction', opts, key='uni_dir_pick',
                                format_func=lambda o: '(Most Populous)' if o == '(most populous)' else o)
            _keep_box('uni_dir_pick')
            if pick != '(most populous)':
                dir_choice = pick.rsplit('  (', 1)[0]

    uni_site_a, uni_site_b = _uni_site_inputs(folder)

    with st.expander('Job Landmarks (Optional: Closure Map / Handholes)'):
        st.caption('One per line: `km, label`, or `km, label, splice` for a '
                   'known closure.  Labels print on the grid’s Handholes '
                   'row; a NON-closure landmark (handhole, replaced section…) '
                   'sitting on a detected splice column demotes it to '
                   'Bend/Damage.  Example:')
        st.code('0.57, Replaced section\n4.05, HH8\n7.91, HH4, splice',
                language=None)
        _seed_box('uni_landmarks_text')
        st.text_area('Landmarks', key='uni_landmarks_text', height=120,
                     label_visibility='collapsed',
                     placeholder='4.05, HH8')
        _keep_box('uni_landmarks_text')
    landmarks, bad_lines = _parse_landmarks_text(
        st.session_state.get('uni_landmarks_text'))
    if bad_lines:
        st.warning('Skipped landmark line(s) with no leading km: '
                   + ' · '.join(bad_lines[:3]))

    import folder_intake as _fi_dest
    with _uni_run_slot:
        _uni_dest = _report_dest_row('uni_report_dest', _fi_dest.default_report_dir())
        _stale = _report_gate('uni')
        # Checked twice, as on the Splice Report: a click made while the
        # settings were up still arrives on the run where they failed.
        _no_settings = _settings_exc is not None
        _run_uni = st.button('Run Unidirectional Report', type='primary',
                             disabled=bool(_stale) or _no_settings) \
            and not _no_settings
        st.caption('⏳ Large folders can take a few minutes. Leave this '
                   'window open and don’t refresh.')
    if _run_uni:
        out_xlsx = _project_run_path(_uni_dest, 'unidirectional_events.xlsx',
                                     traces=(src_folder,))
        st.session_state['uni_pending_cmd'] = uni_cmd(_run_folder(folder), out_xlsx,
                                                      direction=dir_choice,
                                                      landmarks=landmarks,
                                                      overrides=uni_overrides,
                                                      show=uni_show,
                                                      site_a=uni_site_a,
                                                      site_b=uni_site_b)
        st.session_state['uni_out_xlsx'] = out_xlsx
        st.session_state.pop('uni_result', None)
        st.rerun()

    if 'uni_pending_cmd' in st.session_state or 'uni_job' in st.session_state:
        try:
            proc = run_engine_live('uni', running_title='Running unidirectional report')
        except subprocess.TimeoutExpired:
            st.error(f'The unidirectional report timed out after {ENGINE_TIMEOUT_S}s '
                     'and was stopped.')
            report_error("unidirectional — timeout",
                         RuntimeError(f"engine exceeded {ENGINE_TIMEOUT_S}s"),
                         {"folder": os.path.basename(folder)})
            return
        if proc is None:
            return
        manifest = _parse_manifest(proc.stdout)
        if manifest is None:
            if not _engine_damaged_notice(proc.stderr, 'uni'):
                st.error('The unidirectional report did not return a result.')
                with st.expander('Engine Log'):
                    st.code(proc.stderr[-4000:] or '(no output)')
            report_error("unidirectional — no manifest",
                         RuntimeError("runner returned no JSON manifest"),
                         {"returncode": proc.returncode}, log=proc.stderr)
            return
        if not manifest.get('ok'):
            st.error(manifest.get('error', 'Analysis failed.'))
            with st.expander('Engine Log'):
                st.code(proc.stderr[-4000:] or '(no output)')
            report_error("unidirectional — engine returned not-ok",
                         RuntimeError(manifest.get('error', 'analysis failed')),
                         {"folder": os.path.basename(folder)}, log=proc.stderr)
            return
        manifest['_folder'] = folder
        st.session_state['uni_result'] = manifest
        # Disk cache: a grid-cell click into the Viewer is a URL nav that
        # wipes session_state — this is how "← Back" re-shows the report
        # without a re-run (same pattern as Secret Sauce / Splice Report).
        try:
            with open(_hub_cache_path('uni_result_cache.json', folder),
                      'w', encoding='utf-8') as fh:
                json.dump(manifest, fh)
        except Exception:
            pass

    res = st.session_state.get('uni_result')
    if not (res and res.get('ok') and res.get('_folder') == folder):
        # Back from the Viewer (session reset): restore from the disk cache.
        try:
            with open(_hub_cache_path('uni_result_cache.json', folder),
                      encoding='utf-8') as fh:
                _cached = json.load(fh)
            if _cached.get('ok') and _cached.get('_folder') == folder:
                res = _cached
                st.session_state['uni_result'] = res
        except Exception:
            pass
    if not (res and res.get('ok') and res.get('_folder') == folder):
        return
    _clear_report_button('uni')
    u = res.get('uni') or {}
    # The fiber count NEVER appears without its denominator: a 480-fiber
    # report on an 864-file folder must not read as "done, 480 fibers".
    _n_folder = u.get('n_files_in_folder')
    _n_drop = u.get('n_files_not_analysed') or 0
    _covered = (f"{u.get('n_fibers', '?')} of {_n_folder} files"
                if _n_folder and _n_drop else _count(u.get('n_fibers', '?'), 'fiber'))
    # The shot's own direction, site names in full and in the order the
    # distances run (the engine reads it from the files' LocationsDirection);
    # the GenParams signature reads the same for both ends of a span.
    _line = (f"{_covered} · direction "
             f"{u.get('direction_label') or u.get('direction', '?')} · "
             f"span ≈ {u.get('span_km', '?')} km")
    if _n_drop:
        st.error(f"⚠️ PARTIAL COVERAGE: {_line}")
        st.error(u.get('coverage_headline') or '')
    else:
        st.success(f"Done: {_line}")
    counts = u.get('direction_counts') or {}
    merged = u.get('merged_signatures') or []
    for m in merged:
        st.warning(
            f"Mistyped site code: {m.get('n_fibers', '?')} trace file(s) say "
            f"**{m.get('signature', '?')}** where the rest say "
            f"**{u.get('direction', '?')}**. Fibers "
            f"{_fiber_ranges(m.get('fibers') or [])} are INCLUDED in this "
            "report (older builds dropped them silently).  Check the "
            "GenParams site code on those shots.")
    # A signature folded in above is not a second span — don't also tell the
    # tech to re-run for it.
    if len(counts) - len(merged) > 1:
        st.error(
            f"This folder mixes {len(counts) - len(merged)} directions: the "
            f"report covers ONLY '{u.get('direction', '?')}'. "
            + ' '.join(
                f"{n} file(s) shot as '{sig}' were NOT analyzed."
                for sig, n in sorted(counts.items(), key=lambda kv: -kv[1])
                if sig != u.get('direction')
                and sig not in {m.get('signature') for m in merged})
            + "  Pick another from the Direction list and re-run to cover it.")
    cols = st.columns(5)
    cols[0].metric('Splice Columns', len(u.get('splice_columns') or []))
    cols[1].metric('Bend/Damage Columns', len(u.get('bend_columns') or []))
    cols[2].metric('Break Columns', len(u.get('break_columns') or []))
    # A panel-to-panel span has no splices at all — without this metric the
    # header reads 0 / 0 / 0 and the report looks like it found nothing.
    cols[3].metric('Connector Columns', len(u.get('connector_columns') or []))
    rp = u.get('reburn_pct')
    cols[4].metric('Reburn', f"{rp:.2f}%" if rp is not None else '-')
    detail = []
    if u.get('splice_columns'):
        detail.append('Splices @ ' + ', '.join(f"{v:.2f} km" for v in u['splice_columns']))
    if u.get('bend_columns'):
        detail.append('Bend/Damage @ ' + ', '.join(f"{v:.2f} km" for v in u['bend_columns']))
    if u.get('break_columns'):
        detail.append(f"Breaks ({u.get('n_breaks', '?')} fibers) @ "
                      + ', '.join(f"{v:.2f} km" for v in u['break_columns']))
    if u.get('connector_columns'):
        detail.append('Connectors @ ' + ', '.join(f"{v:.2f} km"
                                                  for v in u['connector_columns']))
    if detail:
        st.caption(' · '.join(detail))
    if u.get('connector_columns'):
        _cf, _cr = u.get('connector_flagged', 0), u.get('connector_readings', 0)
        _cd = u.get('connector_dark', 0)
        st.caption(
            f"Connectors: {_cr} reading(s) across {len(u['connector_columns'])} "
            f"connector(s); {_cf} at or above the one-direction threshold"
            + (f"; **{_cd} DARK**: the trace stops at the connector, no light "
               "through the mate" if _cd else "")
            + ".  One direction cannot separate a connector's true loss from "
              "the backscatter step between the fibers it joins, so these "
              "losses are upper bounds; the bidirectional Splice Report "
              "averages that term away.")
    if u.get('prebreak_damage_fibers'):
        st.caption(f"Pre-break damage: {u['prebreak_damage_fibers']} broken "
                   "fiber(s) show trace-measured damage ahead of their break "
                   "point (dying fibers are measured off the raw trace; the "
                   "0.1 dB rule doesn’t apply to them).")
    if u.get('demoted_columns'):
        st.caption('Landmark demotions (splice → bend/damage): '
                   + ', '.join(f"{v:.2f} km" for v in u['demoted_columns']))
    if not u.get('launch_box'):
        st.caption('No launch box detected on this shoot: events past 0.3 km '
                   'are reported as plant (no launch-reel exclusion applied).')

    # ── In-app clickable ribbon grid: every fiber → the Viewer ──
    if u.get('grid_columns') and u.get('cells') is not None:
        st.markdown('###### Click a Fiber → Jump to It in the Viewer')
        gcols = u['grid_columns']
        rs = int(u.get('ribbon_size') or 12)
        max_f = int(u.get('max_fiber') or u.get('n_fibers') or 0)
        n_ribbons = (max_f + rs - 1) // rs if max_f else 0
        # Rows from the first ribbon holding a loaded fiber to the last; an
        # empty ribbon between them keeps its row.
        _url = u.get('ribbons') or []
        uni_ribbon_rows = (list(range(min(_url), max(_url) + 1)) if _url
                           else list(range(n_ribbons)))
        off = float(u.get('launch_offset_km') or 0.0)
        by_rc = {}
        for c in u['cells']:
            by_rc.setdefault(((c['fiber'] - 1) // rs, c['col']), []).append(c)
        # The workbook's colors (UNI_LEGEND), as text on white: each kind's
        # header shade, dark enough to read.  The Cable End readings are
        # plain black, as their cells are unfilled in the workbook.
        _KIND_COLOR = {'splice': '#c2185b', 'bend_damage': '#8a6d00',
                       'break': '#c00000', 'reflective': '#a6340f',
                       'connector': '#8c5300', 'end': '#000000'}
        _uni_port = ensure_trace_server()
        # The side its links open on, and the Viewer slot the report's own
        # folder goes in: the left panel's side the page ran on; else the
        # direction picked from an upload of both; else what the files' own
        # direction stamps say (_stamped_side), the rule the upload's split
        # uses.  A B run on the page's own folder or upload always opened as
        # A: its files in the A slot, listed as A->B (2026-10-01).
        _uni_dir = _uni_pside or _uni_up_side or (
            'b' if folder and _stamped_side(folder) == 'b' else 'a')
        if _uni_pside and (_pa or _pb):
            # Run on one of the left panel's folders: the popped Viewer keeps
            # BOTH of them and opens the fibre on the side the report ran on,
            # as a click into the Viewer tab does (_handle_nav).  Pointing A at
            # the report's folder loaded a B-folder run's files as A->B.
            trace_server.set_dirs(_pa or None, _pb or None)
        elif folder and os.path.isdir(folder) and _uni_dir == 'b':
            trace_server.set_dirs(None, folder)   # B's files, in the B slot
        elif folder and os.path.isdir(folder):
            trace_server.set_dirs(folder, None)   # popped Viewer reads this span
        # Same as the Splice Report grid: the Viewer judges by THIS run's gates.
        # The uni settings panel moves UNI_BEND_THRESHOLD off its 0.250 default
        # and that never reached the Viewer either.
        trace_server.set_thresholds(res.get('thresholds'), source='uni')
        trace_server.set_end_refl(res.get('end_refl'))
        trace_server.set_panel_span(res.get('panel_span'))
        trace_server.set_suite_table(None)        # a uni report has no A+B table
        _uni_popout = _viewer_click_target('uni')
        from urllib.parse import quote as _q
        # The report's folder, as the slot it fills (_handle_nav): `srb` for
        # a B run on the page's own folder, `sra` otherwise.
        _fq = (f"{'srb' if _uni_dir == 'b' and not _uni_pside else 'sra'}="
               + _q(folder, safe=''))
        # ...and which of the panel's folders the report ran on, so the
        # Viewer keeps both and opens the fibre on that side (_handle_nav).
        _uni_pq = _panel_qs() + (f'&pside={_uni_pside}' if _uni_pside else '')
        html = ['<div style="overflow:auto;max-height:62vh;border:1px solid #c9d5e1;'
                'border-radius:4px;color:#000000;background:#ffffff">',
                '<table style="border-collapse:collapse;font-size:11px;'
                'font-family:Consolas,monospace">',
                '<thead><tr><th style="position:sticky;top:0;left:0;z-index:2;'
                'background:#eef3f8;padding:4px 8px;border:1px solid #dbe4ee">Ribbon</th>']
        for gc in gcols:
            lm = (f"<div style='font-size:9px;color:#000000'>{gc['landmark']}</div>"
                  if gc.get('landmark') else '')
            html.append(f"<th style='position:sticky;top:0;z-index:1;"
                        f"padding:4px 8px;border:1px solid #dbe4ee;"
                        f"background:#eef3f8;white-space:nowrap'>"
                        f"<div style='font-weight:600'>{gc['label']}</div>"
                        f"<div style='font-size:10px;color:#000000'>{gc['km']:.2f} km</div>"
                        f"{lm}</th>")
        html.append('</tr></thead><tbody>')
        for ri in uni_ribbon_rows:
            f0, f1 = ri * rs + 1, min((ri + 1) * rs, max_f)
            html.append(f"<tr><td style='position:sticky;left:0;background:#f7fafc;"
                        f"padding:3px 8px;border:1px solid #e3e9f0;"
                        f"white-space:nowrap'>F{f0}–{f1}</td>")
            for ci, gc in enumerate(gcols):
                cell = by_rc.get((ri, ci), [])
                if not cell:
                    html.append("<td style='padding:3px 6px;border:1px solid #eef2f6'></td>")
                    continue
                if gc.get('kind') == 'end':
                    # Cable End: the workbook's one cell for the ribbon, not
                    # a line per fiber (432 lines made every row ~150 px
                    # tall).  It opens the fiber with the strongest end
                    # reflectance, or the ribbon's first fiber at the end.
                    _top = min(cell, key=lambda c: (c['loss'] is None,
                                                    -(c['loss'] or 0), c['fiber']))
                    shown = [(_top, _uni_end_cell(
                        [(c['fiber'], c['loss']) for c in cell],
                        range(f0, min(f0 + rs, max_f + 1))))]
                else:
                    shown = [(c, f"F{c['fiber']}" + (' ✕ broke' if c['loss'] is None
                                                     else f" {c['loss']:.3f}"))
                             for c in sorted(cell, key=lambda x: x['fiber'])]
                links = []
                for c, text in shown:
                    color = _KIND_COLOR.get(c['kind'], '#000000')
                    _km = round(c['km'] + off, 4)
                    links.append(_cell_markup(
                        _uni_popout, c['fiber'], _km, _uni_dir, color, '', text,
                        href=(f"?nav=viewer&fiber={c['fiber']}&km={_km}"
                              f"&dir={_uni_dir}&{_fq}&src=uni{_uni_pq}")))
                html.append("<td style='padding:3px 6px;border:1px solid #eef2f6;"
                            "white-space:nowrap'>" + "<br>".join(links) + "</td>")
            html.append('</tr>')
        html.append('</tbody></table></div>')
        if _uni_popout:
            _render_clickable_grid(''.join(html), _uni_port, src='uni')
        else:
            st.markdown(''.join(html), unsafe_allow_html=True)

    out_xlsx = res.get('out') or st.session_state.get('uni_out_xlsx', '')
    if out_xlsx and os.path.exists(out_xlsx):
        with open(out_xlsx, 'rb') as fh:
            st.download_button(f"⬇ {os.path.basename(out_xlsx)}", data=fh.read(),
                               file_name=os.path.basename(out_xlsx),
                               key='uni_dl')
        st.caption(f'Saved to: {out_xlsx}')



# ═════════════════════════════════════════════════════════════════════════
#  PAGE: FQA Builder — customer submittal package from a production sheet
# ═════════════════════════════════════════════════════════════════════════
# The only page that takes no traces.  It reads the span's ZeroDB
# production sheet -- one tab per location, in route order -- and fills
# the customer's Site Survey form: cover page, Fiber Assignment Table, Event
# Log, Exception Reporting.
#
# It runs IN-PROCESS rather than as a subprocess.  The three engine tools
# are shelled out because they ship divergent sor_reader324802a copies
# that cannot share one namespace; the FQA builder parses no traces and
# imports no reader, so it has nothing to isolate from.  It is openpyxl
# and a zip.
#
# The interface itself lives in fqa/ui.py, which fqa/app.py also renders
# when the tool is run standalone.  One copy, so the two cannot drift.
# ═════════════════════════════════════════════════════════════════════════
#  PAGE: Splice Report FEC
# ═════════════════════════════════════════════════════════════════════════
# FEC = the short facility-entrance shots from each END of a span (launch
# reel, building panel connector at ~1 km, entrance cable).  The two ends
# never see the same glass, so this tool grades each folder on its own and
# never pairs A with B; the Splice Report and the Viewer both pair them.
# Engine: run_splicereport.py --fec (same subprocess, no new engine file).
# Tech C's style (0.500 passes) is the default, Robert 2026-10-01, until the
# boss says which one the customer wants; profile A keeps tech A's.
# The FEC Settings box's two loss rules (Title Case, the hub's label rule).
_FEC_AT_OR_OVER, _FEC_OVER = 'At or Over the Gate', 'Over the Gate'
FEC_DEFAULTS = {"FEC_LOSS_GATE": 0.500, "FEC_LOSS_STRICT": 1,
                "FEC_REFL_GATE": -50.0, "FEC_COMBINE_M": 150.0}


def _fec_settings_from_profile(profile_name):
    """The FEC gates for a customer profile: its "fec" block over the
    defaults (tech C's style).  Profiles without one get the defaults."""
    out = dict(FEC_DEFAULTS)
    prof = CUSTOMER_PROFILES.get(profile_name) or {}
    for k, v in (prof.get("fec") or {}).items():
        if k in out:
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                pass
    return out


def _fec_active_gates():
    """The FEC gates in force: the active profile's, with the FEC Settings
    edits made on the Splice Report FEC page for that profile on top."""
    prof = st.session_state.get('otdr_profile') or next(iter(CUSTOMER_PROFILES))
    gates = _fec_settings_from_profile(prof)
    sfx = f'::{prof}'
    for k in ('FEC_LOSS_GATE', 'FEC_REFL_GATE', 'FEC_COMBINE_M'):
        v = st.session_state.get(f'fec_{k}{sfx}')
        if isinstance(v, (int, float)):
            gates[k] = float(v)
    strict = st.session_state.get(f'fec_FEC_LOSS_STRICT{sfx}')
    if strict in (_FEC_OVER, _FEC_AT_OR_OVER):
        gates['FEC_LOSS_STRICT'] = 1.0 if strict == _FEC_OVER else 0.0
    return gates


def fec_cmd(dir_a, dir_b, out_xlsx, overrides=None):
    """Argv for the FEC report (the splice report runner's --fec mode)."""
    common = ['--fec', '--dir-a', dir_a, '--out', out_xlsx]
    if dir_b:
        common += ['--dir-b', dir_b]
    if overrides:
        common += ['--overrides', json.dumps(overrides)]
    if FROZEN:
        return [sys.executable, '--run-splicereport', *common]
    return [sys.executable, os.path.join(SPLICEREPORT_DIR, 'run_splicereport.py'), *common]


def _fec_resync(key):
    """Re-assign a box's value in THIS run, before it is drawn.  Streamlit
    sends a keyed box's value to the browser only when it was set during the
    run that draws it; a value set on an earlier run (a Viewer FEC link sets
    the folders on the Viewer FEC run, a carried save folder lands on the
    first run) is used by the page but shown as an EMPTY box."""
    if key in st.session_state:
        st.session_state[key] = st.session_state[key]


def _fec_folder_row(slot, label, placeholder):
    _seed_box(slot)                  # Streamlit forgets a box it did not draw
    st.session_state.setdefault(slot, '')
    _fec_resync(slot)
    c1, c2 = st.columns([1, 2])
    with c1:
        if st.button(f'📁 Browse: {label}', key=f'{slot}_browse',
                     use_container_width=True):
            p = pick_folder(f'Choose the {label} folder')
            if p:
                st.session_state[slot] = p
    with c2:
        st.text_input(f'{label} (Folder or .zip)', key=slot, placeholder=placeholder)
    _keep_box(slot)
    raw = (st.session_state.get(slot) or '').strip().strip('"')
    if not raw:
        return ''
    d, note = _resolve_viewer_dir(raw)
    if note and note != 'viewing from .zip':
        st.warning(f'{label}: {note}')
    if not d or not os.path.isdir(d):
        st.warning(f'{label}: folder not found.')
        return ''
    return os.path.abspath(d)


def _fec_rows_html(rows, dir_a, dir_b):
    """The FEC fails as a table whose rows open Viewer FEC on that fiber,
    from its own end, zoomed to the panel connector.  Every cell of a row is
    the same link (Robert 2026-10-01: a click on a row jumps), so the whole
    row is the target, not just the fiber number."""
    import html as _h
    from urllib.parse import quote as _q
    raw_a = (st.session_state.get('fec_dir_a') or '').strip().strip('"')
    raw_b = (st.session_state.get('fec_dir_b') or '').strip().strip('"')
    common = (f"&fa={_q(dir_a, safe='')}&fb={_q(dir_b or '', safe='')}"
              f"&ra={_q(raw_a, safe='')}&rb={_q(raw_b, safe='')}{_panel_qs()}")
    head = ['Fiber Number', 'FAILING @', 'Distance', 'Side', 'Failed On',
            'Connector Loss', 'Combined With']
    # The theme's own colors (--otdr-*): a fixed light header was white
    # lettering on a pale band under the Dark theme.
    td = "padding:0;border:1px solid var(--otdr-line-soft,#eef2f6);white-space:nowrap"
    th = ("padding:3px 8px;border:1px solid var(--otdr-line,#dbe4ee);white-space:nowrap;"
          "background:var(--otdr-panel,#eef3f8);color:var(--otdr-text,#000000)")
    out = ["<style>.fec-rows a{display:block;padding:3px 8px;color:inherit;"
           "text-decoration:none}.fec-rows tbody tr{cursor:pointer}"
           ".fec-rows tbody tr:hover{background:var(--otdr-hover,#dde7f1)}</style>"
           "<div style='overflow-x:auto'><table class='fec-rows' "
           "style='border-collapse:collapse;font-size:13px'><thead><tr>"
           + ''.join(f"<th style='{th}'>{h}</th>" for h in head)
           + "</tr></thead><tbody>"]
    for r in rows:
        href = (f"?nav=viewerfec&fiber={r['fiber']}&km={r['conn_km']}"
                f"&dir={'b' if r['side'] == 'B' else 'a'}{common}")
        comb = '; '.join(f"{c['loss']:.3f} @ {c['km']:.3f} km"
                         for c in r.get('combined') or [])
        link = (f"<a href='{_h.escape(href, quote=True)}' target='_self' "
                f"title='Open F{r['fiber']} in Viewer FEC'")
        cells = [f"<b>{_h.escape(r['fiber_id'])}</b>",
                 r['failing_at'], r['distance'], r['side'],
                 'Reflectance' if r['kind'] == 'refl' else 'Loss',
                 f"{r['conn_loss']:.3f}", _h.escape(comb) or '&nbsp;']
        out.append('<tr>' + ''.join(f"<td style='{td}'>{link}>{c}</a></td>"
                                    for c in cells) + '</tr>')
    out.append('</tbody></table></div>')
    return ''.join(out)


def page_splice_report_fec():
    st.markdown('#### Splice Report FEC')
    st.caption('Facility entrance (FEC) shots: the short traces from each end '
               'of the span. Each end is graded on its own; A and B are never '
               'paired. A fiber fails on its panel connector: loss (the '
               'connector plus any event just behind it, "COMBINE") or '
               'reflectance.')
    _render_profile_picker_box('splice report fec')
    prof = st.session_state.get('otdr_profile') or next(iter(CUSTOMER_PROFILES))
    gates = _fec_settings_from_profile(prof)
    sfx = f'::{prof}'
    for k in ('FEC_LOSS_GATE', 'FEC_REFL_GATE', 'FEC_COMBINE_M'):
        st.session_state.setdefault(f'fec_{k}{sfx}', float(gates[k]))
    st.session_state.setdefault(f'fec_FEC_LOSS_STRICT{sfx}',
                                _FEC_OVER if gates['FEC_LOSS_STRICT']
                                else _FEC_AT_OR_OVER)
    with st.expander('FEC Settings', expanded=False):
        st.caption(f'From the customer profile **{prof}**. Edits last until '
                   'the profile changes.')
        g1, g2, g3, g4 = st.columns(4)
        g1.number_input('Loss Gate (dB)', key=f'fec_FEC_LOSS_GATE{sfx}',
                        min_value=0.0, max_value=5.0, step=0.01, format='%.3f')
        g2.selectbox('Loss Fails When', [_FEC_AT_OR_OVER, _FEC_OVER],
                     key=f'fec_FEC_LOSS_STRICT{sfx}')
        g3.number_input('Reflectance Gate (dB)', key=f'fec_FEC_REFL_GATE{sfx}',
                        min_value=-90.0, max_value=0.0, step=0.5, format='%.1f')
        g4.number_input('Combine Reach (m)', key=f'fec_FEC_COMBINE_M{sfx}',
                        min_value=0.0, max_value=2000.0, step=10.0, format='%.0f')
    gates = {
        'FEC_LOSS_GATE': float(st.session_state[f'fec_FEC_LOSS_GATE{sfx}']),
        'FEC_LOSS_STRICT': 1.0 if st.session_state[f'fec_FEC_LOSS_STRICT{sfx}']
                           == _FEC_OVER else 0.0,
        'FEC_REFL_GATE': float(st.session_state[f'fec_FEC_REFL_GATE{sfx}']),
        'FEC_COMBINE_M': float(st.session_state[f'fec_FEC_COMBINE_M{sfx}']),
    }
    st.caption(f"Fails: loss {'>' if gates['FEC_LOSS_STRICT'] else '≥'} "
               f"{gates['FEC_LOSS_GATE']:.3f} dB (connector + events within "
               f"{gates['FEC_COMBINE_M']:.0f} m behind it), or reflectance > "
               f"{gates['FEC_REFL_GATE']:.1f} dB.")

    dir_a = _fec_folder_row('fec_dir_a', 'A End FEC',
                            r'C:\...\FEC shots, A end')
    dir_b = _fec_folder_row('fec_dir_b', 'B End FEC (Optional)',
                            r'C:\...\FEC shots, B end')
    if not dir_a:
        st.info('👆 Choose the A end FEC folder (and the B end, if you have '
                'it). Each folder holds one end’s short `.sor` shots.')
        return

    import folder_intake as _fi_dest
    _fec_resync('fec_report_dest')
    _dest = _report_dest_row('fec_report_dest', _fi_dest.default_report_dir())
    _stale = _report_gate('fec')
    if st.button('Run FEC Report', type='primary', disabled=bool(_stale)):
        out_xlsx = os.path.join(_dest, 'FEC_OOS.xlsx')
        st.session_state['fec_pending_cmd'] = fec_cmd(dir_a, dir_b, out_xlsx, gates)
        st.session_state.pop('fec_result', None)
        st.rerun()

    if 'fec_pending_cmd' in st.session_state or 'fec_job' in st.session_state:
        try:
            proc = run_engine_live('fec', running_title='Running FEC report')
        except subprocess.TimeoutExpired:
            st.error(f'The FEC report timed out after {ENGINE_TIMEOUT_S}s and '
                     'was stopped.')
            report_error('splice report fec — timeout',
                         RuntimeError(f'engine exceeded {ENGINE_TIMEOUT_S}s'))
            return
        if proc is None:
            return
        manifest = _parse_manifest(proc.stdout)
        if manifest is None or not manifest.get('ok'):
            if manifest is None and _engine_damaged_notice(proc.stderr, 'fec'):
                return
            st.error((manifest or {}).get('error')
                     or 'The FEC report did not return a result.')
            with st.expander('Engine Log'):
                st.code(proc.stderr[-4000:] or '(no output)')
            report_error('splice report fec — failed',
                         RuntimeError((manifest or {}).get('error', 'no manifest')),
                         {'returncode': proc.returncode}, log=proc.stderr)
            return
        manifest['_dirs'] = [dir_a, dir_b]
        st.session_state['fec_result'] = manifest
        # A row click into Viewer FEC is a URL nav that wipes session_state:
        # this is how the page shows the report again on the way back.
        try:
            with open(_hub_cache_path('fec_result_cache.json', dir_a, dir_b),
                      'w', encoding='utf-8') as fh:
                json.dump(manifest, fh)
        except Exception:
            pass

    res = st.session_state.get('fec_result')
    if not (res and res.get('ok') and res.get('_dirs') == [dir_a, dir_b]):
        try:
            with open(_hub_cache_path('fec_result_cache.json', dir_a, dir_b),
                      encoding='utf-8') as fh:
                _cached = json.load(fh)
            if _cached.get('ok') and _cached.get('_dirs') == [dir_a, dir_b]:
                res = st.session_state['fec_result'] = _cached
        except Exception:
            pass
    if not (res and res.get('ok') and res.get('_dirs') == [dir_a, dir_b]):
        return
    fec = res.get('fec') or {}
    sides = fec.get('sides') or []
    st.success('Done: ' + ' · '.join(
        f"{s['side']} end {s['label']}: {s['n_traces']} traces, "
        f"{s['n_fail_fibers']} failing" for s in sides))
    for s in sides:
        pg = s.get('pulse_groups') or []
        if len(pg) > 1:
            st.info(f"{s['side']} end pulse widths: " + '; '.join(
                f"{g['fibers']} at {g['pulse_ns']:g} ns" for g in pg))
        if s.get('no_conn'):
            st.warning(f"{s['side']} end: no panel connector found on "
                       f"{len(s['no_conn'])} trace(s), not graded: "
                       + ', '.join(s['no_conn'][:12])
                       + (' …' if len(s['no_conn']) > 12 else ''))
        if s.get('unreadable'):
            st.warning(f"{s['side']} end: {len(s['unreadable'])} unreadable "
                       'file(s), not graded.')
    rows = [r for s in sides for r in s.get('rows') or []]
    if rows:
        st.caption('Click a row to open that fiber in Viewer FEC.')
        st.markdown(_fec_rows_html(rows, dir_a, dir_b), unsafe_allow_html=True)
    else:
        st.info('No fiber fails.')
    xlsx = res.get('xlsx')
    if xlsx and os.path.isfile(xlsx):
        st.caption(f'Saved: {xlsx}')
        with open(xlsx, 'rb') as fh:
            st.download_button('⬇ Download FEC Report (.xlsx)', fh.read(),
                               file_name=os.path.basename(xlsx),
                               key='fec_download')


def page_fqa_builder():
    import folder_intake as _fi
    from fqa.ui import render
    render(default_out_dir=_fi.default_report_dir(), dest_row=_report_dest_row)


# Event Log distances from the project's traces.  The closures come from the
# Splice Report engine -- the same validated columns its grid prints -- so the
# FQA Builder still parses no traces: the engine runs in its own subprocess
# (its own sor_reader copy) and only its JSON manifest crosses into fqa/.
def _fqa_sr_manifest(dir_a, dir_b):
    """A Splice Report manifest for this A/B pair, in OTDR mode.

    Reuses the grid the Splice Report page cached for the same two folders
    when there is one (FastReporter-mode grids are skipped: their columns
    are FR's event rows, not validated closures); otherwise runs the engine
    into a temporary folder, which takes as long as a Splice Report does."""
    try:
        with open(_hub_cache_path('.sr_grid_cache.json', dir_a),
                  encoding='utf-8') as fh:
            cached = json.load(fh)
        m = cached.get('manifest') or {}
        if (m.get('ok') and m.get('analysis_mode', 'suite') != 'fr'
                and list(cached.get('_dirs') or []) == [dir_a, dir_b]):
            return m
    except Exception:
        pass
    with tempfile.TemporaryDirectory(prefix='fqa_sr_') as tmp:
        proc = run_engine(splicereport_cmd(
            dir_a, dir_b, os.path.join(tmp, 'closures.xlsx'), 'A', 'B',
            analysis='suite'))
    return _parse_manifest(proc.stdout) or {
        'ok': False,
        'error': (proc.stderr or '').strip()[-300:] or 'no manifest'}


def fqa_trace_distances(dir_a, dir_b, prod, **kwargs):
    """One distance from Site A per splice worksheet of `prod`, from the A and
    B trace folders.  See fqa.event_chain.splice_distances for the result
    shape; distances_m is None (with the reason in warnings) whenever the
    closures cannot be matched to the worksheets without guessing."""
    from fqa.event_chain import splice_distances
    return splice_distances(dir_a, dir_b, prod, get_manifest=_fqa_sr_manifest,
                            **kwargs)


# ═════════════════════════════════════════════════════════════════════════
#  PAGE: Field Capture — FQA section 1.2 with the labels in the photos checked
# ═════════════════════════════════════════════════════════════════════════
# The tech's A-Location / Z-Location form: rack location and panel details
# for section 1.2 of the customer's FQA Site Survey, photos, and a check that the
# rack, RMU and panel labels read out of the photos match what was entered.
# It fills the span's FQA (or the blank form) and hands it to Outlook.
#
# The page is a web app (fieldcapture/web), the same code that will run on the
# tech's iPhone.  Here it is shown the way the Viewer is: fieldcapture/server.py
# runs in a daemon thread inside the hub and this page embeds it.  In-process
# for the FQA Builder's reason: it parses no traces and imports no sor_reader.
# The labels are read in the browser (Tesseract.js), so nothing new is
# installed on the PC.
def page_field_capture():
    import folder_intake as _fi
    from fieldcapture import server as _fc
    st.markdown('#### Field Capture')
    st.caption('Section 1.2 (Fiber Panel Information) of the FQA Site Survey for the '
               'A-Location and the Z-Location. The rack, RMU and panel labels in the '
               'photos are read and checked against what is entered, then the FQA is '
               'saved and an email opens with it attached.')
    _fc.CONFIG['dest_dir'] = _report_dest_row('fc_report_dest', _fi.default_report_dir())
    port = _fc.start_in_thread()
    st_iframe(f'http://127.0.0.1:{port}/?host=suite', height=1500, scrolling=True)


# ═════════════════════════════════════════════════════════════════════════
#  PAGE: Project status -- what the FQA package still needs
# ═════════════════════════════════════════════════════════════════════════
# Robert, 2026-09-23: "we will want a project status area, where we see what
# we need. as we collect photos, GPS coordinates, traces, etc those would be
# removed from what we need. the basis for what we need is the completed FQA
# form."  Option 2 of the two he was offered: the tech emails what Field
# Capture exported, and whoever has the project open drops the attachments on
# this page.  Each dropped file is copied into <project folder>/Field/, so the
# status is recomputed from files on disk and survives a restart -- and a
# phone that later saves straight into that folder (option 1) needs no change
# here.
#
# Three groups, every item read from a file, none typed in:
#   * the customer's own Submittal Checklist (the form's last tab): each row there is
#     "is this cell filled", mirrored below cell for cell.  A test reads the
#     template's formulas and fails if a form revision moves a cell.
#   * Field items per end: section 1.2's rack and panel details, a GPS fix,
#     photos.  GPS is only in the capture sheet (.xlsx) -- the FQA workbook
#     carries it only as text burned into the photos.
#   * Traces: both directions, every fiber shot both ways, as many fibers as
#     the form says were tested.
# A cell counts when ANY dropped workbook fills it: the field copy fills 1.2,
# the FQA Builder's copy fills the cover and the Event Log, and both are part
# of what has been collected.
PROJECT_FIELD_DIR = 'Field'
_SS, _EL, _FAT = 'Site Survey Data', 'Event Log', 'FAT'

# (number on the form, what it asks, sheet, cell) -- the Submittal
# Checklist's "is it entered?" rows, in the form's order and wording.
FQA_CHECKLIST_CELLS = [
    ('1.01', 'Bldg 1 (A end) site address', _SS, 'E9'),
    ('1.02', 'Bldg 2 (Z end) site address', _SS, 'E11'),
    ('1.03', 'Bldg 1 alias', _SS, 'E10'),
    ('1.04', 'Bldg 2 alias', _SS, 'E12'),
    ('1.05', 'Bldg 1 CLLI', _SS, 'K9'),
    ('1.06', 'Bldg 2 CLLI', _SS, 'K11'),
    ('1.07', 'Bldg 1 panel port count (the form\'s "test-from device")', _SS, 'J53'),
    ('1.08', 'Bldg 2 panel port count (the form\'s "test-from device")', _SS, 'J61'),
    ('1.09', 'Number of fibers tested', _SS, 'F97'),
    ('1.10', 'Test revision', _SS, 'F86'),
    ('1.11', 'Package type', _SS, 'F85'),
    ('1.12', 'Splicing contractor and package preparer', _SS, 'F87'),
    ('1.13', 'Netbuild or project ID', _SS, 'F91'),
    ('1.14', 'Tester name and phone number', _SS, 'F89'),
    ('1.15', 'Date of most recent calibration', _SS, 'F88'),
]

# Section 1.2 per end (the cells Field Capture writes): the rack's place and
# the panel.  Port count is checklist row 1.07 / 1.08 already.
FQA_END_CELLS = {
    'A': {'rack': [('floor', 'F51'), ('room', 'H51'), ('aisle', 'J51'), ('bay', 'L51')],
          'panel': [('RMU', 'F52'), ('connector', 'F56'), ('panel type', 'M56')]},
    'Z': {'rack': [('floor', 'F59'), ('room', 'H59'), ('aisle', 'J59'), ('bay', 'L59')],
          'panel': [('RMU', 'F60'), ('connector', 'F63'), ('panel type', 'M63')]},
}
_END_NAMES = {'A': 'A end', 'Z': 'Z end'}
_TRACE_EXTS = ('.sor', '.json', '.bdr', '.trc')


def _xl_has(v):
    """A cell the form counts as entered: not blank, not one of its own
    '<Select>' / '<Enter ...>' prompts.  A formula counts -- Excel fills it
    in when the file is opened."""
    if v is None:
        return False
    if isinstance(v, str):
        s = v.strip()
        return bool(s) and not re.fullmatch(r'<[^>]*>', s)
    return True


def _xl_number(v):
    """A cell's number.  The customer's form formats some number cells as dates
    (F97, 'Number Of Fibers Tested', is one), so the reader hands 48 back as
    1900-02-17; the Excel serial is the number the tech typed."""
    import datetime as _dt
    if isinstance(v, (_dt.datetime, _dt.date)):
        # openpyxl's own inverse: it knows Excel's phantom 29 Feb 1900.
        from openpyxl.utils.datetime import to_excel
        n = to_excel(v)
        return int(n) if float(n).is_integer() else n
    try:
        return float(v)
    except (TypeError, ValueError):
        # The FQA Builder writes F97 as '1152 Fibers'.
        m = re.match(r'\s*(\d+(?:\.\d+)?)', str(v)) if isinstance(v, str) else None
        return float(m.group(1)) if m else None


def _xl_show(v):
    """A cell as the tech sees it in Excel: a real date as a date, a number
    in a date-formatted cell (a 1900 'date') as the number."""
    import datetime as _dt
    if isinstance(v, (_dt.datetime, _dt.date)):
        if v.year <= 1900:
            return str(_xl_number(v))
        return v.strftime('%Y-%m-%d')
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)[:60]


def _col_idx(col):
    n = 0
    for ch in col:
        n = n * 26 + (ord(ch) - 64)
    return n


def _split_ref(ref):
    m = re.fullmatch(r'([A-Z]+)(\d+)', ref)
    return _col_idx(m.group(1)), int(m.group(2))


# How much of each sheet the status reads (rows, columns): every cell above
# plus the FAT and Event Log ranges the checklist looks at.
_FQA_WINDOWS = {_SS: (100, 14), _EL: (118, 38), _FAT: (16, 31)}


def read_fqa_workbook(path):
    """{sheet: {(col, row): value}} for the parts of an FQA workbook the
    status needs, plus 'pictures': [(caption, n_photos)].  None when the file
    is not an FQA Site Survey.  Cached on (path, size, mtime): a status page
    re-renders on every click."""
    try:
        st_ = os.stat(path)
    except OSError:
        return None
    return _read_fqa_cached(os.path.abspath(path), st_.st_size, st_.st_mtime)


@st.cache_resource(show_spinner=False, max_entries=32)
def _read_fqa_cached(path, _size, _mtime):
    import openpyxl

    def _grab(data_only):
        wb = openpyxl.load_workbook(path, read_only=True, data_only=data_only)
        try:
            if _SS not in wb.sheetnames:
                return None
            got = {}
            for name, (rows, cols) in _FQA_WINDOWS.items():
                cells = {}
                if name in wb.sheetnames:
                    for r, row in enumerate(wb[name].iter_rows(
                            min_row=1, max_row=rows, max_col=cols,
                            values_only=True), 1):
                        for c, v in enumerate(row, 1):
                            if v is not None:
                                cells[(c, r)] = v
                got[name] = cells
            return got
        finally:
            wb.close()

    out = None
    try:
        # Two reads: what Excel last computed (a form saved by Excel), and
        # the formulas themselves (a package the FQA Builder wrote, which
        # nobody has opened yet, so it has no computed values).  The computed
        # value wins; a formula with nothing computed still counts as filled.
        out = _grab(False)
        if out is not None:
            for name, cells in (_grab(True) or {}).items():
                out.setdefault(name, {}).update(cells)
        if out is not None:
            out['pictures'] = _fqa_picture_bands(path)
    except Exception:
        out = None
    return out


def _fqa_picture_bands(path):
    """The Pictures tab as {'captions': [(row, col, text)], 'images':
    [(row, col)]} (0-based), read straight from the package parts -- the
    photos are drawing anchors, which openpyxl does not report.  Which end a
    photo belongs to is fqa_photos_per_end's job."""
    import zipfile
    import xml.etree.ElementTree as ET
    NS = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
          'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
          'p': 'http://schemas.openxmlformats.org/package/2006/relationships',
          'x': 'http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing'}

    def _resolve(base, target):
        if target.startswith('/'):
            return target[1:]
        parts = base.split('/')[:-1]
        for seg in target.split('/'):
            if seg == '..':
                parts.pop()
            elif seg and seg != '.':
                parts.append(seg)
        return '/'.join(parts)

    def _rels(z, part):
        d, f = part.rsplit('/', 1)
        name = f'{d}/_rels/{f}.rels'
        if name not in z.namelist():
            return {}
        root = ET.fromstring(z.read(name))
        return {r.get('Id'): (r.get('Type', ''), _resolve(part, r.get('Target', '')))
                for r in root.findall('p:Relationship', NS)}

    try:
        with zipfile.ZipFile(path) as z:
            wbx = ET.fromstring(z.read('xl/workbook.xml'))
            wrels = _rels(z, 'xl/workbook.xml')
            sheet = None
            for s in wbx.iter('{%s}sheet' % NS['m']):
                if s.get('name') == 'Pictures':
                    sheet = wrels.get(s.get('{%s}id' % NS['r']), (None, None))[1]
            if not sheet or sheet not in z.namelist():
                return {'captions': [], 'images': []}
            sx = ET.fromstring(z.read(sheet))
            shared = []
            if 'xl/sharedStrings.xml' in z.namelist():
                for si in ET.fromstring(z.read('xl/sharedStrings.xml')).findall('m:si', NS):
                    shared.append(''.join(t.text or '' for t in si.iter('{%s}t' % NS['m'])))
            captions = []                       # (0-based row, col, text)
            for c in sx.iter('{%s}c' % NS['m']):
                ref = c.get('r') or ''
                if not re.fullmatch(r'[A-Z]+\d+', ref):
                    continue
                if c.get('t') == 's':
                    v = c.find('m:v', NS)
                    text = shared[int(v.text)] if v is not None and v.text else ''
                else:
                    text = ''.join(t.text or '' for t in c.iter('{%s}t' % NS['m']))
                if text.strip():
                    col, row = _split_ref(ref)
                    captions.append((row - 1, col - 1, text.strip()))
            captions.sort()
            images = []
            for typ, target in _rels(z, sheet).values():
                if typ.endswith('/drawing') and target in z.namelist():
                    dx = ET.fromstring(z.read(target))
                    for anchor in list(dx):
                        pic = anchor.find('x:pic', NS)
                        frm = anchor.find('x:from', NS)
                        if pic is not None and frm is not None:
                            images.append((int(frm.find('x:row', NS).text),
                                           int(frm.find('x:col', NS).text)))
            return {'captions': captions, 'images': images}
    except Exception:
        return {'captions': [], 'images': []}


def fqa_photos_per_end(pics):
    """{'A': n, 'Z': n} photos on the Pictures tab.

    Two layouts are in use.  Field Capture writes one band per location, a
    caption 'A-Location: ...' / 'Z-Location: ...' with that location's
    photos below it: a photo belongs to the nearest caption above it.  The
    packages the crews have been submitting (Span 4, Span 5, Durkee,
    Winterhaven, Cle Elum ...) put the two site names side by side on one row
    ('FLAGLER ILA' in D23, 'Bethune ILA' in K23) with each end's photos in
    its half: the left name is the A end, and a photo belongs to the nearer
    name by column."""
    caps = (pics or {}).get('captions') or []
    imgs = (pics or {}).get('images') or []
    out = {'A': 0, 'Z': 0, 'unassigned': 0}
    bands = [(r, t[0]) for r, _c, t in caps if re.match(r'[AZ]-Location', t)]
    if bands:
        for r, _c in imgs:
            above = [end for cr, end in bands if cr <= r]
            if above:
                out[above[-1]] += 1
        return out
    rows = {}
    for r, c, _t in caps:
        rows.setdefault(r, []).append(c)
    pair = next(((r, sorted(cs)) for r, cs in sorted(rows.items()) if len(cs) == 2), None)
    if pair is None:
        # No site names on the tab (the Cle Elum packages: six photos in a
        # grid): nothing says which end a photo is from, so none is guessed.
        out['unassigned'] = len(imgs)
        return out
    left, right = pair[1]
    for _r, c in imgs:
        out['A' if abs(c - left) <= abs(c - right) else 'Z'] += 1
    return out


def read_capture_sheet(path):
    """Field Capture's capture sheet (.xlsx): its 'Submissions' tab, one row
    per location, as [{'site': 'A'|'Z'|'other', 'lat', 'lon', 'acc',
    'photos', 'initials', 'date'}].  None when it is not a capture sheet."""
    import openpyxl
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception:
        return None
    try:
        if 'Submissions' not in wb.sheetnames:
            return None
        it = wb['Submissions'].iter_rows(values_only=True)
        head = [str(h or '').strip() for h in next(it, [])]
        if 'Location' not in head or 'GPS Latitude' not in head:
            return None
        col = {h: i for i, h in enumerate(head)}
        get = lambda row, h: row[col[h]] if h in col and col[h] < len(row) else None
        out = []
        for row in it:
            loc = str(get(row, 'Location') or '')
            site = 'A' if loc.startswith('A-') else 'Z' if loc.startswith('Z-') else 'other'
            out.append({'site': site, 'lat': get(row, 'GPS Latitude'),
                        'lon': get(row, 'GPS Longitude'),
                        'acc': get(row, 'GPS Accuracy (m)'),
                        'photos': get(row, 'Photos') or 0,
                        'initials': get(row, 'Tester Initials'),
                        'date': get(row, 'Date')})
        return out
    finally:
        wb.close()


def project_field_dir(project_path):
    return os.path.join(os.path.dirname(os.path.abspath(project_path)), PROJECT_FIELD_DIR)


JOB_DETAILS_SOURCE = 'job details'


def job_details_workbook(fqa_job):
    """The project's job details laid out as the FQA form cells the FQA
    Builder would write them to -- its own cell map (fqa.writer), so the
    status reads the same cells either way.  Listed after any real workbook,
    so a built package speaks first.  None when there are no job details."""
    if not isinstance(fqa_job, dict) or not fqa_job:
        return None
    try:
        from fqa.job_facts import JobFacts
        from fqa.writer import _survey_cells
        cells = _survey_cells(JobFacts.from_dict(fqa_job))
    except Exception:
        return None
    ss_cells = {}
    for c in cells:
        if c.sheet == _SS and c.value not in (None, ''):
            ss_cells[_split_ref(c.ref)] = c.value
    return {_SS: ss_cells, _EL: {}, _FAT: {}, 'pictures': None}


def collect_field_files(*dirs):
    """The FQA workbooks and capture sheets in `dirs` (the work folder's
    Field and FQA folders): (fqa, captures), fqa = [(name, parsed)],
    captures = [(name, rows)].  Newest first; anything else is skipped."""
    fqa, caps = [], []
    paths = []
    for d in dirs:
        try:
            paths += [os.path.join(d, n) for n in os.listdir(d)]
        except OSError:
            continue
    for p in sorted(paths, key=lambda q: -os.path.getmtime(q) if os.path.exists(q) else 0):
        n = os.path.basename(p)
        low = n.lower()
        if n.startswith(('~$', '.')) or not os.path.isfile(p):
            continue
        if low.endswith(('.xlsm', '.xlsx')):
            wb = read_fqa_workbook(p)
            if wb is not None:
                fqa.append((n, wb))
                continue
        if low.endswith('.xlsx'):
            rows = read_capture_sheet(p)
            if rows is not None:
                caps.append((n, rows))
    return fqa, caps


def _trace_fibers(folder):
    """{fiber number} of the trace files in one direction's folder."""
    out = set()
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    for n in names:
        if n.lower().rstrip().endswith(_TRACE_EXTS):
            f = trace_server.extract_fiber_num(n)
            if f is not None:
                out.add(f)
    return out


# ── Event Log locations ──────────────────────────────────────────────────
# The GPS on the FQA form is here: every splice's Event Location, typed as
# degrees-minutes-seconds ('39 21 21.13 N 102 22 18.70 W').  The two ends
# carry the street address.  Finished packages on disk had, marked FINAL:
# '39 18 59 52 N 102 10 06 54 W' (decimal points lost), '39 15 50.16N 101
# 5407.26 W' (a space lost), '32 47 06.0 N 114 47 55.0 E' (a California
# splice put in China).  A reviewer cannot see those; this can.
_COORD_CHARS = re.compile(r"[\d\s.,°'\"NSEWnsew+-]+")
_NUM = re.compile(r'\d+(?:\.\d+)?')


def parse_event_location(text):
    """('blank'|'address'|'gps'|'bad', detail).  gps -> (lat, lon);
    bad -> what is wrong with it, in words."""
    t = str(text or '').strip()
    if not t:
        return 'blank', None
    if not (_COORD_CHARS.fullmatch(t) and sum(ch.isdigit() for ch in t) >= 4):
        return 'address', None
    u = t.upper()
    for ch in "°'\"":
        u = u.replace(ch, ' ')
    m = re.fullmatch(r'\s*([+-]?\d+(?:\.\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?)\s*', u)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return 'bad', 'out of range'
        if lat < 0 or lon > 0:
            return 'bad', 'wrong hemisphere (a US site is north and west: -longitude)'
        return 'gps', (lat, lon)
    m = re.fullmatch(r'\s*([\d.\s]+?)\s*([NS])\s*([\d.\s]+?)\s*([EW])\s*', u)
    if not m:
        return 'bad', 'not a coordinate the form can read'

    def dms(part, top):
        toks = part.split()
        if not 1 <= len(toks) <= 3 or not all(_NUM.fullmatch(x) for x in toks):
            return None, (f'{len(toks)} numbers where degrees, minutes, seconds were expected'
                          + (' (a decimal point lost?)' if len(toks) > 3 else ''))
        vals = [float(x) for x in toks]
        if len(vals) > 1 and not vals[0].is_integer():
            return None, 'decimal degrees followed by minutes'
        if any(v >= 60 for v in vals[1:]):
            return None, 'minutes or seconds of 60 or more (a space or decimal point lost?)'
        deg = vals[0] + (vals[1] / 60 if len(vals) > 1 else 0) + (vals[2] / 3600 if len(vals) > 2 else 0)
        if deg > top:
            return None, 'out of range'
        return deg, None

    lat, why = dms(m.group(1), 90)
    if lat is None:
        return 'bad', 'latitude: ' + why
    lon, why = dms(m.group(3), 180)
    if lon is None:
        return 'bad', 'longitude: ' + why
    if m.group(2) == 'S' or m.group(4) == 'E':
        return 'bad', f'says {m.group(2)}/{m.group(4)}; a US site is N and W'
    return 'gps', (lat, -lon)


def fqa_event_rows(wb):
    """The splice events of an FQA workbook's Event Log (rows 18 on), up to
    Site Z.  A package the FQA Builder wrote marks Site Z with an 'X' in AL;
    a form saved by Excel shows 'Site Z' in B."""
    cells = wb.get(_EL, {})
    rows = []
    for r in range(18, 119):
        g = lambda col: cells.get((_col_idx(col), r))
        b = g('B')
        if (isinstance(b, str) and b.strip().lower() == 'site z') or g('AL') == 'X':
            break
        # A real event has something typed on it.  An untouched form still
        # numbers its rows, shows '<Select>' and a distance of 0 -- that is
        # an empty Event Log, not 101 events missing their data.
        # The form's own formulas (Location, Vault ID) evaluate to blank on
        # an untyped row, so formula text is not typing either; the FQA
        # Builder writes real values for real events.
        typed = lambda v: _xl_has(v) and not (isinstance(v, str) and v.startswith('='))
        if not (any(typed(g(c)) for c in ('D', 'N', 'Q'))
                or (typed(g('AB')) and g('AB') != 0)):
            break
        no = b if isinstance(b, (int, float)) or (isinstance(b, str) and not b.startswith('=')) else r - 17
        rows.append({'row': r, 'no': no, 'loc': g('D'), 'type': g('Q'),
                     'fiber': g('X'), 'dist': g('AB')})
    return rows


def _event_list(rows):
    """'events 3, 5-7' for a list of event rows."""
    nums = []
    for e in rows:
        try:
            nums.append(int(float(e['no'])))
        except (TypeError, ValueError):
            nums.append(e['row'] - 17)
    return ('event ' if len(nums) == 1 else 'events ') + _fiber_ranges(nums)


def gps_fixes_by_hand(gps_manual):
    """{event number or 'A'/'Z': {'lat', 'lon'}} for the GPS tab's readable
    entries.  Keys are stored as text ('3', 'A'); a splice's comes back as
    an int so it lines up with the phone's event numbers."""
    out = {}
    for k, v in (gps_manual or {}).items():
        kind, ll = parse_event_location(v)
        if kind != 'gps':
            continue
        key = int(k) if str(k).isdigit() else str(k)
        out[key] = {'lat': ll[0], 'lon': ll[1]}
    return out


def dms_text(lat, lon):
    """'39 28 7.75 N 102 58 5.43 W': how the FQA Event Log holds a GPS fix."""
    def part(v):
        v = abs(v)
        d = int(v)
        m = int((v - d) * 60)
        sec = round(((v - d) * 60 - m) * 60, 2)
        if sec >= 60:
            m, sec = m + 1, 0.0
        if m >= 60:
            d, m = d + 1, 0
        return f'{d} {m} {sec:.2f}'
    return (f"{part(lat)} {'N' if lat >= 0 else 'S'} "
            f"{part(lon)} {'W' if lon < 0 else 'E'}")


SECTION_TITLES = {1: 'Site Survey Data', 2: 'FAT', 3: 'Event Log', 4: 'Data Files'}


def project_status(snap, fqa, caps, trace_dirs=None, work='', manual=None,
                   pkgs=None, n_splices=None, final_desc=None, gps_manual=None):
    """[{'section', 'item', 'ok', 'detail', 'source'}] -- the Submittal
    Checklist's four sections, each item read from the collected files.

    fqa / caps: what collect_field_files found.  trace_dirs: span 1's (A, B)
    when known.  work: the work folder (section 4's own subfolders).
    manual: the tech's ticks for what no file can prove ({'4.03': True,
    '4.04': 'na', ...})."""
    manual = manual or {}
    pkgs = pkgs or []
    items = []

    def add(sec, item, ok, detail='', source=''):
        items.append({'section': sec, 'item': item, 'ok': bool(ok),
                      'detail': detail, 'source': source or ''})

    def first_with(sheet, ref):
        c = _split_ref(ref)
        for name, wb in fqa:
            v = wb.get(sheet, {}).get(c)
            if _xl_has(v):
                return name, v
        return None, None

    def rng(wb, sheet, a, b):
        (c1, r1), (c2, r2) = _split_ref(a), _split_ref(b)
        cells = wb.get(sheet, {})
        return [cells.get((c, r)) for r in range(r1, r2 + 1) for c in range(c1, c2 + 1)]

    # ── 1  Site Survey Data ──
    for no, what, sheet, ref in FQA_CHECKLIST_CELLS:
        src, v = first_with(sheet, ref)
        add(1, f'{no}  {what}', src, _xl_show(v) if src else '', src)
    for end in ('A', 'Z'):
        for part, title in (('rack', 'rack location'), ('panel', 'panel details')):
            have, missing, src = [], [], ''
            pname, prec = _pkg_site(pkgs, end)
            for label, ref in FQA_END_CELLS[end][part]:
                s, _v = first_with(_SS, ref)
                if not s and prec is not None and _xl_has(_pkg_value(prec, label)):
                    s = pname
                (have if s else missing).append(label)
                src = src or s or ''
            add(1, f'{_END_NAMES[end]} {title} ({", ".join(l for l, _r in FQA_END_CELLS[end][part])})',
                not missing, ('missing: ' + ', '.join(missing)) if missing and have else '', src)
        n_photos, src, loose = 0, '', 0
        for name, wb in fqa:
            per = fqa_photos_per_end(wb.get('pictures'))
            loose = max(loose, per['unassigned'])
            if per[end] > n_photos:
                n_photos, src = per[end], name
        for name, rows in caps:
            k = sum(int(r['photos'] or 0) for r in rows if r['site'] == end)
            if k > n_photos:
                n_photos, src = k, name
        pname, prec = _pkg_site(pkgs, end)
        if prec is not None and len(prec.get('photos') or []) > n_photos:
            n_photos, src = len(prec['photos']), pname
        # The box's labels, read and matched on the phone.
        if prec is not None:
            checks = {c.get('key'): c for c in prec.get('checks') or []}
            not_ok = [title for key, title in BOX_LABELS
                      if key in checks and checks[key].get('status') != 'ok']
            unread = [title for key, title in BOX_LABELS if key not in checks]
            add(1, f'{_END_NAMES[end]} labels match the job', not not_ok and not unread,
                ('check: ' + ', '.join(not_ok + unread)) if (not_ok or unread) else
                'rack, RMU, far end, fiber ranges', pname)
        else:
            add(1, f'{_END_NAMES[end]} labels match the job', False, 'from the phone')
        add(1, f'{_END_NAMES[end]} photos', n_photos > 0,
            f'{n_photos} photo{"s" * (n_photos != 1)}' if n_photos
            else (f'{loose} photos on the Pictures tab, but no site names to tell '
                  'the ends apart' if loose else 'from the phone'), src)

    # ── 2  FAT ──
    fat = next((n for n, wb in fqa
                if sum(1 for v in rng(wb, _FAT, 'B16', 'AE16') if v not in (None, '')) >= 14), None)
    add(2, '2.01  FAT table completed', fat, source=fat)

    # ── 3  Event Log ── the workbook with the most events speaks for it.
    length = next((n for n, wb in fqa
                   if _xl_has(wb.get(_EL, {}).get(_split_ref('W8')))
                   and not (isinstance(wb[_EL][_split_ref('W8')], (int, float))
                            and wb[_EL][_split_ref('W8')] <= 0)), None)
    add(3, '3.01  Length entered', length, source=length)
    src, events = None, []
    for name, wb in fqa:
        ev = fqa_event_rows(wb)
        if len(ev) > len(events):
            src, events = name, ev
    # GPS at every splice point: from the phone (Robert, 2026-09-23), or
    # typed on the GPS tab, which wins where both have one (2026-09-26).
    psrc, psplices, ppkg = next(((n, p.get('splices') or [], p) for n, p in pkgs
                                 if p.get('splices')), (None, [], None))
    hand = gps_fixes_by_hand(gps_manual)
    want_sp = n_splices if n_splices is not None else max(
        [len(psplices)] + [k for k in hand if isinstance(k, int)])
    if want_sp or psplices:
        by_event = {}
        for x in psplices:
            try:
                by_event[int(x.get('event'))] = x.get('gps')
            except (TypeError, ValueError):
                continue
        merged = [{'event': i, 'gps': hand.get(i) or by_event.get(i)}
                  for i in range(1, (want_sp or len(psplices)) + 1)]
        got = [x for x in merged if (x.get('gps') or {}).get('lat') is not None]
        gone = [x for x in merged if (x.get('gps') or {}).get('lat') is None]
        n_hand = sum(1 for x in merged if x['event'] in hand)
        src = ' + '.join(filter(None, (psrc if psplices else None,
                                       'GPS tab' if n_hand else None)))
        detail = f'{len(got)} of {len(merged)}'
        if n_hand:
            detail += f' ({n_hand} typed on the GPS tab)'
        if gone:
            detail += ' · missing on ' + _event_list([{'no': x['event'], 'row': 0} for x in gone])
        add(3, 'GPS at every splice point', got and not gone,
            detail if (psplices or n_hand) else 'from the phone or the GPS tab', src)
        if got:
            A = hand.get('A') or (_pkg_end_fix(ppkg, 'A') if ppkg else None)
            Z = hand.get('Z') or (_pkg_end_fix(ppkg, 'Z') if ppkg else None)
            bad = [(no, why) for no, why in splice_gps_problems(merged, A, Z)
                   if why != 'no GPS fix']
            add(3, 'Splice GPS in order and between the ends', (A and Z) and not bad,
                ('; '.join(f'event {no}: {why}' for no, why in bad[:4])
                 + (f'; and {len(bad) - 4} more' if len(bad) > 4 else '')) if bad
                else ('checked against the A and Z box fixes' if (A and Z)
                      else 'needs a GPS fix at the A and Z boxes'), src)
    if not events:
        add(3, '3.02-3.06  Events', False, 'no events in an Event Log yet')
    else:
        n = len(events)
        # The customer's own checklist only looks at the first two events (D18:D19);
        # a package blank from event 3 on passes it.  This checks every one.
        no_loc = [e for e in events if not _xl_has(e['loc'])]
        add(3, f'3.02  Location entered for each of the {n} events', not no_loc,
            ('missing on ' + _event_list(no_loc)) if no_loc else '', src)
        bad = []
        for e in events:
            if _xl_has(e['loc']) and not str(e['loc']).startswith('='):
                kind, why = parse_event_location(e['loc'])
                if kind == 'bad':
                    bad.append(f"{_event_list([e])}: '{e['loc']}' ({why})")
        n_gps = sum(1 for e in events if parse_event_location(e['loc'])[0] == 'gps')
        add(3, 'GPS locations readable', not bad,
            '; '.join(bad[:4]) + (f'; and {len(bad) - 4} more' if len(bad) > 4 else '')
            if bad else f'{n_gps} GPS, {n - n_gps} addresses', src)
        no_type = [e for e in events if not _xl_has(e['type'])]
        add(3, '3.03  Splice / connection type for each event', not no_type,
            ('missing on ' + _event_list(no_type)) if no_type else '', src)
        no_fiber = [e for e in events if not _xl_has(e['fiber'])]
        add(3, '3.04  Fiber types for each event', not no_fiber,
            ('missing on ' + _event_list(no_fiber)) if no_fiber else '', src)
        no_dist = [e for e in events if not _xl_has(e['dist']) or e['dist'] == 0]
        add(3, '3.05  Distance to each event', not no_dist,
            ('missing on ' + _event_list(no_dist)) if no_dist else '', src)
        back = []
        prev = None
        for e in events:
            d = _xl_number(e['dist']) if not (isinstance(e['dist'], str) and e['dist'].startswith('=')) else None
            if d is not None:
                if d < 0 or (prev is not None and d <= prev):
                    back.append(e)
                prev = d
        add(3, '3.06  Distances increase along the span (no negatives)', not back,
            ('check ' + _event_list(back)) if back else '', src)

    # ── 4  Data Files ──
    s1 = (snap.get('spans') or [{}])[0]
    da, db = trace_dirs or ((s1.get('dir_a'), s1.get('dir_b'))
                            if s1.get('mode') == 'two' else (None, None))
    fa, fb = (_trace_fibers(da) if da else set()), (_trace_fibers(db) if db else set())
    if final_desc:
        add(4, '4.01  Final traces chosen', True, final_desc)
    add(4, '4.01  A-direction traces', fa, f'{len(fa)} fibers' if fa else 'select the traces')
    add(4, '4.01  B-direction traces', fb, f'{len(fb)} fibers' if fb else 'select the traces')
    if fa or fb:
        gaps = []
        if fa - fb:
            gaps.append('B missing ' + _fiber_ranges(sorted(fa - fb)))
        if fb - fa:
            gaps.append('A missing ' + _fiber_ranges(sorted(fb - fa)))
        add(4, '4.01  Every fiber shot both ways', not gaps, '; '.join(gaps))
    fsrc, want = first_with(_SS, 'F97')
    want_n = _xl_number(want) if fsrc else None
    if want_n:
        want_n = int(want_n)
        both = len(fa & fb)
        add(4, f'4.01  {want_n} fibers, as the form says were tested', both >= want_n,
            f'{both} have both directions', fsrc)

    def files_in(key):
        if not work:
            return []
        try:
            return [n for n in os.listdir(work_sub(key, work))
                    if not n.startswith(('.', '~$'))]
        except OSError:
            return []

    pm = files_in('power')
    add(4, '4.02  Power meter files', pm, f'{len(pm)} file{"s" * (len(pm) != 1)}' if pm else '')
    add(4, '4.03  Files named to the naming convention', manual.get('4.03') is True,
        'ticked by hand' if manual.get('4.03') is True else 'tick when checked')
    logs = files_in('splice_logs')
    for no, what in (('4.04', 'Splice logs'), ('4.05', 'Splice logs and exception documents')):
        na = manual.get(no) == 'na'
        add(4, f'{no}  {what}', logs or na,
            'not needed on this job' if na and not logs
            else (f'{len(logs)} file{"s" * (len(logs) != 1)}' if logs else ''))
    return items


# ── the phone job: a QR code out, a capture package back ─────────────────
# Robert, 2026-09-23: "the job will be created in OTDR Suite. We will need
# the phone to scan a QR code that will tell the phone app what it needs in
# terms of photos, GPS locations, etc" ... "then the phone will scan to make
# sure that the photos include the labels required and then send back the
# partial product package".  GPS at every splice point; photos at the A and
# Z boxes (the rack label and the termination box, as in Span 4's package).
#
# The QR is a link to Field Capture with the job in the #fragment, so the
# iPhone's own Camera app reads it -- no scanner in the web app, and a
# fragment never reaches the web host.  The job carries exactly what Field
# Capture's label checks already work from (the site names, the fiber
# count, each end's section 1.2 rack and panel values), so the phone never
# needs the customer's blank form.  The phone sends back a capture package: a .zfc
# (folder_intake.share_write) of capture.json and the photos, emailed; someone saves it
# into Field/.  Older packages were plain .zip and still read.
JOB_VERSION = 1
CAPTURE_FORMAT = 'otdr-capture'
FIELD_CAPTURE_URL_KEY = 'field_capture_url'
# Where Field Capture is hosted for the phones (Robert, 2026-09-24).
FIELD_CAPTURE_DEFAULT_URL = 'https://field-capture.rcolbert.workers.dev'


def project_job_id():
    """The open project's job ID, made on first use and kept in the file."""
    ss = st.session_state
    if not ss.get('project_job_id'):
        import uuid
        ss['project_job_id'] = uuid.uuid4().hex[:8]
        # Made on the run right after an open, it would be taken for part of
        # the opened file and never saved, and the next open would make
        # another -- stranding every package the phone sent for this one.
        ss['_project_force_save'] = True
    return ss['project_job_id']


def job_manifest(prod_path, job_id, span, fqa_job=None):
    """What the phone needs, from the production sheet and the job form."""
    from fqa.job_facts import JobFacts, derive
    from fqa.production_sheet import TERMINATION
    prod = _read_prod(prod_path)
    job = derive(prod, JobFacts.from_dict(fqa_job or {}))

    def sec(s):
        return {k: str(v) for k, v in (('floor', s.floor), ('room', s.room), ('aisle', s.aisle),
                                       ('bay', s.bay), ('rmu', s.rmu)) if v}

    ends = [l for l in prod.locations if l.kind == TERMINATION]
    name_a = job.site_a.alias or (ends[0].name if ends else None)
    name_z = job.site_z.alias or (ends[-1].name if ends else None)
    return {
        'v': JOB_VERSION, 'id': job_id, 'span': span,
        'info': {'aliasA': name_a, 'aliasZ': name_z,
                 'fiberCount': int(job.fiber_count) if job.fiber_count else None,
                 'section12': {'A': sec(job.site_a), 'Z': sec(job.site_z)}},
        # [event number, vault ID, name] -- event numbers as the Event Log
        # counts them: 1 is the first splice after Site A.
        'splices': [[i + 1, l.vault_id, l.name] for i, l in enumerate(prod.splices)],
    }


def job_code(manifest):
    """The manifest as a URL-safe string: compact JSON, zlib, base64url."""
    import base64
    import zlib
    raw = json.dumps(manifest, separators=(',', ':'), default=str).encode('utf-8')
    return base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode('ascii').rstrip('=')


def job_from_code(code):
    """The inverse, for tests and for reading a package's job back."""
    import base64
    import zlib
    pad = '=' * (-len(code) % 4)
    return json.loads(zlib.decompress(base64.urlsafe_b64decode(code + pad)).decode('utf-8'))


def job_link(base_url, manifest):
    base = (base_url or '').strip()
    return f"{base.split('#')[0]}#job={job_code(manifest)}"


def write_job_email(work, subject, body):
    """An unsent email draft (.eml) with the job link, in the work folder."""
    from email import policy
    from email.message import EmailMessage
    msg = EmailMessage(policy=policy.SMTP)
    msg['Subject'] = ' '.join(str(subject).split())[:300]
    msg['X-Unsent'] = '1'
    msg.set_content(body)
    path = os.path.join(work, 'Job link.eml')
    with open(path, 'wb') as fh:
        fh.write(msg.as_bytes())
    return path


def job_qr_png(link):
    """PNG bytes of the link's QR code, or (None, why)."""
    try:
        import io
        import qrcode
        from qrcode.exceptions import DataOverflowError
    except Exception:
        return None, (f'this {PRODUCT_NAME} build has no QR library yet (it comes with the '
                      'next installer); send the link below to the phone instead')
    try:
        q = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=2, box_size=6)
        q.add_data(link)
        q.make(fit=True)
    except DataOverflowError:
        return None, ('too many splice points for one QR code; send the link below '
                      'to the phone instead (email or text it)')
    buf = io.BytesIO()
    q.make_image().save(buf, format='PNG')
    return buf.getvalue(), ''


def read_capture_package(path, why=None):
    """A phone capture package, or None: a .zfc (folder_intake share file, kind
    field-capture) or an older plain .zip from before the .zfc format; both
    hold capture.json + photos.  A file that should be a package but cannot
    be read appends a short reason to ``why`` (a list) when one is given; a
    plain .zip with no capture.json is not a package and adds nothing."""
    import zipfile
    import folder_intake

    def _fail(reason):
        if why is not None:
            why.append(reason)
        return None
    if str(path).lower().endswith('.zip'):
        try:   # pre-.zfc package: no manifest.json
            with zipfile.ZipFile(path) as z:
                names = z.namelist()
                if 'manifest.json' in names or 'capture.json' not in names:
                    return None        # some other zip: not ours, ignore quietly
                data = json.loads(z.read('capture.json').decode('utf-8'))
        except zipfile.BadZipFile:
            return None                # not a zip at all: not ours either
        except Exception as exc:
            return _fail(f'capture.json unreadable: {type(exc).__name__}')
    else:
        try:
            sf = folder_intake.share_open(path, expect='field-capture')
            data = json.loads(sf.read('capture.json').decode('utf-8'))
        except folder_intake.ShareFileError as exc:
            return _fail(str(exc) or 'not a capture package')
        except Exception as exc:
            return _fail(f'capture.json unreadable: {type(exc).__name__}')
    if not isinstance(data, dict) or data.get('format') != CAPTURE_FORMAT:
        return _fail('not a capture package this version reads')
    return data


def capture_package_exts():
    """.zfc (and any later name for it) plus the pre-.zfc .zip."""
    import folder_intake
    return folder_intake.share_extensions('field-capture') + ('.zip',)


def collect_capture_packages(*dirs, unreadable=None):
    """[(name, package)] newest first, from the work folder's Field folder.
    ``unreadable`` (a list) collects (name, reason) for package files that
    could not be read, so the status page can list them."""
    exts = capture_package_exts()
    out = []
    for d in dirs:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            p = os.path.join(d, n)
            if n.lower().endswith(exts) and os.path.isfile(p):
                why = []
                pkg = read_capture_package(p, why)
                if pkg is None and why and unreadable is not None:
                    unreadable.append((n, why[0]))
                if pkg is not None:
                    out.append((os.path.getmtime(p), n, pkg))
    return [(n, pkg) for _t, n, pkg in sorted(out, key=lambda x: -x[0])]


# The label checks the phone must pass at each box, by the key its checks use.
BOX_LABELS = (('rack', 'rack label'), ('rmu', 'RMU tags'), ('toward', 'far-end label'),
              ('fibers', 'fiber-range labels'))
# Section 1.2 as the phone names the fields, for the status's rack/panel items.
_PKG_FIELDS = {'floor': 'floor', 'room': 'room', 'aisle': 'aisle', 'bay': 'bay',
               'RMU': 'rmu', 'connector': 'termination', 'panel type': 'panelType'}


# The phone blocks the send on these (fieldcapture/web/app.js
# spliceGpsProblems); the status recomputes them from the package, so the
# office sees the same verdict even for a package sent with an override.
# A splice must lie in a corridor around the straight A-Z line: no further
# off it than 20% of the span (at least 2 km), and between the two ends
# along it.  (A first try, "A->splice->Z at most 1.5x A->Z", let a point
# 28 km off a 52 km span through: that budget grows with the span.)
CORRIDOR_FRAC, CORRIDOR_MIN_M, END_SLACK_M, ORDER_SLACK_M = 0.20, 2000.0, 500.0, 50.0


def _metres(a, b):
    import math
    R, rad = 6371000.0, math.pi / 180
    dlat, dlon = (b['lat'] - a['lat']) * rad, (b['lon'] - a['lon']) * rad
    h = (math.sin(dlat / 2) ** 2 + math.cos(a['lat'] * rad) * math.cos(b['lat'] * rad)
         * math.sin(dlon / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(h))


def splice_gps_problems(splices, A, Z):
    """[(event, problem)]: every splice has a fix; with both ends fixed,
    each lies in the corridor between them and in order along the span."""
    import math
    out = []
    fixed = [(x.get('event'), x.get('gps')) for x in splices]
    for no, g in fixed:
        if not g or g.get('lat') is None:
            out.append((no, 'no GPS fix'))
    if not (A and Z and A.get('lat') is not None and Z.get('lat') is not None):
        return out
    span = _metres(A, Z)
    corridor = max(CORRIDOR_MIN_M, CORRIDOR_FRAC * span)
    prev = None
    for no, g in fixed:
        if not g or g.get('lat') is None:
            continue
        da, dz = _metres(A, g), _metres(g, Z)
        along = (da ** 2 - dz ** 2 + span ** 2) / (2 * span or 1)
        off = math.sqrt(max(0.0, da ** 2 - along ** 2))
        if off > corridor or along < -END_SLACK_M or along > span + END_SLACK_M:
            out.append((no, f'not between the ends ({off / 1000:.1f} km off the A-Z line, '
                            f'{along / 1000:.1f} km along a {span / 1000:.1f} km span)'))
            continue
        if prev and along < prev[1] - ORDER_SLACK_M:
            out.append((no, f'out of order ({(prev[1] - along) / 1000:.2f} km nearer A '
                            f'than event {prev[0]})'))
        prev = (no, along)
    return out


def _pkg_end_fix(pkg, end):
    for s in pkg.get('sites') or []:
        if s.get('site') == end and (s.get('gps') or {}).get('lat') is not None:
            return s['gps']
    return None


def _pkg_site(pkgs, end):
    """The newest package's record for one end, with the package's name."""
    for name, pkg in pkgs:
        recs = [s for s in pkg.get('sites') or [] if s.get('site') == end]
        if recs:
            return name, recs[0]
    return None, None


def _pkg_value(rec, label):
    key = _PKG_FIELDS[label]
    v = rec.get(key)
    if v in (None, ''):
        v = (rec.get('panel') or {}).get(key)
    return v


def _fqa_names_mismatch(wb, site_names):
    """True when a dropped FQA workbook names its sites and none of them is
    one of this project's -- probably another span's file."""
    names = [n.strip().lower() for n in site_names if n and n.strip() and n not in ('A', 'B')]
    if not names:
        return False
    cells = [wb.get(_SS, {}).get(_split_ref(r)) for r in ('E9', 'E10', 'E11', 'E12', 'K9', 'K11')]
    text = ' '.join(str(v).lower() for v in cells if _xl_has(v))
    if not text:
        return False
    return not any(n in text for n in names)


def _store_dropped(field_dir, upload):
    """Copy one dropped file into a work subfolder.  An identical file is
    not copied twice; a different file with the same name gets ' (2)'."""
    data = upload.getvalue()
    os.makedirs(field_dir, exist_ok=True)
    base, ext = os.path.splitext(os.path.basename(upload.name))
    dest, k = os.path.join(field_dir, base + ext), 2
    while os.path.exists(dest):
        try:
            with open(dest, 'rb') as fh:
                if fh.read() == data:
                    return dest, False
        except OSError:
            pass
        dest = os.path.join(field_dir, f'{base} ({k}){ext}')
        k += 1
    with open(dest, 'wb') as fh:
        fh.write(data)
    return dest, True


def _drop_into(label, key, sub, types, help_text=''):
    """An uploader whose files are kept in one work subfolder, once each.
    `sub` is a PROJECT_DIRS key, or (key, subfolder ...) for a folder inside
    it.  Each file kept is logged in the project's events."""
    ss = st.session_state
    ups = st.file_uploader(label, type=types, accept_multiple_files=True, key=key,
                           help=help_text or None)
    done = ss.setdefault('_status_stored', set())
    parts = (sub,) if isinstance(sub, str) else tuple(sub)
    folder = os.path.join(work_sub(parts[0]), *parts[1:])
    stored = []
    for up in ups or []:
        sig = (key, up.name, up.size, getattr(up, 'file_id', None))
        if sig in done:
            continue
        done.add(sig)
        try:
            dest, new = _store_dropped(folder, up)
            stored.append(dest)
        except OSError as exc:
            st.error(f'Could not keep {up.name}: {exc}')
            continue
        if new:
            try:
                work = work_dir()
                kind, text = _describe_file(work, _rel(work, dest))
                project_log(work, kind, text, [dest])
            except Exception as exc:
                report_error('project: log a dropped file', exc, {})
    return stored


# ── copying traces into the work folder (section 4) ──────────────────────
def copy_traces(src_a, src_b, dest_a, dest_b):
    """Copy the trace files under src_a / src_b (subfolders included) into
    dest_a / dest_b.  Returns (n_a, n_b)."""
    import shutil
    counts = []
    for src, dest in ((src_a, dest_a), (src_b, dest_b)):
        os.makedirs(dest, exist_ok=True)
        k = 0
        for root, _dirs, files in os.walk(src):
            for n in files:
                if n.startswith('._') or not n.lower().rstrip().endswith(_TRACE_EXTS):
                    continue
                shutil.copy2(os.path.join(root, n), os.path.join(dest, n))
                k += 1
        counts.append(k)
    return tuple(counts)


def add_shoot(src_a, src_b, work, date='', label=''):
    """A new shoot: Traces/<date[ label]>/A and /B, copied from src_a /
    src_b.  The date defaults to the day the .sor files were shot (else
    today).  Its date and label go in the project's record.  Returns
    (shoot id, n_a, n_b)."""
    import datetime as _dt
    date = str(date or sor_shot_date(src_a) or sor_shot_date(src_b)
               or _dt.date.today().isoformat())
    sid = new_shoot_folder(work, date, label)
    d = os.path.join(work_sub('traces', work), sid)
    na, nb = copy_traces(src_a, src_b, os.path.join(d, 'A'), os.path.join(d, 'B'))
    ss = st.session_state
    shoots = dict(ss.get('project_shoots') or {})
    shoots[sid] = {'date': date, 'label': label or ''}
    ss['project_shoots'] = shoots
    project_log(work, 'Traces', f"Traces added: shot {date}{' · ' + label if label else ''}"
                f' · {na} A and {nb} B trace files', [d])
    return sid, na, nb


def set_final_shoot(sid, work=None):
    """Make `sid` the final shoot: the one the FQA checklist, the phone job
    and the FQA package use.  The tools are not moved (Robert, 2026-09-27:
    they run on the shoots ticked on the Traces tab)."""
    st.session_state['project_final_shoot'] = sid


def copy_traces_into(src_a, src_b, work):
    """A new shoot from src_a / src_b (the setup screen and older callers).
    Returns (n_a, n_b)."""
    _sid, na, nb = add_shoot(src_a, src_b, work)
    return na, nb


def pick_file(title, types):
    """Native single-file picker; '' on cancel, None without Tk."""
    if _tk_unsafe():
        return None
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.wm_attributes('-topmost', 1)
        path = filedialog.askopenfilename(title=title, filetypes=types)
        root.destroy()
        return path or ''
    except Exception:
        return None


def _production_summary(path):
    """(n locations, n splices, warnings) or (None, None, [error])."""
    try:
        prod = _read_prod(path)
    except Exception as exc:
        return None, None, [f'could not read it: {exc}']
    return len(prod.locations), len(prod.splices), list(prod.warnings)


def _add_production_sheet(src, work):
    """Copy a production sheet into Production/; an older one moves to
    Production/Superseded rather than being deleted."""
    import shutil
    d = work_sub('production', work)
    os.makedirs(d, exist_ok=True)
    old = project_production_sheet(work)
    if old and os.path.basename(old) != os.path.basename(src):
        os.makedirs(os.path.join(d, 'Superseded'), exist_ok=True)
        shutil.move(old, os.path.join(d, 'Superseded', os.path.basename(old)))
    dest = os.path.join(d, os.path.basename(src))
    if os.path.abspath(src) != os.path.abspath(dest):
        shutil.copy2(src, dest)
    return dest


def _bind(key, proj_val, owner):
    """Give a widget a fixed key= that follows the PROJECT's value.

    A widget without a key, drawn with value=/index= taken from the project,
    changes identity the run after it is used (its default changed), so the
    NEXT click on it is dropped: the first pick of a final shoot worked and a
    second did not (2026-09-24).  So: a fixed key, re-seeded from the project
    only when the project's value changed by another route (another project
    opened, a shoot added) -- never over what the tech just picked.  The
    caller applies a pick to the project and calls _bound() with it."""
    ss = st.session_state
    tag = (owner, proj_val)
    if key not in ss or ss.get(key + '__src') != tag:
        ss[key] = proj_val
        ss[key + '__src'] = tag


def _bound(key, new_val, owner):
    st.session_state[key + '__src'] = (owner, new_val)


def _render_needs(items, sec):
    rows = [i for i in items if i['section'] == sec]
    need = [i for i in rows if not i['ok']]
    have = [i for i in rows if i['ok']]
    if need:
        st.markdown('**Still Needed**\n' + '\n'.join(
            f"- {i['item']}" + (f" · {i['detail']}" if i['detail'] else '') for i in need))
    else:
        st.success('Complete.')
    if have:
        with st.expander(f'In Hand ({len(have)})'):
            st.markdown('\n'.join(
                f"- ✓ {i['item']}" + (f" · {i['detail']}" if i['detail'] else '')
                + (f" · _{i['source']}_" if i['source'] else '') for i in have))


def _phone_test_time(pkg, name=''):
    """When the phone sent the test: capture.json's `created` (UTC ISO),
    shown in this PC's local time; blank when the package has none."""
    import datetime as _dt
    raw = str((pkg or {}).get('created') or '')
    try:
        t = _dt.datetime.fromisoformat(raw.replace('Z', '+00:00'))
        if t.tzinfo is not None:
            t = t.astimezone()
        return _when_text(t.timestamp())
    except ValueError:
        return ''


def _render_phone_job(prod, job_id, work, kp='ps'):
    """The job link: emailed to the tech (or scanned as a QR code), it opens
    Field Capture knowing what to collect.  Drawn on the Audit FQA, Pictures
    and GPS tabs (Robert, 2026-09-27); `kp` keeps each copy's widgets apart.
    The Audit FQA copy keeps the original keys."""
    ss = st.session_state
    box = FIELD_CAPTURE_URL_KEY + '_box' + ('' if kp == 'ps' else '_' + kp)
    with st.expander('📱 Field Capture: Send the Tech the Job Link', expanded=False):
        if box not in ss:
            ss[box] = (_settings_read().get(FIELD_CAPTURE_URL_KEY)
                                                  or FIELD_CAPTURE_DEFAULT_URL)
        url = _clean_path(st.text_input(
            'Field Capture Web Address', key=box,
            placeholder='https://… (where Field Capture is hosted)',
            help='Set once; every project uses it.'))
        st.caption('The phone link needs this https address. The Field Capture page '
                   'this app serves itself only opens on this PC.')
        if url != (_settings_read().get(FIELD_CAPTURE_URL_KEY) or ''):
            _settings_update(**{FIELD_CAPTURE_URL_KEY: url})
        # Robert, 2026-09-24: "a button that will allow us to Test Phone
        # Connection ... take one picture and ... one GPS coordinates. When we
        # do both then it will go green on the phone and will allow us to
        # submit".  Submit is the real route back: the phone emails a small
        # test package, which lands in Field/ like any other.
        if st.button('📱 Test Phone Connection', key=f'{kp}_phone_test', disabled=not url,
                     help='Emails the tech a link that asks for one photo and one GPS '
                          'fix, then sends a test back.'):
            import uuid
            tid = 'test-' + uuid.uuid4().hex[:6]
            tlink = job_link(url, {'v': JOB_VERSION, 'id': tid, 'test': True,
                                   'span': os.path.basename(work), 'info': {}, 'splices': []})
            try:
                eml = write_job_email(work, 'Field Capture phone test',
                                      'Tap the link on your iPhone. Take one photo and one GPS '
                                      f'fix, then send the test back to the office:\n\n{tlink}\n')
                from fieldcapture.email_draft import open_with_default_app
                opened, err = open_with_default_app(eml)
                st.success('A test email is open in your mail program: add the tech\'s '
                           'address and send it.' if opened else f'Wrote {eml} ({err}).')
            except Exception as exc:
                report_error('project: phone test email', exc, {})
                st.error(f'Could not write the test email: {exc}')
        for n, p in collect_capture_packages(work_sub('field', work)):
            if p.get('test'):
                g = p.get('gps') or {}
                st.success(f"Phone Test Received ✓ {_phone_test_time(p, n)} ({n}): photo and GPS "
                           f"{g.get('lat', 0):.5f}, {g.get('lon', 0):.5f}"
                           + (f" · {p.get('initials')}" if p.get('initials') else ''))
                break
        if not prod:
            st.caption('The job link needs the production sheet: add it above.')
            return
        try:
            manifest = job_manifest(prod, job_id, os.path.basename(work), ss.get('fqa_job'))
        except Exception as exc:
            report_error('project: job manifest build', exc, {})
            st.error(f'Could not build the job from the production sheet: {exc}')
            return
        info = manifest['info']
        st.caption(f"Job **{job_id}** · A: {info.get('aliasA') or '?'} · Z: "
                   f"{info.get('aliasZ') or '?'} · {len(manifest['splices'])} splice points. "
                   'The phone asks for two photos at each box (the rack label, the box '
                   'with its panel labels and RMU tags), checks the labels against '
                   'this job, and a GPS fix at every splice point.')
        if not url:
            st.info('Enter the web address Field Capture is hosted at, then the QR '
                    'code appears here.')
            return
        link = job_link(url, manifest)
        # The link is the job (Robert: "is it easier instead of qr code to
        # email a link?" -- yes: no size limit, nothing to install).  The QR
        # code is only a picture of the same link, for a tech at this desk.
        subject = f'Field Capture job {job_id}: {os.path.basename(work)}'
        body = ('Tap the link on your iPhone to open Field Capture with this job '
                f'loaded:\n\n{link}\n')
        # An unsent .eml, not a mailto: link -- a long span's job link runs past
        # the ~2,000 characters a mailto: survives on Windows.  Outlook opens a
        # draft marked X-Unsent as a new message, ready to address and send.
        if st.button('✉️ Email the Link to the Tech', key=f'{kp}_job_email', type='primary'):
            try:
                eml = write_job_email(work, subject, body)
                from fieldcapture.email_draft import open_with_default_app
                opened, err = open_with_default_app(eml)
                if opened:
                    st.success('An email with the link is open in your mail program. '
                               'Add the tech\'s address and send it.')
                else:
                    st.warning(f'Wrote {eml} but no mail program opened it ({err}).')
            except Exception as exc:
                report_error('project: job email', exc, {})
                st.error(f'Could not write the email: {exc}')
        st.code(link, language=None)
        with st.expander('QR Code (for a Tech at This Computer)'):
            png, why = job_qr_png(link)
            if png:
                st.image(png, width=260)
            else:
                st.caption(why)


# ── Audit Project: one open item at a time ───────────────────────────────
# Robert, 2026-09-24: "an Audit Project function [that] will hand hold a tech
# going through the project to prepare for FQA.  It goes section by section
# on any that aren't completed, prompts them in a simple screen, and they
# can either take action or skip."  The list is exactly the status page's
# (compute_project_items); a simple fix happens on the audit screen itself
# (a job-detail field, a file, a tick), a bigger one is one button away.
def compute_project_items(work):
    """The status page's items for the open project, gathered the same way."""
    ss = st.session_state
    snap = _project_snapshot(ss, ss.get('project_saved'))
    s1 = (snap.get('spans') or [{}])[0]
    prod = project_production_sheet(work)
    fqa, caps = collect_field_files(work_sub('field', work), work_sub('fqa', work))
    jd = job_details_workbook(ss.get('fqa_job'))
    if jd is not None:
        fqa = fqa + [(JOB_DETAILS_SOURCE, jd)]
    job_id = project_job_id()
    pkgs = [(n, p) for n, p in collect_capture_packages(work_sub('field', work))
            if p.get('job') == job_id and not p.get('test')]
    n_splices = None
    if prod:
        try:
            n_splices = len(_read_prod(prod).splices)
        except Exception as exc:
            n_splices = None
            prod_err = (f"Couldn't read the production sheet {os.path.basename(prod)}: "
                        f"{type(exc).__name__}: {exc}")
            st.warning(prod_err)
    trace_dirs, final_desc = None, None
    if s1.get('mode') == 'one' and s1.get('folder'):
        cached = (ss.get('sr_intake') or {}).get(f"dir:{os.path.abspath(s1['folder'])}")
        if cached and os.path.isdir(cached[0]) and os.path.isdir(cached[1]):
            trace_dirs = (cached[0], cached[1])
    fs = final_shoot(work)
    if fs:
        trace_dirs = (fs['a'], fs['b'])
        d, lab = shoot_info(fs)
        n_sh = len(list_shoots(work))
        final_desc = (f"shot {d or '(no date)'}{' · ' + lab if lab else ''}"
                      + (f' · 1 of {n_sh} shoots' if n_sh > 1 else ''))
    return project_status(snap, fqa, caps, trace_dirs, work, ss.get('project_manual') or {},
                          pkgs=pkgs, n_splices=n_splices, final_desc=final_desc,
                          gps_manual=ss.get('project_gps') or {})


def audit_key(item):
    return f"{item['section']}|{item['item']}"


# The job-detail fields the audit can fill in on its own screen, per item:
# (path in the job form, input kind, label).
AUDIT_FIELDS = {
    '1.01': [('site_a.address', 'text', 'A End Street Address')],
    '1.02': [('site_z.address', 'text', 'Z End Street Address')],
    '1.03': [('site_a.alias', 'text', 'A End Alias')],
    '1.04': [('site_z.alias', 'text', 'Z End Alias')],
    '1.05': [('site_a.clli', 'text', 'A End CLLI')],
    '1.06': [('site_z.clli', 'text', 'Z End CLLI')],
    '1.07': [('site_a.panel_port_count', 'int', 'A End Panel Port Count')],
    '1.08': [('site_z.panel_port_count', 'int', 'Z End Panel Port Count')],
    '1.09': [('fiber_count', 'int', 'Number of Fibers Tested')],
    '1.10': [('revision', 'text', 'Test Revision')],
    '1.11': [('package_type', 'text', 'Package Type')],
    '1.12': [('contractor', 'text', 'Splicing Contractor'),
             ('prepared_by', 'text', 'Package Preparer')],
    '1.13': [('project', 'text', 'NetBuild or Project ID')],
    '1.14': [('tester_1', 'text', 'Tester Name and Phone Number')],
    '1.15': [('calibration_date', 'date', 'Date of Most Recent Calibration')],
}
for _end, _x in (('A', 'site_a'), ('Z', 'site_z')):
    AUDIT_FIELDS[f'{_end} end rack location'] = [
        (f'{_x}.floor', 'text', 'Floor'), (f'{_x}.room', 'text', 'Room'),
        (f'{_x}.aisle', 'text', 'Aisle'), (f'{_x}.bay', 'text', 'Bay')]
    AUDIT_FIELDS[f'{_end} end panel details'] = [
        (f'{_x}.rmu', 'text', 'RMU (Shelf)'), (f'{_x}.connector_type', 'text', 'Connector'),
        (f'{_x}.panel_type', 'text', 'Panel Type')]


def _audit_fields_for(item):
    name = item['item']
    no = name.split()[0]
    if no in AUDIT_FIELDS:
        return AUDIT_FIELDS[no]
    for k, v in AUDIT_FIELDS.items():
        if name.startswith(k):
            return v
    return None


def _job_get(job, path):
    cur = job
    for part in path.split('.'):
        cur = (cur or {}).get(part) if isinstance(cur, dict) else None
    return cur


def _job_set(job, path, value):
    parts = path.split('.')
    cur = job
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def _audit_help(item):
    """What to do about an item, in plain words."""
    n = item['item']
    if n.startswith(('1.', 'A end rack', 'Z end rack', 'A end panel', 'Z end panel')):
        return 'Fill it in here. It goes into the job details, and on to the FQA package.'
    if 'photos' in n or 'labels match' in n:
        return ('This comes from the phone. Email the tech the job link, then save what '
                'they send back here.')
    if 'GPS' in n:
        return ('The splice point GPS comes from the phone (email the tech the job link), '
                'or type it on the project\'s GPS tab. '
                'A location typed wrong in the production sheet is fixed there, then the '
                'package is built again.')
    if n.startswith(('2.', '3.')):
        return 'This is built into the FQA package from the production sheet and the traces.'
    if n.startswith('4.01'):
        return 'Add the traces (or pick the final shoot) on the project\'s Traces tab.'
    if n.startswith('4.02'):
        return 'Add the power meter files here.'
    if n.startswith('4.03'):
        return 'Check the trace file names against the naming convention, then confirm here.'
    if n.startswith(('4.04', '4.05')):
        return 'Add the splice logs and exception documents here, or mark them not needed.'
    return ''


def _audit_actions(work):
    """The audit's clicks, applied BEFORE the screen is drawn: no st.rerun,
    which would drop the widgets of the item just left (the rerun trap)."""
    ss = st.session_state
    items = compute_project_items(work)
    skipped = ss.setdefault('audit_skipped', [])
    todo = [i for i in items if not i['ok'] and audit_key(i) not in skipped]
    cur = todo[0] if todo else None
    if ss.get('aud_again'):
        ss['audit_skipped'] = []
    if ss.get('aud_finish'):
        ss['audit_on'] = False
        ss['audit_skipped'] = []
    if cur is None:
        return
    k = audit_key(cur)
    if ss.get('aud_skip'):
        ss['audit_skipped'] = skipped + [k]
    if ss.get('aud_save'):
        fields = _audit_fields_for(cur) or []
        job = json.loads(json.dumps(ss.get('fqa_job') or {}, default=str))
        for path, _kind, _label in fields:
            v = ss.get('aud_' + path)
            if hasattr(v, 'isoformat'):
                v = v.isoformat()
            elif isinstance(v, str):
                v = v.strip() or None
            elif isinstance(v, (int, float)):
                v = int(v) or None
            _job_set(job, path, v)
        ss['fqa_job'] = job
    if ss.get('aud_403'):
        m = dict(ss.get('project_manual') or {})
        m['4.03'] = True
        ss['project_manual'] = m
    if ss.get('aud_na'):
        m = dict(ss.get('project_manual') or {})
        m[cur['item'].split()[0]] = 'na'
        ss['project_manual'] = m


def _render_audit(work):
    ss = st.session_state
    items = compute_project_items(work)
    open_items = [i for i in items if not i['ok']]
    skipped = ss.setdefault('audit_skipped', [])
    todo = [i for i in open_items if audit_key(i) not in skipped]
    st.markdown(f'#### 🧭 Audit Project · {os.path.basename(work)}')
    done = len(items) - len(open_items)
    st.progress(done / max(1, len(items)),
                text=f'{done} of {len(items)} in hand · {len(todo)} to go'
                     + (f' · {len(skipped)} skipped' if skipped else ''))
    if not todo:
        with st.container(border=True):
            if not open_items:
                st.success('Everything the FQA form asks for is in hand. Ready for FQA.')
            else:
                st.info(f'You went through every open item. {len(skipped)} skipped:')
                st.markdown('\n'.join(f'- {i["item"]}' + (f' · {i["detail"]}' if i['detail'] else '')
                                      for i in open_items if audit_key(i) in skipped))
            c1, c2 = st.columns(2)
            if skipped:
                c1.button('Go Through the Skipped Ones Again', key='aud_again')
            c2.button('Finish', key='aud_finish', type='primary')
        return

    item = todo[0]
    k = audit_key(item)
    with st.container(border=True):
        st.caption(f"Section {item['section']} · {SECTION_TITLES[item['section']]} · "
                   f"item {open_items.index(item) + 1} of {len(open_items)} open")
        st.markdown(f"### {item['item']}")
        if item['detail']:
            st.markdown(f"**Now:** {item['detail']}")
        st.caption(_audit_help(item))
        fields = _audit_fields_for(item)
        n = item['item']
        if fields:
            job = json.loads(json.dumps(ss.get('fqa_job') or {}, default=str))
            vals = {}
            cols = st.columns(len(fields)) if len(fields) > 1 else [st.container()]
            for col, (path, kind, label) in zip(cols, fields):
                key = 'aud_' + path
                cur = _job_get(job, path)
                if kind == 'date':
                    import datetime as _dt
                    try:
                        cur = _dt.date.fromisoformat(cur) if isinstance(cur, str) and cur else None
                    except ValueError:
                        cur = None
                    _bind(key, cur, (work, k))
                    vals[path] = col.date_input(label, key=key)
                elif kind == 'int':
                    _bind(key, int(cur) if str(cur or '').isdigit() else 0, (work, k))
                    vals[path] = col.number_input(label, min_value=0, step=1, key=key)
                else:
                    _bind(key, str(cur or ''), (work, k))
                    vals[path] = col.text_input(label, key=key)
            st.button('Save and Continue', key='aud_save', type='primary')
        elif 'photos' in n or 'labels match' in n or 'GPS' in n:
            if _drop_into('Save What the Phone Sent (Capture Package, Workbook)',
                          'aud_drop_field', 'field', ['zip', 'xlsm', 'xlsx']):
                st.success('Saved. The audit moves on once it counts.')
            st.button('📱 Email the Tech the Job Link (on the Status Page)', key='aud_go_phone')
        elif n.startswith(('2.', '3.')):
            st.button('🧮 Open the FQA Builder', key='aud_go_fqa', type='primary')
        elif n.startswith('4.01'):
            st.button('📂 Go to the Traces Tab', key='aud_go_status', type='primary')
        elif n.startswith('4.02'):
            if _drop_into('Power Meter Files', 'aud_drop_pm', 'power', None):
                st.success('Saved.')
        elif n.startswith('4.03'):
            st.button('The File Names Follow the Convention', key='aud_403', type='primary')
        elif n.startswith(('4.04', '4.05')):
            if _drop_into('Splice Logs and Exception Documents', 'aud_drop_logs',
                          'splice_logs', None):
                st.success('Saved.')
            st.button('Not Needed on This Job', key='aud_na')
        c1, c2, c3 = st.columns(3)
        c1.button('Skip for Now ⏭', key='aud_skip', use_container_width=True)
        c3.button('Exit Audit', key='aud_exit', use_container_width=True)


# ── Project events: everything that happened in the project ──────────────
# Robert, 2026-09-26: "Events tab will show every action that has happened in
# the project, so any time new traces are uploaded the date of those traces
# and description would go into Events.  Every time a report is ran ... any
# time we get photos or GPS from field capture".  The log is a file in the
# work folder, so it travels with the project (and its .zdb).  What OTDR
# Suite does itself is logged as it happens; anything else that appears in
# the folder (a .zfc saved from an email, a photo dragged in, a report the
# tool wrote) is picked up by project_scan the next time the project screen
# draws, dated by the file.
PROJECT_EVENTS_FILE = 'Project Events.json'
EVENT_KINDS = ('Project', 'Traces', 'Report', 'Field Capture', 'Photo', 'GPS',
               'Production Sheet', 'FQA', 'File')
# Where a report tool's output is recognised by its file name.
_REPORT_KINDS = (('splicereport', 'Splice Report'), ('unidirectional', 'Unidirectional'),
                 ('secretsauce', 'Secret Sauce'), ('secret sauce', 'Secret Sauce'))
PICTURE_ENDS = {'A': 'A end', 'Z': 'Z end', 'other': 'Other'}
_PICTURE_EXTS = ('.jpg', '.jpeg', '.png', '.heic', '.webp')


# ── The owner's activity emails ──────────────────────────────────────────
# Robert, 2026-09-27: the Project Owner "would get an email any time activity
# happens in the project, regardless of who does it" -- every event, right
# away, from a company sender mailbox.  The mailbox's login is baked into the
# build from a CI secret (_mail_sender.cfg, JSON: host, port, user, password,
# from), like the Slack error webhook, and never committed: the repository is
# public.  OTDR_MAIL_SENDER (the same JSON) stands in for it in a dev run.
# With neither, nothing is sent.
MAIL_SENDER_FILE = '_mail_sender.cfg'


def _mail_sender():
    raw = os.environ.get('OTDR_MAIL_SENDER')
    if not raw:
        base = (getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
                if getattr(sys, 'frozen', False) else HERE)
        try:
            with open(os.path.join(base, MAIL_SENDER_FILE), encoding='utf-8') as fh:
                raw = fh.read()
        except OSError:
            return None
    try:
        cfg = json.loads(raw)
    except ValueError:
        return None
    return cfg if isinstance(cfg, dict) and cfg.get('host') and cfg.get('from') else None


def owner_mail_ready():
    return _mail_sender() is not None


def _project_owner_of(work):
    """The owner: this session's (just set, maybe not saved yet) when `work`
    is the open project, else the project file's."""
    ss = st.session_state
    try:
        if ss.get('project_path') and os.path.normcase(work_dir()) == os.path.normcase(
                os.path.abspath(work)) and ss.get('project_owner'):
            return dict(ss['project_owner'])
    except Exception:
        pass
    try:
        path, have = project_file_for_folder(work)
        if have:
            with open(path, encoding='utf-8') as fh:
                return dict(json.load(fh).get('owner') or {})
    except (OSError, ValueError):
        pass
    return {}


def owner_email_message(work, ev, owner, sender):
    """One event as an email to the owner (an email.message.EmailMessage)."""
    from email.message import EmailMessage
    import getpass
    import platform
    name = os.path.basename(os.path.abspath(work))
    try:
        who = f"{ev.get('user') or getpass.getuser()} on {platform.node()}"
    except Exception:
        who = platform.node() or 'another PC'
    msg = EmailMessage()
    msg['From'] = sender['from']
    msg['To'] = owner['email']
    msg['Subject'] = f"{name}: {ev.get('kind')} · {str(ev.get('text') or '')[:80]}"
    msg.set_content(
        f"Hello {owner.get('name') or ''},\n\n"
        f"Something happened in the project {name}.\n\n"
        f"What happened: {ev.get('text')}\n"
        f"When: {_when_text(ev.get('when'))}\n"
        f"Done by: {who} ({_how_shown(ev.get('how'))})\n"
        f"Project folder: {os.path.abspath(work)}\n\n"
        f'You get these because you are the project owner in {PRODUCT_NAME}.\n')
    return msg


def _send_owner_mail(sender, messages):
    import smtplib
    import ssl
    try:
        port = int(sender.get('port') or 587)
        ctx = ssl.create_default_context()
        if port == 465:
            srv = smtplib.SMTP_SSL(sender['host'], port, context=ctx, timeout=20)
        else:
            srv = smtplib.SMTP(sender['host'], port, timeout=20)
            srv.starttls(context=ctx)
        with srv:
            if sender.get('user'):
                srv.login(sender['user'], sender.get('password') or '')
            for m in messages:
                srv.send_message(m)
    except Exception as exc:
        report_error('project: owner email', exc, {'n': len(messages)})


def notify_owner(work, events):
    """Email the project owner each event, in the background.  Nothing when
    no owner is set or no sender mailbox is configured."""
    sender = _mail_sender()
    if not sender or not events:
        return
    owner = _project_owner_of(work)
    if not owner.get('email'):
        return
    try:
        msgs = [owner_email_message(work, ev, owner, sender) for ev in events]
    except Exception as exc:
        report_error('project: owner email build', exc, {})
        return
    import threading
    threading.Thread(target=_send_owner_mail, args=(sender, msgs), daemon=True).start()


def _events_path(work):
    return os.path.join(work, PROJECT_EVENTS_FILE)


def events_read(work):
    """{'events': [...], 'known': {rel path: mtime}}; empty when none yet."""
    try:
        with open(_events_path(work), encoding='utf-8') as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return {'events': [e for e in data.get('events') or [] if isinstance(e, dict)],
                    'known': dict(data.get('known') or {}),
                    'report_traces': dict(data.get('report_traces') or {}),
                    'made_by': dict(data.get('made_by') or {})}
    except (OSError, ValueError):
        pass
    return {'events': [], 'known': {}, 'report_traces': {}, 'made_by': {}}


def _events_write(work, data):
    path = _events_path(work)
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, indent=1)
        os.replace(tmp, path)
    except OSError:
        pass


def _rel(work, path):
    return os.path.relpath(os.path.abspath(path), os.path.abspath(work)).replace(os.sep, '/')


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return time.time()


# ── Who did it ───────────────────────────────────────────────────────────
# Robert, 2026-09-29: "a record of who does what to the files ... a user
# column that shows who took what action".  Every event carries the Windows
# login of whoever did it.  A file found in the folder is put down to the
# login that owns it, or left blank when that can't be known.
def current_user():
    """The Windows login of whoever runs this copy of OTDR Suite."""
    import getpass
    try:
        return getpass.getuser()
    except Exception:
        return ''


_IO_REPARSE_TAG_CLOUD = 0x9000001A


def _file_owner(path):
    """The login that owns `path`, '' when it can't be known: a group owner
    (Administrators), or a OneDrive / SharePoint synced file, which belongs
    to whoever syncs it on this PC, not to who saved it."""
    try:
        st_ = os.lstat(path)
    except OSError:
        return ''
    if os.name != 'nt':
        try:
            import pwd
            return pwd.getpwuid(st_.st_uid).pw_name
        except (ImportError, KeyError):
            return ''
    if (getattr(st_, 'st_reparse_tag', 0) & 0xFFFF0FFF) == _IO_REPARSE_TAG_CLOUD:
        return ''
    try:
        import ctypes
        from ctypes import wintypes
        adv = ctypes.WinDLL('advapi32')
        k32 = ctypes.WinDLL('kernel32')
        pp = ctypes.POINTER(ctypes.c_void_p)
        adv.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
                                              pp, pp, pp, pp, pp]
        adv.GetNamedSecurityInfoW.restype = wintypes.DWORD
        adv.LookupAccountSidW.argtypes = [
            wintypes.LPCWSTR, ctypes.c_void_p, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
            wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.c_int)]
        adv.LookupAccountSidW.restype = wintypes.BOOL
        k32.LocalFree.argtypes = [ctypes.c_void_p]
        sid, sd = ctypes.c_void_p(), ctypes.c_void_p()
        # SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION
        if adv.GetNamedSecurityInfoW(path, 1, 1, ctypes.byref(sid), None, None, None,
                                     ctypes.byref(sd)) != 0:
            return ''
        try:
            name, dom = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256)
            n, d, use = wintypes.DWORD(256), wintypes.DWORD(256), ctypes.c_int()
            if not adv.LookupAccountSidW(None, sid, name, ctypes.byref(n), dom, ctypes.byref(d),
                                         ctypes.byref(use)):
                return ''
            return name.value if use.value == 1 else ''     # SidTypeUser only
        finally:
            k32.LocalFree(sd)
    except Exception:
        return ''


def file_users(work, data=None):
    """{rel path: login} -- who last did something to each file, from the
    project's events."""
    data = data or events_read(work)
    out = {}
    for e in sorted(data['events'], key=lambda e: e.get('when') or 0):
        if e.get('user'):
            for r in e.get('files') or ():
                out[r] = e['user']
    return out


def _how_shown(how, blank='OTDR Suite'):
    """An event's How as a person reads it.  The log stores 'OTDR Suite' for
    what the app did (older logs too); it is shown as the product's name."""
    how = how or blank
    return PRODUCT_NAME if how == 'OTDR Suite' else how


def project_log(work, kind, text, paths=(), when=None, how='OTDR Suite'):
    """Add one event; `paths` are the files it made, so the folder scan does
    not log them a second time."""
    if not work:
        return
    data = events_read(work)
    rels = [_rel(work, p) for p in paths or ()]
    data['events'].append({'when': when or time.time(), 'kind': kind, 'text': text,
                           'how': how, 'user': current_user(), 'files': rels})
    for p, r in zip(paths or (), rels):
        data['known'][r] = _mtime(p)
    _events_write(work, data)
    notify_owner(work, [data['events'][-1]])


def _report_kind(name):
    low = name.lower()
    return next((k for pat, k in _REPORT_KINDS if pat in low), None)


def _describe_file(work, rel):
    """(kind, text) for a file (or trace shoot, or Secret Sauce run folder)
    found in the work folder."""
    top, _, rest = rel.partition('/')
    name = os.path.basename(rel)
    path = os.path.join(work, *rel.split('/'))
    if top == PROJECT_DIRS['traces']:
        sh = next((x for x in list_shoots(work)
                   if (x['id'] or '(first shoot)') == rest), None)
        if sh:
            d, lab = shoot_info(sh)
            fa, fb = len(_trace_fibers(sh['a'])), len(_trace_fibers(sh['b']))
            return 'Traces', (f"Traces added: shot {d or '(no date)'}"
                              f"{' · ' + lab if lab else ''} · A {fa} / B {fb} fibers")
        return 'Traces', f'Traces added: {rest}'
    if top == PROJECT_DIRS['reports']:
        return 'Report', f"{_report_kind(rel) or 'Report'} run: {rest}"
    if top == PROJECT_DIRS['field']:
        if name.lower().endswith(capture_package_exts()):
            pkg = read_capture_package(path)
            if pkg is not None:
                if pkg.get('test'):
                    return 'Field Capture', f'Phone test received: {name}'
                n_ph = sum(len(s.get('photos') or []) for s in pkg.get('sites') or [])
                n_gps = sum(1 for x in pkg.get('splices') or []
                            if (x.get('gps') or {}).get('lat') is not None)
                n_gps += sum(1 for s in pkg.get('sites') or []
                             if (s.get('gps') or {}).get('lat') is not None)
                return 'Field Capture', (f'Field Capture received: {n_ph} photo'
                                         f"{'s' * (n_ph != 1)}, {n_gps} GPS fix"
                                         f"{'es' * (n_gps != 1)} ({name})")
        return 'Field Capture', f'Field file: {name}'
    if top == PROJECT_DIRS['pictures']:
        end = next((k for k, v in PICTURE_ENDS.items() if rest.startswith(v + '/')), None)
        return 'Photo', f"Photo added{' (' + PICTURE_ENDS[end] + ')' if end else ''}: {name}"
    if top == PROJECT_DIRS['production']:
        return 'Production Sheet', f'Production sheet: {rest}'
    if top == PROJECT_DIRS['fqa']:
        return 'FQA', f'FQA package: {name}'
    if top == PROJECT_DIRS['power']:
        return 'File', f'Power meter file: {name}'
    if top == PROJECT_DIRS['splice_logs']:
        return 'File', f'Splice log: {name}'
    return 'File', f'File: {rel}'


def _folder_items(work):
    """{rel: mtime} for everything the scan tracks: one entry per trace
    shoot (not per .sor), one per Secret Sauce run folder, one per file
    elsewhere in the project's subfolders."""
    out = {}
    for sh in list_shoots(work):
        out[f"{PROJECT_DIRS['traces']}/{sh['id'] or '(first shoot)'}"] = _mtime(sh['dir'])
    for key, sub in PROJECT_DIRS.items():
        if key == 'traces':
            continue
        base = os.path.join(work, sub)
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if not d.startswith('.') and d != 'Superseded']
            if key == 'reports':
                runs = [d for d in dirs if _report_kind(d) == 'Secret Sauce']
                for d in runs:
                    out[_rel(work, os.path.join(root, d))] = _mtime(os.path.join(root, d))
                dirs[:] = [d for d in dirs if d not in runs]
            for f in files:
                if f.startswith(('.', '~$')) or f.endswith('.tmp'):
                    continue
                p = os.path.join(root, f)
                out[_rel(work, p)] = _mtime(p)
    return out


def project_scan(work):
    """Log whatever appeared in the work folder since the last look.  The
    first look at a project backfills its history from the files' dates.
    Returns how many events were added."""
    if not work or not os.path.isdir(work):
        return 0
    data = events_read(work)
    first = not os.path.exists(_events_path(work))
    known = data['known']
    added = 0
    for rel, mt in sorted(_folder_items(work).items(), key=lambda kv: kv[1]):
        if rel in known and abs(known[rel] - mt) < 1:
            continue
        kind, text = _describe_file(work, rel)
        top = rel.split('/')[0]
        if rel in known:
            text = (text.replace(' run:', ' run again:') if ' run:' in text
                    else 'Replaced: ' + text)
        how = ('OTDR Suite' if top == PROJECT_DIRS['reports'] or first
               else 'Found in folder')
        # A report run in the App was put down to its runner when it
        # started; this scan may be on someone else's PC.
        user = data['made_by'].pop(rel, None) or _file_owner(
            os.path.join(work, *rel.split('/')))
        data['events'].append({'when': mt, 'kind': kind, 'text': text, 'how': how,
                               'user': user, 'files': [rel]})
        known[rel] = mt
        added += 1
    if added or first:
        _events_write(work, data)
    if added and not first:
        notify_owner(work, data['events'][-added:])
    return added


# ── Pictures: from Field Capture and added by hand ────────────────────────
@st.cache_data(show_spinner=False, max_entries=512)
def _package_photo_bytes(path, name, _mtime_):
    import zipfile
    import folder_intake
    try:
        if path.lower().endswith('.zip'):
            with zipfile.ZipFile(path) as z:
                return z.read(name)
        return folder_intake.share_open(path, expect='field-capture').read(name)
    except Exception:
        return None


def project_photos(work, job_id=None):
    """[{'end', 'name', 'source', 'path', 'member', 'when'}] -- every photo
    the project has: each capture package's (newest first) and the ones in
    Pictures/<end>.  `member` is the name inside a package, None for a
    loose file.  Test packages are left out."""
    out = []
    for n, pkg in collect_capture_packages(work_sub('field', work)):
        if pkg.get('test') or (job_id and pkg.get('job') not in (None, job_id)):
            continue
        p = os.path.join(work_sub('field', work), n)
        for s in pkg.get('sites') or []:
            end = s.get('site') if s.get('site') in ('A', 'Z') else 'other'
            for m in s.get('photos') or []:
                out.append({'end': end, 'name': os.path.basename(m), 'source': n,
                            'path': p, 'member': m, 'when': _mtime(p)})
    base = work_sub('pictures', work)
    for end, folder in PICTURE_ENDS.items():
        d = os.path.join(base, folder)
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            if n.lower().endswith(_PICTURE_EXTS) and not n.startswith('.'):
                p = os.path.join(d, n)
                out.append({'end': end, 'name': n, 'source': 'added by hand', 'path': p,
                            'member': None, 'when': _mtime(p)})
    return out


def photo_bytes(ph):
    if ph['member'] is None:
        try:
            with open(ph['path'], 'rb') as fh:
                return fh.read()
        except OSError:
            return None
    return _package_photo_bytes(ph['path'], ph['member'], ph['when'])


# ── GPS: the phone's fixes and the ones typed in ──────────────────────────
def project_gps_rows(prod, pkgs, gps_manual):
    """One row per GPS point of the span: the A box, every splice in order,
    the Z box.  Each row: key ('A', 1, 2 ..., 'Z'), event, vault, name,
    sheet (what the production sheet holds), phone ('lat, lon' text),
    hand (what was typed), used (the fix the FQA gets, as {'lat','lon'} or
    None) and from ('typed' | 'phone' | 'production sheet' | '')."""
    ppkg = next((p for _n, p in pkgs if p.get('splices')), None)
    phone = {}
    for x in (ppkg or {}).get('splices') or []:
        g = x.get('gps') or {}
        try:
            if g.get('lat') is not None:
                phone[int(x.get('event'))] = g
        except (TypeError, ValueError):
            pass
    for end in ('A', 'Z'):
        for _n, p in pkgs:
            g = _pkg_end_fix(p, end)
            if g:
                phone[end] = g
                break
    hand = gps_fixes_by_hand(gps_manual)
    rows = []

    def row(key, event, vault, name, sheet):
        h = str((gps_manual or {}).get(str(key)) or '')
        ph = phone.get(key)
        sheet_fix = None
        kind, ll = parse_event_location(sheet)
        if kind == 'gps':
            sheet_fix = {'lat': ll[0], 'lon': ll[1]}
        used, frm = ((hand[key], 'typed') if key in hand else
                     (ph, 'phone') if ph else
                     (sheet_fix, 'production sheet') if sheet_fix else (None, ''))
        rows.append({'key': key, 'event': event, 'vault': vault, 'name': name,
                     'sheet': sheet or '', 'hand': h,
                     'phone': f"{ph['lat']:.6f}, {ph['lon']:.6f}" if ph else '',
                     'used': used, 'from': frm})

    locs = prod.locations if prod else []
    from fqa.production_sheet import TERMINATION
    ends = [l for l in locs if l.kind == TERMINATION]
    row('A', 'Site A', '', (ends[0].name if ends else '') or '', '')
    for i, l in enumerate(prod.splices if prod else [], 1):
        row(i, str(i), '' if l.vault_id is None else str(l.vault_id), l.name or '',
            l.address or '')
    row('Z', 'Site Z', '', (ends[-1].name if len(ends) > 1 else '') or '', '')
    return rows


def fqa_location_overrides(prod, gps_rows):
    """{production-sheet tab: Event Location text} for the FQA build: every
    splice with a typed or phone fix gets it, as the form's DMS text.  The
    production sheet's own text is left to the builder."""
    by_key = {r['key']: r for r in gps_rows}
    out = {}
    for i, l in enumerate(prod.splices, 1):
        r = by_key.get(i)
        if r and r['used'] and r['from'] in ('typed', 'phone'):
            out[l.sheet] = dms_text(r['used']['lat'], r['used']['lon'])
    return out


# The final traces' closures, as the Splice Report engine found them, kept in
# the work folder so the page never has to run the engine to draw.  Hidden
# (a dot file): it is not a project file for the Events log or an export.
TRACE_CLOSURES_FILE = '.trace_closures.json'


def _closures_key(fs):
    return [os.path.abspath(fs['a']), os.path.abspath(fs['b']),
            round(_mtime(fs['a'])), round(_mtime(fs['b']))]


def _stored_closures(work, fs):
    """The engine's manifest for the final shoot without running it: the
    Splice Report page's saved grid for the same folders, else the one
    stored in the work folder.  None when neither is there."""
    try:
        with open(_hub_cache_path('.sr_grid_cache.json', fs['a']), encoding='utf-8') as fh:
            cached = json.load(fh)
        m = cached.get('manifest') or {}
        if (m.get('ok') and m.get('analysis_mode', 'suite') != 'fr'
                and list(cached.get('_dirs') or []) == [fs['a'], fs['b']]):
            return m
    except Exception:
        pass
    try:
        with open(os.path.join(work, TRACE_CLOSURES_FILE), encoding='utf-8') as fh:
            data = json.load(fh)
        if data.get('key') == _closures_key(fs) and isinstance(data.get('manifest'), dict):
            return data['manifest']
    except (OSError, ValueError):
        pass
    return None


def project_trace_distances(work, prod, run=False):
    """What the final traces say about each splice's distance from A, for
    the FQA build: fqa.event_chain.splice_distances' result, plus 'read':
    False when the closures have not been read yet (run=True reads them,
    which takes as long as a Splice Report)."""
    none = {'distances_m': None, 'span_length_m': None, 'method': 'none', 'read': False}
    fs = final_shoot(work)
    if not fs or not (_trace_fibers(fs['a']) and _trace_fibers(fs['b'])):
        return dict(none, warnings=['no final traces with both directions'])
    manifest = _stored_closures(work, fs)
    if manifest is None and not run:
        return dict(none, warnings=['not read from the final traces yet'])
    if manifest is None:
        manifest = _fqa_sr_manifest(fs['a'], fs['b'])
        if manifest.get('ok'):
            try:
                with open(os.path.join(work, TRACE_CLOSURES_FILE), 'w', encoding='utf-8') as fh:
                    json.dump({'key': _closures_key(fs), 'manifest': manifest}, fh)
            except OSError:
                pass
    try:
        from fqa.event_chain import splice_distances
        out = splice_distances(fs['a'], fs['b'], prod, manifest=manifest)
    except Exception as exc:
        report_error('project: trace distances', exc, {})
        return dict(none, warnings=[f"couldn't match the closures: {type(exc).__name__}: {exc}"])
    out['read'] = True
    return out


def fqa_package_name(work):
    return f'{os.path.basename(work)} - FQA SITE SURVEY {time.strftime("%Y-%m-%d %H%M")}.xlsm'


def build_project_fqa(work, prod_path, job, gps_rows, photos, trace):
    """Build the customer's FQA package into FQA/ from everything the project has.
    Returns the builder's manifest (with 'out')."""
    from fqa.run_fqa import build
    prod = _read_prod(prod_path)
    kw = {}
    if trace.get('distances_m'):
        kw['closures'] = list(trace['distances_m'])
        if trace.get('span_length_m'):
            kw['span_length_m'] = float(trace['span_length_m'])
    locs = fqa_location_overrides(prod, gps_rows)
    if locs:
        kw['locations'] = locs
    from fqa.xlsx_patch import image_info
    pics, skipped = [], []
    for ph in photos:
        if ph['end'] in ('A', 'Z'):
            data = photo_bytes(ph)
            try:
                image_info(data or b'')
            except Exception:
                skipped.append(ph['name'])      # not a JPEG/PNG (a HEIC, a bad file)
                continue
            pics.append({'end': ph['end'], 'data': data, 'caption': ph['name']})
    if pics:
        kw['photos'] = pics
    out_dir = work_sub('fqa', work)
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, fqa_package_name(work))
    import inspect
    accepted = inspect.signature(build).parameters
    dropped = [k for k in kw if k not in accepted]
    manifest = build(prod_path, out, job_data=job,
                     **{k: v for k, v in kw.items() if k in accepted})
    manifest['not_in_this_build'] = dropped
    if skipped:
        manifest.setdefault('warnings', []).insert(
            0, 'photos left out (not a JPEG or PNG): ' + ', '.join(skipped))
    return manifest


# ─── PAGE: the project screen ─────────────────────────────────────────────
# Robert, 2026-09-26: after the setup screen, "our project screen.  At the
# top we need to see the pertinent overview details.  Then we need tabs laid
# out horizontally: Events, Traces, Reports, Pictures, GPS, Audit FQA."  The
# overview is the span and customer, the FQA progress, the final traces and
# the last thing that happened.  Audit FQA is the Submittal Checklist page
# that used to be the whole screen, plus building the customer's FQA workbook.
PROJECT_TAB_CSS = (
    '<style>'
    '[data-testid="stTabs"] [data-baseweb="tab-list"]{gap:.4rem;flex-wrap:wrap}'
    '[data-testid="stTabs"] button[role="tab"]{font-size:1.15rem;padding:.6rem 1rem;'
    'min-height:3rem;background:var(--otdr-panel);border:1px solid var(--otdr-edge-2);border-radius:.6rem;'
    'color:var(--otdr-text)}'
    '[data-testid="stTabs"] button[role="tab"] p{font-size:1.15rem}'
    '[data-testid="stTabs"] button[role="tab"]:hover{background:var(--otdr-hover);border-color:var(--otdr-accent)}'
    '[data-testid="stTabs"] button[role="tab"][aria-selected="true"]{background:var(--otdr-accent);'
    'border-color:var(--otdr-accent);color:var(--otdr-on-accent)}'
    '[data-testid="stTabs"] button[role="tab"][aria-selected="true"] p{color:#fff}'
    '[data-testid="stTabs"] [data-baseweb="tab-highlight"],'
    '[data-testid="stTabs"] [data-baseweb="tab-border"]{display:none}'
    '</style>')
PROJECT_TABS = ['Events', 'Traces', 'Reports', 'Pictures', 'GPS', 'Audit FQA', 'Export Project']


def page_project_status():
    ss = st.session_state
    path = ss.get('project_path')
    if not path:
        st.info('Open a project from the Home screen first.')
        return
    work = work_dir(path)
    if ss.get('aud_exit'):
        ss['audit_on'] = False
    if ss.get('audit_on'):
        _audit_actions(work)
    if ss.get('audit_on'):
        _render_audit(work)
        return
    if ss.get('ps_fqa_read_dist'):
        prod_path = project_production_sheet(work)
        if prod_path:
            with st.spinner('Reading the closures from the final traces (as long as a '
                            'Splice Report takes)…'):
                t = project_trace_distances(work, _read_prod(prod_path), run=True)
            project_log(work, 'Traces', 'Closure distances read from the final traces: '
                        + (f"{len(t['distances_m'])} splices matched" if t.get('distances_m')
                           else '; '.join(t.get('warnings') or ['no match'])))
    if ss.get('ps_fqa_build'):
        _build_fqa_now(work)
    try:
        project_scan(work)
    except Exception as exc:
        report_error('project: folder scan', exc, {})

    overview = st.container()
    # Robert, 2026-09-27: the tabs as big as the old Export project button,
    # each shaded and outlined so it reads as its own tab; the open one in
    # the app's blue.
    st.markdown(PROJECT_TAB_CSS, unsafe_allow_html=True)
    tabs = dict(zip(PROJECT_TABS, st.tabs(PROJECT_TABS)))
    # The tabs that take files in draw before the checklist is read, so the
    # overview and the checklist already count what was just added.
    with tabs['Traces']:
        _project_tab_traces(work)
    with tabs['Pictures']:
        _project_tab_pictures(work)
    with tabs['GPS']:
        _project_tab_gps(work)
    with tabs['Audit FQA']:
        items = _project_tab_audit(work)
    with tabs['Reports']:
        _project_tab_reports(work)
    with tabs['Events']:
        _project_tab_events(work)
    with tabs['Export Project']:
        st.markdown('**Export Project**')
        st.caption('Pack the project folder into one .zdb file to share or email. It opens '
                   f'in {PRODUCT_NAME}: Home, Open Recent Project, Open this file.')
        _render_export(work)
        with st.container(border=True):
            st.markdown('**☁️ Save to SharePoint**')
            _render_sp_save(work)
    with overview:
        _project_overview(work, items)


DEMO_TOUR_JS = r'''// Run Demo: a 90-second guided tour of the project screen, shown on the
// Sample Span (Robert, 2026-09-27: "an automated click through with small
// windows that open and display all of the features to the viewer").
// It is put into the hub's page itself (not the component frame, which a
// Streamlit rerun would remove mid-tour) and only
// switches tabs, opens menus and points at things: it never presses a button
// that changes the project, so the sample span is the same afterwards.
(function () {
  const P = window;
  const doc = document;
  const TOTAL_MS = 90000;
  // [tab, what to point at, title, words, seconds]; seconds add up to 90.
  const STEPS = [
    [null, 'h2', 'Welcome to the project screen',
     'Everything for one span lives here. The strip across the top shows the span, how much of the FQA package is in hand, the final traces and the last thing that happened.', 8],
    [null, 'popover:Work folder', 'The work folder',
     'The project is one folder. Pick any file or set of traces from this list to open it or show it in its folder.', 7],
    ['Events', 'table', 'Events',
     'Every action is logged: traces added, reports run, photos and GPS from Field Capture, the FQA package built. Files saved into the folder by hand are found too.', 8],
    ['Traces', 'text:The FQA checklist, the Field Capture job', 'Final traces',
     'Pick which shoot the FQA package, the checklist and the Field Capture job use.', 7],
    ['Traces', 'popover:Run In', 'Run a set of traces',
     'Every shoot has its own Run In button: open it in the Viewer, the Splice Report, Unidirectional or Secret Sauce. Labels are typed straight into the row.', 8],
    ['Reports', 'text:Every Splice Report', 'Reports',
     'Every report run in the project is kept here with the traces it was run on. Open it, show its folder, show its traces, or export a copy.', 7],
    ['Pictures', 'img', 'Pictures',
     'Photos from Field Capture, sorted by end. The A and Z end photos go on the FQA package\'s Pictures tab.', 7],
    ['Pictures', 'expander:Field Capture: Send', 'Send the tech a job',
     'Email the tech a link. Their phone opens Field Capture knowing which labels, photos and GPS points this span needs.', 8],
    ['GPS', 'grid', 'GPS',
     'One row per splice. Field Capture fills in its column; type a fix by hand where it is missing or wrong. Here Field Capture missed two points: one was typed by hand, the other falls back to the production sheet.', 9],
    ['Audit FQA', 'text:Build the Lumen FQA Package', 'Build the FQA package',
     'Builds Lumen\'s FQA workbook from the production sheet, the traces, the GPS and the photos, straight into the job.', 8],
    ['Audit FQA', 'text:Site Survey Data', 'The Submittal Checklist',
     'Every item Lumen checks, read from the files in the job. Start the Audit walks you through whatever is still missing, one at a time.', 7],
    ['Export Project', 'text:Pack the project folder', 'Share the project',
     'Pack the whole job into one .zdb file to email or put on SharePoint. It opens the same way on any PC with OTDR Suite.', 6],
  ];

  if (P.__otdrTour && P.__otdrTour.running) return;       // one tour at a time
  const tour = P.__otdrTour = { running: true };

  const visible = (el) => el && el.offsetParent !== null && el.getClientRects().length;
  // The page, not the sidebar (its "OTDR Suite" heading is an h2 too).
  const main = () => doc.querySelector('[data-testid="stMain"]') ||
    doc.querySelector('section.main') || doc;
  const all = (sel) => Array.from(main().querySelectorAll(sel)).filter(visible);
  const byText = (sel, t) => all(sel).find((e) => (e.innerText || '').trim().startsWith(t));

  function clickTab(name) {
    const tab = Array.from(doc.querySelectorAll('button[role="tab"]'))
      .find((b) => b.innerText.trim() === name);
    if (tab && tab.getAttribute('aria-selected') !== 'true') tab.click();
  }
  // A menu the tour opened is closed by pressing its button again.
  let opened = null;
  function closeMenus() {
    if (opened && visible(opened)) opened.click();
    opened = null;
  }
  function target(spec) {
    const [kind, arg] = spec.includes(':') ? spec.split(/:(.*)/s) : [spec, ''];
    if (kind === 'h2') return all('h2')[0];
    if (kind === 'table') return all('[data-testid="stDataFrame"]')[0];
    if (kind === 'grid') return all('[data-testid="stDataFrame"]')[0];
    if (kind === 'img') return all('[data-testid="stImage"], [data-testid="stImageContainer"], img')[0];
    if (kind === 'popover') {
      const b = byText('button', arg) || all('button').find((x) => x.innerText.includes(arg));
      if (b) { b.click(); opened = b; }
      return b;
    }
    if (kind === 'expander') {
      const s = all('summary').find((x) => x.innerText.includes(arg));
      if (s && !s.parentElement.open) s.click();
      return s;
    }
    if (kind === 'text') return byText('p, h1, h2, h3, h4, strong', arg) ||
      all('p, h4, strong').find((x) => x.innerText.includes(arg));
    return null;
  }

  // The card: small, bottom right, the app's blue.  Stop pauses: the card
  // stays with Resume Demo (Robert, 2026-09-27); the × ends the tour.
  const card = doc.createElement('div');
  card.id = 'otdr-tour-card';
  card.style.cssText = 'position:fixed;right:24px;bottom:24px;z-index:1000000;width:340px;' +
    'background:var(--otdr-bg);border:2px solid var(--otdr-accent);border-radius:12px;padding:14px 16px;' +
    'box-shadow:0 6px 24px rgba(0,0,0,.25);font-family:"Segoe UI",sans-serif;color:var(--otdr-text)';
  const btn = 'border:1px solid var(--otdr-edge-2);background:var(--otdr-panel);border-radius:6px;' +
    'padding:2px 10px;cursor:pointer;margin-left:6px';
  card.innerHTML =
    '<div style="display:flex;justify-content:space-between;align-items:center">' +
    '<span id="otdr-tour-step" style="font-size:12px;color:var(--otdr-accent);font-weight:600"></span>' +
    '<span><button id="otdr-tour-stop" style="' + btn + '">Stop Demo</button>' +
    '<button id="otdr-tour-close" title="End the demo" style="' + btn + '">×</button></span></div>' +
    '<div id="otdr-tour-title" style="font-size:17px;font-weight:700;margin:6px 0 4px"></div>' +
    '<div id="otdr-tour-text" style="font-size:14px;line-height:1.4"></div>' +
    '<div style="height:6px;background:var(--otdr-panel);border-radius:3px;margin-top:10px">' +
    '<div id="otdr-tour-bar" style="height:6px;width:0;background:var(--otdr-accent);border-radius:3px;' +
    'transition:width .5s linear"></div></div>';
  const old = doc.getElementById('otdr-tour-card');
  if (old) old.remove();
  doc.body.appendChild(card);

  let lit = null;
  function light(el) {
    if (lit) { lit.style.outline = lit.dataset.tourOutline || ''; lit.style.outlineOffset = ''; }
    lit = el;
    if (!el) return;
    el.dataset.tourOutline = el.style.outline || '';
    el.style.outline = '3px solid var(--otdr-accent)';
    el.style.outlineOffset = '4px';
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
  const before = (i) => STEPS.slice(0, i).reduce((t, s) => t + s[4] * 1000, 0);
  let idx = 0, timer = null, stepStart = 0, paused = false, spent = 0;

  function show(i) {
    closeMenus();
    const [tab, spec, title, words] = STEPS[i];
    if (tab) clickTab(tab);
    setTimeout(() => {                         // let the tab draw first
      if (!tour.running || paused || idx !== i) return;
      light(target(spec));
      doc.getElementById('otdr-tour-step').textContent = `Step ${i + 1} of ${STEPS.length}`;
      doc.getElementById('otdr-tour-title').textContent = title;
      doc.getElementById('otdr-tour-text').textContent = words;
    }, 400);
  }
  function run(wait) {
    stepStart = Date.now() - (STEPS[idx][4] * 1000 - wait);
    timer = setTimeout(() => {
      idx += 1;
      if (idx >= STEPS.length) return end();
      show(idx);
      run(STEPS[idx][4] * 1000);
    }, wait);
  }
  function pause() {
    paused = true;
    clearTimeout(timer);
    spent = Date.now() - stepStart;
    light(null);
    closeMenus();
    const b = doc.getElementById('otdr-tour-stop');
    b.textContent = 'Resume Demo';
    b.style.background = 'var(--otdr-accent)'; b.style.color = 'var(--otdr-on-accent)';
    doc.getElementById('otdr-tour-title').textContent = 'Demo paused';
    doc.getElementById('otdr-tour-text').textContent =
      'Look around as you like. Resume Demo carries on from step ' + (idx + 1) + '.';
  }
  function resume() {
    paused = false;
    const b = doc.getElementById('otdr-tour-stop');
    b.textContent = 'Stop Demo';
    b.style.background = 'var(--otdr-panel)'; b.style.color = 'var(--otdr-text)';
    show(idx);
    run(Math.max(1500, STEPS[idx][4] * 1000 - spent));
  }
  function end() {
    tour.running = false;
    clearTimeout(timer);
    light(null);
    closeMenus();
    card.remove();
    clickTab('Events');
    P.scrollTo({ top: 0, behavior: 'smooth' });
  }
  doc.getElementById('otdr-tour-stop').onclick = () => (paused ? resume() : pause());
  doc.getElementById('otdr-tour-close').onclick = end;

  const tick = setInterval(() => {
    if (!tour.running) return clearInterval(tick);
    const done = before(idx) + (paused ? spent : Date.now() - stepStart);
    const bar = doc.getElementById('otdr-tour-bar');
    if (bar) bar.style.width = Math.min(100, done / TOTAL_MS * 100).toFixed(1) + '%';
  }, 500);

  show(0);
  run(STEPS[0][4] * 1000);
})();
'''


OVERVIEW_NAV_JS = """<script>
(function () {
  const P = window.parent;
  if (P.__otdrOverviewNav) return;
  P.__otdrOverviewNav = true;
  const go = {'st-key-ov_progress': 'Audit FQA', 'st-key-ov_final': 'Traces',
              'st-key-ov_last': 'Events'};
  P.document.addEventListener('click', (e) => {
    for (const cls in go) {
      if (!e.target.closest('.' + cls)) continue;
      const tab = Array.from(P.document.querySelectorAll('button[role="tab"]'))
        .find((b) => b.innerText.trim() === go[cls]);
      if (tab) { tab.click(); tab.scrollIntoView({behavior: 'smooth', block: 'start'}); }
      return;
    }
  });
})();
</script>"""


def _project_overview(work, items):
    ss = st.session_state
    snap = _project_snapshot(ss, ss.get('project_saved'))
    job = ss.get('fqa_job') or {}
    s1 = (snap.get('spans') or [{}])[0]
    a = (job.get('site_a') or {}).get('alias') or s1.get('site_a')
    z = (job.get('site_z') or {}).get('alias') or s1.get('site_b')
    if not (a and z):
        try:
            prod = _read_prod(project_production_sheet(work)) if project_production_sheet(work) else None
        except Exception:
            prod = None
        a = a or (prod and prod.site_a and prod.site_a.name)
        z = z or (prod and prod.site_z and prod.site_z.name)
    a, z = a or 'A', z or 'Z'
    cust = ss.get('otdr_profile')
    fs = final_shoot(work)
    n_fib = job.get('fiber_count') or (len(_trace_fibers(fs['a']) | _trace_fibers(fs['b']))
                                       if fs else None)
    st.markdown(f'## {os.path.basename(work)}')
    # Run Demo starts from the home screen only (Robert, 2026-09-27): it opens
    # the Sample Span and the tour starts here, once.  The tour goes into the
    # page itself: the frame this runs in goes away on the next rerun.
    if st.session_state.pop('_start_tour', False) and os.path.basename(work) == DEMO_NAME:
        st_components_html('<script>const d=window.parent.document;'
                           'const s=d.createElement("script");'
                           f"s.textContent={json.dumps(DEMO_TOUR_JS.replace('OTDR Suite', PRODUCT_NAME))};"
                           'd.head.appendChild(s);</script>', height=0)
    c1, c2, c3, c4 = st.columns(4)
    with c1.container(border=True):
        st.caption('Span')
        st.markdown(f'**{a} → {z}**')
        st.caption(' · '.join(filter(None, (
            cust if cust and cust not in _NOT_CUSTOMERS else 'No customer set',
            f'{n_fib} fibers' if n_fib else None))))
    with c2.container(border=True, key='ov_progress'):
        have = sum(i['ok'] for i in items)
        st.caption('FQA Progress')
        st.markdown(f'**{have} of {len(items)} in hand**')
        st.progress(have / max(1, len(items)))
    with c3.container(border=True, key='ov_final'):
        st.caption('Final Traces')
        if fs:
            d, lab = shoot_info(fs)
            n_sh = len(list_shoots(work))
            st.markdown(f"**Shot {d or '(no date)'}**")
            st.caption(' · '.join(filter(None, (lab, f'{n_sh} shoot' + 's' * (n_sh != 1)))))
        else:
            st.markdown('**None yet**')
            st.caption('Add them on the Traces tab.')
    with c4.container(border=True, key='ov_last'):
        st.caption('Last Activity')
        ev = events_read(work)['events']
        if ev:
            last = max(ev, key=lambda e: e.get('when') or 0)
            st.markdown(f"**{last.get('kind')}**")
            st.caption(f"{_when_text(last.get('when'))} · {last.get('text')}")
        else:
            st.markdown('**Nothing yet**')
    _render_project_owner(work)
    _work_folder_picker(work)
    # The boxes open their tab (Robert, 2026-09-27): FQA Progress -> Audit
    # FQA, Final Traces -> Traces, Last Activity -> Events.  Tabs switch in
    # the browser, so this is a click handler put into the page once.
    st.markdown('<style>.st-key-ov_progress,.st-key-ov_final,.st-key-ov_last{cursor:pointer}'
                '.st-key-ov_progress:hover,.st-key-ov_final:hover,.st-key-ov_last:hover'
                '{border-color:var(--otdr-accent)!important;background:var(--otdr-soft)}</style>',
                unsafe_allow_html=True)
    st_components_html(OVERVIEW_NAV_JS, height=0)


def project_files(work):
    """[(label, path)] for the work folder's dropdown: every file in the
    project folders, and each trace shoot as one folder (not its .sor files)."""
    out = []
    for sh in list_shoots(work):
        d, lab = shoot_info(sh)
        out.append((f"📂 {_rel(work, sh['dir'])}  (traces shot {d or 'no date'}"
                    f"{', ' + lab if lab else ''})", sh['dir']))
    for key, sub_ in PROJECT_DIRS.items():
        if key == 'traces':
            continue
        base = os.path.join(work, sub_)
        for root, dirs, files in os.walk(base):
            dirs[:] = sorted(d for d in dirs if not d.startswith('.'))
            for f in sorted(files):
                if not f.startswith(('.', '~$')) and not f.endswith('.tmp'):
                    p = os.path.join(root, f)
                    out.append((f'📄 {_rel(work, p)}', p))
    return out


def _work_folder_picker(work):
    """The work folder as a drop-down (Robert, 2026-09-27): pick any file in
    the project and open it, or show it in its folder."""
    with st.popover(f'📁 Work Folder: {work}', use_container_width=True):
        files = project_files(work)
        labels = {p: l for l, p in files}
        pick = st.selectbox('Files in This Project', [p for _l, p in files], index=None,
                            key='wf_pick', format_func=lambda p: labels.get(p, p),
                            placeholder='Choose a File or a Set of Traces…')
        c1, c2, c3 = st.columns(3)
        from fieldcapture.email_draft import open_with_default_app, reveal
        if c1.button('Open', key='wf_open', disabled=not pick, use_container_width=True):
            ok, err = open_with_default_app(pick)
            if not ok:
                st.error(f'Could not open it: {err}')
        if c2.button('Show in Folder', key='wf_show', disabled=not pick,
                     use_container_width=True):
            reveal(pick)
        if c3.button('Open the Work Folder', key='wf_folder', use_container_width=True):
            ok, err = open_with_default_app(work)
            if not ok:
                st.error(f'Could not open it: {err}')
        st.caption('The project saves itself as you work.')


_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


OWNER_RECENTS_KEY = 'owner_recents'
OWNER_RECENTS_MAX = 8


def owner_recents():
    """[{'name', 'email'}] saved on this PC, newest first."""
    rows = _settings_read().get(OWNER_RECENTS_KEY) or []
    return [{'name': str(r.get('name')), 'email': str(r.get('email'))} for r in rows
            if isinstance(r, dict) and r.get('name') and r.get('email')]


def _remember_owner(owner):
    rows = [r for r in owner_recents() if r['email'].lower() != owner['email'].lower()]
    _settings_update(**{OWNER_RECENTS_KEY: ([{'name': owner['name'], 'email': owner['email']}]
                                             + rows)[:OWNER_RECENTS_MAX]})


def _pick_recent_owner(recents, work):
    """A Recents pick fills the name and email boxes (a callback, so the
    boxes can still be written before they draw)."""
    ss = st.session_state
    i = ss.get('own_recent')
    if i is None or not (0 <= i < len(recents)):
        return
    # Only the boxes change: their sync tag still names the project's owner,
    # so _bind leaves the picked values alone until Save.
    ss['own_name'], ss['own_email'] = recents[i]['name'], recents[i]['email']


def _render_project_owner(work):
    """The project's owner (Robert, 2026-09-27): the person told about every
    change in the project, whoever makes it.  Outlined in orange until one
    is chosen."""
    ss = st.session_state
    owner = dict(ss.get('project_owner') or {})
    have = bool(owner.get('name') and owner.get('email'))
    st.markdown('<style>.st-key-ov_owner{border:2px solid ' +
                ('var(--otdr-rule)' if have else '#e07b00') + '!important;border-radius:.6rem;'
                'padding:.2rem .6rem}</style>', unsafe_allow_html=True)
    with st.container(key='ov_owner'):
        c1, c2 = st.columns([4, 1], vertical_alignment='center')
        if have:
            c1.markdown(f"👤 **Project Owner:** {owner['name']} · {owner['email']} ✅")
            if not owner_mail_ready():
                c1.caption('Emails to the owner start once the company sender mailbox is '
                           'set up in the app.')
        else:
            c1.markdown('👤 **Project Owner:** none chosen. Choose who hears about every '
                        'change in this project.')
        with c2.popover('Change' if have else 'Choose', use_container_width=True):
            # Recents (Robert, 2026-09-28): owners saved before on this PC,
            # newest first; picking one fills the name and email.  Kept in
            # this PC's settings, never in the (public) repository.
            recents = owner_recents()
            if recents:
                st.selectbox('Recents', list(range(len(recents))), index=None,
                             key='own_recent', placeholder='Choose a Recent Owner…',
                             format_func=lambda i: f"{recents[i]['name']} · {recents[i]['email']}",
                             on_change=_pick_recent_owner, args=(recents, work))
            _bind('own_name', owner.get('name') or '', work)
            _bind('own_email', owner.get('email') or '', work)
            name = st.text_input('Name', key='own_name')
            email = st.text_input('Email', key='own_email', placeholder='name@company.com')
            email_ok = _EMAIL_RE.match((email or '').strip()) is not None
            if email and not email_ok:
                st.caption('⚠ That is not an email address.')
            if st.button('Save', key='own_save', type='primary',
                         disabled=not ((name or '').strip() and email_ok)):
                new = {'name': name.strip(), 'email': email.strip()}
                if new != owner:
                    ss['project_owner'] = new
                    _bound('own_name', new['name'], work)
                    _bound('own_email', new['email'], work)
                    _remember_owner(new)
                    project_log(work, 'Project', f"Project owner set to {new['name']} "
                                f"({new['email']})")
                    st.rerun()


def _tz_abbr(tm):
    """'PDT' for a local time.  Windows names zones in full ('Pacific
    Daylight Time'): those are cut to their initials."""
    z = time.strftime('%Z', tm) or ''
    if ' ' in z:
        z = ''.join(w[0] for w in z.split() if w[:1].isalpha()).upper()
    return z


def _when_text(t):
    """A time as '2026-05-06 11:30 AM PDT' (Robert, 2026-09-27: "our times
    need to have am or pm and they need to show what time zone")."""
    try:
        tm = time.localtime(float(t))
    except (TypeError, ValueError, OverflowError):
        return ''
    return f"{time.strftime('%Y-%m-%d %I:%M %p', tm)} {_tz_abbr(tm)}".strip()


def _project_tab_events(work):
    ev = sorted(events_read(work)['events'], key=lambda e: -(e.get('when') or 0))
    if not ev:
        st.caption('Nothing has happened in this project yet.')
        return
    kinds = [k for k in EVENT_KINDS if any(e.get('kind') == k for e in ev)]
    users = sorted({e.get('user') for e in ev if e.get('user')}, key=str.lower)
    c1, c2 = st.columns([2, 1])
    pick = c1.multiselect('Show', kinds, key='ev_filter', placeholder='Everything')
    who = c2.multiselect('User', users, key='ev_user_filter', placeholder='Everyone') \
        if len(users) > 1 else []
    rows = [{'When': _when_text(e.get('when')), 'User': e.get('user') or '',
             'Type': e.get('kind') or '', 'What Happened': e.get('text') or '',
             'How': _how_shown(e.get('how'), '')}
            for e in ev if (not pick or e.get('kind') in pick)
            and (not who or e.get('user') in who)]
    st.dataframe(rows, hide_index=True, use_container_width=True,
                 column_config={'What Happened': st.column_config.TextColumn(width='large')})
    st.caption(f'{len(rows)} of {len(ev)} events. "Found in folder" is a file saved into '
               f'the project folder outside {PRODUCT_NAME}, such as a .zfc saved from an email. '
               'User is the Windows login that did it; for a file found in the folder, '
               'the login that owns the file, blank when that can\'t be told (a synced '
               'OneDrive or SharePoint folder, or events from before users were kept).')


def _pick_final(key, sid, work, text):
    """A shoot row's Final box (Robert, 2026-09-27): ticking it makes that
    shoot the final one; the final's own box cannot be unticked, since there
    is always a final.  A callback, so it lands before the page redraws."""
    ss = st.session_state
    if not ss.get(key):
        ss[key] = True
        return
    if ss.get('project_final_shoot') != sid:
        set_final_shoot(sid, work)
        project_log(work, 'Traces', f'Final traces set to {text}')


def _project_tab_traces(work):
    ss = st.session_state
    shoots = list_shoots(work)
    fin = final_shoot(work)
    if ss.get('_shoot_flash'):
        msg = ss.pop('_shoot_flash')
        level, msg = msg if isinstance(msg, tuple) else ('success', msg)
        getattr(st, level)(msg)
    if not shoots:
        st.markdown('**Trace Shoots**')
        st.caption('No traces yet. Add the first shoot below.')
    else:
        infos = {sh['id']: shoot_info(sh) for sh in shoots}
        counts = {sh['id']: (len(_trace_fibers(sh['a'])), len(_trace_fibers(sh['b'])))
                  for sh in shoots}

        def _shoot_text(sid):
            d, lab = infos[sid]
            fa, fb = counts[sid]
            return f"{d or 'no date'}{' · ' + lab if lab else ''} · A {fa} / B {fb} fibers"

        order = sorted(shoots, key=lambda sh: (infos[sh['id']][0], sh['id']), reverse=True)
        ids = [sh['id'] for sh in order]
        # ── Trace Shoots: one row each, a tick box to run it and its label
        # typed straight into the row.
        st.markdown('**Trace Shoots**')
        # The separate Final Traces list is gone (Robert, 2026-09-27): the
        # Final column picks it.
        st.caption('Tick Final on the shoot the FQA checklist, the Field Capture job and the '
                   'FQA package use.')
        w = [1.2, 2.0, 0.8, 0.8, 1.3, 1.0, 1.0, 1.6, 1.8]
        head = st.columns(w)
        by = file_users(work)
        for col, t in zip(head, ('Shot On', 'Label', 'A Fibers', 'B Fibers', 'Added',
                                 'User', 'Final', 'Folder', '')):
            col.caption(t)
        meta = dict(ss.get('project_shoots') or {})
        # The final shoot's row stands out (Robert, 2026-09-27): a green badge
        # in the Final column and a light green row (no edge: it shifted the
        # columns).
        # Every row gets the same padding, so the final one's tint moves nothing.
        st.markdown('<style>[class*="st-key-shoot_row_"]{padding:.15rem 0;'
                    'border-radius:.4rem}'
                    '.st-key-shoot_row_final{background:var(--otdr-ok-bg)}</style>',
                    unsafe_allow_html=True)
        for i, sh in enumerate(order):
            d, lab = infos[sh['id']]
            is_final = bool(fin and sh['id'] == fin['id'])
            row = st.container(key='shoot_row_final' if is_final else f'shoot_row_{i}')
            c = [None] + row.columns(w, vertical_alignment='center')
            loaded = ss.get('project_run_shoot') == sh['id']
            c[1].markdown(('✅ ' if loaded else '') + (d or 'no date'),
                          help='Loaded in the tools' if loaded else None)
            kl = f'ps_shoot_label_{sh["id"]}'
            _bind(kl, lab, work)
            nl = c[2].text_input(f'Label ({sh["id"] or "Traces"})', key=kl,
                                 label_visibility='collapsed',
                                 placeholder='e.g. reshoot after repair')
            if nl != lab:
                meta[sh['id']] = {'date': d, 'label': nl}
                _bound(kl, nl, work)
            c[3].markdown(str(counts[sh['id']][0]))
            c[4].markdown(str(counts[sh['id']][1]))
            c[5].markdown(_when_text(_mtime(sh['dir'])))
            c[6].markdown(by.get(f"{PROJECT_DIRS['traces']}/{sh['id'] or '(first shoot)'}")
                          or '')
            kf = f'final_cb_{i}'
            _bind(kf, is_final, (work, fin['id'] if fin else None, sh['id']))
            c[7].checkbox('Final', key=kf, label_visibility='collapsed',
                          on_change=_pick_final, args=(kf, sh['id'], work, _shoot_text(sh['id'])),
                          help='The final traces: the FQA checklist, the Field Capture job and '
                               'the FQA package use them. Tick another shoot to change it.')
            c[8].caption(_rel(work, sh['dir']))
            with c[9].popover('Run In…', use_container_width=True):
                for page in RUN_IN_TOOLS:
                    st.button(page, key=run_in_key(sh, page), use_container_width=True)
        ss['project_shoots'] = meta
    with st.expander('➕ Add a Shoot' if shoots else '➕ Add the First Shoot',
                     expanded=not shoots):
        st.caption('Copied into its own dated folder in the project. Two folders, one '
                   'folder holding both directions, or drop them.')
        fill = ss.pop('_sp_target_fill', None)
        if fill and fill[0] == 'ps_tr_one':
            # A SharePoint download on the last run: it is the one folder now.
            ss['ps_tr_one'] = fill[1]
            ss.pop('ps_tr_a', None)
            ss.pop('ps_tr_b', None)
        c1, c2, c3 = st.columns(3)
        for col, key, label in ((c1, 'ps_tr_a', 'A-Direction Folder'),
                                (c2, 'ps_tr_b', 'B-Direction Folder'),
                                (c3, 'ps_tr_one', 'One Folder, Both Directions')):
            with col:
                if st.button('📂 ' + label, key=key + '_pick', use_container_width=True):
                    p = pick_folder('Choose the ' + label)
                    if p:
                        ss[key] = p
                st.text_input(label, key=key, label_visibility='collapsed',
                              placeholder='Or Paste a Path')
        drop = st.file_uploader('…or Drop the Traces: a .zip, Loose Files, or .bdr',
                                type=['zip', 'sor', 'json', 'bdr'],
                                accept_multiple_files=True, key='ps_tr_drop')
        # The project's own SharePoint folder (kept in the project file): on
        # another PC only the Microsoft sign-in is asked for.
        with st.expander('☁️ From SharePoint', expanded=bool(ss.get('project_sp'))):
            _render_sharepoint_box(target='ps_tr_one')
        a, b = _clean_path(ss.get('ps_tr_a')), _clean_path(ss.get('ps_tr_b'))
        one = _clean_path(ss.get('ps_tr_one'))
        import datetime as _dt
        shot = (sor_shot_date(a) if a and os.path.isdir(a) else '') or \
               (sor_shot_date(one) if one and os.path.isdir(one) else '')
        try:
            default_day = _dt.date.fromisoformat(shot) if shot else _dt.date.today()
        except ValueError:
            default_day = _dt.date.today()
        d1, d2 = st.columns([1, 2])
        day = d1.date_input('Shot On', value=default_day,
                            help='Read from the .sor files when they carry it.')
        lab = d2.text_input('Label (Optional)', key='ps_tr_label',
                            placeholder='e.g. reshoot after repair')
        if st.button('Add This Shoot', key='ps_tr_copy', type='primary'):
            if not (a and b) and (one or drop):
                a, b = _resolve_bidir_from_single(one, drop)
            if a and b and os.path.isdir(a) and os.path.isdir(b):
                with st.spinner('Copying traces…'):
                    sid, na, nb = add_shoot(a, b, work, day.isoformat(), (lab or '').strip())
                if not shoots:
                    set_final_shoot(sid, work)
                    ss['_shoot_flash'] = f'Copied {na} A and {nb} B trace files.'
                else:
                    ss['_shoot_flash'] = (f'Added a shoot of {na} A and {nb} B trace '
                                          'files. Tick it to run it; pick it under Final '
                                          'traces to use it for the FQA.')
                # The shoots list sits above this button: redraw so it shows the
                # new one.  Safe here -- the sidebar is drawn, and the tools'
                # folders are re-seeded at the top of every run.
                st.rerun()
            else:
                st.error('Pick an A and a B folder, or one folder / drop that '
                         'holds both directions.')


def _project_tab_reports(work):
    base = work_sub('reports', work)
    rows = []
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        runs = [d for d in dirs if _report_kind(d) == 'Secret Sauce']
        for d in runs:
            p = os.path.join(root, d)
            rows.append(('Secret Sauce', d, p, _mtime(p)))
        dirs[:] = [d for d in dirs if d not in runs]
        for f in files:
            if f.startswith(('.', '~$')) or f.endswith('.tmp'):
                continue
            p = os.path.join(root, f)
            rows.append((_report_kind(os.path.relpath(p, base)) or 'Other', f, p, _mtime(p)))
    rows.sort(key=lambda r: -r[3])
    st.markdown('**Reports**')
    st.caption(f'Every Splice Report, Unidirectional and Secret Sauce report run in this '
               f'project is kept in `{base}`.')
    if not rows:
        st.caption('No reports yet. Run one on the final traces:')
    kinds = sorted({r[0] for r in rows})
    pick = st.multiselect('Show', kinds, key='rep_filter', placeholder='Every Report') \
        if len(kinds) > 1 else []
    shown = [r for r in rows if not pick or r[0] in pick]
    data = events_read(work)
    used = data.get('report_traces') or {}
    by = file_users(work, data)
    w = [2, 1.1, 5, 1, 1, 1.3, 1.1]
    if shown:
        for col, t in zip(st.columns(w), ('Report', 'User', 'File', '', '', '', '')):
            col.caption(t)
    for i, (kind, name, p, mt) in enumerate(shown[:200]):
        traces = used.get(_rel(work, p)) or []
        shoot = _shoot_of(work, traces)
        c1, cu, c2, c3, c4, c5, c6 = st.columns(w)
        c1.markdown(f'**{kind}**  \n<span style="font-size:0.85em;opacity:0.7">'
                    f'{_when_text(mt)}</span>', unsafe_allow_html=True)
        cu.markdown(by.get(_rel(work, p)) or '')
        c2.markdown(f'{name}  \n<span style="font-size:0.85em;opacity:0.7">'
                    f'{_traces_text(work, traces, shoot)}</span>', unsafe_allow_html=True)
        if c3.button('Open', key=f'rep_open_{i}', use_container_width=True):
            from fieldcapture.email_draft import open_with_default_app
            ok, err = open_with_default_app(p)
            if not ok:
                st.error(f'Could not open it: {err}')
        if c4.button('Folder', key=f'rep_show_{i}', use_container_width=True):
            from fieldcapture.email_draft import reveal
            ok, err = reveal(p)
            if not ok:
                st.error(f'Could not show it: {err}')
        if c6.button('Export', key=f'rep_export_{i}', use_container_width=True,
                     help='A copy to your Downloads folder, to send on.'):
            try:
                out = export_report(p)
                st.success(f'Exported a copy to `{out}`.')
                project_log(work, 'Report', f'{kind} exported: {os.path.basename(out)} to '
                            f'{os.path.dirname(out)}')
            except OSError as exc:
                st.error(f'Could not export it: {exc}')
        tdir = shoot['dir'] if shoot else (traces[0] if traces else '')
        if c5.button('Show Traces', key=f'rep_traces_{i}', use_container_width=True,
                     disabled=not (tdir and os.path.isdir(tdir))):
            from fieldcapture.email_draft import open_with_default_app
            ok, err = open_with_default_app(tdir)
            if not ok:
                st.error(f'Could not open the traces folder: {err}')
    st.caption('To run a report, use Run In… on a set of traces on the Traces tab.')


def export_report(path, dest_dir=None):
    """Copy a project report out of the job (Robert, 2026-09-27: reports are
    saved to the job, and exported from the Reports tab).  A Secret Sauce
    run folder goes out as one .zip.  Returns the copy's path."""
    import shutil
    import folder_intake as _fi
    dest_dir = dest_dir or _fi.default_report_dir()
    os.makedirs(dest_dir, exist_ok=True)
    name = os.path.basename(os.path.normpath(path))
    base, ext = os.path.splitext(name) if os.path.isfile(path) else (name, '.zip')
    out, k = os.path.join(dest_dir, base + ext), 2
    while os.path.exists(out):
        out = os.path.join(dest_dir, f'{base} ({k}){ext}')
        k += 1
    if os.path.isdir(path):
        shutil.make_archive(out[:-4], 'zip', path)
    else:
        shutil.copy2(path, out)
    return out


def _shoot_of(work, traces):
    """The project shoot a report's trace folders belong to, or None."""
    if not traces:
        return None
    for sh in list_shoots(work):
        base = os.path.normcase(os.path.abspath(sh['dir']))
        if all(os.path.normcase(os.path.abspath(t)) == base
               or os.path.normcase(os.path.abspath(t)).startswith(base + os.sep)
               for t in traces):
            return sh
    return None


def _traces_text(work, traces, shoot):
    """What a report was run on, in words, for the Reports tab."""
    if shoot:
        d, lab = shoot_info(shoot)
        return (f"Traces shot {d or '(no date)'}{' · ' + lab if lab else ''} · "
                f"{_rel(work, shoot['dir'])}")
    if traces:
        return 'Traces: ' + ' + '.join(traces)
    return 'Traces not recorded (run before this was kept)'


def _drop_zfc(key, work):
    """A drop box for Field Capture's .zfc: it is filed into Field/."""
    for dest in _drop_into('Drop a Field Capture File (.zfc) Here', key, 'field',
                           [e.lstrip('.') for e in capture_package_exts()],
                           'What the phone emailed. It is saved into the Field folder, '
                           'where its photos and GPS are read from.'):
        st.success(f'Saved {os.path.basename(dest)}.')


def _project_tab_pictures(work):
    ss = st.session_state
    st.markdown('**Pictures**')
    st.caption('The photos from Field Capture and any added here. The A end and Z end '
               'photos go on the FQA package\'s Pictures tab.')
    with st.expander('➕ Add Pictures'):
        c1, c2 = st.columns([1, 2])
        with c1:
            end = st.radio('Where They Were Taken', list(PICTURE_ENDS), key='pic_end',
                           format_func=lambda k: PICTURE_ENDS[k].replace(' end', ' End'))
        with c2:
            for dest in _drop_into('Photos (.jpg, .png)', 'pic_drop',
                                   ('pictures', PICTURE_ENDS[end]),
                                   [e.lstrip('.') for e in _PICTURE_EXTS]):
                st.success(f'Added {os.path.basename(dest)}.')
        _drop_zfc('pic_drop_zfc', work)
    _render_phone_job(project_production_sheet(work), project_job_id(), work, kp='pic')
    photos = project_photos(work, ss.get('project_job_id'))
    by = file_users(work)
    if not photos:
        st.caption('No pictures yet. They arrive with Field Capture (.zfc) or are added above.')
        return
    # Robert, 2026-09-27: "a button where we can verify that the labels are
    # legible".  Field Capture's own label reader (Tesseract, on this PC)
    # reads every photo; its server hands it the photos in-process.
    if st.toggle('🔍 Check the Labels Are Legible', key='pic_check',
                 help='Reads the labels in every photo on this PC. It takes a few '
                      'seconds a photo.'):
        from fieldcapture import server as _fc
        _fc.CONFIG['check_photos'] = [
            {'name': ph['name'], 'end': PICTURE_ENDS[ph['end']],
             'data': (lambda b=photo_bytes(ph): b)} for ph in photos]
        port = _fc.start_in_thread()
        st_iframe(f'http://127.0.0.1:{port}/check',
                  height=min(1600, 70 + 215 * len(photos)), scrolling=True)
    for end, label in PICTURE_ENDS.items():
        mine = [p for p in photos if p['end'] == end]
        if not mine:
            continue
        st.markdown(f'**{label}** · {len(mine)} photo{"s" * (len(mine) != 1)}')
        cols = st.columns(4)
        for i, ph in enumerate(mine):
            data = photo_bytes(ph)
            with cols[i % 4]:
                if data:
                    st.image(data, use_container_width=True)
                else:
                    st.warning(f"Couldn't read {ph['name']}")
                user = by.get(_rel(work, ph['path']))
                st.caption(f"{ph['name']} · {ph['source']} · {_when_text(ph['when'])}"
                           + (f' · {user}' if user else ''))


GPS_FROM_WORDS = {'typed': 'Entered by hand', 'phone': 'Field Capture',
                  'production sheet': 'Production sheet'}


def _project_tab_gps(work):
    ss = st.session_state
    st.markdown('**GPS**')
    st.caption('One point per splice on the production sheet, plus the A and Z boxes. '
               'Field Capture fills the Field Capture column; type a fix in Entered by Hand '
               'to add one or to correct it. Decimal ("39.4688, -102.9682") or degrees '
               'minutes seconds ("39 28 7.75 N 102 58 5.43 W"). The FQA package gets the '
               'Used fix.')
    prod_path = project_production_sheet(work)
    _render_phone_job(prod_path, project_job_id(), work, kp='gps')
    _drop_zfc('gps_drop_zfc', work)
    prod = None
    if prod_path:
        try:
            prod = _read_prod(prod_path)
        except Exception as exc:
            st.warning(f"Couldn't read the production sheet: {type(exc).__name__}: {exc}")
    if prod is None:
        st.info('Add the production sheet (on the Audit FQA tab) to list the splice points.')
        return
    job_id = project_job_id()
    pkgs = [(n, p) for n, p in collect_capture_packages(work_sub('field', work))
            if p.get('job') == job_id and not p.get('test')]
    manual = dict(ss.get('project_gps') or {})
    rows = project_gps_rows(prod, pkgs, manual)
    table = [{'Event': r['event'], 'Vault': r['vault'], 'Name': r['name'],
              'Production Sheet': r['sheet'], 'Field Capture': r['phone'],
              'Entered by Hand': r['hand'],
              'Used': (f"{r['used']['lat']:.6f}, {r['used']['lon']:.6f}" if r['used'] else ''),
              'From': GPS_FROM_WORDS.get(r['from'], r['from'])} for r in rows]
    # The editor's key carries the project's values, so another project (or a
    # new package) draws a fresh editor instead of replaying old edits.
    sig = abs(hash((work, tuple((r['key'], r['hand'], r['phone']) for r in rows)))) % 10 ** 8
    edited = st.data_editor(
        table, key=f'gps_editor_{sig}', hide_index=True, use_container_width=True,
        disabled=['Event', 'Vault', 'Name', 'Production Sheet', 'Field Capture', 'Used',
                  'From'],
        column_config={'Entered by Hand': st.column_config.TextColumn(
            width='medium', help='Type a fix, or clear it to go back to the phone\'s.'),
            'Field Capture': st.column_config.TextColumn(width='medium'),
            'Used': st.column_config.TextColumn(width='medium'),
            'Event': st.column_config.TextColumn(width='small'),
            'Vault': st.column_config.TextColumn(width='small')})
    new_manual, bad = {}, []
    for r, e in zip(rows, edited):
        v = str(e.get('Entered by Hand') or '').strip()
        if not v:
            continue
        kind, why = parse_event_location(v)
        if kind != 'gps':
            bad.append(f"{r['event']}: '{v}' ({why or 'not a coordinate'})")
        new_manual[str(r['key'])] = v
    if new_manual != manual:
        ss['project_gps'] = new_manual
        changed = [k for k in set(new_manual) | set(manual) if new_manual.get(k) != manual.get(k)]
        project_log(work, 'GPS', 'GPS entered by hand: ' + ', '.join(
            ('Site ' + k) if k in ('A', 'Z') else f'event {k}' for k in sorted(changed, key=str)),
            how='OTDR Suite')
        st.rerun()
    for b in bad:
        st.error('Not a fix the FQA form can read, ' + b)
    n_sp = len(prod.splices)
    got = sum(1 for r in rows if isinstance(r['key'], int) and r['used'])
    probs = splice_gps_problems([{'event': r['key'], 'gps': r['used']} for r in rows
                                 if isinstance(r['key'], int)],
                                next((r['used'] for r in rows if r['key'] == 'A'), None),
                                next((r['used'] for r in rows if r['key'] == 'Z'), None))
    probs = [(no, why) for no, why in probs if why != 'no GPS fix']
    (st.success if got == n_sp else st.info)(f'{got} of {n_sp} splice points have a fix.')
    for no, why in probs[:8]:
        st.warning(f'Event {no}: {why}')


def _project_tab_audit(work):
    """The Submittal Checklist (Robert: "Audit FQA tab is our audit feature
    we already built") and building the customer's FQA workbook.  Returns the
    checklist items, for the overview."""
    ss = st.session_state
    snap = _project_snapshot(ss, ss.get('project_saved'))
    s1 = (snap.get('spans') or [{}])[0]
    manual = dict(ss.get('project_manual') or {})
    # The customer, at the top (Robert, 2026-09-27): the same dropdown, the
    # same list and the same setting as the reports' customer.
    try:
        _render_customer_profile_picker()
    except Exception as exc:
        report_error('audit fqa: customer picker', exc, {})
    _cust = ss.get('otdr_profile')
    if _cust and _cust not in _NOT_CUSTOMERS and _cust not in FQA_FORM_CUSTOMERS:
        st.info(f'No FQA form set up for {_cust} yet: the checklist below is Lumen\'s.')
    c1, c2 = st.columns([3, 1])
    c1.caption('Laid out on the four sections of Lumen\'s Submittal Checklist. Everything '
               'is read from the files in the project folder, so a file saved into it by '
               'hand counts too.')
    c2.button('🧭 Start the Audit', key='ps_audit_start', type='primary',
              use_container_width=True,
              help='Go through every open item one at a time: act on it or skip it.')
    overall = st.empty()
    build_box = st.container(border=True)

    # ── the project workbook: the production sheet ──
    prod = project_production_sheet(work)
    with st.container(border=True):
        st.markdown('**Production Sheet** (the project workbook)')
        about = st.empty()
        c1, c2 = st.columns([1, 2])
        if c1.button('📄 Choose Production Sheet', key='ps_prod_pick', use_container_width=True):
            p = pick_file('Choose the production sheet', [('Excel', '*.xlsx *.xlsm')])
            if p:
                ss['ps_prod_path'] = p
            elif p is None:
                st.info('No file picker on this machine: paste the path.')
        c2.text_input('Production Sheet Path', key='ps_prod_path',
                      label_visibility='collapsed',
                      placeholder='…or Paste the Production Sheet\'s Path')
        src = _clean_path(ss.get('ps_prod_path'))
        if src and st.button('Add It to the Project', key='ps_prod_add'):
            if not os.path.isfile(src):
                st.error(f'No file at {src}')
            else:
                with st.spinner('Copying the production sheet…'):
                    prod = _add_production_sheet(src, work)
                project_log(work, 'Production Sheet',
                            f'Production sheet added: {os.path.basename(prod)}', [prod])
                ss['fqa_prod'] = prod
                ss.pop('fqa_derived_for', None)
                st.success(f'Added {os.path.basename(prod)}.')
        if prod:
            n_loc, n_spl, warns = _production_summary(prod)
            if n_loc is None:
                about.error(f'`{os.path.basename(prod)}`: ' + '; '.join(warns))
            else:
                about.caption(f'`{os.path.basename(prod)}` · {n_loc} locations, '
                              f'{n_spl} splices. Sections 1 to 3 are built from it.')
        else:
            about.caption('Add the span\'s production sheet. Sections 1 to 3 are '
                          'built from it, with the traces and what the phone sends.')

    fqa, caps = collect_field_files(work_sub('field', work), work_sub('fqa', work))
    _jd = job_details_workbook(ss.get('fqa_job'))
    if _jd is not None:
        fqa = fqa + [(JOB_DETAILS_SOURCE, _jd)]
    job_id = project_job_id()
    unreadable = []
    pkgs_all = collect_capture_packages(work_sub('field', work), unreadable=unreadable)
    pkgs = [(n, p) for n, p in pkgs_all if p.get('job') == job_id and not p.get('test')]
    strays = [n for n, p in pkgs_all if p.get('job') != job_id and not p.get('test')]
    n_splices = None
    if prod:
        try:
            n_splices = len(_read_prod(prod).splices)
        except Exception as exc:
            st.warning(f"Couldn't read the production sheet {os.path.basename(prod)}: "
                       f"{type(exc).__name__}: {exc}")

    # ── 1 ──
    with st.container(border=True):
        box1 = st.container()
        f1, f2 = st.columns(2)
        f1.button('📝 Job Details (FQA Builder)', key='ps_go_fqa',
                  use_container_width=True, disabled=not prod,
                  help='Addresses, CLLIs, contractor, testers, calibration: '
                       'the cover page, pre-filled from the production sheet.')
        with f2:
            for dest in _drop_into('From the Phone: Capture Package (.zfc), FQA '
                                   'Workbook or Capture Sheet',
                                   'ps_drop_field', 'field',
                                   [e.lstrip('.') for e in capture_package_exts()] + ['xlsm', 'xlsx'],
                                   'What the phone emailed. Files saved into the '
                                   'Field folder by hand count too.'):
                wb = read_fqa_workbook(dest)
                if wb is not None and _fqa_names_mismatch(wb, (s1.get('site_a'), s1.get('site_b'))):
                    st.warning(f"**{os.path.basename(dest)}** doesn't mention "
                               f"{s1.get('site_a')} or {s1.get('site_b')}: check it "
                               'belongs to this span.')
        if strays:
            st.warning('From another job, not counted: ' + ', '.join(strays))
        for _n, _why in unreadable:
            st.warning(f"Couldn't read: {_n} ({_why})")
        for n, p in pkgs:
            ov = p.get('override') or {}
            if ov.get('reason'):
                st.warning(f"**{n}** was sent with problems open, and the tech wrote: "
                           f"“{ov['reason']}”. Open problems: "
                           + '; '.join(p.get('problems') or []))
        _render_phone_job(prod, job_id, work)
    # ── 2 ──
    with st.container(border=True):
        box2 = st.container()
    # ── 3 ──
    with st.container(border=True):
        box3 = st.container()
    # ── 4 ──
    with st.container(border=True):
        box4 = st.container()
        st.caption('Traces are added, dated and picked as final on the Traces tab.')
        st.markdown('**Other Data Files**')
        d1, d2 = st.columns(2)
        with d1:
            _drop_into('Power Meter Files (4.02)', 'ps_drop_pm', 'power', None)
        with d2:
            _drop_into('Splice Logs, Exception Documents (4.04, 4.05)',
                       'ps_drop_logs', 'splice_logs', None)
        m1, m2, m3 = st.columns(3)
        _bind('ps_tick_403', manual.get('4.03') is True, work)
        v = m1.checkbox('4.03 File Names Checked', key='ps_tick_403')
        if v:
            manual['4.03'] = True
        else:
            manual.pop('4.03', None)
        _bound('ps_tick_403', v, work)
        for col, no in ((m2, '4.04'), (m3, '4.05')):
            k = 'ps_tick_' + no.replace('.', '')
            _bind(k, manual.get(no) == 'na', work)
            v = col.checkbox(f'{no} Not Needed on This Job', key=k)
            if v:
                manual[no] = 'na'
            else:
                manual.pop(no, None)
            _bound(k, v, work)
        ss['project_manual'] = manual

    items = compute_project_items(work)
    for box, sec in ((box1, 1), (box2, 2), (box3, 3), (box4, 4)):
        rows = [i for i in items if i['section'] == sec]
        n_ok = sum(i['ok'] for i in rows)
        with box:
            st.markdown(f'#### {sec} · {SECTION_TITLES[sec]}  ·  {n_ok} of {len(rows)}')
            _render_needs(items, sec)
    have = sum(i['ok'] for i in items)
    overall.progress(have / max(1, len(items)),
                     text=f'{have} of {len(items)} in hand · {len(items) - have} still needed')
    with build_box:
        _render_fqa_build(work, prod, pkgs)
    return items


def _build_fqa_now(work):
    """The Build button's click, acted on before the screen draws, so the
    checklist and the overview already count the new package."""
    ss = st.session_state
    prod_path = project_production_sheet(work)
    if not prod_path:
        return
    try:
        prod = _read_prod(prod_path)
        pkgs = [(n, p) for n, p in collect_capture_packages(work_sub('field', work))
                if p.get('job') == project_job_id() and not p.get('test')]
        rows = project_gps_rows(prod, pkgs, ss.get('project_gps') or {})
        photos = [p for p in project_photos(work, ss.get('project_job_id'))
                  if p['end'] in ('A', 'Z')]
        trace = project_trace_distances(work, prod)
        with st.spinner('Building the FQA package…'):
            manifest = build_project_fqa(work, prod_path, ss.get('fqa_job') or {}, rows,
                                         photos, trace)
        ss['_fqa_built'] = manifest
        project_log(work, 'FQA',
                    f"FQA package built: {os.path.basename(manifest['out'])} · "
                    f"{manifest.get('events')} events, distances from "
                    f"{manifest.get('distance_source')}"
                    + (f", {manifest.get('photos')} photos" if manifest.get('photos') else ''),
                    [manifest['out']])
    except Exception as exc:
        report_error('project: FQA build', exc, {})
        ss['_fqa_build_error'] = f'Could not build the FQA package: {type(exc).__name__}: {exc}'


def _render_fqa_build(work, prod_path, pkgs):
    """Build the customer's FQA workbook from what the project holds."""
    ss = st.session_state
    st.markdown('**📗 Build the Lumen FQA Package**')
    if not prod_path:
        st.caption('Needs the production sheet: add it below.')
        return
    try:
        prod = _read_prod(prod_path)
    except Exception as exc:
        st.warning(f"Couldn't read the production sheet: {type(exc).__name__}: {exc}")
        return
    rows = project_gps_rows(prod, pkgs, ss.get('project_gps') or {})
    n_gps = sum(1 for r in rows if isinstance(r['key'], int) and r['from'] in ('typed', 'phone'))
    photos = [p for p in project_photos(work, ss.get('project_job_id')) if p['end'] in ('A', 'Z')]
    n_a = sum(1 for p in photos if p['end'] == 'A')
    trace = project_trace_distances(work, prod)
    job = ss.get('fqa_job') or {}
    missing = []
    try:
        from fqa.job_facts import JobFacts
        missing = JobFacts.from_dict(job).missing()
    except Exception:
        pass
    c1, c2, c3, c4 = st.columns(4)
    for col, label, val in (
            (c1, 'Job Details', 'complete' if not missing else f'{len(missing)} blank'),
            (c2, 'Splice GPS', f'{n_gps} of {len(prod.splices)}'),
            (c3, 'Photos', f'{n_a} A · {len(photos) - n_a} Z'),
            (c4, 'Distances', 'from the traces' if trace.get('distances_m') else 'footage marks')):
        col.caption(label)
        col.markdown(f'**{val}**')
    for w in trace.get('warnings') or []:
        st.caption(f'Distances: {w}. The footage marks on the production sheet are used instead.'
                   if not trace.get('distances_m') else f'Distances: {w}')
    if trace.get('distances_m'):
        st.caption(f"Distances: {len(trace['distances_m'])} splices matched to the closures the "
                   f"final traces found ({trace.get('method')}); span "
                   f"{(trace.get('span_length_m') or 0):,.0f} m.")
    if not trace.get('read') and final_shoot(work):
        st.button('📏 Read Distances from the Final Traces', key='ps_fqa_read_dist',
                  help='Runs the Splice Report engine on the final traces to find each '
                       'closure. Takes as long as a Splice Report; the result is kept.')
    if missing:
        st.caption('Blank on the cover page: ' + ', '.join(str(m) for m in missing[:8])
                   + (' …' if len(missing) > 8 else '') + '. Fill them in the job details or '
                   'with the audit.')
    st.button('Build the FQA Package', key='ps_fqa_build', type='primary')
    if ss.get('_fqa_build_error'):
        st.error(ss.pop('_fqa_build_error'))
    m = ss.get('_fqa_built')
    if m and os.path.isfile(m.get('out', '')) and os.path.dirname(m['out']) == work_sub('fqa', work):
        st.success(f"Built `{os.path.basename(m['out'])}` in the FQA folder: "
                   f"{m.get('events')} events, {m.get('fat_rows')} FAT rows, distances from "
                   f"{m.get('distance_source')}.")
        if m.get('not_in_this_build'):
            st.caption('Not written by this build of the FQA Builder: '
                       + ', '.join(m['not_in_this_build']))
        for w in (m.get('warnings') or [])[:6]:
            st.caption('⚠ ' + str(w))
        b1, b2 = st.columns(2)
        if b1.button('Open It in Excel', key='ps_fqa_open', use_container_width=True):
            from fieldcapture.email_draft import open_with_default_app
            ok, err = open_with_default_app(m['out'])
            if not ok:
                st.error(f'Could not open it: {err}')
        if b2.button('Show It in Its Folder', key='ps_fqa_show', use_container_width=True):
            from fieldcapture.email_draft import reveal
            reveal(m['out'])


# ── New project: the setup screen ────────────────────────────────────────
# Robert, 2026-09-24: the home screen's right side offers "Start New
# Project from Traces" and "Start New Project from Production Sheet".  Each
# opens this screen: load the one thing, see what was read from it, confirm
# the name and where the work folder goes, Create.  Create makes the work
# folder, copies the input in, writes a project file already filled with
# everything the input can answer, and opens it on Project status.
PROJECTS_ROOT_KEY = 'projects_root'
# Customers for a new project: the Splice Report customer profiles, less the
# two that are settings choices rather than customers.  The customer becomes
# the project's profile.  One customer's is the only FQA form so far (2026-09-24).
_NOT_CUSTOMERS = ('Default (engine baseline)', 'Custom (edit table below)')
FQA_FORM_CUSTOMERS = ('Lumen',)


def project_customers():
    return [n for n in CUSTOMER_PROFILES if n not in _NOT_CUSTOMERS]


def _documents_folder():
    """The user's real Documents folder. On Windows ask the shell
    (FOLDERID_Documents), so a Documents that OneDrive has moved is found;
    anywhere else, or if the call fails, ~/Documents.  OTDR_DOCUMENTS_DIR
    overrides it for tests, so no test writes into the real Documents."""
    if os.environ.get('OTDR_DOCUMENTS_DIR'):
        return os.environ['OTDR_DOCUMENTS_DIR']
    fallback = os.path.join(os.path.expanduser('~'), 'Documents')
    if sys.platform != 'win32':
        return fallback
    try:
        import ctypes
        import uuid
        from ctypes import wintypes

        class _GUID(ctypes.Structure):
            _fields_ = [('Data1', wintypes.DWORD), ('Data2', wintypes.WORD),
                        ('Data3', wintypes.WORD), ('Data4', ctypes.c_ubyte * 8)]

        u = uuid.UUID('{FDD39AD0-238F-46AF-ADB4-6C85480369C7}')  # FOLDERID_Documents
        g = _GUID(u.fields[0], u.fields[1], u.fields[2],
                  (ctypes.c_ubyte * 8).from_buffer_copy(u.bytes[8:]))
        out = ctypes.c_wchar_p()
        hr = ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None,
                                                        ctypes.byref(out))
        try:
            path = out.value if hr == 0 else None
        finally:
            ctypes.windll.ole32.CoTaskMemFree(out)
        return path or fallback
    except Exception:
        return fallback


def _default_projects_root():
    saved = _settings_read().get(PROJECTS_ROOT_KEY)
    if saved and os.path.isdir(saved):
        return saved
    docs = _documents_folder()
    return os.path.join(docs if os.path.isdir(docs) else os.path.expanduser('~'),
                        'OTDR Projects')


def _safe_folder_name(name):
    out = ''.join(c if (c.isalnum() or c in ' -_.()&') else '_' for c in str(name)).strip(' .')
    return out[:80] or 'New project'


def _write_new_project(work, site_a, site_b, job, shoots=None, final=None, dirs=None,
                       customer=None, sharepoint=None):
    path, exists = project_file_for_folder(work)
    ta, tb = dirs or (os.path.join(work_sub('traces', work), 'A'),
                      os.path.join(work_sub('traces', work), 'B'))
    snap = {'report_dest': work_sub('reports', work), 'manual': {}, 'fqa_job': job,
            'shoots': shoots or {}, 'final_shoot': final,
            'sharepoint': dict(sharepoint or {}),
            # The customer is the Splice Report profile; its tables are
            # derived from the profile when the project opens.
            'profile': customer,
            'spans': [{'mode': 'two', 'dir_a': ta, 'dir_b': tb, 'folder': '',
                       'site_a': site_a or '', 'site_b': site_b or ''}]}
    project_write(path, project_to_file_data(snap, path))
    return path


def _staged_setup_upload(upload):
    """An uploaded production sheet, on disk once (they run to 250 MB)."""
    key = (upload.name, upload.size)
    if st.session_state.get('_setup_upload_key') != key:
        d = tempfile.mkdtemp(prefix='otdr_setup_')
        path = os.path.join(d, os.path.basename(upload.name))
        with open(path, 'wb') as fh:
            fh.write(upload.getbuffer())
        st.session_state['_setup_upload_key'] = key
        st.session_state['_setup_upload_path'] = path
    return st.session_state.get('_setup_upload_path')


# ── Project packages (.zdb): one file to send a whole project ────
# Robert, 2026-09-24: "an export function so we can send an entire project
# to a tech via email".  A .zdb is a zip of the work folder (the
# project file already stores its folders relative to itself, so it opens on
# any PC) plus the share manifest saying what is inside.  Traces make a
# project big -- a 1152-fiber span is hundreds of MB a shoot, and mail stops
# near 20-25 MB -- so the export offers all shoots, the final shoot only, or
# no traces at all.
# Exports are .zdb, the share container (folder_intake.share_write, kind
# 'project'); the old .otdrproject packages always open (user decision).
PACKAGE_EXT = '.zdb'
LEGACY_PACKAGE_EXT = '.otdrproject'
LEGACY_PACKAGE_FORMAT = 'otdr-suite-project-package'
OPEN_FILE_EXTS = ('.zdb', '.zfc', LEGACY_PACKAGE_EXT)
EMAIL_LIMIT_BYTES = 20 * 1024 * 1024
EXPORT_MODES = {'none': 'Without Traces',
                'final': 'With Final Traces',
                'all': 'With All Traces'}


def _export_files(work, mode):
    """[(absolute path, name inside the package)] for an export."""
    work = os.path.abspath(work)
    name = os.path.basename(work)
    traces = os.path.abspath(work_sub('traces', work))
    keep = None
    if mode == 'final':
        fs = final_shoot(work)
        keep = os.path.abspath(fs['dir']) if fs else None
    out = []
    for root, dirs, files in os.walk(work):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        r = os.path.abspath(root)
        in_traces = r == traces or r.startswith(traces + os.sep)
        for f in files:
            if f.startswith(('.', '~$')) or f.lower().endswith((PACKAGE_EXT, LEGACY_PACKAGE_EXT)):
                continue
            p = os.path.join(r, f)
            if in_traces:
                if mode == 'none':
                    continue
                if mode == 'final':
                    if keep is None:
                        continue
                    # The final shoot's folder only (a legacy Traces/A,B shoot
                    # is Traces itself: just its A and B).
                    ok = (p.startswith(keep + os.sep) if keep != traces else
                          os.path.dirname(p) in (os.path.join(traces, 'A'), os.path.join(traces, 'B')))
                    if not ok:
                        continue
            out.append((p, os.path.join(name, os.path.relpath(p, work)).replace(os.sep, '/')))
    return out


def export_size(work, mode):
    return sum(os.path.getsize(p) for p, _a in _export_files(work, mode))


def export_project(work, mode, dest_dir):
    """Write <dest_dir>/<project>.zdb (the share container in folder_intake,
    kind 'project'); returns its path."""
    import folder_intake as fi
    work = os.path.abspath(work)
    name = os.path.basename(work)
    os.makedirs(dest_dir, exist_ok=True)
    stamp = time.strftime('%Y-%m-%d')
    base = f'{name} ({stamp}' + ('' if mode == 'all' else
                                 ', no traces' if mode == 'none' else ', final traces') + ')'
    path, k = os.path.join(dest_dir, base + PACKAGE_EXT), 2
    while os.path.exists(path):
        path = os.path.join(dest_dir, f'{base} {k}{PACKAGE_EXT}')
        k += 1
    files = {arc: p for p, arc in _export_files(work, mode)}
    meta = {'name': name, 'mode': mode, 'exported': time.strftime('%Y-%m-%d %H:%M:%S')}
    return str(fi.share_write(path, 'project', files, meta))


def _unique_project_dest(projects_root, raw_name):
    name = _safe_folder_name(raw_name or 'Project')
    dest, k = os.path.join(projects_root, name), 2
    while os.path.exists(dest):
        dest = os.path.join(projects_root, f'{name} ({k})')
        k += 1
    return os.path.abspath(dest)


def _unpack_members(z, members, root, label):
    """Write each (zip member, name) under root, dropping the packed folder's
    own name (the first path part).  Refuses anything outside root."""
    import shutil
    for info in members:
        parts = info.filename.replace('\\', '/').split('/')
        rel = '/'.join(parts[1:])
        target = os.path.abspath(os.path.join(root, *rel.split('/')))
        if not rel or '..' in rel.split('/') or not target.startswith(root + os.sep):
            raise ValueError(f'unsafe path in {label}: {info.filename}')
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with z.open(info) as src, open(target, 'wb') as out:
            shutil.copyfileobj(src, out)


def _unpack_project(z, members, root):
    """Unpack into root (new: _unique_project_dest never picks an existing
    folder) and check it holds a project file.  Any refusal removes root,
    so a bad package leaves nothing in the projects folder (a test once
    left "y", "y (2)" ... in the real Documents this way, 2026-09-27)."""
    import shutil
    try:
        _unpack_members(z, members, root, 'package')
        if not project_file_for_folder(root)[1]:
            raise ValueError('the package has no project file')
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise
    return root


def import_project(package, projects_root):
    """Unpack a project file into <projects_root>/<name> (made unique) and
    return the new work folder.  Reads the .zdb share file and, forever,
    the old .otdrproject package (user decision: old names always open).
    A .zdb that is newer, the wrong kind or damaged raises
    folder_intake.ShareFileError with the message meant for the tech;
    other refusals raise ValueError."""
    import zipfile
    import folder_intake as fi
    try:
        with zipfile.ZipFile(package) as z:
            names = z.namelist()
    except (zipfile.BadZipFile, OSError):
        names = []                                  # share_open words the error
    if fi.SHARE_MANIFEST in names or 'otdrproject.json' not in names:
        return _import_share_project(fi.share_open(package, expect='project'), projects_root)
    with zipfile.ZipFile(package) as z:
        meta = json.loads(z.read('otdrproject.json').decode('utf-8'))
        if meta.get('format') != LEGACY_PACKAGE_FORMAT:
            raise ValueError(f'not an {PRODUCT_NAME} project package')
        root = _unique_project_dest(projects_root, meta.get('name'))
        return _unpack_project(z, [i for i in z.infolist()
                                   if i.filename != 'otdrproject.json' and not i.is_dir()], root)


def _import_share_project(sf, projects_root):
    import zipfile
    import folder_intake as fi
    meta = (sf.manifest.get('meta') or {})
    first = sf.names[0].split('/')[0] if sf.names else ''
    root = _unique_project_dest(projects_root, meta.get('name') or first)
    with zipfile.ZipFile(sf.path) as z:
        return _unpack_project(z, [i for i in z.infolist()
                                   if i.filename != fi.SHARE_MANIFEST and not i.is_dir()], root)


def _share_open_project(sf):
    """share_dispatch handler for kind 'project': unpack it and queue the
    open (handled before drawing, like every open).  Returns the folder."""
    work = _import_share_project(sf, _default_projects_root())
    st.session_state['_setup_open'] = work
    return work


def _share_save_field_capture(sf):
    """share_dispatch handler for kind 'field-capture': the .zfc goes into the
    open project's Field folder.  No project open: ShareFileError telling the
    tech to open the project first."""
    import shutil
    import folder_intake as fi
    if not st.session_state.get('project_path'):
        raise fi.ShareFileError(f'{sf.path.name} is a Field Capture file. Open its project '
                                'first, then open the file again to add it to the project.')
    dest_dir = work_sub('field')
    os.makedirs(dest_dir, exist_ok=True)
    stem, ext = os.path.splitext(sf.path.name)
    dest, k = os.path.join(dest_dir, sf.path.name), 2
    while os.path.exists(dest):
        if os.path.getsize(dest) == os.path.getsize(sf.path) and \
                open(dest, 'rb').read() == open(sf.path, 'rb').read():
            return dest                            # already there
        dest = os.path.join(dest_dir, f'{stem} ({k}){ext}')
        k += 1
    shutil.copy2(sf.path, dest)
    return dest


def _register_share_openers():
    import folder_intake as fi
    fi.share_register('project', _share_open_project)
    fi.share_register('field-capture', _share_save_field_capture)


def open_share_file(path):
    """The one way into the hub for a .zdb (project) or .zfc (Field Capture)
    file, and old .otdrproject packages.  For the launcher's Windows
    double-click: call it inside a Streamlit run with the file's path.
    Returns (level, message) for st.<level>(message): 'success' or 'error'.
    A project is unpacked and opened on the next run (st.rerun() by the
    caller); a Field Capture file is copied into the open project's Field
    folder.  Errors never raise; the message is the one written for the tech."""
    import folder_intake as fi
    _register_share_openers()
    path = _clean_path(path) if isinstance(path, str) else path
    name = os.path.basename(str(path))
    try:
        if str(path).lower().endswith(LEGACY_PACKAGE_EXT):
            work = import_project(path, _default_projects_root())
            st.session_state['_setup_open'] = work
            return 'success', f'Opened the project {os.path.basename(work)}.'
        sf = fi.share_open(path)
        out = fi.share_dispatch(path)
    except fi.ShareFileError as exc:
        return 'error', str(exc)
    except Exception as exc:
        report_error('project: package import', exc, {})
        return 'error', f'Could not open {name}: {exc}'
    if sf.kind == 'project':
        return 'success', f'Opened the project {os.path.basename(out)}.'
    return 'success', f'Added {name} to the project\'s Field folder.'


# ── Double-click handoff: a path the launcher left (claimed near the top of
# the script) is opened here, once open_share_file exists; the rerun lets
# _mode_actions open a project before anything is drawn and shows the message.
_share_pending = st.session_state.pop('_share_open_pending', None)
if _share_pending:
    st.session_state['_share_open_msg'] = open_share_file(_share_pending)
    st.rerun()

EXPORT_DESTS_KEY = 'export_dests'
EXPORT_OTHER = '__other__'


def export_destinations(work):
    """[(label, folder)] for the Export box's dropdown: Downloads, Desktop,
    the project's folder, and folders exported to before (newest first).  Folders that do not exist on
    this machine are left out."""
    home = os.path.expanduser('~')
    out = []

    def add(label, path):
        if path and os.path.isdir(path) and all(
                os.path.normcase(os.path.abspath(path)) != os.path.normcase(os.path.abspath(p))
                for _l, p in out):
            out.append((label, path))

    add('Downloads', os.path.join(home, 'Downloads'))
    add('Desktop', os.path.join(home, 'Desktop'))
    add("This project's folder", work)
    for p in _settings_read().get(EXPORT_DESTS_KEY) or []:
        if isinstance(p, str):
            add(p, p)
    return out


def _remember_export_dest(path):
    rows = [p for p in (_settings_read().get(EXPORT_DESTS_KEY) or [])
            if isinstance(p, str) and os.path.normcase(p) != os.path.normcase(path)]
    _settings_update(**{EXPORT_DESTS_KEY: [path] + rows[:5]})


def _fmt_size(n):
    return f'{n / 1024 / 1024:.1f} MB' if n >= 1024 * 1024 else f'{max(1, n // 1024)} KB'


def _render_export(work):
    ss = st.session_state
    sizes = {m: export_size(work, m) for m in EXPORT_MODES}
    _bind('ps_export_mode', ss.get('ps_export_mode') or 'none', work)
    mode = st.radio('What to Include', list(EXPORT_MODES), key='ps_export_mode',
                    format_func=lambda m: f'{EXPORT_MODES[m]} · about {_fmt_size(sizes[m])}')
    dests = export_destinations(work)
    paths = [p for _l, p in dests] + [EXPORT_OTHER]
    labels = {p: l for l, p in dests}
    labels[EXPORT_OTHER] = 'Choose another folder…'
    last = (_settings_read().get(EXPORT_DESTS_KEY) or [None])[0]
    _bind('ps_export_where', last if last in paths else paths[0], (work, tuple(paths)))
    if ss.get('ps_export_where') not in paths:
        ss['ps_export_where'] = paths[0]
    where = st.selectbox('Export To', paths, key='ps_export_where',
                         format_func=lambda p: labels.get(p, p))
    if where == EXPORT_OTHER:
        import folder_intake as _fi
        dest = _report_dest_row('ps_export_dest', _fi.default_report_dir())
    else:
        dest = where
        st.caption(f'`{dest}`')
    if st.button('Export', key='ps_export', type='primary'):
        try:
            with st.spinner('Packing the project…'):
                ss['_exported'] = export_project(work, mode, dest)
            project_log(work, 'Project', f"Exported {os.path.basename(ss['_exported'])} "
                        f"({EXPORT_MODES[mode].lower()}) to {dest}")
            _remember_export_dest(os.path.abspath(dest))
        except Exception as exc:
            report_error('project: export', exc, {'mode': mode})
            st.error(f'Could not export: {exc}')
    out = ss.get('_exported')
    if out and os.path.isfile(out):
        size = os.path.getsize(out)
        st.success(f'Exported `{out}` ({_fmt_size(size)}).')
        if size <= EMAIL_LIMIT_BYTES:
            if st.button('✉️ Email It', key='ps_export_email'):
                try:
                    from fieldcapture.email_draft import write_draft, open_with_default_app
                    eml = write_draft(out, '', f'{PRODUCT_NAME} project: {os.path.basename(work)}',
                                      f'The project is attached. In {PRODUCT_NAME}: Home, '
                                      'Open Recent Project, Open this package.\n')
                    opened, err = open_with_default_app(eml)
                    st.success('An email with the project attached is open in your mail '
                               'program.' if opened else f'Wrote {eml} ({err}).')
                except Exception as exc:
                    report_error('project: export email', exc, {})
                    st.error(f'Could not write the email: {exc}')
        else:
            st.info('Too big for most email. Upload it to SharePoint and send the link '
                    'instead, or export it without traces.')


def _render_open_project():
    """Open a Recent Project: the recent list, newest first, and a way to
    open any other work folder.  The clicks are handled before drawing
    (_mode_actions), like every open."""
    ss = st.session_state
    st.markdown('## Open a Project')
    rec = recent_projects()[:PROJECT_RECENT_MAX]
    with st.container(border=True):
        st.markdown('**Recent Projects**')
        if not rec:
            st.caption('None yet. Projects you create or open are listed here.')
        for i, p in enumerate(rec):
            work = os.path.dirname(p)
            try:
                when = _when_text(os.path.getmtime(p))
            except OSError:
                when = ''
            c1, c2 = st.columns([3, 1])
            c1.markdown(f'**📁 {os.path.basename(work)}**  \n'
                        f'<span style="font-size:0.85em;opacity:0.7">{work}'
                        f'{" · saved " + when if when else ""}</span>',
                        unsafe_allow_html=True)
            c2.button('Open', key=f'home_recent_{i}', use_container_width=True)
    with st.container(border=True):
        st.markdown('**Open a .zdb or .zfc File**')
        st.caption('A project (.zdb, or an older .otdrproject) someone sent you, or a Field '
                   'Capture (.zfc) for the open project.')
        c1, c2 = st.columns([1, 2])
        if c1.button('📦 Choose the File', key='open_pkg_pick', use_container_width=True):
            p = pick_file('Choose the file', [(f'{PRODUCT_NAME} file',
                                               ' '.join('*' + e for e in OPEN_FILE_EXTS))])
            if p:
                ss['open_pkg_path'] = p
        c2.text_input('File Path', key='open_pkg_path', label_visibility='collapsed',
                      placeholder='…or Paste Its Path')
        up = st.file_uploader('…or Drop It Here (Up to 200 MB)',
                              type=[e.lstrip('.') for e in OPEN_FILE_EXTS], key='open_pkg_up')
        st.caption(f'A project is unpacked into `{_default_projects_root()}` and opened.')
        src = _clean_path(ss.get('open_pkg_path'))
        if up is not None and not src:
            src = _staged_setup_upload(up)
        if st.button('Open This File', key='open_pkg', type='primary', disabled=not src):
            with st.spinner('Opening…'):
                level, msg = open_share_file(src)
            if level == 'error':
                st.error(msg)
            elif ss.get('_setup_open'):
                st.rerun()
            else:
                st.success(msg)
    with st.container(border=True):
        st.markdown('**☁️ From SharePoint**')
        _render_sp_open()
    with st.container(border=True):
        st.markdown('**Another Project Folder**')
        c1, c2 = st.columns([1, 2])
        c1.button('📁 Choose Its Work Folder', key='home_project', use_container_width=True)
        c2.text_input('Work Folder', key='home_folder', label_visibility='collapsed',
                      placeholder='…or Paste the Work Folder\'s Path')
        if ss.get('_home_need_path'):
            st.caption('No folder picker on this machine: paste the work folder\'s path.')
        st.button('Open This Folder', key='home_open_path')


def new_project(work, customer=None, sheet=None, traces=None, sharepoint=None):
    """One new project from a production sheet, traces, or both (Robert,
    2026-09-24: one Start Project; either is enough).  traces is
    (dir_a, dir_b, site_a, site_b).  The job form comes from the sheet when
    there is one; the traces fill what it leaves (site names, fiber count) --
    derive() never overwrites a value it is handed.  Returns the project file."""
    import datetime as _dt
    from fqa.job_facts import JobFacts, derive
    os.makedirs(work, exist_ok=True)
    job, sites, shoots, final, dirs = {}, ('', ''), {}, None, None
    if traces:
        dir_a, dir_b, site_a, site_b = traces
        date = sor_shot_date(dir_a) or sor_shot_date(dir_b) or _dt.date.today().isoformat()
        sid = new_shoot_folder(work, date)
        d = os.path.join(work_sub('traces', work), sid)
        copy_traces(dir_a, dir_b, os.path.join(d, 'A'), os.path.join(d, 'B'))
        ta, tb = os.path.join(d, 'A'), os.path.join(d, 'B')
        n = max(len(_trace_fibers(ta)), len(_trace_fibers(tb)))
        job = {'site_a': {'alias': site_a or None}, 'site_z': {'alias': site_b or None},
               'fiber_count': n or None}
        sites, shoots, final, dirs = (site_a, site_b), {sid: {'date': date, 'label': ''}}, sid, (ta, tb)
    if sheet:
        dest = _add_production_sheet(sheet, work)
        # The sheet's answers first; the traces only fill what it leaves.
        from_sheet = derive(_read_prod(dest))
        job = json.loads(derive(_read_prod(dest), JobFacts.from_dict(
            _fill_missing(json.loads(from_sheet.to_json()), job))).to_json())
        a = (job.get('site_a') or {}).get('alias') or sites[0]
        z = (job.get('site_z') or {}).get('alias') or sites[1]
        sites = (a, z)
    out = _write_new_project(work, sites[0], sites[1], job, shoots=shoots, final=final,
                             dirs=dirs, customer=customer, sharepoint=sharepoint)
    made = [os.path.join(work_sub('traces', work), sid) for sid in shoots]
    if sheet:
        made.append(dest)
    project_log(work, 'Project', 'Project created from '
                + ' and '.join(filter(None, ('the production sheet' if sheet else None,
                                             f"traces shot {shoots[final]['date']}"
                                             if traces else None)))
                + (f' · customer {customer}' if customer else ''), made)
    return out


def _fill_missing(primary, extra):
    """primary with extra's values where primary has none (nested dicts too)."""
    out = dict(primary)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _fill_missing(out[k], v)
        elif out.get(k) in (None, '') and v not in (None, ''):
            out[k] = v
    return out




# ─── SharePoint: ONE folder, through the person's own sign-in ────────────
# Robert, 2026-09-29 ("option 2 for the boss"): Quick Analysis can load a span
# straight from one SharePoint folder.  The folder is set once by pasting its
# link; the person signs in in a window of the App's own (see
# sharepoint_link.py); then they walk down from that folder, never above it,
# and Load This Folder copies its traces to this PC and loads them like any
# other folder.
SP_LINK_KEY = 'sharepoint_link'
SP_LIST_TTL_S = 120


def _sp_sign_in(link):
    """Open the sign-in window and wait for it.  (kind, message) for the page."""
    import sharepoint_link as spl
    cmd = ([sys.executable, spl.SIGNIN_ARG, link] if FROZEN
           else [sys.executable, os.path.abspath(spl.__file__), spl.SIGNIN_ARG, link])
    kw = {}
    if sys.platform == 'win32':
        kw['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    spl.write_status(False, 'The sign-in window did not start.')
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, **kw)
    except OSError as exc:
        return 'error', f'The sign-in window could not open ({exc}).'
    try:
        proc.wait(timeout=spl.SIGNIN_TIMEOUT_S + 30)
    except subprocess.TimeoutExpired:
        proc.kill()
        return 'warning', 'The sign-in window was open too long, so it was closed. Try again.'
    status, sess = spl.read_status(), spl.load_session()
    if status.get('ok') and sess and sess.get('link') == link:
        return 'success', f"Signed in as {sess.get('user') or sess.get('login')}."
    return 'warning', status.get('message') or 'The sign-in did not finish.'


def _sp_listing(client, path):
    """client.folder(path), kept for a couple of minutes: every click on this
    screen reruns the page, and SharePoint need not be asked each time."""
    cache = st.session_state.setdefault('_sp_cache', {})
    hit = cache.get(path)
    if hit and time.time() - hit[0] < SP_LIST_TTL_S:
        return hit[1]
    listing = client.folder(path)
    cache[path] = (time.time(), listing)
    return listing


def _sp_reset_browse():
    for k in ('sp_path', '_sp_cache', '_sp_confirm'):
        st.session_state.pop(k, None)


def _render_sp_link_form(link):
    import sharepoint_link as spl
    ss = st.session_state
    st.caption('Paste the link to the SharePoint folder: open the folder in SharePoint and '
               'copy the address bar. The App opens that folder and the folders inside it, '
               'nothing else.')
    if 'sp_link_input' not in ss:
        ss['sp_link_input'] = link
    st.text_input('SharePoint Folder Link', key='sp_link_input', label_visibility='collapsed',
                  placeholder='https://….sharepoint.com/…')
    c1, c2 = st.columns(2)
    if c1.button('Save Folder', key='sp_link_save', type='primary', use_container_width=True):
        new = (ss.get('sp_link_input') or '').strip()
        if not spl.is_sharepoint_link(new):
            st.error('That is not a SharePoint link. It starts with https:// and has '
                     '.sharepoint.com in it.')
            return
        if new != link:
            spl.clear_session()
            _sp_reset_browse()
        _settings_update(**{SP_LINK_KEY: new})
        if new != link:
            _sp_remember(new)
        ss.pop('sp_edit', None)
        st.rerun()
    if link and c2.button('Cancel', key='sp_link_cancel', use_container_width=True):
        ss.pop('sp_edit', None)
        st.rerun()


def _sp_load(spl, client, path, msg, target=None):
    """Copy the traces under `path` to this PC and load them.  True when the
    span loaded (the caller moves on to the tools).  With `target` (New
    Project), the copy goes into that page's One folder box instead."""
    ss = st.session_state
    with st.spinner('Looking through the folder…'):
        files = client.walk(path)
    if not files:
        msg.warning('No .sor, .json, .trc, .bdr or .zip files in this folder or the folders inside it.')
        return False
    total = sum(f['size'] for f in files)
    big = total > spl.BIG_BYTES or len(files) > spl.BIG_FILES
    if big and (ss.get('_sp_confirm') or {}).get('ok') != path:
        ss['_sp_confirm'] = {'path': path, 'n': len(files), 'bytes': total}
        st.rerun()
    ss.pop('_sp_confirm', None)
    dest = spl.local_folder(path)
    bar = st.progress(0.0, text='Downloading…')
    shown = [0.0]

    def progress(done, whole, name):
        now = time.time()
        if now - shown[0] > 0.3 or done >= whole:
            shown[0] = now
            bar.progress(min(1.0, done / whole) if whole else 1.0,
                         text=f'Downloading {name} · {_fmt_size(done)} of {_fmt_size(whole)}')
    spl.fetch(client, files, dest, progress)
    bar.empty()
    if target:
        # The box is already drawn this run: filled at the top of the next.
        ss['_sp_target_fill'] = (target, dest)
        return True
    with st.spinner('Loading the traces…'):
        return _load_span(dest, None, out=msg)


def _render_sp_browser(spl, sess, target=None):
    import hashlib
    ss = st.session_state
    msg = st.container()
    client = spl.Client(sess)
    root = client.root
    path = ss.get('sp_path') or root
    if not spl.inside(path, root):
        path = root
    try:
        listing = _sp_listing(client, path)
    except spl.NeedsSignIn as exc:
        spl.clear_session()
        _sp_reset_browse()
        ss['_sp_msg'] = ('warning', str(exc))
        st.rerun()
    except spl.SharePointError as exc:
        st.error(str(exc))
        if path != root and st.button('Back to the Top Folder', key='sp_top'):
            _sp_reset_browse()
            st.rerun()
        return
    _sp_remember(sess.get('link') or _sp_link(), path)
    trail = spl.crumbs(path, root)
    st.markdown('📂 ' + ' › '.join(f'**{n}**' if p == path else n for n, p in trail))
    c2, c3 = st.columns(2)
    if c2.button('⬆ Up', key='sp_up', disabled=len(trail) < 2, use_container_width=True):
        ss['sp_path'] = trail[-2][1]
        st.rerun()
    if c3.button('🔄 Refresh', key='sp_refresh', use_container_width=True):
        ss.pop('_sp_cache', None)
        st.rerun()
    if listing['folders']:
        for d in listing['folders']:
            key = 'sp_dir_' + hashlib.sha1(d['path'].lower().encode('utf-8')).hexdigest()[:10]
            if st.button(f"📁 {d['name']}", key=key, use_container_width=True):
                ss['sp_path'] = d['path']
                st.rerun()
    here = [f for f in listing['files'] if f['name'].lower().endswith(spl.TRACE_EXTS)]
    st.caption(f"{len(listing['folders'])} folder(s) and {len(here)} trace file(s) here. "
               'Load This Folder takes the traces here and in every folder inside it.')
    conf = ss.get('_sp_confirm')
    if conf and conf.get('path') == path and 'ok' not in conf:
        st.warning(f"This folder holds {conf['n']} trace files, {_fmt_size(conf['bytes'])} in "
                   'all. Download them all to this PC?')
        b1, b2 = st.columns([1.3, 1])
        if b1.button('Download and Load', key='sp_big_ok', type='primary',
                     use_container_width=True):
            conf['ok'] = path
            _sp_try_load(spl, client, path, msg, target)
        if b2.button('Cancel', key='sp_big_no', use_container_width=True):
            ss.pop('_sp_confirm', None)
            st.rerun()
    elif st.button('⬇ Load This Folder', key='sp_load', type='primary',
                   disabled=not (listing['folders'] or here)):
        _sp_try_load(spl, client, path, msg, target)
    who = sess.get('user') or sess.get('login') or 'you'
    st.caption(f'Signed in as {who}.')
    c2, c3 = st.columns(2)
    if c2.button('Change Folder', key='sp_edit_btn', use_container_width=True):
        ss['sp_edit'] = True
        st.rerun()
    if c3.button('Sign Out', key='sp_signout', use_container_width=True):
        spl.forget_signin()
        _sp_reset_browse()
        st.rerun()


def _sp_try_load(spl, client, path, msg, target=None):
    """_sp_load, then on to the tools; a sign-in that ran out mid-way goes
    back to the Sign In button, anything else is said in `msg`."""
    ss = st.session_state
    try:
        ok = _sp_load(spl, client, path, msg, target)
    except spl.NeedsSignIn as exc:
        spl.clear_session()
        _sp_reset_browse()
        ss['_sp_msg'] = ('warning', str(exc))
        st.rerun()
    except spl.SharePointError as exc:
        msg.error(str(exc))
        return
    if ok:
        st.rerun()


def _sp_where_key():
    """Where this screen keeps its SharePoint folder: the open project's
    file, the New Project page (written into the project it makes), or
    nowhere (Quick Analysis uses the saved link)."""
    ss = st.session_state
    if ss.get('app_mode') == 'setup':
        return '_setup_sp'
    if ss.get('app_mode') == 'project' and ss.get('project_path'):
        return 'project_sp'
    return None


def _sp_link():
    """The folder link: the project's own when it has one, else the one
    saved on this PC."""
    key = _sp_where_key()
    own = (st.session_state.get(key) or {}).get('link') if key else ''
    return own or _settings_read().get(SP_LINK_KEY) or ''


def _sp_remember(link, path=None):
    """Note the folder in the project (or the project being made)."""
    key = _sp_where_key()
    if not key:
        return
    ss = st.session_state
    old = dict(ss.get(key) or {})
    # Another folder link: the folders noted under the old one go.
    new = dict(old if old.get('link') == link else {}, link=link, **({'path': path} if path else {}))
    if not path:
        new.pop('path', None)
    if old != new:
        ss[key] = new


def _render_sharepoint_box(target=None):
    """Quick Analysis: a span straight from the one SharePoint folder.  Drawn
    in the left panel's From SharePoint section, under the A and B boxes.
    New Project draws the same box in its Traces step, with `target` the
    session key its download fills."""
    import sharepoint_link as spl
    ss = st.session_state
    link = _sp_link()
    with st.container():
        note = ss.pop('_sp_msg', None)
        if note:
            getattr(st, note[0])(note[1])
        if not link or ss.get('sp_edit'):
            _render_sp_link_form(link)
            return
        sess = spl.load_session()
        if not sess or sess.get('link') != link:
            st.caption('Sign in with your work Microsoft account. A window opens, and it '
                       'closes by itself once you are in.')
            c1, c2 = st.columns([1.3, 1])
            if c1.button('Sign In to SharePoint', key='sp_signin', type='primary',
                         use_container_width=True):
                with st.spinner('Waiting for the sign-in window…'):
                    ss['_sp_msg'] = _sp_sign_in(link)
                _sp_reset_browse()
                st.rerun()
            if c2.button('Change Folder', key='sp_edit_btn', use_container_width=True):
                ss['sp_edit'] = True
                st.rerun()
            return
        _render_sp_browser(spl, sess, target)


# ── Saving to and opening from SharePoint (Robert, 2026-09-30: "I don't
# want to use OneDrive. I want to use the Microsoft login") ───────────────
# A project goes up as its .zdb into the one folder (or a folder inside it),
# and a .zdb there opens straight from Open a Project.  Their own widget keys
# (kp), since the Export tab draws in the same run as the Traces tab's box.
def _sp_ready(kp):
    """The sign-in for these boxes, or None after drawing what is missing
    (the folder link, or the Sign In button)."""
    import sharepoint_link as spl
    ss = st.session_state
    note = ss.pop(f'_{kp}_msg', None)
    if note:
        getattr(st, note[0])(note[1])
    link = _sp_link()
    if not link:
        st.caption('No SharePoint folder yet. Set it once in From SharePoint (the left '
                   'panel in Quick Analysis, or the Traces tab in a project).')
        return None
    sess = spl.load_session()
    if not sess or sess.get('link') != link:
        st.caption('Sign in with your work Microsoft account. A window opens, and it '
                   'closes by itself once you are in.')
        if st.button('Sign In to SharePoint', key=f'{kp}_signin', type='primary'):
            with st.spinner('Waiting for the sign-in window…'):
                ss[f'_{kp}_msg'] = _sp_sign_in(link)
            ss.pop('_sp_cache', None)
            st.rerun()
        return None
    return sess


def _sp_pick_folder(spl, sess, kp, start=None):
    """Walk the one folder's folders (never above it).  (client, listing) of
    the folder shown, or None when SharePoint refused (said here)."""
    import hashlib
    ss = st.session_state
    client = spl.Client(sess)
    root = client.root
    path = ss.get(f'{kp}_path') or start or root
    if not spl.inside(path, root):
        path = root
    try:
        listing = _sp_listing(client, path)
    except spl.NeedsSignIn as exc:
        spl.clear_session()
        ss.pop('_sp_cache', None)
        ss[f'_{kp}_msg'] = ('warning', str(exc))
        st.rerun()
    except spl.SharePointError as exc:
        st.error(str(exc))
        if path != root and st.button('Back to the Top Folder', key=f'{kp}_top'):
            ss[f'{kp}_path'] = root
            st.rerun()
        return None
    ss[f'{kp}_path'] = path
    trail = spl.crumbs(path, root)
    st.markdown('📂 ' + ' › '.join(f'**{n}**' if p == path else n for n, p in trail))
    c1, c2 = st.columns(2)
    if c1.button('⬆ Up', key=f'{kp}_up', disabled=len(trail) < 2, use_container_width=True):
        ss[f'{kp}_path'] = trail[-2][1]
        st.rerun()
    if c2.button('🔄 Refresh', key=f'{kp}_refresh', use_container_width=True):
        ss.pop('_sp_cache', None)
        st.rerun()
    for d in listing['folders']:
        key = f'{kp}_dir_' + hashlib.sha1(d['path'].lower().encode('utf-8')).hexdigest()[:10]
        if st.button(f"📁 {d['name']}", key=key, use_container_width=True):
            ss[f'{kp}_path'] = d['path']
            st.rerun()
    return client, listing


def save_project_to_sharepoint(client, work, mode, folder, progress=None):
    """Pack the project (export_project) and put the .zdb into `folder` on
    SharePoint under a name not taken there.  Returns its SharePoint path."""
    import shutil
    tmp = tempfile.mkdtemp(prefix='otdr-sp-save-')
    try:
        local = export_project(work, mode, tmp)
        name = client.free_name(folder, os.path.basename(local))
        return client.upload(local, folder, name, progress)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _render_sp_save(work):
    """The Export tab's Save to SharePoint: the same .zdb as Export, put in
    the SharePoint folder picked here (first the one it was saved to last,
    else the project's traces folder)."""
    import sharepoint_link as spl
    ss = st.session_state
    sess = _sp_ready('spx')
    if not sess:
        return
    mine = ss.get('project_sp') or {}
    got = _sp_pick_folder(spl, sess, 'spx', mine.get('save') or mine.get('path'))
    if not got:
        return
    client, listing = got
    mode = ss.get('ps_export_mode') or 'none'
    st.caption(f'Saves the project {EXPORT_MODES[mode].lower()} (the choice above) into the '
               'folder shown, as a new .zdb file. Nothing already there is replaced.')
    if st.button('☁️ Save to SharePoint Here', key='spx_save', type='primary'):
        bar = st.progress(0.0, text='Packing the project…')
        whole = max(1, export_size(work, mode))
        done = [0]

        def progress(n):
            done[0] += n
            bar.progress(min(1.0, done[0] / whole),
                         text=f'Uploading · {_fmt_size(done[0])}')
        try:
            path = save_project_to_sharepoint(client, work, mode, listing['path'], progress)
        except spl.NeedsSignIn as exc:
            spl.clear_session()
            ss['_spx_msg'] = ('warning', str(exc))
            st.rerun()
        except spl.SharePointError as exc:
            bar.empty()
            st.error(str(exc))
            return
        except Exception as exc:
            bar.empty()
            report_error('project: save to SharePoint', exc, {'mode': mode})
            st.error(f'Could not save to SharePoint: {exc}')
            return
        bar.empty()
        ss.get('_sp_cache', {}).pop(listing['path'], None)
        ss['project_sp'] = dict(mine, link=mine.get('link') or _sp_link(),
                                save=listing['path'])
        where = ' › '.join(n for n, _p in spl.crumbs(listing['path'], client.root))
        project_log(work, 'Project', f"Saved {path.rsplit('/', 1)[-1]} "
                    f"({EXPORT_MODES[mode].lower()}) to SharePoint: {where}")
        ss['_spx_msg'] = ('success', f"Saved **{path.rsplit('/', 1)[-1]}** to SharePoint "
                          f"({where}).")
        st.rerun()
    who = sess.get('user') or sess.get('login') or 'you'
    st.caption(f'Signed in as {who}.')


def _render_sp_open():
    """Open a Project: a .zdb (or .zfc) straight from the SharePoint folder."""
    import sharepoint_link as spl
    ss = st.session_state
    sess = _sp_ready('spo')
    if not sess:
        return
    got = _sp_pick_folder(spl, sess, 'spo')
    if not got:
        return
    client, listing = got
    files = [f for f in listing['files'] if f['name'].lower().endswith(OPEN_FILE_EXTS)]
    if not files:
        st.caption('No .zdb or .zfc files in this folder.')
    for i, f in enumerate(files):
        when = time.strftime('%Y-%m-%d %H:%M', time.localtime(f['modified'])) \
            if f.get('modified') else ''
        if st.button(f"📦 {f['name']} · {_fmt_size(f['size'])}"
                     + (f' · {when}' if when else ''), key=f'spo_file_{i}',
                     use_container_width=True):
            dest = os.path.join(spl.local_folder(listing['path']), spl._safe_name(f['name']))
            try:
                with st.spinner(f"Downloading {f['name']}…"):
                    client.download(f, dest)
            except spl.NeedsSignIn as exc:
                spl.clear_session()
                ss['_spo_msg'] = ('warning', str(exc))
                st.rerun()
            except spl.SharePointError as exc:
                st.error(str(exc))
                return
            with st.spinner('Opening…'):
                level, msg = open_share_file(dest)
            if level == 'error':
                st.error(msg)
            elif ss.get('_setup_open'):
                st.rerun()
            else:
                st.success(msg)


def page_project_setup():
    ss = st.session_state
    st.markdown('<style>[data-testid="stSidebar"],[data-testid="stSidebarCollapsedControl"]'
                '{display:none}</style>', unsafe_allow_html=True)
    kind = ss.get('setup_kind') or 'new'
    if kind == 'demo':
        ss['setup_kind'] = 'new'
        try:
            with st.spinner('Setting up the sample span…'):
                ss['_setup_open'] = demo_project()
        except Exception as exc:
            report_error('home: demo span', exc, {})
            ss.pop('app_mode', None)
            ss['_share_open_msg'] = ('error', f'Could not open the sample span: {exc}')
        st.rerun()
    st.button('← Back', key='setup_back')
    if ss.get('_setup_msg'):
        msg = ss.pop('_setup_msg')
        getattr(st, msg[0])(msg[1])
    if kind == 'open':
        _render_open_project()
        return
    st.markdown('## New Project')
    st.caption('A production sheet, traces, or both. The project fills in everything it can '
               'from what you load.')

    # Boxes in display order; the sources are read first because the name
    # follows what was read from them.
    box_project = st.container(border=True)
    box_sheet = st.container(border=True)
    box_traces = st.container(border=True)
    box_customer = st.container(border=True)

    sheet_src, sheet_name = None, ''
    with box_sheet:
        st.markdown('**2 · The Production Sheet** (optional)')
        st.caption('The span\'s ZeroDB production sheet. It is copied into the project.')
        c1, c2 = st.columns([1, 2])
        if c1.button('📄 Choose Production Sheet', key='setup_prod_pick',
                     use_container_width=True):
            p = pick_file('Choose the production sheet', [('Excel', '*.xlsx *.xlsm')])
            if p:
                ss['setup_prod_path'] = p
            elif p is None:
                st.caption('No file picker here: paste the path, or drop the file.')
        c2.text_input('Production Sheet Path', key='setup_prod_path',
                      label_visibility='collapsed',
                      placeholder='…or Paste the Production Sheet\'s Path')
        up = st.file_uploader('…or Drop It Here (Up to 200 MB; Paste the Path for '
                              'Bigger Sheets)', type=['xlsx', 'xlsm'], key='setup_prod_up')
        src = _clean_path(ss.get('setup_prod_path'))
        if not src and up is not None:
            src = _staged_setup_upload(up)
        if src and os.path.isfile(src):
            n_loc, n_spl, warns = _production_summary(src)
            if n_loc is None:
                st.error('Could not read it: ' + '; '.join(warns))
            else:
                from fqa.job_facts import derive
                job = derive(_read_prod(src))
                a, z = job.site_a.alias or '', job.site_z.alias or ''
                st.success(f"Read: **{a or 'A'} → {z or 'Z'}** · {n_loc} locations, "
                           f"{n_spl} splices"
                           + (f" · {job.fiber_count} fibers" if job.fiber_count else ''))
                for w in warns:
                    st.caption('⚠ ' + w)
                base = os.path.splitext(os.path.basename(src))[0]
                sheet_name = (f'{a} to {z}' if a and z
                              else base.replace('Production Sheet', '').strip(' -_'))
                sheet_src = src
        elif src:
            st.error(f'No file at {src}')

    traces_src, traces_name = None, ''
    fill = ss.pop('_sp_target_fill', None)
    if fill and fill[0] == 'setup_tr_one':
        # A SharePoint download on the last run: it is the one folder now.
        ss['setup_tr_one'] = fill[1]
        ss.pop('setup_tr_a', None)
        ss.pop('setup_tr_b', None)
    with box_traces:
        st.markdown('**3 · The Traces** (optional)')
        st.caption('Two folders (A and B), one folder holding both directions, or drop '
                   'them. They are copied into the project as its first shoot.')
        c1, c2, c3 = st.columns(3)
        for col, key, label in ((c1, 'setup_tr_a', 'A-Direction Folder'),
                                (c2, 'setup_tr_b', 'B-Direction Folder'),
                                (c3, 'setup_tr_one', 'One Folder, Both Directions')):
            with col:
                if st.button('📂 ' + label, key=key + '_pick', use_container_width=True):
                    p = pick_folder('Choose the ' + label)
                    if p:
                        ss[key] = p
                    elif p is None:
                        st.caption('No folder picker here: paste the path.')
                st.text_input(label, key=key, label_visibility='collapsed',
                              placeholder='Or Paste a Path')
        drop = st.file_uploader('…or Drop the Traces: a .zip, Loose Files, or .bdr',
                                type=['zip', 'sor', 'json', 'bdr'],
                                accept_multiple_files=True, key='setup_tr_drop')
        # The same SharePoint folder as Quick Analysis's left panel (the panel
        # is hidden on this page).
        with st.expander('☁️ From SharePoint', expanded=False):
            _render_sharepoint_box(target='setup_tr_one')
        a, b = _clean_path(ss.get('setup_tr_a')), _clean_path(ss.get('setup_tr_b'))
        one = _clean_path(ss.get('setup_tr_one'))
        if not (a and b) and (one or drop):
            a, b = _resolve_bidir_from_single(one, drop)
        if a and b and os.path.isdir(a) and os.path.isdir(b):
            fa, fb = _trace_fibers(a), _trace_fibers(b)
            try:
                sa, sb = _site_names_for(a, b)
            except Exception as exc:
                sa, sb = '', ''
                report_error('new project: site names', exc, {})
                st.caption(f"Couldn't read the site names ({type(exc).__name__}); "
                           'type them in.')
            if fa or fb:
                st.success(f"Read: **{sa or 'A'} → {sb or 'B'}** · A {len(fa)} fibers, "
                           f"B {len(fb)} fibers")
                traces_name = f'{sa} to {sb}' if sa and sb else os.path.basename(a.rstrip('/\\'))
                traces_src = (a, b, sa, sb)
            else:
                st.warning('No trace files in those folders.')
        elif (a or b) and not (one or drop):
            # One direction on its own used to be dropped without a word, and
            # the project was created with no traces.
            st.warning(f"Only the {'A' if a else 'B'}-direction folder is filled in. A "
                       'project needs both directions: add the '
                       f"{'B' if a else 'A'}-direction folder, or use One folder, both "
                       'directions. Without them the project is created with no traces.')

    with box_customer:
        st.markdown('**4 · Customer**')
        st.selectbox('Customer', project_customers(), index=None, key='setup_customer',
                     placeholder='Select the Customer…', label_visibility='collapsed',
                     help='Sets the project\'s Splice Report customer profile. The FQA '
                          'checklist is Lumen\'s, the only FQA form set up so far.')
    customer = ss.get('setup_customer')

    proposal = sheet_name or traces_name
    with box_project:
        st.markdown('**1 · The Project**')
        # The name follows what was read until the tech types their own.
        if proposal and (not ss.get('setup_name') or ss.get('setup_name') == ss.get('_setup_auto')):
            ss['setup_name'] = proposal
            ss['_setup_auto'] = proposal
        ss.setdefault('setup_parent', _default_projects_root())
        n1, n2 = st.columns([1, 1])
        n1.text_input('Project Name', key='setup_name', placeholder='e.g. SITEA to SITEB')
        with n2:
            # Robert, 2026-09-30: no OneDrive-synced libraries here; a project
            # goes to SharePoint through the sign-in (Export Project tab).
            if st.button('📁 Save Projects In…', key='setup_parent_pick'):
                p = pick_folder('Where new projects are kept')
                if p:
                    ss['setup_parent'] = p
            st.text_input('Save Projects In', key='setup_parent', label_visibility='collapsed')
        name = _safe_folder_name(ss.get('setup_name') or '')
        parent = _clean_path(ss.get('setup_parent')) or _default_projects_root()
        work = os.path.join(parent, name)
        st.caption(f'Work folder: `{work}`')
        st.caption(PROJECT_LAYOUT_HELP)

    have_source = bool(sheet_src or traces_src)
    if not have_source:
        st.caption('Load a production sheet or traces (or both) to create the project.')
    if st.button('Create Project', key='setup_create', type='primary',
                 disabled=not (have_source and customer and (ss.get('setup_name') or '').strip())):
        if os.path.isdir(work) and project_file_for_folder(work)[1]:
            st.error('That folder is already a project. Open it from the home screen, or '
                     'pick another name.')
            return
        try:
            with st.spinner('Creating the project…'):
                new_project(work, customer=customer, sheet=sheet_src, traces=traces_src,
                            sharepoint=ss.get('_setup_sp'))
            _settings_update(**{PROJECTS_ROOT_KEY: parent})
        except Exception as exc:
            st.error(f'Could not create the project: {exc}')
            report_error('new project: create', exc, {})
            return
        for k in [k for k in ss.keys() if str(k).startswith(('setup_', '_setup_'))]:
            ss.pop(k, None)
        ss['_setup_open'] = work
        st.rerun()


# ─── Route ────────────────────────────────────────────────────────────────
# Global catch-all: any unhandled error during a page render/action posts to
# Slack, then re-raises so Streamlit still shows the tech its red error box.
_note_tool_change(page)
if page != 'Viewer':
    # The Viewer frame goes with the page, so the address it kept through a
    # drop (see page_viewer) has nothing left to keep.
    st.session_state.pop('_viewer_drop_q', None)
try:
    if _sp_section is not None and st.session_state.get('app_mode') != 'setup':
        # (New Project draws the box itself, in its Traces step.)
        with _sp_section:
            _render_sharepoint_box()
    _render_crumbs(_crumb_slot, page)
    _app_mode = st.session_state.get('app_mode')
    if _app_mode == 'setup':
        page_project_setup()
    elif page == 'Viewer':
        page_viewer()
    elif page == 'Splice Report':
        page_splice_report()
    elif page == 'Splice Report FEC':
        page_splice_report_fec()
    elif page == 'Viewer FEC':
        page_viewer(fec=True)
    elif page == 'Unidirectional':
        page_unidirectional()
    elif page == 'FQA Builder':
        page_fqa_builder()
    elif page == 'Field Capture':
        page_field_capture()
    elif page == 'Project Status':
        page_project_status()
    else:
        page_duplicate_check()
except Exception as _exc:
    report_error(f"hub page: {page}", _exc)
    raise
_after_page(page)


# Project mode saves itself: after the page has drawn, anything the tech
# changed (a site name, a profile, the job form) is written to the work
# folder's project file.  The run right after an open only settles: the page
# has filled in what the file left to it (a table re-derived from the
# profile, a knob a newer build added), and writing that back would touch a
# file nobody changed.
if _PROJECT_MODE:
    try:
        _ss = st.session_state
        _now = _project_snapshot(_ss, _ss.get('project_saved'))
        _rebase = _ss.pop('_project_rebase', False)
        if _rebase and not _ss.pop('_project_force_save', False):
            _ss['project_saved'] = _now
        elif _now != _ss.get('project_saved'):
            _pp = _ss['project_path']
            project_write(_pp, project_to_file_data(_now, _pp, _span_markers_for(_now)))
            _ss['project_saved'] = _now
        _ss.pop('_project_force_save', None)
    except Exception as _exc:
        report_error('project: autosave', _exc)

# ─── Sidebar footer: build identity + one-click update ────────────────────
# Rendered LAST so it sits at the bottom of the sidebar, below any page-
# specific widgets.  "app build N (date)" identifies the frozen exe (CI stamp);
# "engine: ..." identifies the code the launcher chose at boot (bundled vs a
# verified signed update) — so the boss can confirm a tech runs the latest of
# BOTH.  Dev runs collapse to a plain "dev".
# The build line and Check for Updates, pinned to the bottom of the sidebar
# at any window size (Robert, 2026-09-27): the block is pushed to the foot
# of the panel and stays in view when the panel scrolls.
st.sidebar.markdown(
    '<style>'
    '[data-testid="stSidebarUserContent"]{min-height:100%;display:flex;flex-direction:column}'
    '[data-testid="stSidebarUserContent"]>div{flex:1 0 auto;display:flex;flex-direction:column}'
    '[data-testid="stSidebarUserContent"]>div>[data-testid="stVerticalBlock"]{flex:1 0 auto}'
    # Streamlit wraps each block in a layout wrapper: that wrapper is the
    # flex item the column lays out, so it is the one pushed down.
    '[data-testid="stLayoutWrapper"]:has(>.st-key-sidebar_footer){margin-top:auto;'
    'position:sticky;bottom:0;z-index:5;background:var(--otdr-panel);'
    'padding:.5rem 0 .25rem;border-top:1px solid var(--otdr-rule)}'
    '</style>', unsafe_allow_html=True)
_sidebar_footer = st.sidebar.container(key='sidebar_footer')
# Light / Dark switch, on every page, above the build line.
_render_theme_control(_sidebar_footer)
_appv, _engv = _app_version(), _engine_version()
if _appv == 'dev' and _engv == 'dev':
    _sidebar_footer.caption('OTDR Suite · dev'.replace('OTDR Suite', PRODUCT_NAME))
else:
    _sidebar_footer.caption(f'OTDR Suite · app {_appv} · engine: {_engv}'
                            .replace('OTDR Suite', PRODUCT_NAME))


if os.environ.get('OTDR_SUITE_NO_UPDATE'):
    # This build never updates itself (see _latest_manifest): a Check button
    # could only ever say "could not reach the update server".
    _sidebar_footer.caption('Updates: install a newer build to update.')
elif _sidebar_footer.button('🔄 Check for Updates', key='upd_check',
                            use_container_width=True):
    st.session_state['upd_latest'] = _latest_manifest_version()
    st.session_state['upd_checked'] = True
if st.session_state.get('upd_checked'):
    _latest = st.session_state.get('upd_latest')
    _cur = _parse_engine_version(_appv, _engv)
    if _latest is None:
        st.sidebar.warning('Could not reach the update server. Check the '
                           'connection and try again.')
    elif _cur is not None and _latest <= _cur:
        st.sidebar.success(f'Up to date: engine {_cur} is the latest.')
    elif _cur is None:
        st.sidebar.info(f'Latest published update: {_latest} · running: dev '
                        'checkout (updates apply to installed builds only).')
    else:
        st.sidebar.info(f'Update {_latest} is available (running {_cur}).')
        if _cache_pinned():
            _render_cache_pinned_notice(sidebar=True)
        elif _needs_install():
            _render_install_notice(_latest, _cur, sidebar=True)
        elif getattr(sys, 'frozen', False):
            if st.sidebar.button('⬇ Update & Restart Now', key='upd_restart',
                                 type='primary', use_container_width=True):
                if _relaunch_and_exit():
                    _render_restart_watchdog(sidebar=True)
        else:
            st.sidebar.caption('Restart the app to apply. Updates install '
                               'at launch.')

# Rollout ping: when the build identity changed since the last run (the
# launcher applied a verified update, or a fresh install's first boot), tell
# the shared Slack channel — per-machine confirmation without footer-reading.
# Marker-deduped to once per version; silent no-op in dev / without a webhook.
try:
    maybe_report_update()
except Exception:
    pass

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
ANALYSIS_MODE_LABELS = {'suite': 'OTDR Suite', 'fr': 'FastReporter'}
ANALYSIS_MODE_DEFAULT = 'suite'


def _analysis_settings_path():
    """~/.otdrSuite/settings.json -- the launcher already owns that folder
    (engine.meta.json, the update log).  OTDR_SETTINGS_DIR overrides it for
    tests."""
    d = os.environ.get('OTDR_SETTINGS_DIR') or os.path.join(
        os.path.expanduser('~'), '.otdrSuite')
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
    # mode in use and that name is bold.  Knob right = OTDR Mode.
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
                     contract=None, show=None, viewer_table=None):
    """Argv to run the Splice Report engine in a clean subprocess (its own
    sor_reader copy).  Frozen: --run-splicereport sentinel; dev: the runner.

    `overrides` is the engine-global threshold dict from the OTDR settings
    panel (e.g. {'REBURN_THRESHOLD': 0.12, ...}).  It's serialized to JSON
    and forwarded as --overrides so the subprocess can apply it to the
    engine module BEFORE the pipeline runs (the panel lives in this process;
    the engine lives in the subprocess, so the values cross as JSON)."""
    common = ['--dir-a', dir_a, '--dir-b', dir_b, '--out', out_xlsx,
              '--site-a', site_a, '--site-b', site_b,
              '--analysis', analysis_mode()]
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
    body is not a manifest."""
    import urllib.request
    url = ('https://raw.githubusercontent.com/lakeosoyoos/otdr-suite/main/'
           'update_manifest.json')
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'OTDRSuite'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            manifest = json.loads(r.read().decode('utf-8'))
        int(manifest['version'])
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
    due, deadline = _update_due(running)
    if due:
        return ''
    return (f' Reports keep working until {_fmt_clock(deadline)}, then pause '
            'until OTDR Suite is updated.')


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
                st.error('Couldn\'t start the restart. Close OTDR Suite '
                         'completely and open it again to pick up the update.')
    else:
        st.caption('Restart the app to apply. Updates install at launch.')
    return stale


def _restart_marker_path():
    """Where the restart helper records 'the old instance never let go' — the
    app dir the launcher already owns (log + engine.meta.json live there)."""
    return os.path.join(os.path.expanduser('~'), '.otdrSuite',
                        'update_restart_blocked')


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
    target = st.sidebar if sidebar else st
    target.warning(
        'Updates cannot be kept on this computer. Files this app downloads '
        'keep disappearing, so OTDR Suite is running the copy that came with '
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
    target = st.sidebar if sidebar else st
    target.warning(
        f'Update {latest} needs a fresh install (running {running}). It adds '
        'files this copy of OTDR Suite cannot download on its own, so Update '
        '& restart will not apply it. Close OTDR Suite completely, then '
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
    if 'upd_restart_blocked' not in st.session_state:
        blocked = os.path.exists(_restart_marker_path())
        if blocked:
            try:
                os.remove(_restart_marker_path())   # once per failed attempt
            except OSError:
                pass
        st.session_state['upd_restart_blocked'] = blocked
    if st.session_state['upd_restart_blocked']:
        st.error('The update didn\'t start: the previous OTDR Suite is still '
                 'running. Close it completely (or reboot), then start OTDR '
                 'Suite again.')

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
                st.error('Couldn\'t start the restart. Close OTDR Suite and '
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
    return os.path.join(os.path.expanduser('~'), '.otdrSuite', 'repair_requested')


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
    st.set_page_config(page_title='OTDR Suite', layout='centered')
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
    st.set_page_config(page_title='OTDR Suite', layout='centered')
    st.title('OTDR Suite Needs to Repair Itself')
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
            st.error('Close OTDR Suite and open it again to finish the repair.')
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
            st.error('Close OTDR Suite and open it again to finish the repair.')
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

st.set_page_config(page_title='OTDR Suite', layout='wide',
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
# header) was a download.  So the hub page catches every file drop: on the
# Viewer page the files go on to the Viewer, which loads them as a drop on its
# FILES panel; on any other page the drop is refused (no download either).
# A file box of a page (st.file_uploader) still takes its own drops.
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
    var EXTS = ['.sor', '.json', '.trc', '.zip'];
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
    function walk(entry, out) {
      return new Promise(function(resolve){
        if (entry.isFile) {
          // with the folder it came from, so the Viewer names the side after it
          var parts = String(entry.fullPath || '').split('/').filter(Boolean);
          entry.file(function(f){ out.push({ f: f, dir: parts.length > 1 ? parts[parts.length - 2] : '' });
                                  resolve(); }, function(){ resolve(); });
        } else if (entry.isDirectory) {
          var rd = entry.createReader();
          var page = function(){
            rd.readEntries(function(ents){
              if (!ents.length) { resolve(); return; }
              ents.reduce(function(p, e){ return p.then(function(){ return walk(e, out); }); },
                          Promise.resolve()).then(page);
            }, function(){ resolve(); });
          };
          page();
        } else resolve();
      });
    }
    function collect(dt) {
      var items = dt.items ? Array.prototype.slice.call(dt.items) : [];
      var ents = items.map(function(i){ return i.webkitGetAsEntry && i.webkitGetAsEntry(); })
                      .filter(Boolean);
      var out = [];
      var done = ents.length
        ? ents.reduce(function(p, e){ return p.then(function(){ return walk(e, out); }); },
                      Promise.resolve())
        : Promise.resolve(Array.prototype.forEach.call(dt.files || [], function(f){
            out.push({ f: f, dir: '' }); }));
      return done.then(function(){
        return out.filter(function(o){
          var n = (o.f.name || '').toLowerCase();
          return n.charAt(0) !== '.' && EXTS.some(function(x){ return n.slice(-x.length) === x; });
        });
      });
    }
    var lastLit = 0;
    function onOver(ev) {
      if (ev.defaultPrevented || !isFiles(ev)) return;   // a file box's own
      ev.preventDefault();
      var fr = viewerFrame();
      ev.dataTransfer.dropEffect = fr ? 'copy' : 'none';
      // Light the Viewer's FILES panel ("Drop to load"), as a drag over the
      // Viewer itself does: the tech sees the drop will be taken.
      var now = Date.now();
      if (fr && fr.contentWindow && now - lastLit > 200) {
        lastLit = now;
        fr.contentWindow.postMessage({ type: 'otdr-drag' }, new URL(fr.getAttribute('src')).origin);
      }
    }
    function onDrop(ev) {
      if (ev.defaultPrevented || !isFiles(ev)) return;
      ev.preventDefault();
      var fr = viewerFrame();
      if (!fr) return;
      var origin = new URL(fr.getAttribute('src')).origin;
      collect(ev.dataTransfer).then(function(got){
        if (got.length && fr.contentWindow)
          fr.contentWindow.postMessage({ type: 'otdr-drop',
            files: got.map(function(o){ return o.f; }),
            folders: got.map(function(o){ return o.dir; }) }, origin);
      });
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
    d = os.environ.get('OTDR_CACHE_DIR') or os.path.join(os.path.expanduser('~'), '.otdrSuite', 'cache')
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
def pick_folder(title='Choose a folder'):
    """Native folder picker. Returns the chosen path, '' if the user cancelled,
    or None if the picker is UNAVAILABLE — Tcl/Tk isn't bundled in the frozen
    Windows .exe, so tk.Tk() raises and the button would otherwise do nothing
    silently.  Returning None lets the caller tell the tech to paste the path."""
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

    The box's text is also kept in a slot no widget owns (`{key}_saved`).
    Streamlit drops a widget's state on any run that does not draw it, so a
    trip to another tool emptied the box and the next report went to
    Downloads (2026-09-29).  A box Streamlit forgot is seeded from the slot
    before it is drawn.  Never value= as well: key + value on one widget is
    the trap in feedback_streamlit_widget_state."""
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
                     'sr_report_dest', 'uni_report_dest')
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
    if qp.get('nav') == 'viewer' and ('pa' in qp or 'pb' in qp):
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
            if _sra and os.path.isdir(_sra):
                st.session_state['uni_folder_input'] = _sra
            # With the left panel loaded the page runs on its A or its B
            # folder: the way back lands on the one the report ran on.
            if _pside == 'b':
                st.session_state['uni_panel_side'] = 'B folder'
        st.session_state['viewer_jump_announce'] = True   # one-shot caption
        st.session_state['nav_radio'] = 'Viewer'   # set BEFORE the radio widget
        st.query_params.clear()

_handle_nav()
# Streamlit's own theme pick (the ⋮ menu's Settings) beats the hub's Theme
# switch: once a browser has chosen Light or Dark there, Streamlit keeps it
# and ignores the theme the hub sends, so the switch does nothing.  ("Use
# system setting" removes Streamlit's entry instead, so it never blocks.)  The menu is hidden below; this clears a pick already made, once,
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


_install_sidebar_drag_fix()
_install_hub_drop_catch()
_install_theme_pick_clear()

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
    (/var/folders/.../T/otdr_viewer_drop_jnwvux7o/A, demo list #31).
    None for any other folder."""
    try:
        name = trace_server.drop_name(path)
    except Exception:
        return None
    if not name:
        return None
    return name if name.startswith('Dropped files') else f'{name} (dropped)'


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
    dir_a, dir_b, _notes = _panel_dirs()
    return tuple(_d if _d and os.path.isdir(_d) else '' for _d in (dir_a, dir_b))


def _show_panel_notes():
    """What _panel_dirs had to say about the left panel's boxes (a .zip read,
    a folder split into its two directions, a box it could not use), on a
    report page that runs on them."""
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
                 ss.get('uni_folder_input'), *_panel_boxes()]
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
    if _c2.button('Allow', key='clear_traces_allow', type='primary',
                  use_container_width=True, on_click=_allow_clear_traces):
        st.rerun()


@st.dialog('Clear Report')
def _confirm_clear_report(which):
    st.markdown('**Clear Report Only** removes this report. The traces stay '
                'loaded and the same folder will need a fresh run.')
    st.markdown('**Clear Report and Traces** also clears the traces from the '
                'left panel and from every tool, with their reports.')
    st.caption('Report files already saved to a folder are not deleted.')
    if st.button('Skip', key='clear_report_skip', use_container_width=True):
        st.rerun()
    if st.button('Clear Report Only', key='clear_report_only',
                 use_container_width=True, on_click=_drop_report, args=(which,)):
        st.rerun()
    if st.button('Clear Report and Traces', key='clear_report_and_traces',
                 use_container_width=True, on_click=_allow_clear_traces):
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
    '<div style="background:#e7f5ea;border:1px solid #9fd3aa;'
    'border-radius:6px;padding:8px 12px;font-size:1rem;color:#14532d">'
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
st.session_state.setdefault('nav_radio', 'Viewer')
with st.sidebar:
    st.markdown('## 🔬 OTDR Suite')

    # Update nudge FIRST — above the tools, so a stale always-on machine sees
    # it before it starts working (the footer's manual check is still there).
    _render_update_nudge()

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

    def _trace_folders_changed():
        # A new span invalidates the previous deep-link target and report
        # grids, exactly as the old span loader did: a stale click would
        # re-fire against the new folders.
        for _k in ('viewer_target', 'sr_result', 'sr_dirs', 'uni_result',
                   'sr_site_src'):
            st.session_state.pop(_k, None)
        st.session_state['sr_input_mode'] = 'Two folders (A + B)'
        st.session_state.pop('sr_input_mode_saved', None)     # _seed_box

    # The tech pressed Allow, or Clear Report and Traces, in a pop-up (see
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
            and st.session_state.get('nav_radio') != 'Viewer'):
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

    # Asks first (the pop-up is drawn below the sidebar): a click here clears
    # nothing until the tech presses Allow.
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

    st.markdown('##### Select Tool')
    # FQA Builder and Field Capture belong to OTDR Suite App (Robert
    # 2026-09-28): the regular Suite lists the four trace tools.  The App's
    # launcher exports OTDR_SUITE_EDITION; this one does not.  The two pages
    # and their files stay in the tree, so an update's file set is unchanged.
    _tools = ['Viewer', 'Splice Report', 'Unidirectional', 'Secret Sauce']
    if os.environ.get('OTDR_SUITE_EDITION'):
        _tools += ['FQA Builder', 'Field Capture']
    page = st.radio('Tool', _tools,
                    key='nav_radio', label_visibility='collapsed')
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


def page_viewer():
    port = ensure_trace_server()

    with st.sidebar:
        # The A/B folder boxes are the sidebar's Trace Folders loader, drawn
        # on every page above the tool list; the Viewer reads the same slots.
        # A drop on the Viewer's own FILES panel has already reached them:
        # the Trace Folders block checks CONFIG['dropped_at'] on every page.

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
    if st.session_state.get('came_from_uni'):
        def _back_to_uni():
            st.session_state['came_from_uni'] = False
            st.session_state['nav_radio'] = 'Unidirectional'
        st.button('← Back to Unidirectional', key='view_back_uni',
                  on_click=_back_to_uni)

    st.markdown('#### Trace Viewer')
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
    with st.container():
        _render_profile_picker_box('viewer')
        _viewer_box_exc = _render_settings_box('viewer')
        if _viewer_box_exc is None and trace_server.settings_differ_from_report():
            st.caption('Pass/fail in the Viewer follows the Splice Report on '
                       'screen, at the settings it ran with. Generate the report '
                       'again to judge by the settings above.')
    # Pop the Viewer into its own window from HERE too — a tech who came to
    # the Viewer page first (rather than clicking a report cell) had no way
    # to detach it.  Same window NAME as the report grids' button, so the two
    # entry points share ONE window: opening from here and then clicking
    # report cells drives this same window instead of spawning a second.
    _pop_doc = """
<button id="vpop2" style="padding:4px 10px;border:1px solid #c9d5e1;border-radius:4px;
    background:#eef3f8;cursor:pointer;font-weight:600;color:#000000;
    font-family:sans-serif;font-size:13px">&#8862; Open Viewer in Its Own Window</button>
<span style="margin-left:8px;font-size:11px;color:#000000;font-family:sans-serif">
    keeps this page free for the report &middot; report cell clicks drive the same window</span>
<script>
try { window.top.name = "otdr_hub"; } catch (e) {}
document.getElementById("vpop2").addEventListener("click", function(){
  var w = window.open("__ORIGIN__/", "otdr_viewer", "width=1400,height=900");
  if (w) w.focus();
});
</script>
""".replace('__ORIGIN__', f'http://127.0.0.1:{port}')
    st_components_html(theme_recolor(_pop_doc), height=42)
    # The note above the frame keeps ONE slot whether it shows or not:
    # Streamlit places the frame by its position on the page, so this note
    # going away after the first drop moved the frame up a place and rebuilt
    # it, and the Viewer lost the traces it had just loaded.
    _note = st.empty()
    if not dir_a and not dir_b:
        _note.info('Pick an A and/or B folder of OTDR `.sor` / `.json` / `.trc` files in the '
                'sidebar, then type fiber numbers in the viewer to plot them.')
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

    _pa, _pb = _panel_traces()
    _dropped = None
    if _pa or _pb:
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
                folder, _renamed = _take_panel_ss_folder(_pa, _pb)
            except Exception as _exc:
                report_error('secret sauce: A and B folder', _exc,
                             {'dir_a': _pa, 'dir_b': _pb})
                st.warning('The A and B folders could not be read just now '
                           f'({type(_exc).__name__}: {_exc}). If files are '
                           'still being copied in, try again when that is done.')
                return
        else:
            folder = _pa or _pb
        st.caption('Traces: ' + ('the A and B folders' if _pa and _pb
                                 and folder not in (_pa, _pb)
                                 else f"the {'A' if folder == _pa else 'B'} folder")
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
    # averaged.  Off by default (0 = off in the engine); IIG sets 0.250.
    ("fiber_section_atten",       "Fiber Attenuation",          0.400,        "dB/km", True),
    ("span_loss",                 "Span Loss",                  20.000,       "dB",    False),
    ("span_length",               "Span Length",                0.0000,       "km",    False),
    # ORL FLOOR: the OTDR's own total ORL per direction, from the file; a
    # reading below the value fails.  Not the OLTS ORL a contract names, and
    # the sheet says so.  Off by default; IIG sets 30.
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
    # the engine); the AWS / IIG contract sets it at 0.08 dB.
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
    # Source: the customer .prj templates NCT runs FastReporter3 with,
    # forwarded 16 Sep 2026 (FW: FastReporter3 Customer Templates).  Every
    # template applies ONE threshold set to all 16 wavelengths, so each
    # customer is the handful of numbers below.  The mapping is the one the
    # AWS / IIG profile established:
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
    # Lumen and Zayo existed before these templates arrived; their previous
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
        # FR: identical to Lumen's template (splice warn 0.15 / fail 0.25,
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
        # NOT the same numbers as "AWS / IIG MT.1085" below: the template
        # grades every bidir splice at 0.08 and connectors at 0.30 (the RFP
        # figures), while the contract profile follows NCT's 24 Aug 2026
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
    # ── AWS / IIG MT.1085 (Intermountain Infrastructure Group) ────────
    # Sources: RFP-FOT-2025-001 (issued 09 Jul 2026) and the Zero DB SOW
    # (DocuSigned 06 Aug 2026), as reconciled by Northcentral Telcom on
    # 24 Aug 2026.  Only the three rows below are contract thresholds the
    # engine can grade on:
    #
    #   Bidir splice loss     <= 0.20 dB  — both documents agree.  NOTE this
    #     is LOOSER than the engine baseline (0.160), so this profile flags
    #     FEWER splice cells than Default, by contract.
    #   Bidir connector loss  <= 0.50 dB  — the executed SOW governs.  The
    #     RFP says 0.30, but the SOW incorporates it nowhere and NCT's
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
        # = 0.629, both failures in NCT's own review).  The min gate at 0.62
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
        # 1550 nm -- NCT's ruling of 22 Aug 2026: splice loss falls with
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
        # fails even though it prints ".200" (NCT's 2026-09-12 ruling,
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
    rules — AWS / IIG MT.1085 turns the one-sided connector gate off, and a
    profile that could only reach the threshold table would silently keep
    firing it.  Only a profile that declares a "conn" block differs from
    _CONN_DEFAULTS, so every profile that predates this (Default / Lumen /
    Zayo) keeps byte-identical connector behavior.

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
    except below swallowed it, and every IIG span showed "A" / "B" in the
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

    The FOLDER NAME is never the source: AWS / IIG MT.1085 span 27 sits in
    a folder whose two ends are the wrong way round (NCT, 2026-09-12)."""
    if profile_name is None:
        profile_name = st.session_state.get('otdr_profile')
    if _engine_extras_from_profile(profile_name).get(
            'SITE_NAMES_FROM_IDENTIFIERS'):
        try:
            names = _splicereport_json_reader().span_site_names(dir_a, dir_b)
        except Exception:
            names = None            # never block a report on a sidecar read
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
    from today (it now disables instead of reverting to default — e.g. the Zayo
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


def _render_customer_profile_picker():
    """The Customer profile dropdown, on its own above the A/B boxes.

    Robert, 2026-09-16: a tech chooses default or customer settings BEFORE
    selecting A and B.  It used to sit inside the OTDR settings expander
    (which stays where it is, below); the dropdown alone moved up.  Same
    state, same reload-on-change: session_state.otdr_profile drives the
    settings table and the connector knobs exactly as before.
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
    st.markdown('#### Select Customer Profile')
    st.markdown(
        '<style>.st-key-otdr_profile_select div[data-baseweb="select"] '
        '{font-size:1.2rem;font-weight:600;}</style>',
        unsafe_allow_html=True)
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
    _picked = st.selectbox(
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
            # well — a customer rule that lives on that panel (IIG's
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


def _render_profile_picker_box(where):
    """The Customer profile dropdown, guarded: a failure here must not take
    the page down, and the tool runs with the default profile."""
    try:
        _render_customer_profile_picker()
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
        st.caption('Close OTDR Suite completely and open it again.')


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
    two = 'Two folders (A + B)'
    one = 'One folder / zip (both directions)'
    if span == 1:
        k_mode, k_a, k_b = 'sr_input_mode', 'view_dir_a_input', 'view_dir_b_input'
        k_ba, k_bb, k_bone = 'sr_browse_a', 'sr_browse_b', 'sr_browse_one'
        k_one, k_zip, k_tech = 'sr_one_folder', 'sr_zip', 'sr_tech_xlsx'
    else:
        _k = f'sr{span}'
        k_mode, k_a, k_b = f'{_k}_input_mode', f'{_k}_dir_a', f'{_k}_dir_b'
        k_ba, k_bb, k_bone = f'{_k}_browse_a', f'{_k}_browse_b', f'{_k}_browse_one'
        k_one, k_zip, k_tech = f'{_k}_one_folder', f'{_k}_zip', f'{_k}_tech_xlsx'

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
    elif mode == two and span == 1:
        # Span 1's A and B are the sidebar's Trace Folders (Robert
        # 2026-09-26): one place to pick them, shared with the Viewer, so the
        # page shows what is loaded instead of a second pair of boxes.
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
    pre = 'sr' if span == 1 else f'sr{span}'
    k_a, k_b, k_src = f'{pre}_site_a', f'{pre}_site_b', f'{pre}_site_src'
    k_saved = f'{pre}_site_saved'
    # Boxes Streamlit forgot get back what they showed, while the span is
    # the one they showed it for.  Folders loaded or cleared in between get
    # their own names below, or "A" and "B".
    _saved = st.session_state.get(k_saved)
    if _saved and tuple(_saved[0]) == (dir_a, dir_b):
        for _k, _v in zip((k_a, k_b), _saved[1]):
            if _k not in st.session_state:
                st.session_state[_k] = _v
    if dir_a and dir_b and os.path.isdir(dir_a) and os.path.isdir(dir_b):
        # The profile is part of the signature: a tech who loads the span
        # and THEN picks the IIG profile must still get the identifier-based
        # names, not the "A"/"B" derived under the profile that was active
        # at load time (hub click-through, 2026-09-15).
        _sig = (dir_a, dir_b, st.session_state.get('otdr_profile'))
        if st.session_state.get(k_src) != _sig:
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
               f"{_count(res['n_splices'], 'splice')}  ·  span {res['span_km']} km  ·  "
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
    st.caption('Generates the Excel report (saved to your **Downloads**) and a '
               'clickable grid: click any flagged cell to jump to that fiber and '
               'splice in the Viewer.'
               + ('' if any(_panel_traces()) else
                  ' Give it two A/B folders, or one folder / .zip holding both '
                  'directions.'))

    # Customer profile first, above the A/B boxes: default or a customer's
    # thresholds, chosen before the span is picked.  Guarded the same way as
    # the settings panel below — a failure here must not take the page down.
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
    if n_spans < SR_MAX_SPANS:
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
    # version, an App Control block) must NOT take down the page.  But the
    # report does not run unless the WHOLE box drew, the threshold table and
    # the Connector & Launch knobs both (Robert 2026-09-28: "block the report",
    # then "block it if any part fails").  The fallback used to say "default
    # thresholds" and then send every row as UNticked, so the report flagged
    # nothing; a report at thresholds the tech never saw is worse than no
    # report.  The same box sits on the Viewer and Unidirectional pages
    # (_render_settings_box), which still fall back to the engine defaults.
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
            out_xlsx = _unused_report_path(os.path.join(_sr_dest, _name),
                                           [q['out'] for q in queue])
            queue.append({'span': _n, 'dirs': (_da, _db), 'out': out_xlsx,
                          'cmd': splicereport_cmd(_da, _db, out_xlsx, _sa, _sb,
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
    # engine (IIG 0.200 vs 0.160 — a 40 mdB band where the two disagreed).
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
        st.session_state['uni_upload'] = {'dir': sdir, 'n': n, 'dupes': dupes,
                                          'box': box_folder}
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


def _uni_pick_direction(folder):
    """The folder this report runs on, for the page's own upload.  An
    upload that holds both directions (the A and B shots together, loose or
    in one zip) is split the way the left panel splits such a folder
    (_split_panel_folder: the files' own direction stamps say which side is
    A), and the tech picks the direction: A by default.  Only the picked
    direction is analysed, so the other side is not reported as missing.  A
    one-direction upload comes back as it is.  A folder typed into the box
    is not split here: it keeps the Direction pick it always had."""
    split = _split_panel_folder(folder)
    if not split:
        return folder
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
    return split[side]


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
    _pa, _pb = _panel_traces()
    _show_panel_notes()
    if not (_pa or _pb):
        _seed_box('uni_folder_input')
    st.session_state.setdefault('uni_folder_input', '')
    _dropped = None
    _uni_pside = ''            # the left panel's side this page runs on, if any
    if _pa or _pb:
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
        _uni_pside = 'a' if folder == _pa else 'b'
        st.caption(f"Traces: the {'A' if folder == _pa else 'B'} folder loaded "
                   'in the left panel.')
    else:
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

        folder = (st.session_state.get('uni_folder_input') or '').strip().strip('"')
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
    if _from_upload:
        # Both directions in one upload, loose or zipped: the tech picks one.
        # After the foreign-file audit, so a stray from another job is not
        # taken for a second direction.
        folder = _uni_pick_direction(folder)

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
        out_xlsx = _unused_report_path(
            os.path.join(_uni_dest, 'unidirectional_events.xlsx'))
        st.session_state['uni_pending_cmd'] = uni_cmd(folder, out_xlsx,
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
        if _uni_pside and (_pa or _pb):
            # Run on one of the left panel's folders: the popped Viewer keeps
            # BOTH of them and opens the fibre on the side the report ran on,
            # as a click into the Viewer tab does (_handle_nav).  Pointing A at
            # the report's folder loaded a B-folder run's files as A->B.
            trace_server.set_dirs(_pa or None, _pb or None)
        elif folder and os.path.isdir(folder):
            trace_server.set_dirs(folder, None)   # popped Viewer reads this span
        _uni_dir = _uni_pside or 'a'              # the side its links open on
        # Same as the Splice Report grid: the Viewer judges by THIS run's gates.
        # The uni settings panel moves UNI_BEND_THRESHOLD off its 0.250 default
        # and that never reached the Viewer either.
        trace_server.set_thresholds(res.get('thresholds'), source='uni')
        trace_server.set_end_refl(res.get('end_refl'))
        trace_server.set_panel_span(res.get('panel_span'))
        trace_server.set_suite_table(None)        # a uni report has no A+B table
        _uni_popout = _viewer_click_target('uni')
        from urllib.parse import quote as _q
        _fq = _q(folder, safe='')
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
                              f"&dir={_uni_dir}&sra={_fq}&src=uni{_uni_pq}")))
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
#  PAGE: FQA Builder — Lumen submittal package from a production sheet
# ═════════════════════════════════════════════════════════════════════════
# The only page that takes no traces.  It reads the span's ZeroDB
# production sheet -- one tab per location, in route order -- and fills
# the Lumen Site Survey form: cover page, Fiber Assignment Table, Event
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
def page_fqa_builder():
    import folder_intake as _fi
    from fqa.ui import render
    render(default_out_dir=_fi.default_report_dir(), dest_row=_report_dest_row)


# ═════════════════════════════════════════════════════════════════════════
#  PAGE: Field Capture — FQA section 1.2 with the labels in the photos checked
# ═════════════════════════════════════════════════════════════════════════
# The tech's A-Location / Z-Location form: rack location and panel details
# for section 1.2 of the Lumen FQA Site Survey, photos, and a check that the
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


# ─── Route ────────────────────────────────────────────────────────────────
# Global catch-all: any unhandled error during a page render/action posts to
# Slack, then re-raises so Streamlit still shows the tech its red error box.
_note_tool_change(page)
if page != 'Viewer':
    # The Viewer frame goes with the page, so the address it kept through a
    # drop (see page_viewer) has nothing left to keep.
    st.session_state.pop('_viewer_drop_q', None)
try:
    if page == 'Viewer':
        page_viewer()
    elif page == 'Splice Report':
        page_splice_report()
    elif page == 'Unidirectional':
        page_unidirectional()
    elif page == 'FQA Builder':
        page_fqa_builder()
    elif page == 'Field Capture':
        page_field_capture()
    else:
        page_duplicate_check()
except Exception as _exc:
    report_error(f"hub page: {page}", _exc)
    raise
_after_page(page)

# ─── Sidebar footer: build identity + one-click update ────────────────────
# Rendered LAST so it sits at the bottom of the sidebar, below any page-
# specific widgets.  "app build N (date)" identifies the frozen exe (CI stamp);
# "engine: ..." identifies the code the launcher chose at boot (bundled vs a
# verified signed update) — so the boss can confirm a tech runs the latest of
# BOTH.  Dev runs collapse to a plain "dev".
# Light / Dark switch, on every page, above the build line.
_render_theme_control(st.sidebar)
_appv, _engv = _app_version(), _engine_version()
if _appv == 'dev' and _engv == 'dev':
    st.sidebar.caption('OTDR Suite · dev')
else:
    st.sidebar.caption(f'OTDR Suite · app {_appv} · engine: {_engv}')


if st.sidebar.button('🔄 Check for Updates', key='upd_check',
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

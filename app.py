"""
OTDR Suite — desktop hub
========================
One Streamlit app with a sidebar that switches between:

  • Viewer        — EXFO-style bidirectional trace viewer (zoom/pan, A/B
                    stacking).  Rendered by a small canvas server that runs
                    as a background thread inside this process; embedded here
                    via an iframe.
  • Duplicate Check — Secret Sauce duplicate classifier.  Runs in a clean
                    subprocess (it ships its own divergent sor_reader copy,
                    which can't share this process's namespace).

Dev run:   streamlit run app.py
Packaged:  launched by desktop/launcher.py inside OTDRSuite.exe (phase 2).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

import streamlit as st
from streamlit.components.v1 import iframe as st_iframe

# In a frozen build the launcher exports OTDR_SUITE_HOME (the bundle root);
# in dev it's just this file's directory.
HERE = os.environ.get('OTDR_SUITE_HOME') or os.path.dirname(os.path.abspath(__file__))
VIEWER_DIR = os.path.join(HERE, 'viewer')
SECRETSAUCE_DIR = os.path.join(HERE, 'secretsauce')
FROZEN = bool(getattr(sys, 'frozen', False))


def secretsauce_cmd(folder, out_dir, fmt):
    """Argv to run the Secret Sauce engine in a clean subprocess.
    Frozen: re-invoke this exe with the --run-secretsauce sentinel (the
    launcher dispatches it).  Dev: run the runner .py with python."""
    common = ['--folder', folder, '--out-dir', out_dir, '--format', fmt]
    if FROZEN:
        return [sys.executable, '--run-secretsauce', *common]
    return [sys.executable, os.path.join(SECRETSAUCE_DIR, 'run_secretsauce.py'), *common]

# The viewer's engine lives in viewer/ — put it first so `import trace_server`
# resolves its sor_reader copy (NOT Secret Sauce's).  Secret Sauce is never
# imported in this process; it runs as a subprocess with its own path.
if VIEWER_DIR not in sys.path:
    sys.path.insert(0, VIEWER_DIR)

import trace_server  # noqa: E402  (after sys.path setup)

TRACE_PORT_BASE = 8771

st.set_page_config(page_title='OTDR Suite', layout='wide',
                   initial_sidebar_state='expanded')


# ─── Background trace server (started once) ──────────────────────────────
def ensure_trace_server():
    if 'trace_port' not in st.session_state:
        st.session_state['trace_port'] = trace_server.start_in_thread(TRACE_PORT_BASE)
    return st.session_state['trace_port']


# ─── Native folder picker (works locally + in the packaged .exe) ─────────
def pick_folder(title='Choose a folder'):
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
        return ''


# ─── Sidebar nav ─────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown('## 🔬 OTDR Suite')
    page = st.radio('Tool', ['Viewer', 'Duplicate Check'], label_visibility='collapsed')
    st.divider()


# ═════════════════════════════════════════════════════════════════════════
#  PAGE: Viewer
# ═════════════════════════════════════════════════════════════════════════
def page_viewer():
    port = ensure_trace_server()

    with st.sidebar:
        st.markdown('### Trace folders')

        # Keyed widgets, no value= (mixing key+value with a programmatic write
        # is a Streamlit footgun).  Buttons write the widget-key slot BEFORE
        # the text_input is created this run, so the picked path shows up.
        st.session_state.setdefault('view_dir_a_input', trace_server.CONFIG['dir_a'] or '')
        st.session_state.setdefault('view_dir_b_input', trace_server.CONFIG['dir_b'] or '')

        if st.button('📁 A-direction folder', use_container_width=True):
            p = pick_folder('Choose the A-direction folder')
            if p:
                st.session_state['view_dir_a_input'] = p
        st.text_input('A folder', key='view_dir_a_input',
                      label_visibility='collapsed', placeholder='A-direction folder path')

        if st.button('📁 B-direction folder', use_container_width=True):
            p = pick_folder('Choose the B-direction folder')
            if p:
                st.session_state['view_dir_b_input'] = p
        st.text_input('B folder', key='view_dir_b_input',
                      label_visibility='collapsed', placeholder='B-direction folder path')

        dir_a = (st.session_state.get('view_dir_a_input') or '').strip().strip('"')
        dir_b = (st.session_state.get('view_dir_b_input') or '').strip().strip('"')

        # Validate + push into the trace server's shared config.
        warn = []
        if dir_a and not os.path.isdir(dir_a):
            warn.append('A folder not found')
            dir_a = ''
        if dir_b and not os.path.isdir(dir_b):
            warn.append('B folder not found')
            dir_b = ''
        if dir_a and dir_b and os.path.abspath(dir_a) == os.path.abspath(dir_b):
            warn.append('A and B are the same folder')
        trace_server.set_dirs(dir_a or None, dir_b or None)
        for w in warn:
            st.warning(w)

        na = len(trace_server.list_fibers(dir_a)) if dir_a else 0
        nb = len(trace_server.list_fibers(dir_b)) if dir_b else 0
        st.caption(f'A: {na} fibers · B: {nb} fibers')

    st.markdown('#### Trace Viewer')
    if not dir_a and not dir_b:
        st.info('Pick an A and/or B folder of OTDR `.sor` / `.json` files in the '
                'sidebar, then type fiber numbers in the viewer to plot them.')
    # Embed the canvas viewer.  Cache-bust on folder change so the iframe
    # re-reads /api/list.
    bust = abs(hash((dir_a, dir_b))) % 100000
    st_iframe(f'http://127.0.0.1:{port}/?b={bust}', height=760, scrolling=False)


# ═════════════════════════════════════════════════════════════════════════
#  PAGE: Duplicate Check (Secret Sauce)
# ═════════════════════════════════════════════════════════════════════════
def page_duplicate_check():
    st.markdown('#### Duplicate Check — Secret Sauce')
    st.caption('Pick a folder of `.sor` / `.trc` / `.json` files. Reports are '
               'written to a `SecretSauce_reports` subfolder and offered for download.')

    st.session_state.setdefault('ss_folder_input', '')

    c1, c2 = st.columns([1, 2])
    with c1:
        if st.button('📁 Browse for folder', type='primary', use_container_width=True):
            p = pick_folder('Choose a folder of OTDR files')
            if p:
                st.session_state['ss_folder_input'] = p
    with c2:
        st.text_input('…or paste a folder path',
                      key='ss_folder_input',
                      placeholder=r'C:\Users\you\Desktop\fiber files')

    folder = (st.session_state.get('ss_folder_input') or '').strip().strip('"')
    if not folder or not os.path.isdir(folder):
        st.info('👆 Choose the folder that holds your `.sor` / `.trc` / `.json` files.')
        return

    out_format = st.radio('Output format', ['Excel (xlsx)', 'PDF'], horizontal=True)
    fmt = 'xlsx' if out_format.startswith('Excel') else 'pdf'

    if st.button('Run analysis', type='primary'):
        out_dir = os.path.join(folder, 'SecretSauce_reports')
        cmd = secretsauce_cmd(folder, out_dir, fmt)
        with st.spinner('Running Secret Sauce…'):
            proc = subprocess.run(cmd, capture_output=True, text=True)

        manifest = _parse_manifest(proc.stdout)
        if manifest is None:
            st.error('Secret Sauce did not return a result.')
            with st.expander('Engine log'):
                st.code(proc.stderr[-4000:] or '(no output)')
            return
        if not manifest.get('ok'):
            st.error(manifest.get('error', 'Analysis failed.'))
            if manifest.get('counts'):
                st.caption(f"Inventory: {manifest['counts']}")
            with st.expander('Engine log'):
                st.code(proc.stderr[-4000:] or '(no output)')
            return

        st.session_state['ss_result'] = manifest

    # Show last result (persists across reruns so downloads work).
    res = st.session_state.get('ss_result')
    if res and res.get('ok'):
        c = res.get('counts', {})
        st.success(f"Done — {c.get('sor',0)} SOR · {c.get('trc',0)} TRC · "
                   f"{c.get('json',0)} JSON processed.")
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


# ─── Route ────────────────────────────────────────────────────────────────
if page == 'Viewer':
    page_viewer()
else:
    page_duplicate_check()

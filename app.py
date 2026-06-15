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
SPLICEREPORT_DIR = os.path.join(HERE, 'splicereport')
FROZEN = bool(getattr(sys, 'frozen', False))


def secretsauce_cmd(folder, out_dir, fmt):
    """Argv to run the Secret Sauce engine in a clean subprocess.
    Frozen: re-invoke this exe with the --run-secretsauce sentinel (the
    launcher dispatches it).  Dev: run the runner .py with python."""
    common = ['--folder', folder, '--out-dir', out_dir, '--format', fmt]
    if FROZEN:
        return [sys.executable, '--run-secretsauce', *common]
    return [sys.executable, os.path.join(SECRETSAUCE_DIR, 'run_secretsauce.py'), *common]


def splicereport_cmd(dir_a, dir_b, out_xlsx, site_a, site_b, overrides=None):
    """Argv to run the Splice Report engine in a clean subprocess (its own
    sor_reader copy).  Frozen: --run-splicereport sentinel; dev: the runner.

    `overrides` is the engine-global threshold dict from the OTDR settings
    panel (e.g. {'REBURN_THRESHOLD': 0.12, ...}).  It's serialized to JSON
    and forwarded as --overrides so the subprocess can apply it to the
    engine module BEFORE the pipeline runs (the panel lives in this process;
    the engine lives in the subprocess, so the values cross as JSON)."""
    common = ['--dir-a', dir_a, '--dir-b', dir_b, '--out', out_xlsx,
              '--site-a', site_a, '--site-b', site_b]
    if overrides:
        common += ['--overrides', json.dumps(overrides)]
    if FROZEN:
        return [sys.executable, '--run-splicereport', *common]
    return [sys.executable, os.path.join(SPLICEREPORT_DIR, 'run_splicereport.py'), *common]

# Repo root on path so the stdlib-only error_report module imports (in the hub
# AND in trace_server, which lives in viewer/).
if HERE not in sys.path:
    sys.path.insert(0, HERE)
try:
    from error_report import report_error
except Exception:                                  # reporting is best-effort
    def report_error(*a, **k):
        pass

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


# ─── Deep-link nav: a Splice Report cell click lands as ?nav=viewer&fiber=&km=
#     → switch to the Viewer page + stash the target for the iframe URL. ──────
def _handle_nav():
    qp = st.query_params
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
        st.session_state['viewer_target'] = {
            'fibers': qp.get('fibers'),
            'dir': qp.get('dir', 'a'),
        }
        st.session_state['came_from_dupcheck'] = True
        st.session_state['nav_radio'] = 'Viewer'   # set BEFORE the radio widget
        st.query_params.clear()
        return
    if qp.get('nav') == 'viewer' and qp.get('fiber'):
        st.session_state['viewer_target'] = {
            'fiber': qp.get('fiber'),
            'km': qp.get('km'),
            'dir': qp.get('dir', 'both'),
        }
        st.session_state['nav_radio'] = 'Viewer'   # set BEFORE the radio widget
        st.query_params.clear()

_handle_nav()

# ─── Sidebar nav ─────────────────────────────────────────────────────────
st.session_state.setdefault('nav_radio', 'Viewer')
with st.sidebar:
    st.markdown('## 🔬 OTDR Suite')
    page = st.radio('Tool', ['Viewer', 'Splice Report', 'Secret Sauce'],
                    key='nav_radio', label_visibility='collapsed')
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

    st.markdown('#### Trace Viewer')
    if not dir_a and not dir_b:
        st.info('Pick an A and/or B folder of OTDR `.sor` / `.json` files in the '
                'sidebar, then type fiber numbers in the viewer to plot them.')
    # Embed the canvas viewer.  Cache-bust on folder change so the iframe
    # re-reads /api/list.  A deep-link target is appended so the viewer
    # auto-loads:  a single fiber + km (Splice Report cell), OR a pair of
    # fibers overlaid (Duplicate Check "Stay in app").
    from urllib.parse import urlencode
    q = {'b': abs(hash((dir_a, dir_b))) % 100000}
    tgt = st.session_state.pop('viewer_target', None)   # consume once
    if tgt and tgt.get('fibers'):
        q['fibers'] = tgt['fibers']
        q['dir'] = tgt.get('dir', 'a')
        st.caption(f"Overlaying duplicate-pair fibers {tgt['fibers']} "
                   f"(direction {q['dir'].upper()})")
    elif tgt and tgt.get('fiber'):
        q['fiber'] = tgt['fiber']
        if tgt.get('km'):
            q['km'] = tgt['km']
        q['dir'] = tgt.get('dir', 'both')
        st.caption(f"Jumped to fiber {tgt['fiber']}"
                   + (f" @ {tgt['km']} km" if tgt.get('km') else ''))
    st_iframe(f'http://127.0.0.1:{port}/?{urlencode(q)}', height=760, scrolling=False)


# ═════════════════════════════════════════════════════════════════════════
#  PAGE: Duplicate Check (Secret Sauce)
# ═════════════════════════════════════════════════════════════════════════
def page_duplicate_check():
    st.markdown('#### Secret Sauce')
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

    out_format = st.radio('Output', ['Excel (xlsx)', 'PDF', 'Stay in app'],
                          horizontal=True)
    fmt = {'Excel (xlsx)': 'xlsx', 'PDF': 'pdf'}.get(out_format, 'pairs')

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
            report_error("secret sauce — no manifest",
                         RuntimeError("runner returned no JSON manifest"),
                         {"returncode": proc.returncode,
                          "stderr_tail": (proc.stderr or '')[-300:]})
            return
        if not manifest.get('ok'):
            st.error(manifest.get('error', 'Analysis failed.'))
            if manifest.get('counts'):
                st.caption(f"Inventory: {manifest['counts']}")
            with st.expander('Engine log'):
                st.code(proc.stderr[-4000:] or '(no output)')
            report_error("secret sauce — engine returned not-ok",
                         RuntimeError(manifest.get('error', 'analysis failed')),
                         {"counts": manifest.get('counts'), "format": fmt})
            return

        # Stash the folder so the in-app pair links can point the viewer at it.
        manifest['_folder'] = folder
        if manifest.get('mode') == 'pairs':
            st.session_state['ss_pairs_result'] = manifest
            # Cache to disk so "← Back" from the Viewer (which reset session_state
            # via the URL nav) re-shows the pairs list instantly — no re-run.
            try:
                import json as _json
                os.makedirs(out_dir, exist_ok=True)
                with open(os.path.join(out_dir, 'pairs_cache.json'), 'w',
                          encoding='utf-8') as fh:
                    _json.dump(manifest, fh)
            except Exception:
                pass
        else:
            st.session_state['ss_result'] = manifest

    # ── In-app duplicate report (persists across reruns; restore from the
    #    on-disk cache after a pair-click round trip cleared session_state) ──
    pres = st.session_state.get('ss_pairs_result')
    if not (pres and pres.get('mode') == 'pairs'):
        try:
            import json as _json
            cache = os.path.join(folder, 'SecretSauce_reports', 'pairs_cache.json')
            if os.path.exists(cache):
                with open(cache, encoding='utf-8') as fh:
                    cached = _json.load(fh)
                if (cached.get('ok') and cached.get('mode') == 'pairs'
                        and cached.get('_folder') == folder):
                    pres = cached
                    st.session_state['ss_pairs_result'] = cached
        except Exception:
            pass
    if pres and pres.get('ok') and pres.get('mode') == 'pairs':
        _render_pairs_report(pres)
        return

    # ── Excel / PDF download result (persists across reruns) ──
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


# Likelihood-tier colors for the in-app duplicate-pair report.
_DUP_COLOR = {'CONFIRMED duplicate': '#c0392b', 'Likely duplicate': '#e67e22',
              'Possible duplicate': '#b97000', 'Unique': '#7f8c8d'}


def _render_pairs_report(res):
    """Render the Secret Sauce pair list IN the page — one row per suspected-
    duplicate pair (worst-first), each a link that overlays BOTH fibers in the
    Viewer (?nav=viewer&fibers=A,B&dir=a&ssfolder=…)."""
    from urllib.parse import quote
    folder = res.get('folder') or res.get('_folder') or ''
    pairs = res.get('pairs', [])
    st.success(f"{res.get('n_files','?')} files · {res.get('n_pairs',0)} pairs · "
               f"{res.get('n_flagged',0)} at ≥50% likelihood.")
    st.markdown('###### Click a pair → overlay BOTH fibers in the Viewer')
    if not pairs:
        st.info('No comparable pairs were produced for this folder.')
        return

    ssq = quote(folder, safe='')
    rows = ['<div style="overflow:auto;max-height:62vh;border:1px solid #c9d5e1;'
            'border-radius:4px;color:#1f2a36;background:#ffffff">',
            '<table style="border-collapse:collapse;font-size:12px;'
            'font-family:Consolas,monospace;width:100%">',
            '<thead><tr>'
            "<th style='padding:5px 10px;border:1px solid #dbe4ee;background:#eef3f8;text-align:left'>Pair</th>"
            "<th style='padding:5px 10px;border:1px solid #dbe4ee;background:#eef3f8'>Likelihood</th>"
            "<th style='padding:5px 10px;border:1px solid #dbe4ee;background:#eef3f8'>Score σ</th>"
            "<th style='padding:5px 10px;border:1px solid #dbe4ee;background:#eef3f8'>Shape r</th>"
            "<th style='padding:5px 10px;border:1px solid #dbe4ee;background:#eef3f8;text-align:left'>Verdict</th>"
            '</tr></thead><tbody>']
    for p in pairs:
        color = _DUP_COLOR.get(p['verdict'], '#555')
        fa, fb = p.get('fiberA'), p.get('fiberB')
        label = f"F{fa} ↔ F{fb}"
        if p.get('viewable') and fa is not None and fb is not None:
            href = (f"?nav=viewer&fibers={fa},{fb}&dir=a&ssfolder={ssq}")
            pair_cell = (f"<a href='{href}' target='_self' "
                         f"title='Overlay {p['fileA']} + {p['fileB']}' "
                         f"style='color:#1a5fb4;text-decoration:none;font-weight:600'>"
                         f"{label}</a>")
        else:
            pair_cell = (f"<span title='not viewable: {p.get('reason','')}' "
                         f"style='color:#888'>{label} ⚠</span>")
        pct = f"{p['p_dup']*100:.0f}%"
        r_txt = '—' if p.get('shape_r') is None else f"{p['shape_r']:.3f}"
        rows.append(
            "<tr>"
            f"<td style='padding:4px 10px;border:1px solid #eef2f6'>{pair_cell}</td>"
            f"<td style='padding:4px 10px;border:1px solid #eef2f6;text-align:center;"
            f"font-weight:600;color:{color}'>{pct}</td>"
            f"<td style='padding:4px 10px;border:1px solid #eef2f6;text-align:right'>{p['score']:.4f}</td>"
            f"<td style='padding:4px 10px;border:1px solid #eef2f6;text-align:right'>{r_txt}</td>"
            f"<td style='padding:4px 10px;border:1px solid #eef2f6;color:{color}'>{p['verdict']}</td>"
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
    ("unidir_splice_loss",        "Unidir. splice loss",        0.250,        "dB",    True),
    ("bidir_splice_loss",         "Bidir splice loss",          0.160,        "dB",    True),
    ("unidir_connector_loss",     "Unidir. connector loss",     0.750,        "dB",    False),
    ("bidir_connector_loss",      "Bidir connector loss",       0.500,        "dB",    True),
    ("splitter_loss",             "Splitter Loss",              4.500,        "dB",    False),
    ("reflectance",               "Reflectance",                -49.9,        "dB",    True),
    ("fiber_section_atten",       "Fiber section attenuation",  0.400,        "dB/km", False),
    ("span_loss",                 "Span loss",                  20.000,       "dB",    False),
    ("span_length",               "Span length",                0.0000,       "km",    False),
    ("span_orl",                  "Span ORL",                   15.00,        "dB",    False),
]
# Pre-checked rows (match what the splice report flags out of the box):
OTDR_DEFAULT_APPLY = {"unidir_splice_loss", "bidir_splice_loss",
                       "bidir_connector_loss", "reflectance"}

# ── Customer threshold profiles ──────────────────────────────────────
# Each entry is a named preset that overrides the per-row 'fail' values
# and 'apply' flags above.  Pick one from the dropdown to switch.  To add
# a new customer, append a dict here — the dropdown picks it up.
CUSTOMER_PROFILES = {
    "Default (engine baseline)": {
        "apply":      set(OTDR_DEFAULT_APPLY),
        "thresholds": {},
    },
    "Lumen": {
        "apply":      {"unidir_splice_loss", "bidir_splice_loss",
                        "bidir_connector_loss", "reflectance"},
        "thresholds": {
            "bidir_splice_loss":     0.120,
            "unidir_splice_loss":    0.200,
            "bidir_connector_loss":  0.400,
            "reflectance":          -50.0,
        },
    },
    "Zayo": {
        "apply":      {"bidir_splice_loss", "bidir_connector_loss"},
        "thresholds": {
            "bidir_splice_loss":     0.200,
            "bidir_connector_loss":  0.600,
        },
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
    "bidir_connector_loss": "BIDIR_CONNECTOR_LOSS",
    "reflectance":          "LAUNCH_BAD_REFL_DB",
}


def _otdr_settings_from_profile(profile_name):
    """Return a fresh otdr_settings dict for the named profile."""
    prof = CUSTOMER_PROFILES.get(profile_name) or {}
    apply_set = prof.get("apply")
    overrides = prof.get("thresholds") or {}
    out = {}
    for key, _, fail_default, _, _ in OTDR_ROWS:
        fail = float(overrides.get(key, fail_default))
        applied = ((apply_set is not None and key in apply_set)
                   if apply_set is not None
                   else (key in OTDR_DEFAULT_APPLY))
        out[key] = {"apply": applied, "fail": fail, "warning": fail}
    return out


def _overrides_from_settings(otdr_settings):
    """Translate the OTDR panel's per-row settings into the engine-global
    overrides dict that crosses the subprocess boundary.

    Only rows whose Apply checkbox is ticked AND that map to a real engine
    global contribute.  Unticked rows fall back to the engine default (we
    simply omit them, so run_splicereport keeps the module constant).
    Returns {} when nothing is overridden — i.e. the run reproduces today's
    baseline behavior, exactly like the 'Default (engine baseline)' profile
    where the ticked rows all hold their engine-default values.
    """
    out = {}
    settings = otdr_settings or {}
    for row_key, engine_global in _OTDR_KEY_TO_ENGINE_GLOBAL.items():
        row = settings.get(row_key) or {}
        if row.get("apply") and row.get("fail") is not None:
            out[engine_global] = float(row["fail"])
    return out


def _render_otdr_settings_panel():
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

    with st.expander('OTDR settings (thresholds)', expanded=False):
        # ── Customer profile dropdown ─────────────────────────────────
        st.markdown('**Customer profile**')
        _profile_names = list(CUSTOMER_PROFILES.keys())

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
            label_visibility='collapsed',
            key='otdr_profile_select',
            help=("Each profile selects a different bundle of Apply / Fail "
                  "values for the OTDR settings table below.  Pick 'Custom' "
                  "to keep your own manual edits."),
        )
        # If the user just changed the profile, reload the table from that
        # profile's preset (unless they picked 'Custom').
        if _picked != _cur:
            st.session_state.otdr_profile = _picked
            if 'Custom' not in _picked:
                st.session_state.otdr_settings = _otdr_settings_from_profile(_picked)
            st.rerun()

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
            }
            for key, label, _fail, unit, supported in OTDR_ROWS
        ]
        # The component key encodes the active profile so switching customers
        # forces a re-mount with the new initial values.
        _commit = otdr_settings_component(
            _rows, default=None,
            key=f"otdr_component::{st.session_state.otdr_profile}",
        )
        if _commit:
            # Component reported its state (auto-commit on edit, or Apply
            # click) — persist to session_state for the run to read.
            for key, vals in _commit.items():
                st.session_state.otdr_settings[key] = {
                    'apply':   bool(vals.get('apply')),
                    'fail':    float(vals.get('fail', 0.0)),
                    'warning': float(vals.get('warning', 0.0)),
                }

        # Show which thresholds will actually be pushed onto the engine.
        _ov = _overrides_from_settings(st.session_state.otdr_settings)
        if _ov:
            st.caption('Active overrides → ' + ', '.join(
                f'{k} = {v:g}' for k, v in sorted(_ov.items())))
        else:
            st.caption('No overrides active — engine defaults in effect.')

    return st.session_state.otdr_settings


_CAT_COLOR = {
    'reburn': '#e74c3c', 'break': '#c0392b', 'broke': '#922b21',
    'bend': '#e67e22', 'ref': '#d35400', 'gainer': '#27ae60',
    'bfill': '#2980b9', 'a_only': '#8e44ad', 'b_only': '#16a085',
    'deadzone': '#7f8c8d', 'event': '#555',
}

def page_splice_report():
    st.markdown('#### Splice Report — bidirectional')
    st.caption('Uses the same A/B folders as the Viewer. Generates the Excel '
               'report and a clickable grid — click any flagged cell to jump to '
               'that fiber and splice in the Viewer.')

    # Reuse the viewer's A/B folder slots so both tools share one selection.
    st.session_state.setdefault('view_dir_a_input', trace_server.CONFIG.get('dir_a') or '')
    st.session_state.setdefault('view_dir_b_input', trace_server.CONFIG.get('dir_b') or '')

    c1, c2 = st.columns(2)
    with c1:
        if st.button('📁 A-direction folder', use_container_width=True, key='sr_browse_a'):
            p = pick_folder('Choose the A-direction folder')
            if p:
                st.session_state['view_dir_a_input'] = p
        st.text_input('A folder', key='view_dir_a_input', placeholder='A-direction folder')
    with c2:
        if st.button('📁 B-direction folder', use_container_width=True, key='sr_browse_b'):
            p = pick_folder('Choose the B-direction folder')
            if p:
                st.session_state['view_dir_b_input'] = p
        st.text_input('B folder', key='view_dir_b_input', placeholder='B-direction folder')

    dir_a = (st.session_state.get('view_dir_a_input') or '').strip().strip('"')
    dir_b = (st.session_state.get('view_dir_b_input') or '').strip().strip('"')
    s1, s2 = st.columns(2)
    site_a = s1.text_input('A-end site name', value=st.session_state.get('sr_site_a', 'A'), key='sr_site_a')
    site_b = s2.text_input('B-end site name', value=st.session_state.get('sr_site_b', 'B'), key='sr_site_b')

    if not (dir_a and os.path.isdir(dir_a) and dir_b and os.path.isdir(dir_b)):
        st.info('Pick **both** an A and a B folder (a bidirectional report needs both).')
        return

    # ── OTDR settings panel (pixel-perfect EXFO threshold table) ─────────
    # Renders the customer-profile dropdown + the custom HTML component.
    # The values it commits land in session_state.otdr_settings and become
    # the engine overrides forwarded to the subprocess on Generate.
    # Guarded: a settings-panel failure (component path quirk, Streamlit
    # version) must NOT take down the core Splice Report — fall back to the
    # engine's default thresholds with a visible warning.
    try:
        _render_otdr_settings_panel()
    except Exception as _exc:
        st.warning('OTDR settings panel unavailable — running with default '
                   'thresholds. (Details sent to support.)')
        report_error('splice report — settings panel render', _exc)
        st.session_state.pop('otdr_settings', None)   # → empty overrides below

    if st.button('Generate Splice Report', type='primary'):
        out_xlsx = os.path.join(dir_a, 'SpliceReport',
                                f'{site_a}_{site_b}_SpliceReport.xlsx')
        # Read the panel values straight out of session_state (which the
        # component's auto-commit keeps current) and translate to engine
        # globals.  This is the value the run actually uses — see the
        # iframe-state footgun note in _render_otdr_settings_panel.
        overrides = _overrides_from_settings(st.session_state.get('otdr_settings'))
        cmd = splicereport_cmd(dir_a, dir_b, out_xlsx, site_a, site_b,
                               overrides=overrides)
        with st.spinner('Running the bidirectional splice pipeline…'):
            proc = subprocess.run(cmd, capture_output=True, text=True)
        manifest = _parse_manifest(proc.stdout)
        if manifest is None or not manifest.get('ok'):
            st.error((manifest or {}).get('error', 'Splice report failed.'))
            with st.expander('Engine log'):
                st.code(proc.stderr[-4000:] or '(no output)')
            report_error('splice report (hub)',
                         RuntimeError((manifest or {}).get('error', 'no manifest')),
                         {'dir_a': dir_a, 'dir_b': dir_b})
            return
        st.session_state['sr_result'] = manifest

    res = st.session_state.get('sr_result')
    if not (res and res.get('ok')):
        return

    # Summary + Excel download
    st.success(f"{res['site_a']} → {res['site_b']}  ·  {res['n_fibers']} fibers  ·  "
               f"{res['n_splices']} splices  ·  span {res['span_km']} km  ·  "
               f"{res['n_flagged']} flagged events")
    xp = res.get('xlsx')
    if xp and os.path.exists(xp):
        with open(xp, 'rb') as fh:
            st.download_button('⬇ Excel report', data=fh.read(),
                               file_name=os.path.basename(xp), key='sr_dl')

    st.markdown('###### Click a flagged cell → jump to it in the Viewer')

    # Build a ribbon × splice-column grid (mirrors the Excel), flagged cells
    # link to ?nav=viewer&fiber=&km= which the hub turns into a viewer deep-link.
    cols = res['columns']
    ribbon_size = res['ribbon_size']
    n_fibers = res['n_fibers']
    n_ribbons = (n_fibers + ribbon_size - 1) // ribbon_size
    # group flagged cells by (ribbon, column index)
    by_rc = {}
    for c in res['cells']:
        ri = (c['fiber'] - 1) // ribbon_size
        by_rc.setdefault((ri, c['splice']), []).append(c)

    def hdr(col):
        tag = f"S{col['num']}" if col['kind'] == 'splice' and col['num'] else col['kind'].title()
        return f"<div style='font-weight:600'>{tag}</div><div style='font-size:10px;color:#789'>{col['km']:.3f} km</div>"

    html = ['<div style="overflow:auto;max-height:62vh;border:1px solid #c9d5e1;border-radius:4px;color:#1f2a36;background:#ffffff">',
            '<table style="border-collapse:collapse;font-size:11px;font-family:Consolas,monospace">',
            '<thead><tr><th style="position:sticky;left:0;background:#eef3f8;padding:4px 8px;border:1px solid #dbe4ee">Ribbon</th>']
    for col in cols:
        html.append(f"<th style='padding:4px 8px;border:1px solid #dbe4ee;background:#eef3f8;white-space:nowrap'>{hdr(col)}</th>")
    html.append('</tr></thead><tbody>')
    for ri in range(n_ribbons):
        f0, f1 = ri * ribbon_size + 1, min((ri + 1) * ribbon_size, n_fibers)
        html.append(f"<tr><td style='position:sticky;left:0;background:#f7fafc;padding:3px 8px;border:1px solid #e3e9f0;white-space:nowrap'>F{f0}–{f1}</td>")
        for ci, col in enumerate(cols):
            cell = by_rc.get((ri, ci), [])
            if not cell:
                html.append("<td style='padding:3px 6px;border:1px solid #eef2f6'></td>")
                continue
            links = []
            for c in sorted(cell, key=lambda x: x['fiber']):
                color = _CAT_COLOR.get(c['category'], '#555')
                loss = '' if c['loss'] is None else f" {c['loss']:.3f}"
                href = f"?nav=viewer&fiber={c['fiber']}&km={c['km']}&dir=both"
                links.append(f"<a href='{href}' target='_self' title='{c['label']}' "
                             f"style='color:{color};text-decoration:none;font-weight:600'>F{c['fiber']}{loss}</a>")
            html.append("<td style='padding:3px 6px;border:1px solid #eef2f6;white-space:nowrap'>"
                        + "<br>".join(links) + "</td>")
        html.append('</tr>')
    html.append('</tbody></table></div>')
    st.markdown(''.join(html), unsafe_allow_html=True)


# ─── Route ────────────────────────────────────────────────────────────────
# Global catch-all: any unhandled error during a page render/action posts to
# Slack, then re-raises so Streamlit still shows the tech its red error box.
try:
    if page == 'Viewer':
        page_viewer()
    elif page == 'Splice Report':
        page_splice_report()
    else:
        page_duplicate_check()
except Exception as _exc:
    report_error(f"hub page: {page}", _exc)
    raise

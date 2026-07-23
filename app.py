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
import time

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


def _engine_tail(job, n=1):
    """Last n non-empty lines of the engine's (live) stderr log."""
    try:
        with open(job['err_path'], 'r', encoding='utf-8', errors='replace') as fh:
            lines = [ln.strip() for ln in fh.read().splitlines() if ln.strip()]
        return lines[-n:]
    except OSError:
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


def run_engine_live(prefix, *, running_title, timeout_s=None):
    """Drive a background engine run across reruns with a live progress panel and
    a Cancel button.  Start it by setting st.session_state[f'{prefix}_pending_cmd'].

    Returns the finished subprocess.CompletedProcess when done, or None if there
    is nothing to run / the run was cancelled.  While the engine is running it
    renders the progress panel and calls st.rerun() (so it does not return).
    Raises subprocess.TimeoutExpired if the engine exceeds the timeout, so the
    caller's existing TimeoutExpired handler fires."""
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
        st.info('Run cancelled.')
        return None

    state = _engine_poll(job, timeout_s)
    if state == 'running':
        elapsed = int(time.monotonic() - job['started'])
        st.info(f'⏳ {running_title} — {elapsed}s elapsed. '
                'You can leave this open or keep working; cancel below if needed.')
        tail = _engine_tail(job, 1)
        if tail:
            st.caption(f'current step · {tail[0][:140]}')
        st.button('Cancel run', key=f'{prefix}_cancel_btn',
                  on_click=_flag_cancel, args=(cancel_key,))
        time.sleep(0.8)
        st.rerun()

    proc = job.get('result')
    args = job['proc'].args
    _engine_cleanup(job)
    st.session_state.pop(job_key, None)
    if state == 'timeout':
        raise subprocess.TimeoutExpired(args, timeout_s)
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
    files live in `folder`.  GenParams carries both cable endpoints; which one
    this direction was shot FROM comes from the filename prefix (SEANOR* →
    Seattle, NORSEA* → North Bend; HOWLAN* → How, LANHOW* → Lan).  Returns
    ('', '') when nothing is readable."""
    import glob
    sors = sorted(glob.glob(os.path.join(folder, '*.sor')) +
                  glob.glob(os.path.join(folder, '*.SOR')))
    if not sors:
        return ('', '')
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
    if zip_file is not None:
        key = f"zip:{getattr(zip_file, 'name', 'zip')}:{getattr(zip_file, 'size', 0)}"
    elif folder and os.path.isdir(folder):
        key = f"dir:{os.path.abspath(folder)}"
    else:
        st.info('👆 Choose a folder that contains **both** directions, or upload a .zip.')
        return ('', '')
    cache = st.session_state.setdefault('sr_intake', {})
    cached = cache.get(key)
    if not (cached and os.path.isdir(cached[0]) and os.path.isdir(cached[1])):
        work = tempfile.mkdtemp(prefix='otdr_intake_')
        try:
            if zip_file is not None:
                files = fi.extract_zip(zip_file, os.path.join(work, 'unzipped'))
            else:
                files = fi.find_otdr_files(folder)
            if not files:
                st.error('No .sor / .json files found in that folder/zip.')
                return ('', '')
            da, db, info = fi.materialize_two_directions(files, work)
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
    msg = (f"Auto-split by direction → **A:** {info['a_prefix']} "
           f"({info['a_count']} files)  ·  **B:** {info['b_prefix']} ({info['b_count']} files)")
    if info.get('dropped'):
        msg += f"  ·  ⚠ ignored extra group(s): {', '.join(info['dropped'])}"
    st.caption(msg)
    return (da, db)


def _load_span(folder, zip_file):
    """Load ONE span (a folder or a .zip holding BOTH directions) into ALL three
    tools at once: split into A/B (Viewer + Splice Report) and a combined folder
    (Secret Sauce), then populate the shared input slots every page reads.
    Returns True on success; renders its own sidebar message on failure."""
    import folder_intake as fi
    # zip_file may be a single uploaded file, a LIST of them (multi-upload — e.g.
    # separate per-direction zips like HOWLAN.zip + LANHOW.zip), or None.
    zips = ((list(zip_file) if isinstance(zip_file, (list, tuple)) else [zip_file])
            if zip_file else [])
    if zips:
        src_label = ', '.join(getattr(z, 'name', 'uploaded.zip') for z in zips)
    elif folder and os.path.isdir(folder):
        src_label = os.path.basename(folder.rstrip('/\\')) or folder
    else:
        st.sidebar.warning('Pick a folder with both directions, or upload its .zip(s), first.')
        return False
    work = tempfile.mkdtemp(prefix='otdr_span_')
    try:
        if zips:
            # One or more uploaded zips (e.g. a per-direction zip each) →
            # extract each into its own subdir, then combine.
            files = []
            for _i, _z in enumerate(zips):
                files += fi.extract_zip(_z, os.path.join(work, 'unzipped_%d' % _i))
            files = sorted(files)
        else:
            # A folder — which may itself CONTAIN the per-direction zips (spans
            # are often delivered that way), so descend into any zips found.
            files = fi.find_otdr_files_with_zips(folder, os.path.join(work, 'zips'))
        if not files:
            st.sidebar.error('No .sor / .json files found in that folder/zip '
                             '(if the span is split into per-direction zips, '
                             'select the folder that holds them, or upload them).')
            return False
        dir_a, dir_b, info = fi.materialize_two_directions(files, work)
        # Secret Sauce must compare the SAME two directions the Viewer + Splice
        # Report use — not every group. On a >2-group span (e.g. Miller↔Topeka's
        # MILTOP/TOPMIL plus the short-shot MILTOPSH/TOPMILSH) feeding ALL files
        # here made Secret Sauce mix full + short traces and disagree with the
        # other tools about which fibers exist.
        chosen = [p for p in files
                  if fi.direction_prefix(p) in (info['a_prefix'], info['b_prefix'])]
        combined = fi.materialize_all(chosen, os.path.join(work, 'all'))
    except ValueError as exc:                          # not exactly two directions
        st.sidebar.error(str(exc))
        return False
    except Exception as exc:                           # bad zip, IO, …
        st.sidebar.error(f'Could not load that folder/zip: {exc}')
        report_error('unified span loader', exc, {'src': src_label})
        return False
    ila_a, _ = _derive_ila(dir_a)
    ila_b, _ = _derive_ila(dir_b)
    # Fill the shared slots every page already reads.
    st.session_state['view_dir_a_input'] = dir_a       # Viewer + Splice Report (A)
    st.session_state['view_dir_b_input'] = dir_b       # Viewer + Splice Report (B)
    st.session_state['ss_folder_input'] = combined     # Secret Sauce (one folder)
    st.session_state['sr_input_mode'] = 'Two folders (A + B)'
    st.session_state['sr_site_a'] = ila_a or info['a_prefix']
    st.session_state['sr_site_b'] = ila_b or info['b_prefix']
    st.session_state['sr_site_src'] = (dir_a, dir_b)   # so the SR page keeps these
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
    }
    return True


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
        st.session_state['viewer_jump_announce'] = True   # one-shot caption
        st.session_state['came_from_dupcheck'] = True
        st.session_state['nav_radio'] = 'Viewer'   # set BEFORE the radio widget
        st.query_params.clear()
        return
    if qp.get('nav') == 'viewer' and qp.get('fiber'):
        # Splice Report / Unidirectional cell click: the link carries the
        # run's own dirs (incl. one-folder/zip staging) — seed the viewer
        # slots so the fresh session resolves the SAME span the grid was
        # built from, instead of whatever stale folders the process-global
        # server config held.
        _sra, _srb = qp.get('sra'), qp.get('srb')
        if _sra and os.path.isdir(_sra):
            st.session_state['view_dir_a_input'] = _sra
        if _srb and os.path.isdir(_srb):
            st.session_state['view_dir_b_input'] = _srb
        st.session_state['viewer_target'] = {
            'fiber': qp.get('fiber'),
            'km': qp.get('km'),
            'dir': qp.get('dir', 'both'),
        }
        # `src` names the report the click came from, so the Viewer can offer
        # the right "← Back" AND the origin page can restore its report from
        # the disk cache after this nav wiped session_state.
        _src = qp.get('src')
        if _src == 'sr':
            st.session_state['came_from_splicereport'] = True
        elif _src == 'uni':
            st.session_state['came_from_uni'] = True
            if _sra and os.path.isdir(_sra):
                st.session_state['uni_folder_input'] = _sra
        st.session_state['viewer_jump_announce'] = True   # one-shot caption
        st.session_state['nav_radio'] = 'Viewer'   # set BEFORE the radio widget
        st.query_params.clear()

_handle_nav()

# ─── Sidebar nav ─────────────────────────────────────────────────────────
st.session_state.setdefault('nav_radio', 'Viewer')
with st.sidebar:
    st.markdown('## 🔬 OTDR Suite')

    # ── Load span (both directions) → all three tools at once ──────────────
    _span = st.session_state.get('span_loaded')
    with st.expander('📂 Load span (both directions)', expanded=not _span):
        st.caption('One folder — or its .zip(s) — holding BOTH directions. '
                   'Per-direction zips (e.g. HOWLAN.zip + LANHOW.zip) are fine; '
                   'they\'re extracted for you. One click loads all three tools.')
        if st.button('📁 Choose folder', use_container_width=True, key='span_browse'):
            p = pick_folder('Choose a folder containing both directions')
            if p:
                st.session_state['span_folder'] = p
            elif p is None:
                st.session_state['_picker_unavailable'] = True
        if st.session_state.get('_picker_unavailable'):
            st.caption('⚠ The folder picker isn\'t available in this build — '
                       'paste the folder path below, or upload the .zip(s).')
        st.text_input('Folder (paste the path if Browse does nothing)',
                      key='span_folder', label_visibility='collapsed',
                      placeholder='paste or choose a folder with both directions')
        _zf = st.file_uploader('…or upload the .zip(s) — both directions',
                               type=['zip'], accept_multiple_files=True,
                               key='span_zip')
        if st.button('⬆ Load into all tools', type='primary',
                     use_container_width=True, key='span_load'):
            if _load_span((st.session_state.get('span_folder') or '').strip().strip('"'), _zf):
                st.rerun()
    if _span:
        st.success(f"✓ **{_span['ila_a']} ↔ {_span['ila_b']}**  ·  A {_span['a_count']} / "
                   f"B {_span['b_count']} files — loaded in all three tools")
        if _span.get('dropped'):
            st.warning(
                "⚠ This span had more than two direction groups; only **"
                f"{_span['a_prefix']}** + **{_span['b_prefix']}** were loaded "
                f"(into all three tools). Ignored: **{', '.join(_span['dropped'])}** "
                "— e.g. short-shot / FEC traces. If you meant a different pair, "
                "load just those two.")
    st.divider()

    page = st.radio('Tool', ['Viewer', 'Splice Report', 'Unidirectional',
                             'Secret Sauce'],
                    key='nav_radio', label_visibility='collapsed')
    st.divider()


# ═════════════════════════════════════════════════════════════════════════
#  PAGE: Viewer
# ═════════════════════════════════════════════════════════════════════════
# Per-session cache: a Viewer folder input that is a .zip (or a folder holding
# zips) is extracted ONCE to a temp dir, keyed on the source path, so the Viewer
# doesn't re-unzip on every Streamlit rerun.
_VIEWER_DIR_CACHE = {}


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
    try:
        _zsig = os.path.getmtime(p) if is_zip else None
    except OSError:
        _zsig = None
    cached = _VIEWER_DIR_CACHE.get(p)
    if isinstance(cached, tuple):
        _csig, cached_dir = cached
    else:                                   # legacy entry
        _csig, cached_dir = None, cached
    if (cached_dir and os.path.isdir(cached_dir)
            and trace_server.list_fibers(cached_dir)
            and _csig == _zsig):
        return cached_dir, 'viewing from .zip'
    try:
        dest = tempfile.mkdtemp(prefix='viewer_zip_')
        files = (fi.extract_zip(p, os.path.join(dest, 'unzipped')) if is_zip
                 else fi.find_otdr_files_with_zips(p, os.path.join(dest, 'zips')))
        if not files:
            return p, None        # nothing extractable; fall through to the folder
        # Flatten everything discoverable into one dir the trace server can list
        # (extract_zip / find_otdr_files_with_zips may leave files in subfolders).
        flat = fi.materialize_all(files, os.path.join(dest, 'all'))
        _VIEWER_DIR_CACHE[p] = (_zsig, flat)
        return flat, 'viewing from .zip'
    except Exception as exc:                           # bad zip / IO
        return '', f'could not read that .zip ({exc})'


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

        # Resolve each input (a folder, a .zip, or a folder holding zip(s)) to a
        # directory the trace server can list — so a zipped SOR span views
        # without the bidirectional 'Load span' flow.
        dir_a, _a_note = _resolve_viewer_dir(st.session_state.get('view_dir_a_input'))
        dir_b, _b_note = _resolve_viewer_dir(st.session_state.get('view_dir_b_input'))

        # Validate + push into the trace server's shared config.
        warn = []
        if _a_note and _a_note.startswith('could not'):
            warn.append(f'A: {_a_note}')
            dir_a = ''
        if _b_note and _b_note.startswith('could not'):
            warn.append(f'B: {_b_note}')
            dir_b = ''
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
    if not dir_a and not dir_b:
        st.info('Pick an A and/or B folder of OTDR `.sor` / `.json` files in the '
                'sidebar, then type fiber numbers in the viewer to plot them.')
    # Embed the canvas viewer.  Cache-bust on folder change so the iframe
    # re-reads /api/list.  A deep-link target is appended so the viewer
    # auto-loads:  a single fiber + km (Splice Report cell), OR a pair of
    # fibers overlaid (Duplicate Check "Stay in app").
    from urllib.parse import urlencode
    q = {'b': abs(hash((dir_a, dir_b))) % 100000}
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
            st.caption(f"Overlaying duplicate-pair fibers {tgt['fibers']} "
                       f"(direction {q['dir'].upper()})")
    elif tgt and tgt.get('fiber'):
        q['fiber'] = tgt['fiber']
        if tgt.get('km'):
            q['km'] = tgt['km']
        q['dir'] = tgt.get('dir', 'both')
        if announce:
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
    # Normalize to an absolute path (as the Viewer and Splice Report pages do)
    # before we build the output dir inside it — a relative/CWD-dependent folder
    # would put SecretSauce_reports somewhere the engine can't reliably write.
    folder = os.path.abspath(folder)

    out_format = st.radio('Output', ['Excel (xlsx)', 'PDF', 'Stay in app'],
                          horizontal=True)
    fmt = {'Excel (xlsx)': 'xlsx', 'PDF': 'pdf'}.get(out_format, 'pairs')

    st.caption("⏳ Large folders can take several minutes. After you click you'll see "
               "live progress here — **leave this window open and don't refresh.**")
    if st.button('Run analysis', type='primary'):
        out_dir = os.path.join(folder, 'SecretSauce_reports')
        st.session_state['ss_pending_cmd'] = secretsauce_cmd(folder, out_dir, fmt)
        st.session_state['ss_out_dir'] = out_dir
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
        except subprocess.TimeoutExpired:
            st.error(f'Secret Sauce timed out after {ENGINE_TIMEOUT_S}s '
                     'and was stopped. Try a smaller folder, or check for a '
                     'wedged engine.')
            report_error("secret sauce — timeout",
                         RuntimeError(f"engine exceeded {ENGINE_TIMEOUT_S}s"),
                         {"folder": folder, "format": fmt})
            return
        if proc is None:
            return                                     # cancelled — clean slate
        manifest = _parse_manifest(proc.stdout)
        if manifest is None:
            st.error('Secret Sauce did not return a result.')
            with st.expander('Engine log'):
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
            with st.expander('Engine log'):
                st.code(proc.stderr[-4000:] or '(no output)')
            report_error("secret sauce — engine returned not-ok",
                         RuntimeError(manifest.get('error', 'analysis failed')),
                         {"counts": manifest.get('counts'), "format": fmt},
                         log=proc.stderr)
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
    if res.get('pairs_truncated') or n_pairs_total > len(pairs):
        st.caption(f"Showing the top {len(pairs)} most-likely-duplicate pairs "
                   f"of {n_pairs_total:,} (worst-first); the rest are "
                   f"low-likelihood non-duplicates.")
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
    ("midspan_reflectance",       "Mid-span reflectance",       -50.0,        "dB",    True),
    ("fiber_section_atten",       "Fiber section attenuation",  0.400,        "dB/km", False),
    ("span_loss",                 "Span loss",                  20.000,       "dB",    False),
    ("span_length",               "Span length",                0.0000,       "km",    False),
    ("span_orl",                  "Span ORL",                   15.00,        "dB",    False),
    # Bend/damage clusters within this distance of a validated splice column
    # stay IN that splice column (cells keep their bend labels); farther out
    # they get their own "Bends @ X km" column.  Unchecking reverts to the
    # legacy 75 m gate (Platteville-Cheyenne: short-lay fibers put splice
    # events 107-128 m before the column and grew phantom bend columns).
    ("bend_fold_distance",        "Bend fold distance",         0.200,        "km",    True),
]
# Pre-checked rows (match what the splice report flags out of the box):
OTDR_DEFAULT_APPLY = {"unidir_splice_loss", "bidir_splice_loss",
                       "bidir_connector_loss", "reflectance",
                       "midspan_reflectance", "bend_fold_distance"}

# Rows whose Warning threshold differs from Fail (most rows use a single
# threshold, warning == fail).  Mid-span reflectance is a BAND: Fail at the
# strong end (-50 dB), Warning floor at the weak end (-80 dB).
_OTDR_WARN_DEFAULT = {"midspan_reflectance": -80.0}

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
                        "bidir_connector_loss", "reflectance",
                        "midspan_reflectance", "bend_fold_distance"},
        "thresholds": {
            "bidir_splice_loss":     0.120,
            "unidir_splice_loss":    0.200,
            "bidir_connector_loss":  0.400,
            "reflectance":          -50.0,
        },
    },
    "Zayo": {
        "apply":      {"bidir_splice_loss", "bidir_connector_loss",
                        "midspan_reflectance", "bend_fold_distance"},
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
    "midspan_reflectance":  "MIDSPAN_REFL_FAIL_DB",
    "bend_fold_distance":   "BEND_SPLICE_FOLD_KM",
}
# Rows that ALSO push a separate Warning-threshold global to the engine.
_OTDR_KEY_TO_WARN_GLOBAL = {
    "midspan_reflectance":  "MIDSPAN_REFL_WARN_DB",
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
}


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
    out = {}
    settings = otdr_settings or {}
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
        else:
            # OFF → sentinel the gate global(s) so the detection never fires.
            # Distance-tuning rows (see _OTDR_KEY_DISABLE_VALUE) send their
            # legacy-behavior value instead — 1e9 would invert their meaning.
            off_val = _OTDR_KEY_DISABLE_VALUE.get(row_key, _OTDR_DISABLE_SENTINEL)
            out[engine_global] = off_val
            if warn_global:
                out[warn_global] = off_val
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
        if _ov:
            st.caption('Active overrides → ' + ', '.join(
                f'{k} = {v:g}' for k, v in sorted(_ov.items())))
        else:
            st.caption('No overrides active — engine defaults in effect.')

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

    st.markdown('**Cable type (helix factor)**')

    def _fmt(key):
        e = entries.get(key)
        if not e:
            return key
        return (f"{e.label}  —  m {e.m_low:.3f}–{e.m_high:.3f} "
                f"(EFL {e.efl_low:.1f}–{e.efl_high:.1f}%)")

    _cur = st.session_state['cable_type']
    _picked = st.selectbox(
        'Cable type', options,
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
    'deadzone': '#7f8c8d', 'event': '#555',
}

def page_splice_report():
    st.markdown('#### Splice Report — bidirectional')
    st.caption('Generates the Excel report (saved to your **Downloads**) and a '
               'clickable grid — click any flagged cell to jump to that fiber and '
               'splice in the Viewer. Give it two A/B folders, or one folder / .zip '
               'holding both directions.')

    # Input mode: two A/B folders (shared with the Viewer) OR a single folder /
    # .zip that holds both directions (auto-split by direction).
    mode = st.radio('Input', ['Two folders (A + B)',
                              'One folder / zip (both directions)'],
                    horizontal=True, key='sr_input_mode')

    if mode == 'Two folders (A + B)':
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
    else:
        c1, c2 = st.columns(2)
        with c1:
            if st.button('📁 Folder with BOTH directions', use_container_width=True,
                         key='sr_browse_one'):
                p = pick_folder('Choose a folder containing both directions')
                if p:
                    st.session_state['sr_one_folder'] = p
            st.text_input('Folder (both directions)', key='sr_one_folder',
                          placeholder='one folder with both directions of .sor files')
        with c2:
            zf = st.file_uploader('…or upload a .zip of both directions',
                                  type=['zip'], key='sr_zip')
        dir_a, dir_b = _resolve_bidir_from_single(
            (st.session_state.get('sr_one_folder') or '').strip().strip('"'), zf)

    # Auto-derive the real ILA/site names from the SOR GenParams so the report
    # shows WHICH ILA is the A-direction and which is the B-direction (instead of
    # a literal "A"/"B").  Re-derive when the folder pair changes; the tech can
    # still override the fields below.  Keyed-state pattern (set session_state
    # BEFORE the widget) — never mix value= and key= on a widget we write to.
    if dir_a and dir_b and os.path.isdir(dir_a) and os.path.isdir(dir_b):
        _sig = (dir_a, dir_b)
        if st.session_state.get('sr_site_src') != _sig:
            _ila_a, _ = _derive_ila(dir_a)
            _ila_b, _ = _derive_ila(dir_b)
            st.session_state['sr_site_a'] = _ila_a or 'A'
            st.session_state['sr_site_b'] = _ila_b or 'B'
            st.session_state['sr_site_src'] = _sig
    st.session_state.setdefault('sr_site_a', 'A')
    st.session_state.setdefault('sr_site_b', 'B')

    s1, s2 = st.columns(2)
    site_a = s1.text_input('A-direction ILA / site', key='sr_site_a')
    site_b = s2.text_input('B-direction ILA / site', key='sr_site_b')
    if site_a and site_b and (site_a, site_b) != ('A', 'B'):
        st.caption(f"📍 **A direction:** {site_a} → {site_b}  ·  "
                   f"**B direction:** {site_b} → {site_a}")

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

    st.caption("⏳ Large spans can take several minutes. After you click you'll see "
               "live progress here — **leave this window open and don't refresh.**")
    if st.button('Generate Splice Report', type='primary'):
        # Save the report to the user's Downloads — NOT the traces folder (which
        # in one-folder/zip mode is a temp dir that gets cleaned up).
        import folder_intake as _fi
        _safe = lambda s: ''.join(c if (c.isalnum() or c in ' -_') else '_' for c in str(s)).strip() or 'site'
        out_xlsx = os.path.join(_fi.default_report_dir(),
                                f'{_safe(site_a)}_to_{_safe(site_b)}_SpliceReport.xlsx')
        # Read the panel values straight out of session_state (which the
        # component's auto-commit keeps current) and translate to engine
        # globals.  This is the value the run actually uses — see the
        # iframe-state footgun note in _render_otdr_settings_panel.
        overrides = _overrides_from_settings(st.session_state.get('otdr_settings'))
        st.session_state['sr_pending_cmd'] = splicereport_cmd(
            dir_a, dir_b, out_xlsx, site_a, site_b, overrides=overrides)
        # The dirs this run used — cell-click deep links carry them so the
        # Viewer (a FRESH session after the anchor nav) can find the span,
        # including one-folder/zip runs staged into temp dirs the viewer was
        # never told about (the boss's 'clicks a cell, trace never loads').
        st.session_state['sr_dirs'] = (dir_a, dir_b)
        st.session_state.pop('sr_result', None)        # clear any prior result
        st.rerun()

    # Background run with a live progress panel + Cancel; the engine runs as a
    # concurrent subprocess so the page never freezes.  Stashes sr_result on done.
    if 'sr_pending_cmd' in st.session_state or 'sr_job' in st.session_state:
        try:
            proc = run_engine_live('sr', running_title='Generating the splice report')
        except subprocess.TimeoutExpired:
            st.error(f'Splice report timed out after {ENGINE_TIMEOUT_S}s '
                     'and was stopped. Try fewer files, or check for a '
                     'wedged engine.')
            report_error('splice report (hub) — timeout',
                         RuntimeError(f"engine exceeded {ENGINE_TIMEOUT_S}s"),
                         {'dir_a': dir_a, 'dir_b': dir_b})
            proc = None
        if proc is not None:
            manifest = _parse_manifest(proc.stdout)
            if manifest is None or not manifest.get('ok'):
                st.error((manifest or {}).get('error', 'Splice report failed.'))
                with st.expander('Engine log'):
                    st.code(proc.stderr[-4000:] or '(no output)')
                report_error('splice report (hub)',
                             RuntimeError((manifest or {}).get('error', 'no manifest')),
                             {'dir_a': dir_a, 'dir_b': dir_b},
                             log=proc.stderr)
            else:
                st.session_state['sr_result'] = manifest
                # Disk cache (same idea as Secret Sauce's pairs_cache.json):
                # a cell-click into the Viewer is a URL nav that WIPES
                # session_state — this file is how "← Back" re-shows the grid
                # without re-running the multi-minute engine.
                try:
                    _sd = st.session_state.get('sr_dirs') or (None, None)
                    if _sd[0] and os.path.isdir(_sd[0]):
                        with open(os.path.join(_sd[0], '.sr_grid_cache.json'),
                                  'w', encoding='utf-8') as fh:
                            json.dump({'manifest': manifest, '_dirs': list(_sd)}, fh)
                except Exception:
                    pass

    res = st.session_state.get('sr_result')
    if not (res and res.get('ok')):
        # Back from the Viewer (or any session reset): restore the last grid
        # from the disk cache.  Candidate dirs: this page's own sr_dirs if it
        # survived, else the viewer slots the deep link seeded (sra/srb).
        for _cand in (st.session_state.get('sr_dirs'),
                      (st.session_state.get('view_dir_a_input'),
                       st.session_state.get('view_dir_b_input'))):
            if not (_cand and _cand[0] and os.path.isdir(_cand[0])):
                continue
            try:
                with open(os.path.join(_cand[0], '.sr_grid_cache.json'),
                          encoding='utf-8') as fh:
                    _cached = json.load(fh)
                if (_cached.get('manifest', {}).get('ok')
                        and _cached.get('_dirs', [None])[0] == _cand[0]):
                    res = _cached['manifest']
                    st.session_state['sr_result'] = res
                    st.session_state['sr_dirs'] = tuple(_cached['_dirs'])
                    break
            except Exception:
                continue
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
    # Deep-link support: viewer frame conversion + span-dir carriage.
    _mani = st.session_state.get('sr_result') or {}
    _launch_a = float(_mani.get('launch_a_km') or 0.0)
    def _vkm(km):
        return round(float(km) + _launch_a, 4)
    _sd = st.session_state.get('sr_dirs') or (None, None)
    from urllib.parse import quote as _q
    _dirs_qs = ''
    if _sd[0] and os.path.isdir(_sd[0]):
        _dirs_qs += f"&sra={_q(_sd[0])}"
    if _sd[1] and os.path.isdir(_sd[1]):
        _dirs_qs += f"&srb={_q(_sd[1])}"
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
                href = (f"?nav=viewer&fiber={c['fiber']}&km={_vkm(c['km'])}"
                        f"&dir=both{_dirs_qs}&src=sr")
                links.append(f"<a href='{href}' target='_self' title='{c['label']}' "
                             f"style='color:{color};text-decoration:none;font-weight:600'>F{c['fiber']}{loss}</a>")
            html.append("<td style='padding:3px 6px;border:1px solid #eef2f6;white-space:nowrap'>"
                        + "<br>".join(links) + "</td>")
        html.append('</tr>')
    html.append('</tbody></table></div>')
    st.markdown(''.join(html), unsafe_allow_html=True)


# ═════════════════════════════════════════════════════════════════════════
#  PAGE: Unidirectional (A-only one-shot)  — splice report engine, --uni mode
# ═════════════════════════════════════════════════════════════════════════
def uni_cmd(folder, out_xlsx, direction=None, overrides=None, landmarks=None):
    """Argv for the unidirectional one-shot — the splice report engine's
    --uni mode (same subprocess, same sor_reader isolation, ZK-format
    workbook out)."""
    common = ['--uni', '--dir-a', folder, '--out', out_xlsx]
    if direction:
        common += ['--direction', direction]
    if landmarks:
        common += ['--landmarks', json.dumps(landmarks)]
    if overrides:
        common += ['--overrides', json.dumps(overrides)]
    if FROZEN:
        return [sys.executable, '--run-splicereport', *common]
    return [sys.executable, os.path.join(SPLICEREPORT_DIR, 'run_splicereport.py'), *common]


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


def page_unidirectional():
    st.markdown('#### Unidirectional one-shot')
    st.caption('One folder, one direction — finds splice closures, possible '
               'bend/damage, and breaks from A-side traces alone.  Output is '
               'the ribbon-grid workbook (Zach-approved format).')

    st.session_state.setdefault('uni_folder_input', '')
    c1, c2 = st.columns([1, 2])
    with c1:
        if st.button('📁 Browse for folder', type='primary', use_container_width=True):
            p = pick_folder('Choose a folder of OTDR files')
            if p:
                st.session_state['uni_folder_input'] = p
    with c2:
        st.text_input('…or paste a folder path',
                      key='uni_folder_input',
                      placeholder=r'C:\Users\you\Desktop\uni shots')

    folder = (st.session_state.get('uni_folder_input') or '').strip().strip('"')
    if not folder or not os.path.isdir(folder):
        st.info('👆 Choose the folder that holds the one-direction `.sor` / '
                '`.json` shots.')
        return
    folder = os.path.abspath(folder)

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
            pick = st.selectbox('Direction', opts, key='uni_dir_pick')
            if pick != '(most populous)':
                dir_choice = pick.rsplit('  (', 1)[0]

    with st.expander('Job landmarks (optional — closure map / handholes)'):
        st.caption('One per line: `km, label` — or `km, label, splice` for a '
                   'known closure.  Labels print on the grid’s Handholes '
                   'row; a NON-closure landmark (handhole, replaced section…) '
                   'sitting on a detected splice column demotes it to '
                   'Bend/Damage.  Example:')
        st.code('0.57, Replaced section\n4.05, HH8\n7.91, HH4, splice',
                language=None)
        st.text_area('Landmarks', key='uni_landmarks_text', height=120,
                     label_visibility='collapsed',
                     placeholder='4.05, HH8')
    landmarks, bad_lines = _parse_landmarks_text(
        st.session_state.get('uni_landmarks_text'))
    if bad_lines:
        st.warning('Skipped landmark line(s) with no leading km: '
                   + ' · '.join(bad_lines[:3]))

    st.caption('⏳ Large folders can take a few minutes — leave this window '
               'open and don’t refresh.')
    if st.button('Run unidirectional report', type='primary'):
        out_xlsx = os.path.join(folder, 'unidirectional_events.xlsx')
        st.session_state['uni_pending_cmd'] = uni_cmd(folder, out_xlsx,
                                                      direction=dir_choice,
                                                      landmarks=landmarks)
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
            st.error('The unidirectional report did not return a result.')
            with st.expander('Engine log'):
                st.code(proc.stderr[-4000:] or '(no output)')
            report_error("unidirectional — no manifest",
                         RuntimeError("runner returned no JSON manifest"),
                         {"returncode": proc.returncode}, log=proc.stderr)
            return
        if not manifest.get('ok'):
            st.error(manifest.get('error', 'Analysis failed.'))
            with st.expander('Engine log'):
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
            with open(os.path.join(folder, '.uni_result_cache.json'),
                      'w', encoding='utf-8') as fh:
                json.dump(manifest, fh)
        except Exception:
            pass

    res = st.session_state.get('uni_result')
    if not (res and res.get('ok') and res.get('_folder') == folder):
        # Back from the Viewer (session reset): restore from the disk cache.
        try:
            with open(os.path.join(folder, '.uni_result_cache.json'),
                      encoding='utf-8') as fh:
                _cached = json.load(fh)
            if _cached.get('ok') and _cached.get('_folder') == folder:
                res = _cached
                st.session_state['uni_result'] = res
        except Exception:
            pass
    if not (res and res.get('ok') and res.get('_folder') == folder):
        return
    u = res.get('uni') or {}
    st.success(f"Done — {u.get('n_fibers', '?')} fibers · direction "
               f"{u.get('direction', '?')} · span ≈ {u.get('span_km', '?')} km")
    counts = u.get('direction_counts') or {}
    if len(counts) > 1:
        st.warning(f"This folder mixes {len(counts)} directions — the report "
                   "covers the one shown above.  Pick another from the "
                   "Direction list and re-run to cover it.")
    cols = st.columns(4)
    cols[0].metric('Splice columns', len(u.get('splice_columns') or []))
    cols[1].metric('Bend/Damage columns', len(u.get('bend_columns') or []))
    cols[2].metric('Break columns', len(u.get('break_columns') or []))
    rp = u.get('reburn_pct')
    cols[3].metric('Reburn', f"{rp:.2f}%" if rp is not None else '—')
    detail = []
    if u.get('splice_columns'):
        detail.append('Splices @ ' + ', '.join(f"{v:.2f} km" for v in u['splice_columns']))
    if u.get('bend_columns'):
        detail.append('Bend/Damage @ ' + ', '.join(f"{v:.2f} km" for v in u['bend_columns']))
    if u.get('break_columns'):
        detail.append(f"Breaks ({u.get('n_breaks', '?')} fibers) @ "
                      + ', '.join(f"{v:.2f} km" for v in u['break_columns']))
    if detail:
        st.caption(' · '.join(detail))
    if u.get('prebreak_damage_fibers'):
        st.caption(f"Pre-break damage: {u['prebreak_damage_fibers']} broken "
                   "fiber(s) show trace-measured damage ahead of their break "
                   "point (dying fibers are measured off the raw trace — the "
                   "0.1 dB rule doesn’t apply to them).")
    if u.get('demoted_columns'):
        st.caption('Landmark demotions (splice → bend/damage): '
                   + ', '.join(f"{v:.2f} km" for v in u['demoted_columns']))
    if not u.get('launch_box'):
        st.caption('No launch box detected on this shoot — events past 0.3 km '
                   'are reported as plant (no launch-reel exclusion applied).')

    # ── In-app clickable ribbon grid: every fiber → the Viewer ──
    if u.get('grid_columns') and u.get('cells') is not None:
        st.markdown('###### Click a fiber → jump to it in the Viewer')
        gcols = u['grid_columns']
        rs = int(u.get('ribbon_size') or 12)
        max_f = int(u.get('max_fiber') or u.get('n_fibers') or 0)
        n_ribbons = (max_f + rs - 1) // rs if max_f else 0
        off = float(u.get('launch_offset_km') or 0.0)
        by_rc = {}
        for c in u['cells']:
            by_rc.setdefault(((c['fiber'] - 1) // rs, c['col']), []).append(c)
        _KIND_COLOR = {'splice': '#1f4e79', 'bend_damage': '#8a6d00',
                       'break': '#c00000'}
        from urllib.parse import quote as _q
        _fq = _q(folder, safe='')
        html = ['<div style="overflow:auto;max-height:62vh;border:1px solid #c9d5e1;'
                'border-radius:4px;color:#1f2a36;background:#ffffff">',
                '<table style="border-collapse:collapse;font-size:11px;'
                'font-family:Consolas,monospace">',
                '<thead><tr><th style="position:sticky;left:0;background:#eef3f8;'
                'padding:4px 8px;border:1px solid #dbe4ee">Ribbon</th>']
        for gc in gcols:
            lm = (f"<div style='font-size:9px;color:#977'>{gc['landmark']}</div>"
                  if gc.get('landmark') else '')
            html.append(f"<th style='padding:4px 8px;border:1px solid #dbe4ee;"
                        f"background:#eef3f8;white-space:nowrap'>"
                        f"<div style='font-weight:600'>{gc['label']}</div>"
                        f"<div style='font-size:10px;color:#789'>{gc['km']:.2f} km</div>"
                        f"{lm}</th>")
        html.append('</tr></thead><tbody>')
        for ri in range(n_ribbons):
            f0, f1 = ri * rs + 1, min((ri + 1) * rs, max_f)
            html.append(f"<tr><td style='position:sticky;left:0;background:#f7fafc;"
                        f"padding:3px 8px;border:1px solid #e3e9f0;"
                        f"white-space:nowrap'>F{f0}–{f1}</td>")
            for ci, gc in enumerate(gcols):
                cell = by_rc.get((ri, ci), [])
                if not cell:
                    html.append("<td style='padding:3px 6px;border:1px solid #eef2f6'></td>")
                    continue
                links = []
                for c in sorted(cell, key=lambda x: x['fiber']):
                    color = _KIND_COLOR.get(c['kind'], '#555')
                    loss = (' ✕ broke' if c['loss'] is None
                            else f" {c['loss']:.3f}")
                    href = (f"?nav=viewer&fiber={c['fiber']}"
                            f"&km={round(c['km'] + off, 4)}&dir=a"
                            f"&sra={_fq}&src=uni")
                    links.append(f"<a href='{href}' target='_self' "
                                 f"style='color:{color};text-decoration:none;"
                                 f"font-weight:600'>F{c['fiber']}{loss}</a>")
                html.append("<td style='padding:3px 6px;border:1px solid #eef2f6;"
                            "white-space:nowrap'>" + "<br>".join(links) + "</td>")
            html.append('</tr>')
        html.append('</tbody></table></div>')
        st.markdown(''.join(html), unsafe_allow_html=True)

    out_xlsx = res.get('out') or st.session_state.get('uni_out_xlsx', '')
    if out_xlsx and os.path.exists(out_xlsx):
        with open(out_xlsx, 'rb') as fh:
            st.download_button(f"⬇ {os.path.basename(out_xlsx)}", data=fh.read(),
                               file_name=os.path.basename(out_xlsx),
                               key='uni_dl')
        st.caption(f'Saved to: {out_xlsx}')


# ─── Route ────────────────────────────────────────────────────────────────
# Global catch-all: any unhandled error during a page render/action posts to
# Slack, then re-raises so Streamlit still shows the tech its red error box.
try:
    if page == 'Viewer':
        page_viewer()
    elif page == 'Splice Report':
        page_splice_report()
    elif page == 'Unidirectional':
        page_unidirectional()
    else:
        page_duplicate_check()
except Exception as _exc:
    report_error(f"hub page: {page}", _exc)
    raise

# ─── Sidebar footer: build identity ───────────────────────────────────────
# Rendered LAST so it sits at the bottom of the sidebar, below any page-
# specific widgets.  "app build N (date)" identifies the frozen exe (CI stamp);
# "engine: ..." identifies the code the launcher chose at boot (bundled vs a
# verified signed update) — so the boss can confirm a tech runs the latest of
# BOTH.  Dev runs collapse to a plain "dev".
_appv, _engv = _app_version(), _engine_version()
if _appv == 'dev' and _engv == 'dev':
    st.sidebar.caption('OTDR Suite · dev')
else:
    st.sidebar.caption(f'OTDR Suite · app {_appv} · engine: {_engv}')

# Rollout ping: when the build identity changed since the last run (the
# launcher applied a verified update, or a fresh install's first boot), tell
# the shared Slack channel — per-machine confirmation without footer-reading.
# Marker-deduped to once per version; silent no-op in dev / without a webhook.
try:
    maybe_report_update()
except Exception:
    pass

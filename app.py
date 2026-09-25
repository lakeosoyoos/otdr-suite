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


def _render_analysis_mode_control():
    """The OTDR Suite / FastReporter switch, right under the Tool list so
    it is visible on every page.  Seeded from settings.json on the first run of a
    session and written back on every change, so a tech's choice survives a
    restart.  Same key= discipline as the profile picker: the toggle's own
    key holds the switch position, session_state.analysis_mode holds the mode."""
    if 'analysis_mode' not in st.session_state:
        st.session_state['analysis_mode'] = load_analysis_mode()
    _on = st.session_state['analysis_mode'] == 'fr'
    # A toggle, not a radio (Robert, 2026-09-22): the setting is one of two
    # states and reads as a switch -- off is OTDR Suite, on is FastReporter.
    # The widget's own key holds the switch position; session_state.
    # analysis_mode holds the mode, and a stale key from an older build is
    # dropped before the widget is drawn so value= never fights key=.
    # Robert, 2026-09-24: both modes on show, FR Mode on the left and OTDR
    # Mode on the right, the switch between them; the knob points at the
    # mode in use and that name is bold.  Knob right = OTDR Mode.  A new key
    # (the old 'analysis_toggle' meant the opposite), and value= only when the
    # key is not already set, so value= never fights key=.
    st.markdown('**Analysis Mode**')
    if not isinstance(st.session_state.get('analysis_switch'), bool):
        st.session_state['analysis_switch'] = not _on
    l, m, r = st.columns([5, 3, 5], vertical_alignment='center')
    l.markdown(('**FR Mode**' if _on else 'FR Mode'),
               help=("FR Mode: reproduce EXFO FastReporter's analysis from the same "
                     "files, to the digit, with only your pass/fail thresholds on top."))
    _right = m.toggle('Analysis mode', key='analysis_switch', label_visibility='collapsed')
    r.markdown(('**OTDR Mode**' if not _on else 'OTDR Mode'),
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
                     contract=None, show=None):
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
        st.info(f'⏳ {running_title}: {elapsed}s elapsed. '
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
    'download on its own, so Update & restart will not apply it. Different '
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
    if _needs_install():
        st.error(INSTALL_BLOCK_MSG.format(latest=latest, running=running,
                                          url=INSTALLER_URL))
        return stale
    st.error(STALE_BLOCK_MSG.format(latest=latest, running=running))
    if getattr(sys, 'frozen', False):
        if st.button('⬇ Update & restart now', key=f'{key}_stale_restart',
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
      + 'padding:8px 18px;font-size:14px;cursor:pointer">Reload this page</button>';
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
            st_components_html(_restart_watchdog_html(), height=40)
    else:
        st_components_html(_restart_watchdog_html(), height=40)


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
    if _needs_install():
        _render_install_notice(latest, running)
        return
    st.warning(f'Update {latest} is available (running {running}).')
    if getattr(sys, 'frozen', False):
        if st.button('⬇ Update & restart now', key='upd_nudge_restart',
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
    if st.button('Repair and restart', type='primary'):
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
    if st.button('Repair and restart', type='primary', key=f'repair_{key}'):
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


def _report_dest_row(key, default_dir):
    """The 'Save reports to' row every report page shows: a Browse button that
    opens the native folder picker, and a path box the tech can paste into.
    Returns the folder reports go to -- what the tech chose, else
    `default_dir`, which is the tech's Downloads folder on every page: the
    boss's rule for everything the suite saves, after a report written "next
    to the traces" landed beside a drag-and-drop staging copy in a temp folder.
    The default is shown as the placeholder so the tech sees where the report
    WILL land before running anything."""
    st.session_state.setdefault(key, '')
    c1, c2 = st.columns([1, 2])
    with c1:
        if st.button('📁 Save reports to…', use_container_width=True, key=key + '_browse'):
            p = pick_folder('Choose where to save the reports')
            if p:
                st.session_state[key] = p
            elif p is None:
                st.info('No folder picker on this machine. Paste the path instead.')
    with c2:
        st.text_input('Save reports to', key=key, placeholder=default_dir,
                      help='Leave blank to use the folder shown.')
    chosen = (st.session_state.get(key) or '').strip().strip('"')
    if chosen:
        parent = os.path.dirname(os.path.abspath(chosen)) or chosen
        if not os.path.isdir(chosen) and not os.path.isdir(parent):
            st.warning(f'That folder cannot be created: {chosen}. Reports will go to {default_dir}')
            return default_dir
        return os.path.abspath(chosen)
    return default_dir


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
    trace_uploads = [u for u in uploads if _ext(u, '.sor', '.json')]
    if bdr_uploads and (zip_uploads or trace_uploads):
        st.error('Drop either the .bdr files or the .sor/.json/.zip span, '
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
                st.error('No .sor / .json / .bdr files found in that folder/zip.')
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


def _load_span(folder, zip_file):
    """Load ONE span (a folder or a .zip holding BOTH directions) into ALL three
    tools at once: split into A/B (Viewer + Splice Report) and a combined folder
    (Secret Sauce), then populate the shared input slots every page reads.
    Returns True on success; renders its own sidebar message on failure."""
    import folder_intake as fi
    # zip_file may be a single uploaded file, a LIST of them (multi-upload —
    # per-direction zips like HOWLAN.zip + LANHOW.zip, loose .sor/.json
    # traces, a dropped folder's contents, or any mix), or None.
    uploads = ((list(zip_file) if isinstance(zip_file, (list, tuple)) else [zip_file])
               if zip_file else [])
    zips = [u for u in uploads if u.name.lower().endswith('.zip')]
    loose = [u for u in uploads if not u.name.lower().endswith('.zip')]
    if uploads:
        src_label = (', '.join(getattr(z, 'name', 'uploaded.zip') for z in zips)
                     or f'{len(loose)} dropped trace file(s)')
    elif folder and os.path.isdir(folder):
        src_label = os.path.basename(folder.rstrip('/\\')) or folder
    else:
        st.sidebar.warning('Pick a folder with both directions, or drop its '
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
        else:
            # A folder — which may itself CONTAIN the per-direction zips (spans
            # are often delivered that way), so descend into any zips found.
            files = fi.find_otdr_files_with_zips(folder, os.path.join(work, 'zips'))
        if not files:
            st.sidebar.error('No .sor / .json files found in that folder/zip '
                             '(if the span is split into per-direction zips, '
                             'select the folder that holds them, or upload them).')
            return False
        # Files shot on another job (different location pair AND a different
        # pulse/range) are excluded here, before the direction split, so they
        # neither spawn a junk direction group nor reach Secret Sauce.
        files, foreign = fi.audit_foreign_files(files)
        dir_a, dir_b, info = fi.materialize_two_directions(files, work)
        # Secret Sauce must compare the SAME two directions the Viewer + Splice
        # Report use — not every group. On a >2-group span (e.g. Miller↔Topeka's
        # MILTOP/TOPMIL plus the short-shot MILTOPSH/TOPMILSH) feeding ALL files
        # here made Secret Sauce mix full + short traces and disagree with the
        # other tools about which fibers exist.
        chosen = list(info['a_files']) + list(info['b_files'])
        combined = fi.materialize_all(chosen, os.path.join(work, 'all'))
    except ValueError as exc:                          # not exactly two directions
        st.sidebar.error(str(exc))
        return False
    except Exception as exc:                           # bad zip, IO, …
        st.sidebar.error(f'Could not load that folder/zip: {exc}')
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
    st.session_state['view_dir_a_input'] = dir_a       # Viewer + Splice Report (A)
    st.session_state['view_dir_b_input'] = dir_b       # Viewer + Splice Report (B)
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
             site_src=f'{pre}_site_src')
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
    }


def project_from_file_data(data, project_path):
    """(snapshot, span-1 markers) from a project file's JSON.  Raises
    ValueError on something that is not an OTDR Suite project."""
    if not isinstance(data, dict) or data.get('format') != PROJECT_FORMAT:
        raise ValueError('not an OTDR Suite project file')
    if int(data.get('version') or 0) > PROJECT_VERSION:
        raise ValueError('this project was saved by a newer OTDR Suite -- '
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
# page, laid out on the four sections of Lumen's Submittal Checklist.
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
}
# Run Traces is the trace tools only; the FQA Builder and Field Capture are
# project work (Robert, 2026-09-24).
TOOLS_TRACES = ['Viewer', 'Splice Report', 'Unidirectional', 'Secret Sauce']
TOOLS_PROJECT = ['Project Status'] + TOOLS_TRACES + ['FQA Builder', 'Field Capture']
# Home screen off (OTDR_HOME_SCREEN=0): the suite as main ships it, every tool.
TOOLS_ALL = TOOLS_TRACES + ['FQA Builder', 'Field Capture']


def _home_screen_enabled():
    """The test suite predates the home screen and drives the tools from the
    first run, so conftest turns it off (OTDR_HOME_SCREEN=0); the home
    screen's own tests turn it back on."""
    return os.environ.get('OTDR_HOME_SCREEN', '1') != '0'


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
    best = None
    try:
        names = sorted(n for n in os.listdir(folder) if n.lower().rstrip().endswith('.sor'))[:3]
    except OSError:
        return ''
    for n in names:
        try:
            with open(os.path.join(folder, n), 'rb') as fh:
                data = fh.read()
            ts = _sr._parse_fxd_params(data, _sr._parse_block_directory(data)).get('date_time') or 0
            if ts > 0:
                best = min(best, ts) if best else ts
        except Exception:
            continue
    return _dt.datetime.fromtimestamp(best).strftime('%Y-%m-%d') if best else ''


def sor_shot_date(folder):
    """'YYYY-MM-DD' the traces in `folder` were shot (the .sor header's
    date), or '' when there is no .sor to read."""
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
    project_open(path)
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
    ss.setdefault('view_dir_a_input', s1.get('dir_a') or ta)
    ss.setdefault('view_dir_b_input', s1.get('dir_b') or tb)
    ss.setdefault('uni_folder_input', s1.get('dir_a') or ta)
    _fs = final_shoot(work)
    ss.setdefault('ss_folder_input', _fs['dir'] if _fs else work_sub('traces', work))
    for key in ('sr_report_dest', 'uni_report_dest'):
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
            'uni_report_dest', 'fc_report_dest', 'fqa_dest', 'fqa_prod']
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


def _mode_actions():
    """Home-screen, Home-button and Project-status navigation clicks, read
    from session_state at the top of the run, before anything is drawn (the
    rerun trap: see _project_actions' history).  Returns (kind, message)
    for the home screen, or None."""
    ss = st.session_state
    if ss.get('go_home') or ss.get('setup_back'):
        ss.pop('app_mode', None)
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
    if ss.get('home_traces'):
        # OTDR Suite as it was: no project behind the tools, and a Viewer
        # click-through must not bring one back.
        for k in ('project_path', 'project_saved'):
            ss.pop(k, None)
        _settings_update(last_project=None)
        ss['app_mode'] = 'traces'
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
    # The top bar's Audit Project, from any tool: to Project status, audit on.
    if ss.get('bar_audit'):
        ss['audit_on'] = True
        ss['audit_skipped'] = []
        ss['nav_radio'] = 'Project Status'
    # Audit buttons that hand over to the status page (the audit is left
    # on pause; its button picks it up again from the top).
    if ss.get('aud_go_status') or ss.get('aud_go_phone'):
        ss['audit_on'] = False
    # Project status buttons that switch tools.
    for key, page in (('ps_go_viewer', 'Viewer'), ('ps_go_sr', 'Splice Report'),
                      ('ps_go_uni', 'Unidirectional'), ('ps_go_ss', 'Secret Sauce'),
                      ('ps_go_fqa', 'FQA Builder'), ('ps_go_fqa2', 'FQA Builder'),
                      ('ps_go_fqa3', 'FQA Builder'), ('ps_go_fc', 'Field Capture'),
                      ('aud_go_fqa', 'FQA Builder')):
        if ss.get(key):
            ss['nav_radio'] = page
    return None


def _render_home(msg):
    """The two-choice start screen.  No sidebar: nothing in it applies yet."""
    st.markdown('<style>[data-testid="stSidebar"],[data-testid="stSidebarCollapsedControl"]'
                '{display:none}</style>', unsafe_allow_html=True)
    _render_update_nudge()
    st.markdown('## 🔬 OTDR Suite')
    st.caption('What are you doing today?')
    # One column, three choices stacked, all the same blue (Robert, 2026-09-24).
    _l, mid, _r = st.columns([1, 2, 1])
    with mid:
        st.button('🔬 Quick Analysis', key='home_traces', type='primary',
                  use_container_width=True)
        st.caption('The Viewer, Splice Report, Unidirectional and Secret Sauce, '
                   'the way you use them today.')
        st.button('📁 Start Project', key='home_new', type='primary',
                  use_container_width=True)
        st.caption('Load what you have for a span. The project fills in everything it '
                   'can, then shows what the FQA package still needs.')
        st.button('📂 Open Recent Project', key='home_open_recent', type='primary',
                  use_container_width=True)
        st.caption('Pick up a project you or a colleague started.')
        if msg:
            getattr(st, msg[0])(msg[1])
    _appv, _engv = _app_version(), _engine_version()
    st.caption('OTDR Suite · dev' if (_appv, _engv) == ('dev', 'dev')
               else f'OTDR Suite · app {_appv} · engine: {_engv}')


def _render_project_sidebar():
    path = st.session_state.get('project_path') or ''
    st.markdown(f"**📁 {os.path.basename(work_dir(path)) or 'Project'}**")
    st.caption(f'`{work_dir(path)}` · saved automatically')


# ─── Deep-link nav: a Splice Report cell click lands as ?nav=viewer&fiber=&km=
#     → switch to the Viewer page + stash the target for the iframe URL. ──────
def _handle_nav():
    qp = st.query_params
    if qp.get('nav'):
        st.session_state['_nav_arrived'] = True     # the mode gate skips Home
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
try:
    _project_reattach()
except Exception as _exc:
    report_error('project: reattach after click-through', _exc)

# ─── Home screen / mode gate ──────────────────────────────────────────────
_home_msg = _mode_actions()
if st.session_state.get('app_mode') not in ('traces', 'project', 'setup'):
    if not _home_screen_enabled():
        st.session_state['app_mode'] = 'traces'
    elif st.session_state.get('project_path') and st.session_state.get('_project_reattached'):
        st.session_state['app_mode'] = 'project'
    elif st.session_state.get('_nav_arrived'):
        st.session_state['app_mode'] = 'traces'
if st.session_state.get('app_mode') not in ('traces', 'project', 'setup'):
    _render_home(_home_msg)
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

# ─── Sidebar nav ─────────────────────────────────────────────────────────
st.session_state.setdefault('nav_radio', 'Viewer')
with st.sidebar:
    # Home at the very top of the sidebar, in a project and in Run Traces.
    if _home_screen_enabled():
        st.button('🏠 Home', key='go_home', use_container_width=True)
    st.markdown('## 🔬 OTDR Suite')

    # Update nudge FIRST — above the tools, so a stale always-on machine sees
    # it before it starts working (the footer's manual check is still there).
    _render_update_nudge()

    if _PROJECT_MODE:
        _render_project_sidebar()

    # ── Load span (both directions) → all three tools at once ──────────────
    # Run Traces only: a project's traces come in through its section 4.
    if not _PROJECT_MODE:
        _span = st.session_state.get('span_loaded')
        with st.expander('📂 Load Span (Both Directions)', expanded=not _span):
            st.caption('One folder, or its .zip(s), holding BOTH directions. '
                       'Per-direction zips (e.g. HOWLAN.zip + LANHOW.zip) are fine; '
                       'they\'re extracted for you. One click loads all three tools.')
            if st.button('📁 Choose folder', use_container_width=True, key='span_browse'):
                p = pick_folder('Choose a folder containing both directions')
                if p:
                    st.session_state['span_folder'] = p
                elif p is None:
                    st.session_state['_picker_unavailable'] = True
            if st.session_state.get('_picker_unavailable'):
                st.caption('⚠ The folder picker isn\'t available in this build. '
                           'Paste the folder path below, or upload the .zip(s).')
            st.text_input('Folder (paste the path if Browse does nothing)',
                          key='span_folder', label_visibility='collapsed',
                          placeholder='paste or choose a folder with both directions')
            _zf = st.file_uploader('…or drag & drop the span here: .zip(s), '
                                   'loose traces, or a whole folder (both '
                                   'directions)',
                                   type=['zip', 'sor', 'json'],
                                   accept_multiple_files=True,
                                   key='span_zip')
            if st.button('⬆ Load into all tools', type='primary',
                         use_container_width=True, key='span_load'):
                if _load_span((st.session_state.get('span_folder') or '').strip().strip('"'), _zf):
                    st.rerun()
        if _span:
            st.success(f"✓ **{_span['ila_a']} ↔ {_span['ila_b']}**  ·  A {_span['a_count']} / "
                       f"B {_span['b_count']} files, loaded in all three tools")
            if _span.get('dropped'):
                st.warning(
                    "⚠ This span had more than two direction groups; only **"
                    f"{_span['a_prefix']}** + **{_span['b_prefix']}** were loaded "
                    f"(into all three tools). Ignored: **{', '.join(_span['dropped'])}** "
                    "(e.g. short-shot / FEC traces). If you meant a different pair, "
                    "load just those two.")
            if _span.get('foreign'):
                import folder_intake as _fi
                st.warning('⚠ ' + _fi.foreign_files_message(_span['foreign']))
            if _span.get('dupes'):
                import folder_intake as _fi_d
                st.warning('⚠ ' + _fi_d.duplicate_names_message(_span['dupes']))
    st.divider()

    st.markdown('##### Select Tool')
    page = st.radio('Tool', TOOLS_PROJECT if _PROJECT_MODE
                    else (TOOLS_TRACES if _home_screen_enabled() else TOOLS_ALL),
                    key='nav_radio', label_visibility='collapsed')
    st.divider()

    # The Analysis switch sits right under the Tool list, on every page.
    # Below rather than above so the Tool radio stays the sidebar's first
    # radio -- six tests (and any tech's muscle memory) address it that way.
    _render_analysis_mode_control()
    st.divider()


# ═════════════════════════════════════════════════════════════════════════
#  PAGE: Viewer
# ═════════════════════════════════════════════════════════════════════════
# Per-session cache: a Viewer folder input that is a .zip (or a folder holding
# zips) is extracted ONCE to a temp dir, keyed on the source path, so the Viewer
# doesn't re-unzip on every Streamlit rerun.
_VIEWER_DIR_CACHE = {}


_FOREIGN_STAGE_CACHE = {}


def _exclude_foreign_files(folder, exts=None):
    """Run the foreign-file audit on a one-folder tool's input.  When files
    from another job are found, stage the remaining files into a temp folder
    and return (staged_folder, foreign); otherwise (folder, []).  Renders the
    tech-facing warning itself.  Cached per (folder, file-list signature) so a
    rerun neither re-reads 800 headers nor re-copies the span.  Never raises —
    any failure returns the folder untouched."""
    import folder_intake as fi
    try:
        files = fi.find_otdr_files(folder, exts or fi.OTDR_EXTS)
        sig = (len(files), max((os.path.getmtime(f) for f in files), default=0))
        cached = _FOREIGN_STAGE_CACHE.get(folder)
        if cached and cached[0] == sig and (cached[1] == folder or os.path.isdir(cached[1])):
            staged, foreign = cached[1], cached[2]
        else:
            kept, foreign = fi.audit_foreign_files(files)
            staged = folder
            if foreign:
                staged = fi.materialize_all(
                    kept, os.path.join(tempfile.mkdtemp(prefix='otdr_clean_'), 'all'))
            _FOREIGN_STAGE_CACHE[folder] = (sig, staged, foreign)
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
        st.markdown('### Trace Folders')

        # Keyed widgets, no value= (mixing key+value with a programmatic write
        # is a Streamlit footgun).  Buttons write the widget-key slot BEFORE
        # the text_input is created this run, so the picked path shows up.
        st.session_state.setdefault('view_dir_a_input', trace_server.CONFIG['dir_a'] or '')
        st.session_state.setdefault('view_dir_b_input', trace_server.CONFIG['dir_b'] or '')
        # Files dropped on the Viewer's own FILES panel point the trace server
        # at a staged folder from inside the page.  A hub rerun must not put
        # the sidebar's old paths back, so a fresh drop seeds the boxes.
        _drop_at = trace_server.CONFIG.get('dropped_at') or 0
        if _drop_at > st.session_state.get('view_drop_seen', 0):
            st.session_state['view_drop_seen'] = _drop_at
            st.session_state['view_dir_a_input'] = trace_server.CONFIG['dir_a'] or ''
            st.session_state['view_dir_b_input'] = trace_server.CONFIG['dir_b'] or ''

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
    # Pop the Viewer into its own window from HERE too — a tech who came to
    # the Viewer page first (rather than clicking a report cell) had no way
    # to detach it.  Same window NAME as the report grids' button, so the two
    # entry points share ONE window: opening from here and then clicking
    # report cells drives this same window instead of spawning a second.
    _pop_doc = """
<button id="vpop2" style="padding:4px 10px;border:1px solid #c9d5e1;border-radius:4px;
    background:#eef3f8;cursor:pointer;font-weight:600;color:#000000;
    font-family:sans-serif;font-size:13px">&#8862; Open Viewer in its own window</button>
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
    st_components_html(_pop_doc, height=42)
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
               'saved to the folder you choose below (Downloads by default) and '
               'offered for download.')

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
    _dropped = st.file_uploader(
        '…or drag & drop the files here (.sor / .trc / .json, a whole '
        'folder, or a .zip)',
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

    out_format = st.radio('Output', ['Excel (xlsx)', 'PDF', 'Stay in app'],
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
    if st.button('Run analysis', type='primary', disabled=bool(_stale)):
        out_dir = _ss_dest
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
        manifest['_folder'] = folder
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
_MATING_LEAD = ('**The fibre fingerprint cannot be measured here; the mating '
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
        return None, f'Fibre {int(token)} has no splice reading in this folder.'
    hits = [n for n in loss if token.lower() in n.lower()]
    if len(hits) == 1:
        return hits[0], loss[hits[0]]
    return None, f'"{token}" matches {len(hits)} files; type the fibre number or the full name.'


def _pct_text(pct):
    if pct is None:
        return ''
    return 'under 0.01%' if pct < 0.01 else f'{pct:.2f}%'


def _near_splice_check(ns, a, b):
    """Plain-language answer for two fibres: {'ok', 'cleared', 'sd', 'text'}."""
    fa, la = _near_splice_lookup(ns, a)
    fb, lb = _near_splice_lookup(ns, b)
    if fa is None or fb is None:
        why = (la if fa is None else lb) or 'Enter two fibres.'
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
                'text': (f'**Different fibres.** {head}. Two shots of one fibre differ '
                         f'this much {_pct_text(pct)} of the time.')}
    # Always give the rate.  At 3.9x "the splices match" is not what the number
    # says: shots of one fibre differ that much about 1 time in 400 on Goodland.
    return {'ok': True, 'cleared': False, 'sd': sd,
            'text': (f'**Not cleared.** {head}. Two shots of one fibre differ this much '
                     f'{_pct_text(pct)} of the time; the line for calling them different '
                     f'fibres is {clear:g}x. A close reading would not make them '
                     f'duplicates either: many different fibres have similar splices.')}


def _render_near_splice(res):
    """One line about the splice, and the two-fibre check.  Nothing when the
    engine abstained (no manifest key)."""
    for ns in res.get('near_splice') or []:
        if not isinstance(ns, dict) or not ns.get('loss'):
            continue
        clear = ns.get('clear_sd') or _NEAR_SPLICE_CLEAR_SD_DEFAULT
        st.info(f"This span has a splice {ns['offset_m']:.0f} m behind the panel. It is "
                f"glass, so unplugging and re-plugging cannot change it: two shots of one "
                f"fibre read it within {ns['sd_pair_db']:.3f} dB. Two files that read it "
                f"more than {clear:g}x that far apart are different fibres.")
        st.markdown('**Check Two Fibres**')
        key = f"ns_check_{ns.get('group', 'report')}"
        c1, c2 = st.columns(2)
        a = c1.text_input('Fibre', key=key + '_a', placeholder='e.g. 350')
        b = c2.text_input('Other fibre', key=key + '_b', placeholder='e.g. 351')
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
    with st.expander(f'Shot Out of Order: {n} Fibre(s) Skipped and Shot Later'):
        st.caption('Each was shot long after both neighbouring fibres, which were shot '
                   'back to back, so its port had to be found again. Worth checking '
                   'against the port log. Not a duplicate finding.')
        for r in runs:
            st.write(f"**{', '.join(r.get('names') or [])}**: shot {_t(r['shot_at'])}, "
                     f"{r.get('minutes_later', 0) / 60:.1f} h after {r.get('before')} "
                     f"({_t(r['before_at'])}) and {r.get('after')} ({_t(r['after_at'])})")


def _splice_cell(p, clear_sd):
    """The mating table's Splice column for one pair."""
    sd = p.get('splice_sd')
    style = 'padding:4px 10px;border:1px solid #eef2f6;text-align:right'
    if sd is None:
        return f"<td style='{style}'></td>"
    if sd > clear_sd:
        return (f"<td style='{style};color:#1e7b34;font-weight:600'>"
                f"different fibres ({sd:.1f}x)</td>")
    return f"<td style='{style};color:#000000'>{sd:.1f}x</td>"


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
    rows = ['<div style="overflow:auto;max-height:50vh;border:1px solid #c9d5e1;'
            'border-radius:4px;color:#000000;background:#ffffff">',
            '<table style="border-collapse:collapse;font-size:12px;'
            'font-family:Consolas,monospace;width:100%">',
            '<thead><tr>'
            "<th style='padding:5px 10px;border:1px solid #dbe4ee;background:#eef3f8'>Rank</th>"
            "<th style='padding:5px 10px;border:1px solid #dbe4ee;background:#eef3f8;text-align:left'>Pair</th>"
            "<th style='padding:5px 10px;border:1px solid #dbe4ee;background:#eef3f8'>Mating Likelihood</th>"
            "<th style='padding:5px 10px;border:1px solid #dbe4ee;background:#eef3f8'>Ratio</th>"
            + ("<th style='padding:5px 10px;border:1px solid #dbe4ee;background:#eef3f8' "
               "title='How far apart the two files read the splice behind the panel, in "
               "multiples of the two-shot wobble'>Splice</th>" if has_splice else '')
            + '</tr></thead><tbody>']
    for i, p in enumerate(top, 1):
        fa, fb = p.get('fiberA'), p.get('fiberB')
        label = (f"F{fa} ↔ F{fb}" if fa is not None and fb is not None
                 else f"{p.get('fileA')} ↔ {p.get('fileB')}")
        if p.get('viewable') and fa is not None and fb is not None:
            href = f"?nav=viewer&fibers={fa},{fb}&dir=a&ssfolder={ssq}"
            cell = (f"<a href='{href}' target='_self' "
                    f"title='Overlay {p.get('fileA')} + {p.get('fileB')}' "
                    f"style='color:#1a5fb4;text-decoration:none;font-weight:600'>{label}</a>")
        else:
            cell = f"<span title='not viewable: {p.get('reason','')}' style='color:#888'>{label}</span>"
        rows.append(
            "<tr>"
            f"<td style='padding:4px 10px;border:1px solid #eef2f6;text-align:center'>{i}</td>"
            f"<td style='padding:4px 10px;border:1px solid #eef2f6'>{cell}</td>"
            f"<td style='padding:4px 10px;border:1px solid #eef2f6;text-align:right'>{p['mating_p']*100:.1f}%</td>"
            f"<td style='padding:4px 10px;border:1px solid #eef2f6;text-align:right'>{p['mating_lr']:.0f}x</td>"
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
    rows = ['<div style="overflow:auto;max-height:62vh;border:1px solid #c9d5e1;'
            'border-radius:4px;color:#000000;background:#ffffff">',
            '<table style="border-collapse:collapse;font-size:12px;'
            'font-family:Consolas,monospace;width:100%">',
            '<thead><tr>'
            "<th style='padding:5px 10px;border:1px solid #dbe4ee;background:#eef3f8;text-align:left'>Pair</th>"
            "<th style='padding:5px 10px;border:1px solid #dbe4ee;background:#eef3f8'>Likelihood</th>"
            "<th style='padding:5px 10px;border:1px solid #dbe4ee;background:#eef3f8'>Score σ</th>"
            "<th style='padding:5px 10px;border:1px solid #dbe4ee;background:#eef3f8'>Shape r</th>"
            "<th style='padding:5px 10px;border:1px solid #dbe4ee;background:#eef3f8' title='Connector-mating similarity: a ranking to check against the port log, not a verdict'>Mating</th>"
            "<th style='padding:5px 10px;border:1px solid #dbe4ee;background:#eef3f8;text-align:left'>Verdict</th>"
            '</tr></thead><tbody>']
    for p in pairs:
        color = _DUP_COLOR.get(p['verdict'], '#000000')
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
        r_txt = '-' if p.get('shape_r') is None else f"{p['shape_r']:.3f}"
        m_txt = ('-' if p.get('mating_p') is None
                 else f"{p['mating_p']*100:.1f}% ({p.get('mating_lr', 0):.0f}x)")
        rows.append(
            "<tr>"
            f"<td style='padding:4px 10px;border:1px solid #eef2f6'>{pair_cell}</td>"
            f"<td style='padding:4px 10px;border:1px solid #eef2f6;text-align:center;"
            f"font-weight:600;color:{color}'>{pct}</td>"
            f"<td style='padding:4px 10px;border:1px solid #eef2f6;text-align:right'>{p['score']:.4f}</td>"
            f"<td style='padding:4px 10px;border:1px solid #eef2f6;text-align:right'>{r_txt}</td>"
            f"<td style='padding:4px 10px;border:1px solid #eef2f6;text-align:right'>{m_txt}</td>"
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
    ("unidir_splice_loss",        "Unidir. splice loss",        0.200,        "dB",    True),
    ("bidir_splice_loss",         "Bidir splice loss",          0.160,        "dB",    True),
    ("unidir_connector_loss",     "Unidir. connector loss",     0.750,        "dB",    False),
    ("bidir_connector_loss",      "Bidir connector loss",       0.500,        "dB",    True),
    ("splitter_loss",             "Splitter Loss",              4.500,        "dB",    False),
    ("reflectance",               "Reflectance",                -50.0,        "dB",    True),
    ("reflectance_ceiling",       "Reflectance ceiling",        0.0,          "dB",    True),
    ("midspan_reflectance",       "Mid-span reflectance band",  -50.0,        "dB",    True),
    # Optional BAND ceiling for the row above: tick it to flag ONLY the
    # band [warn floor, ceiling] — e.g. -80..-40 isolates faint fusion
    # glints while connector-grade reflections stay with the connector
    # rules.  Unticked (default) = no ceiling, shipped behavior.
    ("midspan_refl_ceiling",      "Mid-span refl ceiling",      -40.0,        "dB",    True),
    # NOTE: the launch-connector loss gates used to live here as two rows.
    # They moved to the 'Connector & launch' knobs panel below, which carries
    # per-knob help text and holds the REST of the connector path beside them
    # (re-measure tolerance, search windows, tailbox outlier margin).  One
    # control per engine global — see _CONN_ROWS.
    # Per-FIBER span attenuation: EXFO's stored span loss (the number FR
    # prints as Span Loss) over the stored span length, both directions
    # averaged.  Off by default (0 = off in the engine); IIG sets 0.250.
    ("fiber_section_atten",       "Fiber attenuation",          0.400,        "dB/km", True),
    ("span_loss",                 "Span loss",                  20.000,       "dB",    False),
    ("span_length",               "Span length",                0.0000,       "km",    False),
    # ORL FLOOR: the OTDR's own total ORL per direction, from the file; a
    # reading below the value fails.  Not the OLTS ORL a contract names, and
    # the sheet says so.  Off by default; IIG sets 30.
    ("span_orl",                  "Span ORL (floor)",           15.00,        "dB",    True),
    # Bend/damage clusters within this distance of a validated splice column
    # stay IN that splice column (cells keep their bend labels); farther out
    # they get their own "Bends @ X km" column.  Unchecking reverts to the
    # legacy 75 m gate (Platteville-Cheyenne: short-lay fibers put splice
    # events 107-128 m before the column and grew phantom bend columns).
    ("bend_fold_distance",        "Bend fold distance",         0.200,        "km",    True),
    # Per-FIBER average splice loss, FastReporter's "Avg. Splice Loss": the
    # signed mean of (A->B + B->A)/2 over every splice either direction
    # recorded.  A per-span statistic, not a per-cell gate, so it grades on
    # its own sheet and never colours the grid.  Off by default (0 = off in
    # the engine); the AWS / IIG contract sets it at 0.08 dB.
    ("avg_splice_loss",           "Avg. splice loss (per fiber)", 0.080,      "dB",    True),
]
# Pre-checked rows (match what the splice report flags out of the box):
OTDR_DEFAULT_APPLY = {"unidir_splice_loss", "bidir_splice_loss",
                       "bidir_connector_loss", "reflectance",
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
    "midspan_reflectance": ("band low", "band high"),
    # Launch/tailbox reflectance reads as a band for the same reason: a
    # connector has an acceptable WINDOW, not a single edge.  -49.9 was
    # calibrated for a fusion-spliced launch pigtail (Tulsa measures -51.8
    # median, 0 of 60 flagged).  A mechanical connector legitimately reflects
    # near -45 — two polished ferrules always leave an index step — so a
    # tie-panel job reads -44.9 across every fiber (Reubensville: 60 fibers
    # inside 0.15 dB) and every one of them trips a fusion-splice threshold.
    # With a band the panel job sets the low end to -40 and only genuinely bad
    # mates flag; FTH's -39.2 outliers still stand out at 12x the floor.
    "reflectance": ("band low", "band high"),
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
    {'key': 'conn_bidi', 'label': 'Connector loss (bidirectional)', 'unit': 'dB',
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

    {'key': 'conn_uni', 'label': 'Connector loss (1 direction)', 'unit': 'dB',
     'kind': 'scalar', 'globals': {'value': 'LAUNCH_CONN_UNI_MIN_DB'},
     'defaults': {'value': 0.649}, 'min': 0.0, 'max': 5.0, 'step': 0.001,
     'int': False,
     'help': ('Flag when EITHER direction alone reaches this, however good '
              'the other one is. A purely bidirectional gate cannot see a '
              'one-sided failure: on Defuniak, min and average both flag 0 of '
              '144 fibers while F34 reads B=1.090 and F98 B=1.108 at a '
              'connector. Cells that fire only here print A side or B side '
              'so the reader knows the pair averages lower. 0 turns it off.')},

    {'key': 'conn_avg', 'label': 'Connector loss (bidirectional average)', 'unit': 'dB',
     'kind': 'scalar', 'globals': {'value': 'LAUNCH_CONN_AVG_MIN_DB'},
     'defaults': {'value': 0.0}, 'min': 0.0, 'max': 5.0, 'step': 0.01,
     'int': False,
     'help': ('Flag on the connector’s actual loss, (A + B) / 2: the number '
              'the report prints, the number FastReporter reports, and the '
              'number a reviewer hand-types. It runs beside the two gates '
              'above rather than replacing them, so their calibration does not '
              'move. Sacramento↔Suisun F1013 is why it exists: near 0.318 / '
              'far 1.088 averages 0.703, exactly the value the field sheet '
              'carries, but min = 0.318 never reached 0.62. Ships OFF: across '
              'that whole 1152-fiber span it adds no fiber the other two gates '
              'miss, and the sheet records the worst cells as one-way values '
              'anyway. Turn it on for a span you want judged on the pair’s own '
              'loss. Cells that fire only here print the average, without the '
              'side marker.')},

    {'key': 'conn_confirm', 'label': 'Connector re-measure tolerance', 'unit': 'dB',
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

    {'key': 'tailbox_outlier', 'label': 'Tailbox reflectance outlier margin', 'unit': 'dB',
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

    {'key': 'conn_far_window', 'label': 'Far-end connector search window', 'unit': 'km',
     'kind': 'scalar', 'globals': {'value': 'LAUNCH_CONN_FAR_WINDOW_KM'},
     'defaults': {'value': 2.0}, 'min': 0.1, 'max': 10.0, 'step': 0.1,
     'int': False,
     'help': ('How far back from a direction’s own end-of-fiber to hunt for '
              'the OTHER end’s connector, which is what makes the reading '
              'bidirectional. Also sets the window the tailbox reflectance '
              'baseline is drawn from. One launch reel plus slack; widening '
              'it starts pulling real plant near the tail into a connector '
              'rule.')},

    {'key': 'conn_reel_slack', 'label': 'Reel-length match slack', 'unit': 'km',
     'kind': 'scalar', 'globals': {'value': 'LAUNCH_CONN_REEL_SLACK_KM'},
     'defaults': {'value': 0.3}, 'min': 0.01, 'max': 2.0, 'step': 0.01,
     'int': False,
     'help': ('How far the far view’s distance may sit from the measured '
              'launch-reel length and still be judged the SAME connector. The '
              'two directions derive distance with their own IOR, so the two '
              'views never agree exactly. Too tight and the pair is never '
              'formed, so nothing is bidirectional; too loose and a nearby '
              'splice can be mistaken for the far view of the connector.')},

    {'key': 'launch_step_guard', 'label': 'Launch step guard', 'unit': 'km',
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

    {'key': 'launch_high_loss', 'label': 'Launch event loss rule', 'unit': 'dB',
     'kind': 'scalar', 'globals': {'value': 'LAUNCH_HIGH_LOSS_DB'},
     'defaults': {'value': 0.0}, 'min': 0.0, 'max': 5.0, 'step': 0.01,
     'int': False,
     'help': ('Flag the launch event itself when its own stored loss exceeds '
              'this. Ships OFF (0), by tech direction: the launch end is '
              'judged on reflectance and on the connector gates above, not on '
              'a bare loss reading, because a launch event’s stored loss '
              'includes the backscatter step between two different fibers and '
              'reads high on healthy launches. Set a value only if you want '
              'the old HIGH_LAUNCH_LOSS behaviour back.')},
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

    with st.expander('OTDR Settings (Thresholds)', expanded=False):
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
                #   warnUsed — the engine reads its Warning (one row today)
                'wired':     key in _OTDR_KEY_TO_ENGINE_GLOBAL,
                'warnUsed':  key in _OTDR_KEY_TO_WARN_GLOBAL,
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

def _render_conn_settings_panel():
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

    with st.expander('Connector & Launch Settings', expanded=False):
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
    'deadzone': '#7f8c8d', 'event': '#000000',
}

def _viewer_click_target(page_key):
    """Do report-grid cell clicks open the separate Viewer WINDOW (default —
    what the pop-out shipped as) or load the in-app Viewer TAB (the
    pre-pop-out behavior, kept for techs who prefer a single window)?
    Returns True when the pop-out window should be used."""
    k = f'{page_key}_click_target'
    st.session_state.setdefault(k, 'Separate window')
    choice = st.radio(
        'Cell clicks open in', ['Separate window', 'This tab (Viewer page)'],
        key=k, horizontal=True,
        help='Separate window: one Viewer window stays open beside the report '
             'and re-plots as you click cells (shift-click adds a fiber). '
             'This tab: cells load the in-app Viewer page with a Back button.')
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
      &#8862; Open / focus Viewer window</button>
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
    doc = doc.replace("__TABLE__", table_html).replace("__ORIGIN__", origin)
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
#   row, one row per ribbon, every splice spanning a km+ft column pair).  The
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

    # Columns: every header-row cell with a label from column 2 on.  Our
    # merged km+ft pairs leave the ft column's header empty, so it is
    # skipped naturally.
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
    wsum.cell(r, 1, 'Colour key').font = Font(bold=True)
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
    c = wsum.cell(r, 1, 'Grey column header')
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
                   "other report) are shown with grey headers; everything in "
                   "them counts as a difference.")
    st.caption(f"Saved to `{cached['xlsx']}`")
    try:
        with open(cached['xlsx'], 'rb') as fh:
            st.download_button('⬇ Differences vs tech (Excel)', data=fh.read(),
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

    # Input mode: two A/B folders (shared with the Viewer) OR a single folder /
    # .zip that holds both directions (auto-split by direction).
    mode = st.radio('Select Traces', [two, one], horizontal=True, key=k_mode)

    if mode == two:
        if span == 1:
            # Reuse the viewer's A/B folder slots so both tools share one selection.
            st.session_state.setdefault(k_a, trace_server.CONFIG.get('dir_a') or '')
            st.session_state.setdefault(k_b, trace_server.CONFIG.get('dir_b') or '')
        c1, c2 = st.columns(2)
        with c1:
            if st.button('📁 A-direction folder', use_container_width=True, key=k_ba):
                p = pick_folder('Choose the A-direction folder')
                if p:
                    st.session_state[k_a] = p
            st.text_input('A folder', key=k_a, placeholder='A-direction folder')
        with c2:
            if st.button('📁 B-direction folder', use_container_width=True, key=k_bb):
                p = pick_folder('Choose the B-direction folder')
                if p:
                    st.session_state[k_b] = p
            st.text_input('B folder', key=k_b, placeholder='B-direction folder')
        dir_a = (st.session_state.get(k_a) or '').strip().strip('"')
        dir_b = (st.session_state.get(k_b) or '').strip().strip('"')
    else:
        c1, c2 = st.columns(2)
        with c1:
            if st.button('📁 Folder with BOTH directions', use_container_width=True,
                         key=k_bone):
                p = pick_folder('Choose a folder containing both directions')
                if p:
                    st.session_state[k_one] = p
            st.text_input('Folder (both directions)', key=k_one,
                          placeholder='one folder with both directions '
                                      '(.sor / .json, or .bdr)')
        with c2:
            zf = st.file_uploader('…or drop the span here: its traces '
                                  '(a whole folder works), a .zip, or the '
                                  '.bdr files themselves',
                                  type=['zip', 'bdr', 'sor', 'json'],
                                  key=k_zip, accept_multiple_files=True)
        dir_a, dir_b = _resolve_bidir_from_single(
            (st.session_state.get(k_one) or '').strip().strip('"'), zf)

    # The tech's own splice report (optional).  When one is here, the run
    # also writes a <A>_to_<B>_SpliceReport_vs_Tech.xlsx beside the report
    # that highlights every cell where the two disagree — the tech_compare block.
    # Sits under the A/B inputs on both input modes (the boss's placement).
    tech_xlsx = st.file_uploader(
        "Tech's splice report to compare against (.xlsx, optional)",
        type=['xlsx', 'xlsm'], key=k_tech,
        help='Upload the splice report the tech built. After the report runs, '
             'a second workbook highlighting every difference is saved next '
             'to it.')
    return dir_a, dir_b, tech_xlsx


def _sr_site_inputs(span, dir_a, dir_b):
    """The A/B ILA-site boxes for one span, auto-derived from the SOR
    GenParams so the report shows WHICH ILA is the A-direction and which is
    the B-direction (instead of a literal "A"/"B").  Re-derived when the
    folder pair (or the profile) changes; the tech can still override.
    Keyed-state pattern (set session_state BEFORE the widget) — never mix
    value= and key= on a widget we write to.  Returns (site_a, site_b)."""
    _k = _sr_span_keys(span)
    k_a, k_b, k_src = _k['site_a'], _k['site_b'], _k['site_src']
    if dir_a and dir_b and os.path.isdir(dir_a) and os.path.isdir(dir_b):
        # The profile is part of the signature: a tech who loads the span
        # and THEN picks the IIG profile must still get the identifier-based
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
    site_a = s1.text_input('A-direction ILA / site', key=k_a)
    site_b = s2.text_input('B-direction ILA / site', key=k_b)
    if site_a and site_b and (site_a, site_b) != ('A', 'B'):
        st.caption(f"📍 **A direction:** {site_a} → {site_b}  ·  "
                   f"**B direction:** {site_b} → {site_a}")
    return site_a, site_b


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
    st.success(f"{res['site_a']} → {res['site_b']}  ·  {res['n_fibers']} fibers  ·  "
               f"{res['n_splices']} splices  ·  span {res['span_km']} km  ·  "
               f"{res['n_flagged']} flagged events")
    xp = res.get('xlsx')
    if xp and os.path.exists(xp):
        with open(xp, 'rb') as fh:
            st.download_button('⬇ Excel report', data=fh.read(),
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
    n_fibers = res['n_fibers']
    n_ribbons = (n_fibers + ribbon_size - 1) // ribbon_size
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
_SHOW_ROWS = [('loss', 'Splice loss'), ('bend', 'Bend/Damage'),
              ('break', 'Breaks')]


def _render_show_hide_box(prefix, rows=_SHOW_ROWS):
    """One toggle per row, all on by default.  Returns the dict for --show, or
    None when everything is shown (the engine default)."""
    with st.expander('Show/Hide in Report', expanded=False):
        st.caption('Switch a category off to leave it out of the report. '
                   'Reflectance and other findings always show. The report '
                   'gets a Display sheet listing what was hidden.')
        show = {k: st.toggle(label, value=True, key=f'{prefix}_show_{k}')
                for k, label in rows}
    return None if all(show.values()) else show


def page_splice_report():
    _p = 'sr'
    _cache_name = '.sr_grid_cache.json'
    st.markdown('#### Bidirectional Splice Report')
    st.caption('Generates the Excel report (saved to your **Downloads**) and a '
               'clickable grid: click any flagged cell to jump to that fiber and '
               'splice in the Viewer. Give it two A/B folders, or one folder / .zip '
               'holding both directions.')

    # Customer profile first, above the A/B boxes: default or a customer's
    # thresholds, chosen before the span is picked.  Guarded the same way as
    # the settings panel below — a failure here must not take the page down.
    try:
        _render_customer_profile_picker()
    except Exception as _exc:
        st.warning('Customer profile picker unavailable, running with the '
                   'default profile. (Details sent to support.)')
        report_error('splice report — profile picker render', _exc)

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
        if _n == n_spans and h2.button(f'✖ Remove span {_n}', key=f'sr_del_span{_n}',
                                       use_container_width=True):
            st.session_state['sr_n_spans'] = _n - 1
            # Drop its finished result too — a report block for a span the
            # tech removed would be a stale page.
            for _k in (f'{_p}_result{_n}', f'{_p}_dirs{_n}', f'{_p}{_n}_techcmp'):
                st.session_state.pop(_k, None)
            st.rerun()
        _da, _db, _tech = _sr_span_inputs(_n)
        _sa, _sb = _sr_site_inputs(_n, _da, _db)
        extra[_n] = (_da, _db, _sa, _sb, _tech)
    if n_spans < SR_MAX_SPANS:
        if st.button('➕ Add span…', key='sr_add_span',
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
    try:
        _render_otdr_settings_panel()
    except Exception as _exc:
        st.warning('OTDR settings panel unavailable, running with default '
                   'thresholds. (Details sent to support.)')
        _policy_block_caption(_exc)
        report_error('splice report — settings panel render', _exc)
        st.session_state.pop('otdr_settings', None)   # → empty overrides below
    # Connector/launch knobs, same guard: a component failure here must leave
    # the report running on engine defaults, not take the page down.
    try:
        _render_conn_settings_panel()
    except Exception as _exc:
        st.warning('Connector & launch settings unavailable, running with '
                   'default connector thresholds. (Details sent to support.)')
        _policy_block_caption(_exc)
        report_error('splice report — connector settings panel render', _exc)
        st.session_state.pop('conn_settings', None)   # → engine defaults below
    sr_show = _render_show_hide_box(
        'sr', _SHOW_ROWS + [('conn', 'Connector loss (1 direction)')])

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
    if st.button(_gen_label, type='primary',
                 disabled=bool(_stale) or bool(_not_ready)):
        _safe = lambda s: ''.join(c if (c.isalnum() or c in ' -_') else '_' for c in str(s)).strip() or 'site'
        _suffix = '_SpliceReport.xlsx'
        # Read the panel values straight out of session_state (which the
        # component's auto-commit keeps current) and translate to engine
        # globals.  This is the value the run actually uses — see the
        # iframe-state footgun note in _render_otdr_settings_panel.
        overrides = _overrides_from_settings(st.session_state.get('otdr_settings'))
        # Connector/launch knobs ride the SAME --overrides channel.  Read from
        # the committed session_state slot (not the component return), for the
        # iframe-state reason in _render_conn_settings_panel.  Absent slot =
        # engine defaults, which is exactly what the panel shows.
        _conn = st.session_state.get('conn_settings')
        if isinstance(_conn, dict):
            overrides.update({g: v for g, v in _conn.items()
                              if g in _CONN_DEFAULTS})
        # Profile-level engine settings with no panel row (grading
        # wavelength) ride the same channel, and the profile's contract
        # figures go to the audit.  Both keyed off the ACTIVE profile; the
        # Default / Lumen / Zayo profiles declare neither, so their runs are
        # byte-identical to before.
        _prof_name = st.session_state.get('otdr_profile')
        overrides.update(_engine_extras_from_profile(_prof_name))
        _contract = _contract_from_profile(_prof_name)
        # One queue entry per span; the same profile / thresholds / contract
        # apply to all of them (they were chosen once, above the boxes).
        spans = [(1, dir_a, dir_b, site_a, site_b)]
        for _n in sorted(extra):
            _da, _db, _sa, _sb, _t = extra[_n]
            spans.append((_n, _da, _db, _sa, _sb))
        queue, used_names = [], set()
        for _n, _da, _db, _sa, _sb in spans:
            _name = f'{_safe(_sa)}_to_{_safe(_sb)}{_suffix}'
            if _name in used_names:                   # same sites twice → keep both files
                _name = f'{_safe(_sa)}_to_{_safe(_sb)}_span{_n}{_suffix}'
            used_names.add(_name)
            out_xlsx = os.path.join(_sr_dest, _name)
            queue.append({'span': _n, 'dirs': (_da, _db),
                          'cmd': splicereport_cmd(_da, _db, out_xlsx, _sa, _sb,
                                                  contract=_contract,
                                                  overrides=overrides,
                                                  show=sr_show)})
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
        for _cand in (st.session_state.get(f'{_p}_dirs'),
                      (st.session_state.get('view_dir_a_input'),
                       st.session_state.get('view_dir_b_input'))):
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
    trace_server.set_thresholds(res.get('thresholds'))

    for _n, _r, _d, _t in shown:
        _render_sr_result(_p, _r, span=_n, n_spans=len(shown), dirs=_d,
                          dest=_sr_dest, tech_xlsx=_t, popout=_popout, port=_port)


# ═════════════════════════════════════════════════════════════════════════
#  PAGE: Unidirectional (A-only one-shot)  — splice report engine, --uni mode
# ═════════════════════════════════════════════════════════════════════════
def uni_cmd(folder, out_xlsx, direction=None, overrides=None, landmarks=None,
            show=None):
    """Argv for the unidirectional one-shot — the splice report engine's
    --uni mode (same subprocess, same sor_reader isolation, ZK-format
    workbook out)."""
    common = ['--uni', '--dir-a', folder, '--out', out_xlsx,
              '--analysis', analysis_mode()]
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
# The SR settings panel's six rows are all BIDIRECTIONAL thresholds — the
# uni engine reads none of them.  Uni's adjustable knobs are the UNI_*
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
    {'key': 'flag_threshold', 'label': 'Flag threshold', 'unit': 'dB',
     'kind': 'scalar', 'globals': {'value': 'UNI_BEND_THRESHOLD'},
     'defaults': {'value': 0.250}, 'min': 0.005, 'max': 2.0, 'step': 0.005,
     'int': False,
     'help': 'A-side event this far off a validated closure is flagged.'},

    {'key': 'min_pop', 'label': 'Min fibers for a splice column', 'unit': 'fibers',
     'kind': 'scalar', 'globals': {'value': 'UNI_MIN_POP_SPLICE'},
     'defaults': {'value': 20}, 'min': 2, 'max': 500, 'step': 1, 'int': True,
     'help': 'Population in a 1 km bin needed to call a candidate closure.'},

    {'key': 'closure_radius', 'label': 'At-splice radius', 'unit': 'km',
     'kind': 'scalar', 'globals': {'value': 'UNI_CLOSURE_MATCH_KM'},
     'defaults': {'value': 0.075}, 'min': 0.005, 'max': 1.0, 'step': 0.005,
     'int': False,
     'help': 'How close an event must sit to a closure to count as at it.'},

    {'key': 'refl_band', 'label': 'Mid-span reflectance band', 'unit': 'dB',
     'kind': 'range', 'globals': {'low': 'UNI_REFL_FLOOR_DB',
                                  'high': 'UNI_REFL_CEIL_DB'},
     'defaults': {'low': -80.0, 'high': 0.0},
     'min': -90.0, 'max': 0.0, 'step': 1.0, 'int': False,
     'help': ('Flag reflective glints at or above the low end. High end '
              'excludes reflections STRONGER than itself (0 = no ceiling); '
              'set it to keep connector-grade reflections out of the band. '
              'Low end 0 turns the whole category off. Every flag is '
              'confirmed as a spike in the raw trace, and where the OTDR '
              'left the reflectance blank it is measured from the trace.')},

    {'key': 'conn_loss', 'label': 'Connector loss (1 direction)', 'unit': 'dB',
     'kind': 'scalar', 'globals': {'value': 'UNI_CONN_LOSS_DB'},
     'defaults': {'value': 0.649}, 'min': 0.0, 'max': 5.0, 'step': 0.001,
     'int': False,
     'help': ('Flag a connector whose loss reads at or above this in the one '
              'direction shot: a bare threshold, not judged against the '
              'population. Connectors are found either way and every reading '
              'is listed; this only decides which ones shade a cell. 0 turns '
              'the flag off. One direction cannot separate a connector\'s true '
              'loss from the backscatter step between the fibers it joins, so '
              'the number is an upper bound; the bidirectional Splice Report '
              'averages that term away.')},

    {'key': 'break_floor', 'label': 'Break floor: min EOF', 'unit': 'km',
     'kind': 'scalar', 'globals': {'value': 'UNI_BREAK_MIN_KM'},
     'defaults': {'value': 0.3}, 'min': 0.05, 'max': 10.0, 'step': 0.05,
     'int': False,
     'help': 'A fiber ending below this is too short to count as a break.'},

    {'key': 'break_short_by', 'label': 'Break: EOF short of span by', 'unit': 'km',
     'kind': 'scalar', 'globals': {'value': 'UNI_BREAK_PREMATURE_KM'},
     'defaults': {'value': 3.0}, 'min': 0.1, 'max': 50.0, 'step': 0.1,
     'int': False,
     'help': 'A fiber ending this far short of the cable end is a break.'},

    {'key': 'end_region', 'label': 'End exclusion, full-span fibers', 'unit': 'km',
     'kind': 'scalar', 'globals': {'value': 'UNI_END_REGION_KM'},
     'defaults': {'value': 0.5}, 'min': 0.0, 'max': 10.0, 'step': 0.1,
     'int': False,
     'help': 'Tail of a fiber that reaches the far end, excluded from flags.'},

    {'key': 'zone_certify', 'label': 'Damage-zone certify radius', 'unit': 'km',
     'kind': 'scalar', 'globals': {'value': 'UNI_DAMAGE_ZONE_BREAK_KM'},
     'defaults': {'value': 0.5}, 'min': 0.05, 'max': 5.0, 'step': 0.05,
     'int': False,
     'help': 'A damage anchor this close to a break column certifies the zone.'},

    {'key': 'zone_anchor', 'label': 'Damage-zone anchor confirm', 'unit': 'dB',
     'kind': 'scalar', 'globals': {'value': 'UNI_PREBREAK_CONFIRM_DB'},
     'defaults': {'value': 0.03}, 'min': 0.005, 'max': 1.0, 'step': 0.005,
     'int': False,
     'help': 'Step a stored zone event must show in the trace to anchor a zone.'},

    {'key': 'zone_member', 'label': 'Zone membership floor (stored / sweep)',
     'unit': 'dB',
     'kind': 'range', 'globals': {'low': 'UNI_PREBREAK_STORED_DB',
                                  'high': 'UNI_PREBREAK_MEMBER_DB'},
     'defaults': {'low': 0.02, 'high': 0.03},
     'min': 0.001, 'max': 1.0, 'step': 0.005, 'int': False,
     'help': ('Two floors for two evidence classes. Low applies when the '
              'stored table and the trace agree; high applies to sweep-only '
              'membership, where the bar is higher because nothing '
              'corroborates it (control noise tops out near 0.026 dB).')},

    {'key': 'landmark_radius', 'label': 'Landmark radius (demote / label)',
     'unit': 'km',
     'kind': 'range', 'globals': {'low': 'UNI_LANDMARK_DEMOTE_KM',
                                  'high': 'UNI_LANDMARK_MATCH_KM'},
     'defaults': {'low': 0.10, 'high': 0.15},
     'min': 0.01, 'max': 2.0, 'step': 0.01, 'int': False,
     'help': ('Nested radii. Within the high radius a landmark prints on the '
              'Handholes row; within the tighter low radius a NON-closure '
              'landmark also demotes a splice column to Bend/Damage.')},

    {'key': 'ribbon_size', 'label': 'Ribbon size', 'unit': 'fibers',
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


# Per-session staging dirs for drag-and-dropped inputs, keyed on the drop's
# (name, size) signature so Streamlit reruns reuse the dir instead of
# re-writing hundreds of files every rerun.
_DROP_STAGE_CACHE = {}


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
    sig = tuple(sorted((f.name, getattr(f, 'size', 0)) for f in files))
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
    # Count staged trace files ourselves — folder_intake.find_otdr_files
    # deliberately excludes .trc, but Secret Sauce accepts it.
    n = 0
    for _root, _dirs, _files in os.walk(td):
        n += sum(1 for x in _files
                 if not x.startswith('.')
                 and x.lower().endswith(('.sor', '.trc', '.json')))
    _DROP_STAGE_CACHE[sig] = (td, n, dupes)
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


def page_unidirectional():
    st.markdown('#### Unidirectional')

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
    _dropped = st.file_uploader(
        '…or drag & drop the shots here (.sor / .json files, a whole '
        'folder, or a .zip)',
        type=['sor', 'json', 'zip'], accept_multiple_files=True,
        key='uni_drop')
    if _dropped:
        _sdir, _sn, _sdupes = _stage_dropped(_dropped)
        if _sn:
            st.caption(f'📥 {_sn} trace file(s) staged from the drop, used as '
                       'the input.')
            folder = _sdir
        else:
            st.warning('The drop contained no readable `.sor` / `.json` files.')
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
    # page — fall back to engine defaults with a visible warning.
    try:
        uni_overrides = _render_uni_settings_panel()
    except Exception as _exc:
        st.warning('Unidirectional settings panel unavailable, running with default '
                   'thresholds. (Details sent to support.)')
        _policy_block_caption(_exc)
        report_error('unidirectional — settings panel render', _exc)
        uni_overrides = None
    uni_show = _render_show_hide_box(
        'uni', _SHOW_ROWS + [('conn', 'Connector loss (1 direction)')])

    if not folder or not os.path.isdir(folder):
        st.info('👆 Choose the folder that holds the one-direction `.sor` / '
                '`.json` shots, or drag & drop them above.')
        return
    folder = os.path.abspath(folder)
    _remove_legacy_caches(folder)
    src_folder = folder
    folder, _foreign = _exclude_foreign_files(folder)

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

    with st.expander('Job Landmarks (Optional: Closure Map / Handholes)'):
        st.caption('One per line: `km, label`, or `km, label, splice` for a '
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

    import folder_intake as _fi_dest
    with _uni_run_slot:
        _uni_dest = _report_dest_row('uni_report_dest', _fi_dest.default_report_dir())
        _stale = _report_gate('uni')
        _run_uni = st.button('Run unidirectional report', type='primary',
                             disabled=bool(_stale))
        st.caption('⏳ Large folders can take a few minutes. Leave this '
                   'window open and don’t refresh.')
    if _run_uni:
        out_xlsx = os.path.join(_uni_dest, 'unidirectional_events.xlsx')
        st.session_state['uni_pending_cmd'] = uni_cmd(folder, out_xlsx,
                                                      direction=dir_choice,
                                                      landmarks=landmarks,
                                                      overrides=uni_overrides,
                                                      show=uni_show)
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
    u = res.get('uni') or {}
    # The fiber count NEVER appears without its denominator: a 480-fiber
    # report on an 864-file folder must not read as "done, 480 fibers".
    _n_folder = u.get('n_files_in_folder')
    _n_drop = u.get('n_files_not_analysed') or 0
    _covered = (f"{u.get('n_fibers', '?')} of {_n_folder} files"
                if _n_folder and _n_drop else f"{u.get('n_fibers', '?')} fibers")
    _line = (f"{_covered} · direction {u.get('direction', '?')} · "
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
                f"{n} file(s) shot as '{sig}' were NOT analysed."
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
        off = float(u.get('launch_offset_km') or 0.0)
        by_rc = {}
        for c in u['cells']:
            by_rc.setdefault(((c['fiber'] - 1) // rs, c['col']), []).append(c)
        _KIND_COLOR = {'splice': '#1f4e79', 'bend_damage': '#8a6d00',
                       'break': '#c00000', 'reflective': '#6c3483',
                       'connector': '#0e6655', 'end': '#595959'}
        _uni_port = ensure_trace_server()
        if folder and os.path.isdir(folder):
            trace_server.set_dirs(folder, None)   # popped Viewer reads this span
        # Same as the Splice Report grid: the Viewer judges by THIS run's gates.
        # The uni settings panel moves UNI_BEND_THRESHOLD off its 0.250 default
        # and that never reached the Viewer either.
        trace_server.set_thresholds(res.get('thresholds'))
        _uni_popout = _viewer_click_target('uni')
        from urllib.parse import quote as _q
        _fq = _q(folder, safe='')
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
                    color = _KIND_COLOR.get(c['kind'], '#000000')
                    if c['kind'] == 'end':
                        # Cable End cell: the fiber's stored end reflectance.
                        loss = (' end' if c['loss'] is None
                                else f" REFL{c['loss']:.1f}dB")
                    else:
                        loss = (' ✕ broke' if c['loss'] is None
                                else f" {c['loss']:.3f}")
                    _km = round(c['km'] + off, 4)
                    links.append(_cell_markup(
                        _uni_popout, c['fiber'], _km, 'a', color, '',
                        f"F{c['fiber']}{loss}",
                        href=(f"?nav=viewer&fiber={c['fiber']}&km={_km}"
                              f"&dir=a&sra={_fq}&src=uni")))
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
#   * Lumen's own Submittal Checklist (the form's last tab): each row there is
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
    """A cell's number.  Lumen's form formats some number cells as dates
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


SECTION_TITLES = {1: 'Site Survey Data', 2: 'FAT', 3: 'Event Log', 4: 'Data Files'}


def project_status(snap, fqa, caps, trace_dirs=None, work='', manual=None,
                   pkgs=None, n_splices=None, final_desc=None):
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
    # GPS at every splice point, from the phone (Robert, 2026-09-23).
    psrc, psplices = next(((n, p.get('splices') or []) for n, p in pkgs if p.get('splices')), (None, []))
    want_sp = n_splices if n_splices is not None else len(psplices)
    if want_sp or psplices:
        got = [x for x in psplices if (x.get('gps') or {}).get('lat') is not None]
        gone = [x for x in psplices if (x.get('gps') or {}).get('lat') is None]
        detail = f'{len(got)} of {want_sp or len(psplices)}'
        if gone:
            detail += ' · missing on ' + _event_list([{'no': x.get('event'), 'row': 0} for x in gone])
        add(3, 'GPS at every splice point (phone)', psplices and not gone and len(got) >= want_sp,
            detail if psplices else 'from the phone', psrc)
        if got:
            ppkg = next(p for n, p in pkgs if n == psrc)
            A, Z = _pkg_end_fix(ppkg, 'A'), _pkg_end_fix(ppkg, 'Z')
            bad = [(no, why) for no, why in splice_gps_problems(psplices, A, Z)
                   if why != 'no GPS fix']
            add(3, 'Splice GPS in order and between the ends', (A and Z) and not bad,
                ('; '.join(f'event {no}: {why}' for no, why in bad[:4])
                 + (f'; and {len(bad) - 4} more' if len(bad) > 4 else '')) if bad
                else ('checked against the A and Z box fixes' if (A and Z)
                      else 'needs a GPS fix at the A and Z boxes'), psrc)
    if not events:
        add(3, '3.02-3.06  Events', False, 'no events in an Event Log yet')
    else:
        n = len(events)
        # Lumen's own checklist only looks at the first two events (D18:D19);
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
# needs Lumen's blank form.  The phone sends back a capture package: a .zfc
# (folder_intake.share_write) of capture.json and the photos, emailed; someone saves it
# into Field/.  Older packages were plain .zip and still read.
JOB_VERSION = 1
CAPTURE_FORMAT = 'otdr-capture'
FIELD_CAPTURE_URL_KEY = 'field_capture_url'


def project_job_id():
    """The open project's job ID, made on first use and kept in the file."""
    ss = st.session_state
    if not ss.get('project_job_id'):
        import uuid
        ss['project_job_id'] = uuid.uuid4().hex[:8]
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
        return None, ('this OTDR Suite build has no QR library yet (it comes with the '
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


def read_capture_package(path):
    """A phone capture package, or None: a .zfc (folder_intake share file, kind
    field-capture) or an older plain .zip from before the .zfc format; both
    hold capture.json + photos."""
    import zipfile
    import folder_intake
    try:
        sf = folder_intake.share_open(path, expect='field-capture')
        data = json.loads(sf.read('capture.json').decode('utf-8'))
    except folder_intake.ShareFileError:
        try:   # pre-.zfc package: no manifest.json
            with zipfile.ZipFile(path) as z:
                if 'manifest.json' in z.namelist():
                    return None
                data = json.loads(z.read('capture.json').decode('utf-8'))
        except Exception:
            return None
    except Exception:
        return None
    if not isinstance(data, dict) or data.get('format') != CAPTURE_FORMAT:
        return None
    return data


def capture_package_exts():
    """.zfc (and any later name for it) plus the pre-.zfc .zip."""
    import folder_intake
    return folder_intake.share_extensions('field-capture') + ('.zip',)


def collect_capture_packages(*dirs):
    """[(name, package)] newest first, from the work folder's Field folder."""
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
                pkg = read_capture_package(p)
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
    """An uploader whose files are kept in one work subfolder, once each."""
    ss = st.session_state
    ups = st.file_uploader(label, type=types, accept_multiple_files=True, key=key,
                           help=help_text or None)
    done = ss.setdefault('_status_stored', set())
    stored = []
    for up in ups or []:
        sig = (key, up.name, up.size, getattr(up, 'file_id', None))
        if sig in done:
            continue
        done.add(sig)
        try:
            dest, _new = _store_dropped(work_sub(sub), up)
            stored.append(dest)
        except OSError as exc:
            st.error(f'Could not keep {up.name}: {exc}')
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
    return sid, na, nb


def set_final_shoot(sid, work=None):
    """Make `sid` the final shoot and point every tool at it."""
    ss = st.session_state
    ss['project_final_shoot'] = sid
    sh = next((x for x in list_shoots(work) if x['id'] == sid), None)
    if sh is None:
        return
    for key, val in (('view_dir_a_input', sh['a']), ('view_dir_b_input', sh['b']),
                     ('uni_folder_input', sh['a']), ('ss_folder_input', sh['dir'])):
        ss[key] = val
    _forget_trace_reports(work or work_dir())


def copy_traces_into(src_a, src_b, work):
    """A new shoot from src_a / src_b (the setup screen and older callers).
    Returns (n_a, n_b)."""
    _sid, na, nb = add_shoot(src_a, src_b, work)
    return na, nb


def _forget_trace_reports(work):
    """New traces make every cached report on the old ones wrong."""
    ss = st.session_state
    ta, _tb = work_trace_dirs(work)
    for folder, names in ((ta, ('.sr_grid_cache.json',)),
                          (work_sub('traces', work), SS_CACHE_NAMES)):
        for n in names:
            try:
                os.remove(_hub_cache_path(n, folder))
            except OSError:
                pass
    for k in list(ss.keys()):
        if k.startswith(('sr_result', 'sr_dirs', 'uni_result', 'ss_result', 'viewer_target')):
            ss.pop(k, None)


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


def _render_phone_job(prod, job_id, work):
    """The job link: emailed to the tech (or scanned as a QR code), it opens
    Field Capture knowing what to collect."""
    ss = st.session_state
    with st.expander('📱 Phone Job: Send the Tech the Job Link', expanded=False):
        if FIELD_CAPTURE_URL_KEY + '_box' not in ss:
            ss[FIELD_CAPTURE_URL_KEY + '_box'] = _settings_read().get(FIELD_CAPTURE_URL_KEY) or ''
        url = _clean_path(st.text_input(
            'Field Capture web address', key=FIELD_CAPTURE_URL_KEY + '_box',
            placeholder='https://… (where Field Capture is hosted)',
            help='Set once; every project uses it.'))
        if url != (_settings_read().get(FIELD_CAPTURE_URL_KEY) or ''):
            _settings_update(**{FIELD_CAPTURE_URL_KEY: url})
        # Robert, 2026-09-24: "a button that will allow us to Test Phone
        # Connection ... take one picture and ... one GPS coordinates. When we
        # do both then it will go green on the phone and will allow us to
        # submit".  Submit is the real route back: the phone emails a small
        # test package, which lands in Field/ like any other.
        if st.button('📱 Test Phone Connection', key='ps_phone_test', disabled=not url,
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
                st.error(f'Could not write the test email: {exc}')
        for n, p in collect_capture_packages(work_sub('field', work)):
            if p.get('test'):
                g = p.get('gps') or {}
                st.success(f"✓ Phone test received ({n}): photo and GPS "
                           f"{g.get('lat', 0):.5f}, {g.get('lon', 0):.5f}"
                           + (f" · {p.get('initials')}" if p.get('initials') else ''))
                break
        if not prod:
            st.caption('The job link needs the production sheet: add it above.')
            return
        try:
            manifest = job_manifest(prod, job_id, os.path.basename(work), ss.get('fqa_job'))
        except Exception as exc:
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
        if st.button('✉️ Email the link to the tech', key='ps_job_email', type='primary'):
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
        except Exception:
            n_splices = None
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
                          pkgs=pkgs, n_splices=n_splices, final_desc=final_desc)


def audit_key(item):
    return f"{item['section']}|{item['item']}"


# The job-detail fields the audit can fill in on its own screen, per item:
# (path in the job form, input kind, label).
AUDIT_FIELDS = {
    '1.01': [('site_a.address', 'text', 'A end street address')],
    '1.02': [('site_z.address', 'text', 'Z end street address')],
    '1.03': [('site_a.alias', 'text', 'A end alias')],
    '1.04': [('site_z.alias', 'text', 'Z end alias')],
    '1.05': [('site_a.clli', 'text', 'A end CLLI')],
    '1.06': [('site_z.clli', 'text', 'Z end CLLI')],
    '1.07': [('site_a.panel_port_count', 'int', 'A end panel port count')],
    '1.08': [('site_z.panel_port_count', 'int', 'Z end panel port count')],
    '1.09': [('fiber_count', 'int', 'Number of fibers tested')],
    '1.10': [('revision', 'text', 'Test revision')],
    '1.11': [('package_type', 'text', 'Package type')],
    '1.12': [('contractor', 'text', 'Splicing contractor'),
             ('prepared_by', 'text', 'Package preparer')],
    '1.13': [('project', 'text', 'NetBuild or project ID')],
    '1.14': [('tester_1', 'text', 'Tester name and phone number')],
    '1.15': [('calibration_date', 'date', 'Date of most recent calibration')],
}
for _end, _x in (('A', 'site_a'), ('Z', 'site_z')):
    AUDIT_FIELDS[f'{_end} end rack location'] = [
        (f'{_x}.floor', 'text', 'Floor'), (f'{_x}.room', 'text', 'Room'),
        (f'{_x}.aisle', 'text', 'Aisle'), (f'{_x}.bay', 'text', 'Bay')]
    AUDIT_FIELDS[f'{_end} end panel details'] = [
        (f'{_x}.rmu', 'text', 'RMU (shelf)'), (f'{_x}.connector_type', 'text', 'Connector'),
        (f'{_x}.panel_type', 'text', 'Panel type')]


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
        return ('The splice point GPS comes from the phone: email the tech the job link. '
                'A location typed wrong in the production sheet is fixed there, then the '
                'package is built again.')
    if n.startswith(('2.', '3.')):
        return 'This is built into the FQA package from the production sheet and the traces.'
    if n.startswith('4.01'):
        return 'Add the traces (or pick the final shoot) in section 4 of the status page.'
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
                c1.button('Go through the skipped ones again', key='aud_again')
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
            st.button('Save and continue', key='aud_save', type='primary')
        elif 'photos' in n or 'labels match' in n or 'GPS' in n:
            if _drop_into('Save what the phone sent (capture package, workbook)',
                          'aud_drop_field', 'field', ['zip', 'xlsm', 'xlsx']):
                st.success('Saved. The audit moves on once it counts.')
            st.button('📱 Email the tech the job link (on the status page)', key='aud_go_phone')
        elif n.startswith(('2.', '3.')):
            st.button('🧮 Open the FQA Builder', key='aud_go_fqa', type='primary')
        elif n.startswith('4.01'):
            st.button('📂 Go to section 4 to add traces', key='aud_go_status', type='primary')
        elif n.startswith('4.02'):
            if _drop_into('Power meter files', 'aud_drop_pm', 'power', None):
                st.success('Saved.')
        elif n.startswith('4.03'):
            st.button('The file names follow the convention', key='aud_403', type='primary')
        elif n.startswith(('4.04', '4.05')):
            if _drop_into('Splice logs and exception documents', 'aud_drop_logs',
                          'splice_logs', None):
                st.success('Saved.')
            st.button('Not needed on this job', key='aud_na')
        c1, c2, c3 = st.columns(3)
        c1.button('Skip for now ⏭', key='aud_skip', use_container_width=True)
        c3.button('Exit audit', key='aud_exit', use_container_width=True)


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
    snap = _project_snapshot(ss, ss.get('project_saved'))
    s1 = (snap.get('spans') or [{}])[0]
    manual = dict(ss.get('project_manual') or {})
    st.markdown(f'#### {os.path.basename(work)}')
    _cust = ss.get('otdr_profile')
    if _cust and _cust not in _NOT_CUSTOMERS:
        st.markdown(f'**Customer:** {_cust}')
        if _cust not in FQA_FORM_CUSTOMERS:
            st.info(f'No FQA form set up for {_cust} yet: the checklist below is Lumen\'s.')
    st.caption(f'Work folder `{work}` · laid out on the four sections of Lumen\'s '
               'Submittal Checklist. Everything is read from the files in this '
               'folder, so a file saved into it by hand counts too.')
    overall = st.empty()          # filled once the sections are read

    # ── the project workbook: the production sheet ──
    prod = project_production_sheet(work)
    with st.container(border=True):
        st.markdown('**Production Sheet** (the project workbook)')
        about = st.empty()            # filled after Add, so it shows the new sheet
        c1, c2 = st.columns([1, 2])
        if c1.button('📄 Choose production sheet', key='ps_prod_pick', use_container_width=True):
            p = pick_file('Choose the production sheet', [('Excel', '*.xlsx *.xlsm')])
            if p:
                ss['ps_prod_path'] = p
            elif p is None:
                st.info('No file picker on this machine: paste the path.')
        c2.text_input('Production sheet path', key='ps_prod_path',
                      label_visibility='collapsed',
                      placeholder='…or paste the production sheet\'s path')
        src = _clean_path(ss.get('ps_prod_path'))
        if src and st.button('Add it to the project', key='ps_prod_add'):
            if not os.path.isfile(src):
                st.error(f'No file at {src}')
            else:
                with st.spinner('Copying the production sheet…'):
                    prod = _add_production_sheet(src, work)
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

    # Files that change the status are taken in before it is computed, so the
    # list below already reflects them.
    fqa, caps = collect_field_files(work_sub('field', work), work_sub('fqa', work))
    # What the production sheet (and anything typed in Job details) already
    # answered counts now, before any package is built.
    _jd = job_details_workbook(ss.get('fqa_job'))
    if _jd is not None:
        fqa = fqa + [(JOB_DETAILS_SOURCE, _jd)]
    job_id = project_job_id()
    pkgs_all = collect_capture_packages(work_sub('field', work))
    pkgs = [(n, p) for n, p in pkgs_all if p.get('job') == job_id and not p.get('test')]
    strays = [n for n, p in pkgs_all if p.get('job') != job_id and not p.get('test')]
    n_splices = None
    if prod:
        try:
            n_splices = len(_read_prod(prod).splices)
        except Exception:
            n_splices = None
    trace_dirs = None
    if s1.get('mode') == 'one' and s1.get('folder'):
        cached = (ss.get('sr_intake') or {}).get(f"dir:{os.path.abspath(s1['folder'])}")
        if cached and os.path.isdir(cached[0]) and os.path.isdir(cached[1]):
            trace_dirs = (cached[0], cached[1])

    sections = st.container()

    with sections:
        # ── 1 ──
        with st.container(border=True):
            box1 = st.container()
            f1, f2 = st.columns(2)
            f1.button('📝 Job details (FQA Builder)', key='ps_go_fqa',
                      use_container_width=True, disabled=not prod,
                      help='Addresses, CLLIs, contractor, testers, calibration: '
                           'the cover page, pre-filled from the production sheet.')
            with f2:
                for dest in _drop_into('From the phone: capture package (.zfc), FQA '
                                       'workbook or capture sheet',
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
            st.button('🧮 Build the FQA package (FQA Builder)', key='ps_go_fqa2',
                      disabled=not prod,
                      help='The FAT comes from the production sheet and the fiber count.')
        # ── 3 ──
        with st.container(border=True):
            box3 = st.container()
            st.button('🧮 Build the FQA package (FQA Builder)', key='ps_go_fqa3',
                      disabled=not prod,
                      help='Distances come from the traces, locations from the '
                           'production sheet; the phone\'s GPS fills the gaps.')
        # ── 4 ──
        with st.container(border=True):
            box4 = st.container()
            # ── Shoots: every dated set of traces, and which one is final ──
            shoots = list_shoots(work)
            fin = final_shoot(work)
            st.markdown('**Trace Shoots**')
            if ss.get('_shoot_flash'):
                st.success(ss.pop('_shoot_flash'))
            if not shoots:
                st.caption('No traces yet. Add the first shoot below.')
            else:
                import datetime as _dt
                infos = {sh['id']: shoot_info(sh) for sh in shoots}

                def _shoot_text(sid):
                    d, lab = infos[sid]
                    sh = next(x for x in shoots if x['id'] == sid)
                    fa, fb = len(_trace_fibers(sh['a'])), len(_trace_fibers(sh['b']))
                    return (f"{d or 'no date'}{' · ' + lab if lab else ''} · A {fa} / B {fb} fibers")

                order = sorted(shoots, key=lambda sh: (infos[sh['id']][0], sh['id']), reverse=True)
                ids = [sh['id'] for sh in order]
                # The options are part of the widget's identity: when a shoot is
                # added the radio is a NEW widget and would fall back to its first
                # option -- read as a pick, that moved the final (2026-09-24).  So
                # the options go in the sync tag too.
                _owner = (work, tuple(ids))
                _bind('ps_final', fin['id'], _owner)
                if ss.get('ps_final') not in ids:
                    ss['ps_final'] = fin['id']
                picked = st.radio('Final traces (every tool, the checks and the phone '
                                  'job use these)', ids, key='ps_final',
                                  format_func=_shoot_text)
                if picked != fin['id']:
                    set_final_shoot(picked, work)
                    _bound('ps_final', picked, _owner)
                    fin = next(x for x in shoots if x['id'] == picked)
                    st.success(f'Final traces: {_shoot_text(picked)}. Every tool now '
                               'opens on them; reports made on the old ones were cleared.')
                with st.expander('Dates and Labels'):
                    meta = dict(ss.get('project_shoots') or {})
                    for i, sh in enumerate(order):
                        d, lab = infos[sh['id']]
                        c1, c2 = st.columns([1, 2])
                        try:
                            dv = _dt.date.fromisoformat(d) if d else None
                        except ValueError:
                            dv = None
                        kd, kl = f'ps_shoot_date_{sh["id"]}', f'ps_shoot_label_{sh["id"]}'
                        _bind(kd, dv, work)
                        _bind(kl, lab, work)
                        nd = c1.date_input(f'Shot on ({sh["id"] or "Traces"})', key=kd)
                        nl = c2.text_input(f'Label ({sh["id"] or "Traces"})', key=kl,
                                           placeholder='e.g. reshoot after repair')
                        nd_s = nd.isoformat() if isinstance(nd, _dt.date) else ''
                        if (nd_s, nl) != (d, lab):
                            meta[sh['id']] = {'date': nd_s, 'label': nl}
                            _bound(kd, nd, work)
                            _bound(kl, nl, work)
                    ss['project_shoots'] = meta
            with st.expander('➕ Add a Shoot' if shoots else '➕ Add the First Shoot',
                             expanded=not shoots):
                st.caption('Copied into its own folder in the work folder. Two folders, one '
                           'folder holding both directions, or drop them.')
                c1, c2, c3 = st.columns(3)
                for col, key, label in ((c1, 'ps_tr_a', 'A-direction folder'),
                                        (c2, 'ps_tr_b', 'B-direction folder'),
                                        (c3, 'ps_tr_one', 'One folder, both directions')):
                    with col:
                        if st.button('📂 ' + label, key=key + '_pick', use_container_width=True):
                            p = pick_folder('Choose the ' + label)
                            if p:
                                ss[key] = p
                        st.text_input(label, key=key, label_visibility='collapsed',
                                      placeholder='or paste a path')
                drop = st.file_uploader('…or drop the traces: a .zip, loose files, or .bdr',
                                        type=['zip', 'sor', 'json', 'bdr'],
                                        accept_multiple_files=True, key='ps_tr_drop')
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
                day = d1.date_input('Shot on', value=default_day,
                                    help='Read from the .sor files when they carry it.')
                lab = d2.text_input('Label (optional)', key='ps_tr_label',
                                    placeholder='e.g. reshoot after repair')
                if st.button('Add this shoot', key='ps_tr_copy', type='primary'):
                    if not (a and b) and (one or drop):
                        a, b = _resolve_bidir_from_single(one, drop)
                    if a and b and os.path.isdir(a) and os.path.isdir(b):
                        with st.spinner('Copying traces…'):
                            sid, na, nb = add_shoot(a, b, work, day.isoformat(),
                                                    (lab or '').strip())
                        if not shoots:
                            set_final_shoot(sid, work)
                            ss['_shoot_flash'] = (f'Copied {na} A and {nb} B trace files. '
                                                  'Every tool now opens on them.')
                        else:
                            ss['_shoot_flash'] = (f'Added a shoot of {na} A and {nb} B trace '
                                                  'files. Pick it under Final traces to use it.')
                        # The shoots list sits above this button: redraw so it shows the
                        # new one.  Safe here -- the sidebar is drawn, and the tools'
                        # folders are re-seeded at the top of every run.
                        st.rerun()
                    else:
                        st.error('Pick an A and a B folder, or one folder / drop that '
                                 'holds both directions.')
            have_traces = any(_trace_fibers(d) for d in work_trace_dirs(work))
            g1, g2, g3, g4 = st.columns(4)
            for col, key, label in ((g1, 'ps_go_viewer', 'Viewer'), (g2, 'ps_go_sr', 'Splice Report'),
                                    (g3, 'ps_go_uni', 'Unidirectional'), (g4, 'ps_go_ss', 'Secret Sauce')):
                col.button(label, key=key, disabled=not have_traces, use_container_width=True)
            st.markdown('**Other Data Files**')
            d1, d2 = st.columns(2)
            with d1:
                _drop_into('Power meter files (4.02)', 'ps_drop_pm', 'power', None)
            with d2:
                _drop_into('Splice logs, exception documents (4.04, 4.05)',
                           'ps_drop_logs', 'splice_logs', None)
            m1, m2, m3 = st.columns(3)
            _bind('ps_tick_403', manual.get('4.03') is True, work)
            v = m1.checkbox('4.03 file names checked', key='ps_tick_403')
            if v:
                manual['4.03'] = True
            else:
                manual.pop('4.03', None)
            _bound('ps_tick_403', v, work)
            for col, no in ((m2, '4.04'), (m3, '4.05')):
                k = 'ps_tick_' + no.replace('.', '')
                _bind(k, manual.get(no) == 'na', work)
                v = col.checkbox(f'{no} not needed on this job', key=k)
                if v:
                    manual[no] = 'na'
                else:
                    manual.pop(no, None)
                _bound(k, v, work)
            ss['project_manual'] = manual

    fs = final_shoot(work)
    final_desc = None
    if fs:
        trace_dirs = (fs['a'], fs['b'])
        d, lab = shoot_info(fs)
        final_desc = f"shot {d or '(no date)'}{' · ' + lab if lab else ''}" + (
            f" · 1 of {len(list_shoots(work))} shoots" if len(list_shoots(work)) > 1 else '')
    items = project_status(snap, fqa, caps, trace_dirs, work, ss['project_manual'],
                           pkgs=pkgs, n_splices=n_splices, final_desc=final_desc)
    for box, sec in ((box1, 1), (box2, 2), (box3, 3), (box4, 4)):
        rows = [i for i in items if i['section'] == sec]
        n_ok = sum(i['ok'] for i in rows)
        with box:
            st.markdown(f'#### {sec} · {SECTION_TITLES[sec]}  ·  {n_ok} of {len(rows)}')
            _render_needs(items, sec)
    have = sum(i['ok'] for i in items)
    overall.progress(have / max(1, len(items)),
                     text=f'{have} of {len(items)} in hand · {len(items) - have} still needed')


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
# the project's profile.  Lumen's is the only FQA form so far (2026-09-24).
_NOT_CUSTOMERS = ('Default (engine baseline)', 'Custom (edit table below)')
FQA_FORM_CUSTOMERS = ('Lumen',)


def project_customers():
    return [n for n in CUSTOMER_PROFILES if n not in _NOT_CUSTOMERS]


def _default_projects_root():
    saved = _settings_read().get(PROJECTS_ROOT_KEY)
    if saved and os.path.isdir(saved):
        return saved
    docs = os.path.join(os.path.expanduser('~'), 'Documents')
    return os.path.join(docs if os.path.isdir(docs) else os.path.expanduser('~'),
                        'OTDR Projects')


def _safe_folder_name(name):
    out = ''.join(c if (c.isalnum() or c in ' -_.()&') else '_' for c in str(name)).strip(' .')
    return out[:80] or 'New project'


def _write_new_project(work, site_a, site_b, job, shoots=None, final=None, dirs=None,
                       customer=None):
    path, exists = project_file_for_folder(work)
    ta, tb = dirs or (os.path.join(work_sub('traces', work), 'A'),
                      os.path.join(work_sub('traces', work), 'B'))
    snap = {'report_dest': work_sub('reports', work), 'manual': {}, 'fqa_job': job,
            'shoots': shoots or {}, 'final_shoot': final,
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


# ── Project packages (.otdrproject): one file to send a whole project ────
# Robert, 2026-09-24: "an export function so we can send an entire project
# to a tech via email".  A .otdrproject is a zip of the work folder (the
# project file already stores its folders relative to itself, so it opens on
# any PC) plus a small otdrproject.json saying what is inside.  Traces make a
# project big -- a 1152-fiber span is hundreds of MB a shoot, and mail stops
# near 20-25 MB -- so the export offers all shoots, the final shoot only, or
# no traces at all.
PACKAGE_EXT = '.otdrproject'
PACKAGE_FORMAT = 'otdr-suite-project-package'
EMAIL_LIMIT_BYTES = 20 * 1024 * 1024
EXPORT_MODES = {'none': 'Without traces',
                'final': 'With final traces',
                'all': 'With all traces'}


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
            if f.startswith(('.', '~$')) or f.lower().endswith(PACKAGE_EXT):
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
    """Write <dest_dir>/<project>.otdrproject; returns its path."""
    import zipfile
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
    tmp = path + '.part'
    with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        z.writestr('otdrproject.json', json.dumps({
            'format': PACKAGE_FORMAT, 'version': 1, 'name': name, 'mode': mode,
            'exported': time.strftime('%Y-%m-%d %H:%M:%S'), 'app': _app_version()}, indent=1))
        for p, arc in _export_files(work, mode):
            # Traces and spreadsheets are already compressed; storing them is faster.
            ctype = (zipfile.ZIP_STORED if p.lower().endswith(('.zip', '.xlsx', '.xlsm', '.jpg', '.png'))
                     else zipfile.ZIP_DEFLATED)
            z.write(p, arc, compress_type=ctype)
    os.replace(tmp, path)
    return path


def import_project(package, projects_root):
    """Unpack a .otdrproject into <projects_root>/<name> (made unique) and
    return the new work folder.  Refuses anything that is not our package or
    that would write outside that folder."""
    import zipfile
    with zipfile.ZipFile(package) as z:
        try:
            meta = json.loads(z.read('otdrproject.json').decode('utf-8'))
        except KeyError:
            raise ValueError('not an OTDR Suite project package')
        if meta.get('format') != PACKAGE_FORMAT:
            raise ValueError('not an OTDR Suite project package')
        name = _safe_folder_name(meta.get('name') or 'Project')
        dest, k = os.path.join(projects_root, name), 2
        while os.path.exists(dest):
            dest = os.path.join(projects_root, f'{name} ({k})')
            k += 1
        root = os.path.abspath(dest)
        for info in z.infolist():
            if info.filename == 'otdrproject.json' or info.is_dir():
                continue
            parts = info.filename.split('/')
            rel = '/'.join(parts[1:])             # drop the packed folder's own name
            target = os.path.abspath(os.path.join(root, *rel.split('/')))
            if not rel or not target.startswith(root + os.sep):
                raise ValueError(f'unsafe path in package: {info.filename}')
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(info) as src, open(target, 'wb') as out:
                import shutil
                shutil.copyfileobj(src, out)
    if not project_file_for_folder(root)[1]:
        raise ValueError('the package has no project file')
    return root


def sharepoint_libraries():
    """[(label, folder)] for the SharePoint libraries this PC syncs.

    Robert, 2026-09-24: SharePoint through Windows' built-in sync ("option
    1"), and the synced libraries listed by name wherever a folder is picked.
    The sync app records each library it syncs under HKCU\\Software\\
    SyncEngines\\Providers\\OneDrive\\<id>: MountPoint is the folder,
    UrlNamespace the SharePoint address.  A personal OneDrive is left out
    (its address is a /personal/ one): only SharePoint libraries are wanted.
    Not Windows, or nothing synced: []."""
    out = []
    try:
        import winreg
    except ImportError:
        return out
    base = r'Software\SyncEngines\Providers\OneDrive'
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, base)
    except OSError:
        return out
    i = 0
    while True:
        try:
            sub = winreg.EnumKey(root, i)
        except OSError:
            break
        i += 1
        try:
            k = winreg.OpenKey(root, sub)
            mount = winreg.QueryValueEx(k, 'MountPoint')[0]
            try:
                url = winreg.QueryValueEx(k, 'UrlNamespace')[0] or ''
            except OSError:
                url = ''
        except OSError:
            continue
        if not mount or not os.path.isdir(mount) or '/personal/' in url.lower():
            continue
        out.append((f'SharePoint · {os.path.basename(mount.rstrip(chr(92) + "/"))}', mount))
    return sorted(out)


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
    for label, path in sharepoint_libraries():
        add(label, path)
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


@st.dialog('📦 Export project')
def _export_dialog(work):
    _render_export(work)


def _render_project_bar():
    """Audit Project and Export project together, in a bar pinned to the top
    of the window on every page of a project (Robert, 2026-09-24).  Audit is
    handled before drawing (_mode_actions: to Project status, audit on);
    Export opens its pop-up here."""
    with st.container(key='project_bar'):
        c1, c2 = st.columns(2)
        c1.button('🧭 Audit Project', key='bar_audit', type='primary', use_container_width=True,
                  help='Go through every open item one at a time: act on it or skip it.')
        if c2.button('📦 Export project', key='fx_export', type='primary',
                     use_container_width=True):
            _export_dialog(work_dir())
    # Pinned under Streamlit's own header, clear of the sidebar; the page is
    # pushed down by the bar's height so nothing hides under it.
    st.markdown(
        '<style>'
        '.st-key-project_bar{position:fixed;top:3.75rem;right:1.5rem;z-index:999990;'
        'width:auto!important;min-width:26rem;background:var(--background-color,#fff);'
        'padding:.5rem .75rem;border:1px solid #d5dde6;border-radius:.75rem;'
        'box-shadow:0 2px 10px rgba(0,0,0,.15)}'
        '.st-key-project_bar button{font-size:1.15rem;padding:.6rem 1rem;min-height:3rem}'
        '.st-key-project_bar button p{font-size:1.15rem}'
        '[data-testid="stMainBlockContainer"]{padding-top:6.5rem!important}'
        '</style>', unsafe_allow_html=True)


def _render_export(work):
    ss = st.session_state
    if True:
        sizes = {m: export_size(work, m) for m in EXPORT_MODES}
        _bind('ps_export_mode', ss.get('ps_export_mode') or 'none', work)
        mode = st.radio('What to include', list(EXPORT_MODES), key='ps_export_mode',
                        format_func=lambda m: f'{EXPORT_MODES[m]} · about {_fmt_size(sizes[m])}')
        dests = export_destinations(work)
        paths = [p for _l, p in dests] + [EXPORT_OTHER]
        labels = {p: l for l, p in dests}
        labels[EXPORT_OTHER] = 'Choose another folder…'
        last = (_settings_read().get(EXPORT_DESTS_KEY) or [None])[0]
        _bind('ps_export_where', last if last in paths else paths[0], (work, tuple(paths)))
        if ss.get('ps_export_where') not in paths:
            ss['ps_export_where'] = paths[0]
        where = st.selectbox('Export to', paths, key='ps_export_where',
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
                _remember_export_dest(os.path.abspath(dest))
            except Exception as exc:
                st.error(f'Could not export: {exc}')
        out = ss.get('_exported')
        if out and os.path.isfile(out):
            size = os.path.getsize(out)
            st.success(f'Exported `{out}` ({_fmt_size(size)}).')
            if size <= EMAIL_LIMIT_BYTES:
                if st.button('✉️ Email it', key='ps_export_email'):
                    try:
                        from fieldcapture.email_draft import write_draft, open_with_default_app
                        eml = write_draft(out, '', f'OTDR Suite project: {os.path.basename(work)}',
                                          'The project is attached. In OTDR Suite: Home, '
                                          'Open Recent Project, Open this package.\n')
                        opened, err = open_with_default_app(eml)
                        st.success('An email with the project attached is open in your mail '
                                   'program.' if opened else f'Wrote {eml} ({err}).')
                    except Exception as exc:
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
                when = time.strftime('%d %b %Y, %H:%M', time.localtime(os.path.getmtime(p)))
            except OSError:
                when = ''
            c1, c2 = st.columns([3, 1])
            c1.markdown(f'**📁 {os.path.basename(work)}**  \n'
                        f'<span style="font-size:0.85em;opacity:0.7">{work}'
                        f'{" · saved " + when if when else ""}</span>',
                        unsafe_allow_html=True)
            c2.button('Open', key=f'home_recent_{i}', use_container_width=True)
    with st.container(border=True):
        st.markdown('**A Project Package (.otdrproject) Someone Sent You**')
        c1, c2 = st.columns([1, 2])
        if c1.button('📦 Choose the package', key='open_pkg_pick', use_container_width=True):
            p = pick_file('Choose the project package', [('OTDR Suite project', '*' + PACKAGE_EXT)])
            if p:
                ss['open_pkg_path'] = p
        c2.text_input('Package path', key='open_pkg_path', label_visibility='collapsed',
                      placeholder='…or paste its path')
        up = st.file_uploader('…or drop it here (up to 200 MB)', type=[PACKAGE_EXT.lstrip('.')],
                              key='open_pkg_up')
        st.caption(f'It is unpacked into `{_default_projects_root()}` and opened.')
        src = _clean_path(ss.get('open_pkg_path'))
        if up is not None and not src:
            src = _staged_setup_upload(up)
        if st.button('Open this package', key='open_pkg', type='primary', disabled=not src):
            try:
                with st.spinner('Unpacking…'):
                    ss['_setup_open'] = import_project(src, _default_projects_root())
                st.rerun()
            except Exception as exc:
                st.error(f'Could not open that package: {exc}')
    with st.container(border=True):
        st.markdown('**Another Project Folder**')
        c1, c2 = st.columns([1, 2])
        c1.button('📁 Choose its work folder', key='home_project', use_container_width=True)
        c2.text_input('Work folder', key='home_folder', label_visibility='collapsed',
                      placeholder='…or paste the work folder\'s path')
        if ss.get('_home_need_path'):
            st.caption('No folder picker on this machine: paste the work folder\'s path.')
        st.button('Open this folder', key='home_open_path')


def new_project(work, customer=None, sheet=None, traces=None):
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
    return _write_new_project(work, sites[0], sites[1], job, shoots=shoots, final=final,
                              dirs=dirs, customer=customer)


def _fill_missing(primary, extra):
    """primary with extra's values where primary has none (nested dicts too)."""
    out = dict(primary)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _fill_missing(out[k], v)
        elif out.get(k) in (None, '') and v not in (None, ''):
            out[k] = v
    return out


def page_project_setup():
    ss = st.session_state
    st.markdown('<style>[data-testid="stSidebar"],[data-testid="stSidebarCollapsedControl"]'
                '{display:none}</style>', unsafe_allow_html=True)
    kind = ss.get('setup_kind') or 'new'
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
        if c1.button('📄 Choose production sheet', key='setup_prod_pick',
                     use_container_width=True):
            p = pick_file('Choose the production sheet', [('Excel', '*.xlsx *.xlsm')])
            if p:
                ss['setup_prod_path'] = p
            elif p is None:
                st.caption('No file picker here: paste the path, or drop the file.')
        c2.text_input('Production sheet path', key='setup_prod_path',
                      label_visibility='collapsed',
                      placeholder='…or paste the production sheet\'s path')
        up = st.file_uploader('…or drop it here (up to 200 MB; paste the path for '
                              'bigger sheets)', type=['xlsx', 'xlsm'], key='setup_prod_up')
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
    with box_traces:
        st.markdown('**3 · The Traces** (optional)')
        st.caption('Two folders (A and B), one folder holding both directions, or drop '
                   'them. They are copied into the project as its first shoot.')
        c1, c2, c3 = st.columns(3)
        for col, key, label in ((c1, 'setup_tr_a', 'A-direction folder'),
                                (c2, 'setup_tr_b', 'B-direction folder'),
                                (c3, 'setup_tr_one', 'One folder, both directions')):
            with col:
                if st.button('📂 ' + label, key=key + '_pick', use_container_width=True):
                    p = pick_folder('Choose the ' + label)
                    if p:
                        ss[key] = p
                    elif p is None:
                        st.caption('No folder picker here: paste the path.')
                st.text_input(label, key=key, label_visibility='collapsed',
                              placeholder='or paste a path')
        drop = st.file_uploader('…or drop the traces: a .zip, loose files, or .bdr',
                                type=['zip', 'sor', 'json', 'bdr'],
                                accept_multiple_files=True, key='setup_tr_drop')
        a, b = _clean_path(ss.get('setup_tr_a')), _clean_path(ss.get('setup_tr_b'))
        one = _clean_path(ss.get('setup_tr_one'))
        if not (a and b) and (one or drop):
            a, b = _resolve_bidir_from_single(one, drop)
        if a and b and os.path.isdir(a) and os.path.isdir(b):
            fa, fb = _trace_fibers(a), _trace_fibers(b)
            try:
                sa, sb = _site_names_for(a, b)
            except Exception:
                sa, sb = '', ''
            if fa or fb:
                st.success(f"Read: **{sa or 'A'} → {sb or 'B'}** · A {len(fa)} fibers, "
                           f"B {len(fb)} fibers")
                traces_name = f'{sa} to {sb}' if sa and sb else os.path.basename(a.rstrip('/\\'))
                traces_src = (a, b, sa, sb)
            else:
                st.warning('No trace files in those folders.')

    with box_customer:
        st.markdown('**4 · Customer**')
        st.selectbox('Customer', project_customers(), index=None, key='setup_customer',
                     placeholder='Select the customer…', label_visibility='collapsed',
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
        n1.text_input('Project name', key='setup_name', placeholder='e.g. Flagler to Bethune')
        with n2:
            libs = sharepoint_libraries()
            if libs:
                opts = [''] + [p for _l, p in libs]
                names = dict((p, l) for l, p in libs)
                sp = st.selectbox('Save projects in', opts, key='setup_parent_sp',
                                  format_func=lambda p: names.get(p, 'SharePoint library…'))
                if sp and ss.get('_setup_parent_sp_last') != sp:
                    ss['setup_parent'] = sp
                ss['_setup_parent_sp_last'] = sp
            if st.button('📁 Save projects in…', key='setup_parent_pick'):
                p = pick_folder('Where new projects are kept')
                if p:
                    ss['setup_parent'] = p
            st.text_input('Save projects in', key='setup_parent', label_visibility='collapsed')
        name = _safe_folder_name(ss.get('setup_name') or '')
        parent = _clean_path(ss.get('setup_parent')) or _default_projects_root()
        work = os.path.join(parent, name)
        st.caption(f'Work folder: `{work}`')

    have_source = bool(sheet_src or traces_src)
    if not have_source:
        st.caption('Load a production sheet or traces (or both) to create the project.')
    if st.button('Create project', key='setup_create', type='primary',
                 disabled=not (have_source and customer and (ss.get('setup_name') or '').strip())):
        if os.path.isdir(work) and project_file_for_folder(work)[1]:
            st.error('That folder is already a project. Open it from the home screen, or '
                     'pick another name.')
            return
        try:
            with st.spinner('Creating the project…'):
                new_project(work, customer=customer, sheet=sheet_src, traces=traces_src)
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
try:
    if st.session_state.get('app_mode') == 'setup':
        page_project_setup()
    elif page == 'Viewer':
        page_viewer()
    elif page == 'Splice Report':
        page_splice_report()
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

if _PROJECT_MODE:
    try:
        _render_project_bar()
    except Exception as _exc:
        report_error('project: top bar', _exc)

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
        if _ss.pop('_project_rebase', False):
            _ss['project_saved'] = _now
        elif _now != _ss.get('project_saved'):
            _pp = _ss['project_path']
            project_write(_pp, project_to_file_data(_now, _pp, _span_markers_for(_now)))
            _ss['project_saved'] = _now
    except Exception as _exc:
        report_error('project: autosave', _exc)

# ─── Sidebar footer: build identity + one-click update ────────────────────
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


if st.sidebar.button('🔄 Check for updates', key='upd_check',
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
            if st.sidebar.button('⬇ Update & restart now', key='upd_restart',
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

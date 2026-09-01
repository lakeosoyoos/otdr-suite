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
from streamlit.components.v1 import html as st_components_html

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


def splicereport_cmd(dir_a, dir_b, out_xlsx, site_a, site_b, overrides=None,
                     fr=False):
    """Argv to run the Splice Report engine in a clean subprocess (its own
    sor_reader copy).  Frozen: --run-splicereport sentinel; dev: the runner.

    `overrides` is the engine-global threshold dict from the OTDR settings
    panel (e.g. {'REBURN_THRESHOLD': 0.12, ...}).  It's serialized to JSON
    and forwarded as --overrides so the subprocess can apply it to the
    engine module BEFORE the pipeline runs (the panel lives in this process;
    the engine lives in the subprocess, so the values cross as JSON)."""
    common = ['--dir-a', dir_a, '--dir-b', dir_b, '--out', out_xlsx,
              '--site-a', site_a, '--site-b', site_b]
    if fr:
        common += ['--fr']     # Splice Report FR (beta): trace-confirmation gates
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
RESTART_PORT = 8510                  # must match desktop/launcher.py PORT
RESTART_WAIT_S = 10                  # how long the old server may take to go
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


def _latest_manifest_version(timeout=8):
    """Version number of the live signed manifest — DISPLAY-ONLY.  No code is
    fetched and nothing here is trusted: applying an update stays exclusively
    in the frozen launcher's signed fetch/verify/swap at boot (the signing
    key lives there; an auto-updatable file must never carry the trust
    anchor).  Returns None when the server is unreachable."""
    import urllib.request
    url = ('https://raw.githubusercontent.com/lakeosoyoos/otdr-suite/main/'
           'update_manifest.json')
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'OTDRSuite'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return int(json.loads(r.read().decode('utf-8'))['version'])
    except Exception:
        return None


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
    real clock/fetcher; every caller in the hub goes through here."""
    return _stale_check(
        st.session_state, time.time(), STALE_RECHECK_S,
        lambda: _latest_manifest_version(timeout=3),
        lambda: _parse_engine_version(_app_version(), _engine_version()))


# Shown in place of a report when the engine is behind.  It has to answer the
# tech's first question — "why won't it let me?" — or the next move is a phone
# call, not a restart.
STALE_BLOCK_MSG = (
    '🔒 **Report generation is paused — OTDR Suite needs a restart.**\n\n'
    'This session is running **engine {running}**, but **engine {latest}** '
    'has been published. Different engines can print different numbers for '
    'the same traces, so reports are held until this copy is up to date.\n\n'
    '**Nothing is lost.** Finish what you are doing, then restart when you '
    'are ready — the update is verified and applied at launch.'
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
    st.error(STALE_BLOCK_MSG.format(latest=latest, running=running))
    if getattr(sys, 'frozen', False):
        if st.button('⬇ Update & restart now', key=f'{key}_stale_restart',
                     type='primary'):
            if _relaunch_and_exit():
                _render_restart_watchdog()
            else:
                st.error('Couldn\'t start the restart — close OTDR Suite '
                         'completely and open it again to pick up the update.')
    else:
        st.caption('Restart the app to apply — updates install at launch.')
    return stale


def _restart_marker_path():
    """Where the restart helper records 'the old instance never let go' — the
    app dir the launcher already owns (log + engine.meta.json live there)."""
    return os.path.join(os.path.expanduser('~'), '.otdrSuite',
                        'update_restart_blocked')


def _restart_command(exe, marker, port, wait_s, os_name=None):
    """argv for the detached helper that restarts the app after an update click.

    It must NOT start the new exe until the old server is really gone.  The
    launcher's first act is a health check on this port, and if the dying
    instance still answers it prints 'Another instance is already serving',
    opens a tab and exits — the click looks like it worked and the update
    silently never applies.  So the helper polls the SAME health URL the
    launcher uses, starts the exe the moment it stops answering (usually
    within a second, faster than a blind sleep), and if it never stops it
    writes `marker` rather than launching into that no-op — _render_update_nudge
    turns that file into a visible 'close it or reboot' message.

    `os_name` defaults to this machine's os.name (callers never pass it); the
    tests pass it explicitly so BOTH shapes get asserted on whichever platform
    CI happens to run."""
    url = f'http://127.0.0.1:{port}/_stcore/health'
    if (os_name or os.name) == 'nt':
        def _q(s):                       # PowerShell single-quote escaping
            return str(s).replace("'", "''")
        ps = ("$d=(Get-Date).AddSeconds({w});"
              "while((Get-Date) -lt $d){{"
              "$up=$true;"
              "try{{Invoke-WebRequest -UseBasicParsing -TimeoutSec 1 "
              "-Uri '{u}'|Out-Null}}catch{{$up=$false}};"
              "if(-not $up){{Start-Process -FilePath '{e}';exit 0}};"
              "Start-Sleep -Milliseconds 400}};"
              "New-Item -Force -ItemType File -Path '{m}'|Out-Null"
              ).format(w=int(wait_s), u=_q(url), e=_q(exe), m=_q(marker))
        # Absolute path when we can resolve it — a tech machine with a mangled
        # PATH must still be able to restart (this is not shell=True any more,
        # so a bare name would just raise).
        shell_exe = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'),
                                 'System32', 'WindowsPowerShell', 'v1.0',
                                 'powershell.exe')
        if not os.path.exists(shell_exe):
            shell_exe = 'powershell'
        return [shell_exe, '-NoProfile', '-NonInteractive',
                '-WindowStyle', 'Hidden', '-Command', ps]

    def _q(s):                           # /bin/sh single-quote escaping
        return str(s).replace("'", "'\\''")
    sh = ("i=0; while [ $i -lt {n} ]; do "
          "if ! curl -sf -m 1 '{u}' >/dev/null 2>&1; then exec '{e}'; fi; "
          "i=$((i+1)); sleep 0.5; done; "
          ": > '{m}'"
          ).format(n=max(1, int(wait_s / 0.5)), u=_q(url), e=_q(exe),
                   m=_q(marker))
    return ['/bin/sh', '-c', sh]


def _relaunch_and_exit():
    """Restart the frozen app so the launcher's boot path applies the update.

    Sequencing matters: the launcher's already-running guard runs BEFORE its
    signed-update path, so the OLD instance must be gone before the NEW one
    health-checks — otherwise it re-attaches to the dying server and the
    update never lands.  We hand that off to a detached helper (see
    _restart_command) that waits for this server's health endpoint to go
    quiet and only then starts the exe, then we exit immediately.

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
    cmd = _restart_command(sys.executable, marker, RESTART_PORT, RESTART_WAIT_S)
    try:
        if os.name == 'nt':
            _sp.Popen(cmd,
                      creationflags=(getattr(_sp, 'CREATE_NO_WINDOW', 0)
                                     | 0x00000008))       # DETACHED_PROCESS
        else:
            _sp.Popen(cmd, start_new_session=True, close_fds=True)
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
<div id="wd" style="font-family:sans-serif;font-size:13px;color:#555"></div>
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
        st.error('The update didn\'t start — the previous OTDR Suite is still '
                 'running. Close it completely (or reboot), then start OTDR '
                 'Suite again.')

    nudge = _update_state()
    if not nudge:
        return
    latest, running = nudge
    st.warning(f'Update {latest} is available (running {running}).')
    if getattr(sys, 'frozen', False):
        if st.button('⬇ Update & restart now', key='upd_nudge_restart',
                     type='primary', use_container_width=True):
            if _relaunch_and_exit():
                _render_restart_watchdog()
            else:
                st.error('Couldn\'t start the restart — close OTDR Suite and '
                         'open it again to pick up the update.')
    else:
        st.caption('Restart the app to apply — updates install at launch.')


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
    st.title('OTDR Suite needs to repair itself')
    st.error('A file this app needs is missing from this computer.')
    st.write(
        'The app checks its own files at every start, and one of them is no '
        'longer there. Security software removing a file after the app '
        'downloaded it is the usual reason. Nothing you have saved is '
        'affected, and no reports are lost.')
    st.write(
        'Click the button below. The app will download a fresh copy of its '
        'files and start again. It takes about a minute.')
    if st.button('Repair and restart', type='primary'):
        _request_repair()
        report_error('engine file missing', exc,
                     {'engine': HERE, 'source': os.environ.get('OTDR_SUITE_SOURCE', '?')})
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
    # A missing engine file is not a crash to show a tech a traceback for.
    _engine_file_missing_page(_engine_exc)

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
                os.makedirs(_ld, exist_ok=True)
                for _f in loose:
                    with open(os.path.join(_ld, os.path.basename(_f.name)),
                              'wb') as _out:
                        _out.write(_f.getbuffer())
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
        elif _src == 'srfr':
            st.session_state['came_from_splicereport_fr'] = True
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

    # Update nudge FIRST — above the tools, so a stale always-on machine sees
    # it before it starts working (the footer's manual check is still there).
    _render_update_nudge()

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
        _zf = st.file_uploader('…or drag & drop the span here — .zip(s), '
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
                   f"B {_span['b_count']} files — loaded in all three tools")
        if _span.get('dropped'):
            st.warning(
                "⚠ This span had more than two direction groups; only **"
                f"{_span['a_prefix']}** + **{_span['b_prefix']}** were loaded "
                f"(into all three tools). Ignored: **{', '.join(_span['dropped'])}** "
                "— e.g. short-shot / FEC traces. If you meant a different pair, "
                "load just those two.")
    st.divider()

    page = st.radio('Tool', ['Viewer', 'Splice Report',
                             'Splice Report FR (beta)', 'Unidirectional',
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
    if st.session_state.get('came_from_splicereport_fr'):
        def _back_to_srfr():
            st.session_state['came_from_splicereport_fr'] = False
            st.session_state['nav_radio'] = 'Splice Report FR (beta)'
        st.button('← Back to Splice Report FR', key='view_back_srfr',
                  on_click=_back_to_srfr)
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
    background:#eef3f8;cursor:pointer;font-weight:600;color:#1f2a36;
    font-family:sans-serif;font-size:13px">&#8862; Open Viewer in its own window</button>
<span style="margin-left:8px;font-size:11px;color:#789;font-family:sans-serif">
    keeps this page free for the report &middot; report cell clicks drive the same window</span>
<script>
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
    _dropped = st.file_uploader(
        '…or drag & drop the files here (.sor / .trc / .json, a whole '
        'folder, or a .zip)',
        type=['sor', 'trc', 'json', 'zip'], accept_multiple_files=True,
        key='ss_drop')
    if _dropped:
        _sdir, _sn = _stage_dropped(_dropped)
        if _sn:
            st.caption(f'📥 {_sn} file(s) staged from the drop — used as the '
                       'input folder.')
            folder = _sdir
        else:
            st.warning('The drop contained no readable OTDR files.')
    if not folder or not os.path.isdir(folder):
        st.info('👆 Choose the folder that holds your `.sor` / `.trc` / `.json` '
                'files — or drag & drop them above.')
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
    _stale = _report_gate('ss')
    if st.button('Run analysis', type='primary', disabled=bool(_stale)):
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
                with st.expander('How far it got'):
                    st.code(_phase)
            _nf, _nb = _count_input_files(folder)
            report_error("secret sauce — timeout",
                         RuntimeError(f"engine exceeded {ENGINE_TIMEOUT_S}s"),
                         {"folder": folder, "format": fmt,
                          "n_files": _nf,
                          "input_mb": (None if _nb is None
                                       else round(_nb / 1_048_576.0, 1)),
                          "reached": (_phase.splitlines() or ['(no output — '
                                      'died before the first phase)'])[-1]},
                         log=_phase or None)
            return
        if proc is None:
            return                                     # cancelled — clean slate
        manifest = _parse_manifest(proc.stdout)
        if manifest is None:
            if not _engine_damaged_notice(proc.stderr, 'ss'):
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
                   f"{c.get('json',0)} JSON found.")
        # The engine excludes suspected-broken traces from the comparison and
        # says so in the manifest; until now nothing rendered it, so a folder
        # could report on fewer fibers than it found with no explanation on
        # screen.  DURANC 1-144: 144 found, 141 compared, 3 excluded.
        _short = res.get('short_traces') or []
        _excl = [e for e in _short if e.get('excluded')]
        if _excl:
            st.warning(
                f"{len(_excl)} trace(s) excluded from the comparison as "
                f"suspected breaks — the report covers the rest.")
            with st.expander(f'Excluded traces ({len(_excl)})'):
                for e in _excl:
                    st.write(f"**{e.get('file','?')}** — {e.get('note','')}")
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
    "reflectance_ceiling":  "LAUNCH_REFL_CEIL_DB",
    "midspan_reflectance":  "MIDSPAN_REFL_FAIL_DB",
    "midspan_refl_ceiling": "MIDSPAN_REFL_CEIL_DB",
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
    # Unticked ceiling = NO ceiling (0.0 sentinel — the engine only applies
    # the band when the value is negative), NOT the 1e9 detection-off value.
    "midspan_refl_ceiling": 0.0,
    # Unticked ceiling = NO ceiling (0.0 sentinel — the engine only applies the
    # band's top when the value is negative), NOT the 1e9 detection-off value.
    "reflectance_ceiling": 0.0,
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
     'defaults': {'value': 0.620}, 'min': 0.0, 'max': 5.0, 'step': 0.01,
     'int': False,
     'help': ('Flag a launch/box connector when BOTH directions measure at '
              'least this much loss on it — the gate is min(A, B). Every '
              'mated connector costs real loss, so this sits well above the '
              'population median: BKF↔DEL runs a 0.42 dB median with 405 of '
              '432 fibers over 0.3, and 0.62 is the value calibrated against '
              'that adjudicated set (its bad fibers sit at 0.716 / 0.690 / '
              '0.645, the next fiber at 0.587). 0 turns this gate off.')},

    {'key': 'conn_uni', 'label': 'Connector loss (1 direction)', 'unit': 'dB',
     'kind': 'scalar', 'globals': {'value': 'LAUNCH_CONN_UNI_MIN_DB'},
     'defaults': {'value': 0.650}, 'min': 0.0, 'max': 5.0, 'step': 0.01,
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
     'help': ('Flag on the connector’s actual loss, (A + B) / 2 — the number '
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
              'all the flag stands — a defect is never hidden because the '
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
              'under the tightest of the three, and costs nothing — no other '
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
              'here. Widening it is NOT the way to recover cells like those — '
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


def _conn_settings_state():
    """The committed connector/launch settings, {global: number}."""
    cur = st.session_state.get('conn_settings')
    if not isinstance(cur, dict):
        cur = dict(_CONN_DEFAULTS)
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

    with st.expander('Connector & launch settings', expanded=False):
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
        commit = otdr_settings_component(rows, default=None, mode='knobs',
                                         key='conn_settings_component')
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
    # Phase-3 sweep discovery (FR beta): loss measured in the raw glass
    # that no stored table marked.
    'sweep': '#6c3483',
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
      border-radius:4px;background:#eef3f8;cursor:pointer;font-weight:600;color:#1f2a36">
      &#8862; Open / focus Viewer window</button>
  <span style="margin-left:8px;font-size:11px;color:#789">click any cell &rarr;
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


def page_splice_report(fr=False):
    # fr=True → "Splice Report FR (beta)": the SAME page and engine with the
    # FastReporter-style trace-confirmation gates (--fr) turned on.  Run
    # state, result, disk cache, and viewer src token are all kept separate
    # from the classic page so the two never show each other's grids; the
    # input widgets (folders / sites / settings panel) are shared on purpose
    # so a tech can flip between the two tools on the same span.
    _p = 'srfr' if fr else 'sr'
    _cache_name = '.srfr_grid_cache.json' if fr else '.sr_grid_cache.json'
    if fr:
        st.markdown('#### Splice Report FR — bidirectional 🧪 *beta*')
        st.caption('Same report, plus **trace-confirmation gates**: every stored '
                   'event-table loss is corroborated against the raw trace '
                   '(FastReporter-style) and values the glass can\'t support are '
                   're-measured — so stale/copied event tables can\'t flag '
                   'phantom cells. Compare its output against the classic '
                   'Splice Report while the feature bakes.')
    else:
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

    # ── OTDR settings panel (pixel-perfect EXFO threshold table) ─────────
    # Renders the customer-profile dropdown + the custom HTML component.
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
        st.warning('OTDR settings panel unavailable — running with default '
                   'thresholds. (Details sent to support.)')
        report_error('splice report — settings panel render', _exc)
        st.session_state.pop('otdr_settings', None)   # → empty overrides below
    # Connector/launch knobs, same guard: a component failure here must leave
    # the report running on engine defaults, not take the page down.
    try:
        _render_conn_settings_panel()
    except Exception as _exc:
        st.warning('Connector & launch settings unavailable — running with '
                   'default connector thresholds. (Details sent to support.)')
        report_error('splice report — connector settings panel render', _exc)
        st.session_state.pop('conn_settings', None)   # → engine defaults below

    if not (dir_a and os.path.isdir(dir_a) and dir_b and os.path.isdir(dir_b)):
        st.info('Pick **both** an A and a B folder (a bidirectional report needs both).')
        return

    st.caption("⏳ Large spans can take several minutes. After you click you'll see "
               "live progress here — **leave this window open and don't refresh.**")
    _stale = _report_gate('sr_fr' if fr else 'sr')
    if st.button('Generate Splice Report', type='primary',
                 disabled=bool(_stale)):
        # Save the report to the user's Downloads — NOT the traces folder (which
        # in one-folder/zip mode is a temp dir that gets cleaned up).
        import folder_intake as _fi
        _safe = lambda s: ''.join(c if (c.isalnum() or c in ' -_') else '_' for c in str(s)).strip() or 'site'
        _suffix = '_SpliceReport_FR.xlsx' if fr else '_SpliceReport.xlsx'
        out_xlsx = os.path.join(_fi.default_report_dir(),
                                f'{_safe(site_a)}_to_{_safe(site_b)}{_suffix}')
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
        st.session_state[f'{_p}_pending_cmd'] = splicereport_cmd(
            dir_a, dir_b, out_xlsx, site_a, site_b, overrides=overrides, fr=fr)
        # The dirs this run used — cell-click deep links carry them so the
        # Viewer (a FRESH session after the anchor nav) can find the span,
        # including one-folder/zip runs staged into temp dirs the viewer was
        # never told about (the boss's 'clicks a cell, trace never loads').
        st.session_state[f'{_p}_dirs'] = (dir_a, dir_b)
        st.session_state.pop(f'{_p}_result', None)     # clear any prior result
        st.rerun()

    # Background run with a live progress panel + Cancel; the engine runs as a
    # concurrent subprocess so the page never freezes.  Stashes sr_result on done.
    if f'{_p}_pending_cmd' in st.session_state or f'{_p}_job' in st.session_state:
        try:
            proc = run_engine_live(_p, running_title='Generating the splice report'
                                   + (' (FR beta)' if fr else ''))
        except subprocess.TimeoutExpired:
            st.error(f'Splice report timed out after {ENGINE_TIMEOUT_S}s '
                     'and was stopped. Try fewer files, or check for a '
                     'wedged engine.')
            report_error(f'splice report{" FR" if fr else ""} (hub) — timeout',
                         RuntimeError(f"engine exceeded {ENGINE_TIMEOUT_S}s"),
                         {'dir_a': dir_a, 'dir_b': dir_b})
            proc = None
        if proc is not None:
            manifest = _parse_manifest(proc.stdout)
            if manifest is None or not manifest.get('ok'):
                if not _engine_damaged_notice(proc.stderr, 'sr'):
                    st.error((manifest or {}).get('error', 'Splice report failed.'))
                    with st.expander('Engine log'):
                        st.code(proc.stderr[-4000:] or '(no output)')
                report_error(f'splice report{" FR" if fr else ""} (hub)',
                             RuntimeError((manifest or {}).get('error', 'no manifest')),
                             {'dir_a': dir_a, 'dir_b': dir_b},
                             log=proc.stderr)
            else:
                st.session_state[f'{_p}_result'] = manifest
                # Disk cache (same idea as Secret Sauce's pairs_cache.json):
                # a cell-click into the Viewer is a URL nav that WIPES
                # session_state — this file is how "← Back" re-shows the grid
                # without re-running the multi-minute engine.
                try:
                    _sd = st.session_state.get(f'{_p}_dirs') or (None, None)
                    if _sd[0] and os.path.isdir(_sd[0]):
                        with open(os.path.join(_sd[0], _cache_name),
                                  'w', encoding='utf-8') as fh:
                            json.dump({'manifest': manifest, '_dirs': list(_sd)}, fh)
                except Exception:
                    pass

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
                with open(os.path.join(_cand[0], _cache_name),
                          encoding='utf-8') as fh:
                    _cached = json.load(fh)
                # The fr-provenance check is belt+suspenders on top of the
                # per-mode cache filename: never show the other tool's grid.
                if (_cached.get('manifest', {}).get('ok')
                        and bool(_cached.get('manifest', {}).get('fr')) == fr
                        and _cached.get('_dirs', [None])[0] == _cand[0]):
                    res = _cached['manifest']
                    st.session_state[f'{_p}_result'] = res
                    st.session_state[f'{_p}_dirs'] = tuple(_cached['_dirs'])
                    break
            except Exception:
                continue
    if not (res and res.get('ok')):
        return

    # Summary + Excel download
    st.success(f"{res['site_a']} → {res['site_b']}  ·  {res['n_fibers']} fibers  ·  "
               f"{res['n_splices']} splices  ·  span {res['span_km']} km  ·  "
               f"{res['n_flagged']} flagged events")
    if fr:
        st.caption('🧪 **FR beta** — trace-confirmation gates were active for '
                   'this run. Cross-check surprises against the classic '
                   'Splice Report on the same folders.')
    xp = res.get('xlsx')
    if xp and os.path.exists(xp):
        with open(xp, 'rb') as fh:
            st.download_button('⬇ Excel report', data=fh.read(),
                               file_name=os.path.basename(xp), key=f'{_p}_dl')

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
    # Viewer frame conversion; the popped Viewer window reads this report's
    # span from the trace server, so point it here (one span at a time).
    _mani = st.session_state.get(f'{_p}_result') or {}
    _launch_a = float(_mani.get('launch_a_km') or 0.0)
    def _vkm(km):
        return round(float(km) + _launch_a, 4)
    _sd = st.session_state.get(f'{_p}_dirs') or (None, None)
    _port = ensure_trace_server()
    if _sd[0] and os.path.isdir(_sd[0]):
        trace_server.set_dirs(_sd[0], _sd[1] if (_sd[1] and os.path.isdir(_sd[1])) else None)
    _popout = _viewer_click_target(_p)
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
                links.append(_cell_markup(
                    _popout, c['fiber'], _vkm(c['km']), 'both', color,
                    c['label'], f"F{c['fiber']}{loss}",
                    href=(f"?nav=viewer&fiber={c['fiber']}&km={_vkm(c['km'])}"
                          f"&dir=both{_dirs_qs}&src={_p}")))
            html.append("<td style='padding:3px 6px;border:1px solid #eef2f6;white-space:nowrap'>"
                        + "<br>".join(links) + "</td>")
        html.append('</tr>')
    html.append('</tbody></table></div>')
    if _popout:
        _render_clickable_grid(''.join(html), _port, src=_p)
    else:
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
     'defaults': {'value': 0.650}, 'min': 0.0, 'max': 5.0, 'step': 0.01,
     'int': False,
     'help': ('Flag a connector whose loss reads at or above this in the one '
              'direction shot — a bare threshold, not judged against the '
              'population. Connectors are found either way and every reading '
              'is listed; this only decides which ones shade a cell. 0 turns '
              'the flag off. One direction cannot separate a connector\'s true '
              'loss from the backscatter step between the fibers it joins, so '
              'the number is an upper bound; the bidirectional Splice Report '
              'averages that term away.')},

    {'key': 'break_floor', 'label': 'Break floor — min EOF', 'unit': 'km',
     'kind': 'scalar', 'globals': {'value': 'UNI_BREAK_MIN_KM'},
     'defaults': {'value': 0.3}, 'min': 0.05, 'max': 10.0, 'step': 0.05,
     'int': False,
     'help': 'A fiber ending below this is too short to count as a break.'},

    {'key': 'break_short_by', 'label': 'Break — EOF short of span by', 'unit': 'km',
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

    with st.expander('Uni settings (thresholds & bands)', expanded=False):
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
    files.  Returns (staging_dir, n_trace_files)."""
    import tempfile
    import folder_intake as fi
    sig = tuple(sorted((f.name, getattr(f, 'size', 0)) for f in files))
    hit = _DROP_STAGE_CACHE.get(sig)
    if hit and os.path.isdir(hit[0]):
        return hit
    td = tempfile.mkdtemp(prefix='otdr_drop_')
    for f in files:
        try:
            if f.name.lower().endswith('.zip'):
                fi.extract_zip(f, td)
            else:
                with open(os.path.join(td, os.path.basename(f.name)), 'wb') as out:
                    out.write(f.getbuffer())
        except Exception as exc:
            print(f'drop staging: skipped {f.name}: {exc}')
    # Count staged trace files ourselves — folder_intake.find_otdr_files
    # deliberately excludes .trc, but Secret Sauce accepts it.
    n = 0
    for _root, _dirs, _files in os.walk(td):
        n += sum(1 for x in _files
                 if not x.startswith('.')
                 and x.lower().endswith(('.sor', '.trc', '.json')))
    _DROP_STAGE_CACHE[sig] = (td, n)
    return td, n


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
    _dropped = st.file_uploader(
        '…or drag & drop the shots here (.sor / .json files, a whole '
        'folder, or a .zip)',
        type=['sor', 'json', 'zip'], accept_multiple_files=True,
        key='uni_drop')
    if _dropped:
        _sdir, _sn = _stage_dropped(_dropped)
        if _sn:
            st.caption(f'📥 {_sn} trace file(s) staged from the drop — used as '
                       'the input.')
            folder = _sdir
        else:
            st.warning('The drop contained no readable `.sor` / `.json` files.')
    # ── Uni settings box (thresholds & radii → engine overrides) ──────
    # Rendered BEFORE the folder guard (2026-07-31): thresholds are
    # settable before any data is loaded, same as the SR/FR panel.
    # Guarded like the SR panel: a render failure must not take down the
    # page — fall back to engine defaults with a visible warning.
    try:
        uni_overrides = _render_uni_settings_panel()
    except Exception as _exc:
        st.warning('Uni settings panel unavailable — running with default '
                   'thresholds. (Details sent to support.)')
        report_error('unidirectional — settings panel render', _exc)
        uni_overrides = None

    if not folder or not os.path.isdir(folder):
        st.info('👆 Choose the folder that holds the one-direction `.sor` / '
                '`.json` shots — or drag & drop them above.')
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
    _stale = _report_gate('uni')
    if st.button('Run unidirectional report', type='primary',
                 disabled=bool(_stale)):
        out_xlsx = os.path.join(folder, 'unidirectional_events.xlsx')
        st.session_state['uni_pending_cmd'] = uni_cmd(folder, out_xlsx,
                                                      direction=dir_choice,
                                                      landmarks=landmarks,
                                                      overrides=uni_overrides)
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
    # The fiber count NEVER appears without its denominator: a 480-fiber
    # report on an 864-file folder must not read as "done, 480 fibers".
    _n_folder = u.get('n_files_in_folder')
    _n_drop = u.get('n_files_not_analysed') or 0
    _covered = (f"{u.get('n_fibers', '?')} of {_n_folder} files"
                if _n_folder and _n_drop else f"{u.get('n_fibers', '?')} fibers")
    _line = (f"{_covered} · direction {u.get('direction', '?')} · "
             f"span ≈ {u.get('span_km', '?')} km")
    if _n_drop:
        st.error(f"⚠️ PARTIAL COVERAGE — {_line}")
        st.error(u.get('coverage_headline') or '')
    else:
        st.success(f"Done — {_line}")
    counts = u.get('direction_counts') or {}
    merged = u.get('merged_signatures') or []
    for m in merged:
        st.warning(
            f"Mistyped site code: {m.get('n_fibers', '?')} trace file(s) say "
            f"**{m.get('signature', '?')}** where the rest say "
            f"**{u.get('direction', '?')}** — fibers "
            f"{_fiber_ranges(m.get('fibers') or [])} are INCLUDED in this "
            "report (older builds dropped them silently).  Check the "
            "GenParams site code on those shots.")
    # A signature folded in above is not a second span — don't also tell the
    # tech to re-run for it.
    if len(counts) - len(merged) > 1:
        st.error(
            f"This folder mixes {len(counts) - len(merged)} directions — the "
            f"report covers ONLY '{u.get('direction', '?')}'. "
            + ' '.join(
                f"{n} file(s) shot as '{sig}' were NOT analysed."
                for sig, n in sorted(counts.items(), key=lambda kv: -kv[1])
                if sig != u.get('direction')
                and sig not in {m.get('signature') for m in merged})
            + "  Pick another from the Direction list and re-run to cover it.")
    cols = st.columns(5)
    cols[0].metric('Splice columns', len(u.get('splice_columns') or []))
    cols[1].metric('Bend/Damage columns', len(u.get('bend_columns') or []))
    cols[2].metric('Break columns', len(u.get('break_columns') or []))
    # A panel-to-panel span has no splices at all — without this metric the
    # header reads 0 / 0 / 0 and the report looks like it found nothing.
    cols[3].metric('Connector columns', len(u.get('connector_columns') or []))
    rp = u.get('reburn_pct')
    cols[4].metric('Reburn', f"{rp:.2f}%" if rp is not None else '—')
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
            + (f"; **{_cd} DARK** — the trace stops at the connector, no light "
               "through the mate" if _cd else "")
            + ".  One direction cannot separate a connector's true loss from "
              "the backscatter step between the fibers it joins, so these "
              "losses are upper bounds — the bidirectional Splice Report "
              "averages that term away.")
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
                       'break': '#c00000', 'reflective': '#6c3483',
                       'connector': '#0e6655'}
        _uni_port = ensure_trace_server()
        if folder and os.path.isdir(folder):
            trace_server.set_dirs(folder, None)   # popped Viewer reads this span
        _uni_popout = _viewer_click_target('uni')
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


# ─── Route ────────────────────────────────────────────────────────────────
# Global catch-all: any unhandled error during a page render/action posts to
# Slack, then re-raises so Streamlit still shows the tech its red error box.
try:
    if page == 'Viewer':
        page_viewer()
    elif page == 'Splice Report':
        page_splice_report()
    elif page == 'Splice Report FR (beta)':
        page_splice_report(fr=True)
    elif page == 'Unidirectional':
        page_unidirectional()
    else:
        page_duplicate_check()
except Exception as _exc:
    report_error(f"hub page: {page}", _exc)
    raise

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
        st.sidebar.warning('Could not reach the update server — check the '
                           'connection and try again.')
    elif _cur is not None and _latest <= _cur:
        st.sidebar.success(f'Up to date — engine {_cur} is the latest.')
    elif _cur is None:
        st.sidebar.info(f'Latest published update: {_latest} · running: dev '
                        'checkout (updates apply to installed builds only).')
    else:
        st.sidebar.info(f'Update {_latest} is available (running {_cur}).')
        if getattr(sys, 'frozen', False):
            if st.sidebar.button('⬇ Update & restart now', key='upd_restart',
                                 type='primary', use_container_width=True):
                if _relaunch_and_exit():
                    _render_restart_watchdog(sidebar=True)
        else:
            st.sidebar.caption('Restart the app to apply — updates install '
                               'at launch.')

# Rollout ping: when the build identity changed since the last run (the
# launcher applied a verified update, or a fresh install's first boot), tell
# the shared Slack channel — per-machine confirmation without footer-reading.
# Marker-deduped to once per version; silent no-op in dev / without a webhook.
try:
    maybe_report_update()
except Exception:
    pass

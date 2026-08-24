"""After 'Update & restart now', the page must RE-RENDER, not just reconnect.

WHAT WENT WRONG.  ``_relaunch_and_exit`` hands the restart to a detached
helper and exits, and its docstring asserted:

    "The tech's browser tab auto-reconnects once the new server binds the
     same port."

Half right, and the wrong half is the dangerous one.  Measured against a plain
Streamlit 1.50 app with no watchdog:

    45 s outage    modal shown throughout, then recovered on its own
    >4 min outage  modal shown throughout, then recovered on its own

So the client does NOT give up — "Connection error: Streamlit server is not
responding" is what it shows WHILE retrying.  But on both recoveries the page
came back still displaying the render from BEFORE the outage ("rendered at
17:00:48"), served by a brand-new process that had never heard of that
session.  It reconnects; it does not re-render.

After a restart that is exactly backwards from what the tech needs.  The new
process is running a NEW ENGINE, and different engines print different numbers
for the same traces — which is the entire reason the restart exists.  A page
that silently keeps showing the old engine's numbers is worse than one that
plainly looks dead, because nothing about it looks wrong.

Meanwhile all three restart buttons printed "this page will reconnect in about
half a minute", which described neither behaviour.

THE FIX.  ``_render_restart_watchdog()`` renders a components iframe — ALREADY
LOADED in the browser, so it outlives the server that served it — which polls
the same ``/_stcore/health`` the launcher and the restart helper poll, and
RELOADS the hub once a new server answers.  A reload is the only way to get a
fresh session rendered by the engine that is actually running.

THE DANGEROUS FAILURE MODE is the opposite one: a reload starts a FRESH
session, so a watchdog that fired while the process was alive would throw away
the tech's loaded span.  Hence the down-then-up rule — it reloads only after
the old server has been SEEN to go away.

WHAT THESE TESTS PIN.  There is no JS engine in this repo, so, following
test_viewer_boot_race, they pin the INVARIANT and the STRUCTURE:

  * every restart call site renders the watchdog, and no call site still
    makes the old promise;
  * the down-then-up guard is really in the emitted script;
  * a Python mirror of the state machine never reloads a healthy server,
    reloads exactly once across a restart, and gives up at the deadline —
    and the SAME mirror with the guard removed reloads a healthy server,
    so the check cannot pass vacuously.
"""
from __future__ import annotations

import re

from conftest import APP_PATH


def _app_src():
    return open(APP_PATH, encoding='utf-8').read()


def _watchdog_ns():
    """Exec just the watchdog helpers, so nothing imports the Streamlit hub."""
    src = _app_src()
    m = re.search(r"RESTART_RECONNECT_TIMEOUT_S = .*?(?=\ndef _render_update_nudge)",
                  src, re.S)
    assert m, "watchdog helper block not found in app.py"
    ns = {'st': None, 'st_components_html': None}
    exec(m.group(0), ns)
    return ns


# ─── structure ───────────────────────────────────────────────────────────

def test_every_restart_call_site_renders_the_watchdog():
    """A restart that leaves the page dead is the whole bug — no call site may
    kick off _relaunch_and_exit without arming the watchdog."""
    src = _app_src()
    sites = [m.start() for m in re.finditer(r'if _relaunch_and_exit\(\):', src)]
    assert len(sites) == 3, f"expected 3 restart call sites, found {len(sites)}"
    for pos in sites:
        window = src[pos:pos + 400]
        assert '_render_restart_watchdog(' in window, (
            "a restart call site does not arm the reconnect watchdog:\n"
            + window.splitlines()[0])


def test_the_old_promise_is_gone():
    """The caption claimed the page reconnects on its own.  If it comes back,
    the watchdog has been bypassed somewhere."""
    assert 'this page will reconnect' not in _app_src()


def test_the_docstring_no_longer_claims_auto_reconnect():
    assert 'browser tab auto-reconnects' not in _app_src()


# ─── the emitted script ──────────────────────────────────────────────────

def test_watchdog_probes_the_launcher_health_endpoint():
    ns = _watchdog_ns()
    html = ns['_restart_watchdog_html']()
    assert '/_stcore/health' in html
    assert ns['RESTART_HEALTH_PATH'] == '/_stcore/health'


def test_watchdog_reload_is_guarded_by_having_seen_the_server_go_down():
    """The load-bearing safety property, read straight out of the script."""
    html = _watchdog_ns()['_restart_watchdog_html']()
    # the reload must sit behind sawDown, and sawDown must only be set on a
    # failed probe
    assert re.search(r'if\s*\(!up\)\s*\{\s*sawDown\s*=\s*true', html), html
    assert re.search(r'else if\s*\(sawDown\)\s*\{', html), html
    # the CALL, not the function definition (which is hoisted above the loop)
    reload_at = html.index('!reloadHub()')
    guard_at = html.index('else if (sawDown)')
    assert guard_at < reload_at, "reload is not behind the sawDown guard"


def test_watchdog_deadline_is_baked_in_milliseconds():
    ns = _watchdog_ns()
    html = ns['_restart_watchdog_html'](timeout_s=7)
    assert '7000' in html
    assert str(ns['RESTART_RECONNECT_TIMEOUT_S'] * 1000) in ns['_restart_watchdog_html']()


# ─── behavioural mirror ──────────────────────────────────────────────────

def _mirror(health_seq, guarded=True, deadline_after=None):
    """Python mirror of the watchdog loop.  `health_seq` is the sequence of
    probe results; returns (reloaded, polls, timed_out)."""
    saw_down = False
    for i, up in enumerate(health_seq):
        if deadline_after is not None and i >= deadline_after:
            return False, i, True
        if not up:
            saw_down = True
        elif saw_down or not guarded:
            return True, i + 1, False
    return False, len(health_seq), False


def test_mirror_never_reloads_a_healthy_server():
    """15 polls of a live server — the tech's session must survive."""
    reloaded, _, _ = _mirror([True] * 15)
    assert reloaded is False


def test_mirror_reloads_once_across_a_restart():
    seq = [True, True] + [False] * 20 + [True, True, True]
    reloaded, polls, _ = _mirror(seq)
    assert reloaded is True
    # fires on the FIRST probe after the server returns, not later
    assert polls == 23


def test_mirror_gives_up_at_the_deadline():
    reloaded, _, timed_out = _mirror([False] * 50, deadline_after=10)
    assert reloaded is False and timed_out is True


def test_the_guard_has_teeth():
    """Same mirror, guard removed: a healthy server now gets reloaded — so
    test_mirror_never_reloads_a_healthy_server is not passing vacuously."""
    reloaded, _, _ = _mirror([True] * 15, guarded=False)
    assert reloaded is True

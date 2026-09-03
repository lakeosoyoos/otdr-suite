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

WHAT THE WATCHDOG STILL GOT WRONG (and the overlay tests below pin).  The
reload was right; being SEEN was not.  About six seconds after the old process
exits, Streamlit's own client raises a "Connection error" modal over a dimmed
backdrop, and the watchdog's reassurance was 13 px of grey text in the sidebar
UNDERNEATH it.  Because the launcher serves the hub on 127.0.0.1 and Streamlit
picks its wording by ``hostname === "localhost"``, the modal a tech actually
reads is "Streamlit server is not responding. Are you connected to the
internet?" — a WiFi question, during a working update.  Both the boss and a
tech closed the app instead of waiting; closing it is why the update appeared
to land only on the NEXT launch.

So the watchdog now paints a full-viewport panel in the PARENT document the
moment it is armed.  These tests pin that it outranks the modal, that it is
painted at arm time rather than after the first probe, that the old in-iframe
caption survives as the fallback when the parent is unreachable, and that
every give-up route hands over a way out instead of spinning forever.
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
    # A floor, not an exact count: legitimate new restart routes get added (the
    # engine-repair button is one), and pinning the number only ever fails the
    # build for the person adding one.  The loop below is the real check — it
    # holds for EVERY site, however many there are.
    assert len(sites) >= 3, f"restart call sites went missing: {len(sites)}"
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


# ─── the overlay ─────────────────────────────────────────────────────────

def test_overlay_is_painted_into_the_parent_document():
    """A panel inside the 40 px iframe would sit under Streamlit's modal just
    like the caption did.  It has to be in the parent DOM."""
    html = _watchdog_ns()['_restart_watchdog_html']()
    assert 'window.parent.document' in html
    assert re.search(r'd\.body\.appendChild\(el\)', html), html


def test_overlay_outranks_the_connection_error_modal():
    """Full viewport, and the top of the stacking order — anything less and
    the tech is back to reading about their internet connection."""
    html = _watchdog_ns()['_restart_watchdog_html']()
    assert 'position:fixed' in html
    for side in ('top:0', 'left:0', 'right:0', 'bottom:0'):
        assert side in html, side
    assert 'z-index:2147483647' in html


def test_overlay_is_painted_at_arm_time_not_after_the_first_probe():
    """Arming and the 0.7 s exit are the same click, so the page is already
    going down: waiting for a failed probe just leaves a ~1.5 s window where
    the modal can land first.  paint() must run before the poll loop."""
    html = _watchdog_ns()['_restart_watchdog_html']()
    paint_at = html.rindex('paint();')
    tick_at = html.rindex('tick();')
    assert paint_at < tick_at, 'paint() must precede the first tick()'
    # ...and not from inside the loop, where it would run once per poll
    loop = html[html.index('function tick()'):html.rindex('paint();')]
    assert 'paint()' not in loop, 'paint() must not be called from the poll loop'


def test_overlay_says_the_one_thing_that_stops_the_bad_outcome():
    """The whole failure was techs closing the app mid-restart."""
    html = _watchdog_ns()['_restart_watchdog_html']()
    assert 'Leave this window open' in html


def test_overlay_takes_its_colours_from_the_running_theme():
    """Hard-coded white would flash a dark-theme machine at the exact moment
    we are asking the tech to trust the screen."""
    html = _watchdog_ns()['_restart_watchdog_html']()
    assert 'getComputedStyle' in html
    assert '.stApp' in html


def test_the_iframe_caption_survives_as_the_fallback():
    """If the parent is ever unreachable (a future non-srcdoc component host),
    say() must still write somewhere — that strip is the whole reason it is
    still in the markup."""
    html = _watchdog_ns()['_restart_watchdog_html']()
    assert 'id="wd"' in html
    say = re.search(r'function say\(t\)\{(.*?)\n  \}', html, re.S)
    assert say, html
    assert 'note.textContent' in say.group(1)
    assert 'msg.textContent' in say.group(1)


def test_every_give_up_route_hands_over_a_way_out():
    """Two ways the restart can end badly — the deadline, and a reload we are
    not allowed to perform.  Both must reveal the button; a panel that keeps
    spinning forever is the modal all over again."""
    html = _watchdog_ns()['_restart_watchdog_html']()
    bail = re.search(r'function bail\(t\)\{(.*?)\n  \}', html, re.S)
    assert bail, html
    body = bail.group(1)
    assert 'esc.style.display  = "inline-block"' in body
    assert 'spin.style.display = "none"' in body, 'a stopped restart must stop spinning'
    # the deadline branch and the reload-refused branch both go through it
    assert html.count('bail("') == 2, html
    assert 'if (Date.now() > DEADLINE){\n      bail(' in html
    assert 'if (!reloadHub())\n          bail(' in html


def test_the_give_up_hint_does_not_contradict_the_button():
    """'Leave this window open' next to a 'Reload this page' button is how a
    tech ends up doing neither."""
    html = _watchdog_ns()['_restart_watchdog_html']()
    bail = re.search(r'function bail\(t\)\{(.*?)\n  \}', html, re.S).group(1)
    assert 'hint.textContent' in bail
    assert 'close OTDR Suite completely' in bail


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

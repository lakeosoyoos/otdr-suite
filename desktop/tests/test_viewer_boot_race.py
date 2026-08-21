"""Viewer boot: gTraces must never come back holding a key twice.

WHAT WENT WRONG.  ``bootLoad()`` had TWO entry points that could run at the
same time:

    viewer.html  the boot IIFE at the bottom of the file   ``await bootLoad()``
    viewer.html  the window 'focus' listener               ``await bootLoad()``

The focus listener is not hypothetical.  The hub's pop-out calls ``vw.focus()``
on the viewer window (app.py) immediately before it posts a jump, and a window
manager raising a freshly opened viewer fires the same event — either lands
inside boot's first fetch.

Both passes reach ``addFibers()``, which PLANS synchronously and COMMITS after
its awaits: it decides which keys are missing with
``gTraces.some(t => t.key === key)`` up front, then fetches, then pushes in
``loadOne``.  A pass that starts while the first is still fetching therefore
sees an empty gTraces, plans the very same keys, and pushes them again — the
dedupe cannot see a pass that has not committed yet.

Rendered against a folder with 20 s of fetch latency, the default bidirectional
boot produced::

    gTraces  ["b-64", "a-64", "b-64", "a-64"]      (expected two keys)
    readout  "4 traces loaded"                     (expected "2 traces loaded")
    events   "4 traces, 40 events — 16 flagged"    (expected 2 / 20 / 8)

and the FastReporter grid disappeared altogether, because ``renderEventTable``
only draws the FR layout for <= 2 traces — 4 fell through to the flat list.

THE FIX.  ``bootLoadOnce()`` — a second caller JOINS the pass already in flight
instead of starting its own, and the handle clears when the pass settles so the
retry the focus listener exists for (folders seeded after boot) still works.

WHAT THESE TESTS PIN.  A race cannot be pinned by timing from pytest, and this
repo has no JS engine, so these tests pin the INVARIANT and the STRUCTURE that
delivers it:

  * every ``bootLoad()`` call site goes through the guard;
  * the guard really is single-flight (joins, and clears on settle);
  * an asyncio mirror of plan-then-commit, driven through the guard rule PARSED
    OUT OF viewer.html, ends boot with unique keys under every interleaving —
    and the same mirror with the guard removed reproduces the duplicate, so the
    check has teeth rather than passing vacuously.
"""
from __future__ import annotations

import asyncio
import os
import re

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
VIEWER_HTML = os.path.join(ROOT, 'viewer', 'viewer.html')


def _viewer_src():
    return open(VIEWER_HTML, encoding='utf-8').read()


def _strip_comments(src):
    """Drop // line comments so prose about bootLoad() is not mistaken for a
    call to it.  Crude but sufficient: viewer.html has no // inside strings on
    the lines that matter, and block comments are not used for these."""
    return '\n'.join(re.sub(r'//.*$', '', ln) for ln in src.split('\n'))


# ─── structure: nobody may call bootLoad() around the guard ───────────────

def test_every_bootload_call_goes_through_the_guard():
    """The defect was a SECOND caller.  Any future third caller that reaches
    bootLoad() directly re-opens it, so the call sites are pinned here."""
    src = _strip_comments(_viewer_src())
    # Calls to bootLoad(...) that are not the declaration and not bootLoadOnce.
    calls = [m.start() for m in re.finditer(r'(?<!Once)\bbootLoad\s*\(', src)]
    decl = src.index('async function bootLoad(')
    calls = [c for c in calls if c != decl + len('async function ')]

    guard = src.index('function bootLoadOnce()')
    guard_end = src.index('\n}', guard)
    outside = [c for c in calls if not (guard < c < guard_end)]
    assert not outside, (
        'bootLoad() is called directly at offset(s) %r — every entry point must '
        'go through bootLoadOnce() or the two passes race again' % outside)

    assert len(calls) == 1, (
        'expected exactly one bootLoad() call (the one inside bootLoadOnce); '
        'found %d' % len(calls))


def test_both_boot_entry_points_use_the_guard():
    """The IIFE and the focus listener are the two real doors — both must use
    bootLoadOnce()."""
    src = _strip_comments(_viewer_src())

    i = src.index("window.addEventListener('focus'")
    focus_body = src[i:src.index('\n});', i)]
    assert 'bootLoadOnce()' in focus_body, (
        "the 'focus' listener no longer boots through the guard")

    j = src.index('(async function boot()')
    iife = src[j:]
    assert 'bootLoadOnce()' in iife, 'the boot IIFE no longer uses the guard'


def test_jump_messages_wait_for_a_boot_in_flight():
    """Second door onto the same overlap: the hub does vw.focus() then
    postMessage, so a jump can arrive while the focus-started boot is still
    fetching.  applyTarget must queue behind it."""
    src = _strip_comments(_viewer_src())
    i = src.index("window.addEventListener('message'")
    body = src[i:src.index('\n});', i)]
    assert 'gBootInFlight' in body and 'applyTarget' in body, (
        'the otdr-jump listener no longer waits for a boot in flight — a jump '
        'that arrives mid-boot plans against an uncommitted gTraces')


# ─── the guard's shape, read out of source and mirrored below ────────────

def guard_rule_from_source(src=None):
    """Return 'single-flight' | 'none' for bootLoadOnce().

    'single-flight' means: a call made while a pass is in flight returns THAT
    pass, and the handle is released when the pass settles (so a later, genuine
    re-boot is still possible).  Parsed, not assumed, so deleting either half
    of the guard flips the mirror below and its numbers fail.
    """
    src = src if src is not None else _viewer_src()
    if 'function bootLoadOnce()' not in src:
        return 'none'
    i = src.index('function bootLoadOnce()')
    body = src[i:src.index('\n}', i)]
    joins = re.search(r'if\s*\(\s*gBootInFlight\s*\)\s*return\s+gBootInFlight\s*;',
                      body) is not None
    releases = re.search(r'finally\s*\{\s*gBootInFlight\s*=\s*null\s*;\s*\}',
                         body) is not None
    if joins and releases:
        return 'single-flight'
    raise AssertionError(
        "bootLoadOnce() changed shape (joins=%r, releases=%r) — this test "
        "models the guard, so teach the model the new form rather than "
        "deleting the check" % (joins, releases))


def test_guard_is_single_flight():
    assert guard_rule_from_source() == 'single-flight'


# ─── the invariant: an asyncio mirror of plan-then-commit ────────────────
#
# This mirrors addFibers()'s two-phase shape — plan the missing keys against
# gTraces synchronously, await the fetches, then push — and drives it through
# BOTH boot entry points with the fetch made slow enough that they overlap.
# Not the browser's timing; the browser's STRUCTURE.

class _Viewer:
    """The parts of viewer.html this invariant lives in."""

    def __init__(self, guard, fetch_delay=0.05):
        self.traces = []                 # gTraces
        self.guard = guard               # 'single-flight' | 'none'
        self.fetch_delay = fetch_delay
        self._in_flight = None           # gBootInFlight
        self.passes = 0                  # how many boot passes actually ran

    async def _load_one(self, key):
        await asyncio.sleep(self.fetch_delay)      # /api/trace
        self.traces.append(key)                    # loadOne's push

    async def add_fibers(self, fibers, dirs):
        # PLAN — synchronous, against gTraces as it stands right now.
        tasks = [f'{d}-{f}' for f in fibers for d in dirs
                 if not any(t == f'{d}-{f}' for t in self.traces)]
        # COMMIT — only after the awaits.
        await asyncio.gather(*(self._load_one(k) for k in tasks))

    async def boot_load(self):
        self.passes += 1
        await self.add_fibers([64], ['a', 'b'])    # autoloadDefault: both dirs

    async def boot_load_once(self):
        if self.guard == 'none':                   # the pre-fix shape
            return await self.boot_load()
        if self._in_flight is not None:
            return await self._in_flight           # join the pass in flight
        self._in_flight = asyncio.ensure_future(self.boot_load())
        try:
            return await self._in_flight
        finally:
            self._in_flight = None


async def _boot_with_focus_at(offset, guard):
    """Boot, then fire the 'focus' entry point `offset` seconds in — i.e. while
    boot's fetches are still outstanding."""
    v = _Viewer(guard)

    async def focus_entry():
        await asyncio.sleep(offset)
        if not v.traces:                     # the listener's own condition
            await v.boot_load_once()

    await asyncio.gather(v.boot_load_once(), focus_entry())
    return v


@pytest.mark.parametrize('offset', [0.0, 0.005, 0.01, 0.02, 0.04])
def test_boot_leaves_unique_keys_under_every_interleaving(offset):
    """THE INVARIANT.  However the focus entry point interleaves with the boot
    IIFE, boot ends with each key exactly once."""
    v = asyncio.new_event_loop().run_until_complete(
        _boot_with_focus_at(offset, guard_rule_from_source()))
    assert sorted(v.traces) == ['a-64', 'b-64'], (
        'boot left gTraces = %r — a key was loaded twice' % (v.traces,))
    assert len(v.traces) == len(set(v.traces))
    assert v.passes == 1, (
        'two boot passes ran (%d) — they must not both run' % v.passes)


def test_the_mirror_reproduces_the_bug_without_the_guard():
    """Teeth check.  The same mirror with the guard removed must produce the
    duplicate that was observed in the browser — otherwise the test above
    passes for the wrong reason."""
    v = asyncio.new_event_loop().run_until_complete(
        _boot_with_focus_at(0.005, 'none'))
    assert len(v.traces) == 4 and len(set(v.traces)) == 2, (
        'the unguarded mirror no longer reproduces the duplicate (%r) — it has '
        'stopped modelling addFibers plan-then-commit' % (v.traces,))
    assert v.passes == 2


def test_guard_still_allows_a_later_reboot():
    """The focus listener exists to retry when folders are seeded AFTER boot.
    Single-flight must not degrade into run-once-ever."""
    async def scenario():
        v = _Viewer(guard_rule_from_source())
        await v.boot_load_once()               # first pass
        v.traces.clear()                       # folders changed; nothing loaded
        await v.boot_load_once()               # must run again
        return v

    v = asyncio.new_event_loop().run_until_complete(scenario())
    assert v.passes == 2, 'the guard blocked a legitimate later re-boot'
    assert sorted(v.traces) == ['a-64', 'b-64']

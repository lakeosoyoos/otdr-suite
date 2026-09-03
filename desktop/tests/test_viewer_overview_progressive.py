"""Whole-cable overview: the canvas must fill in DURING the load, not after it.

WHAT WENT WRONG.  ``loadOverview()`` fetches 1152 fibers in chunks of 144 and
calls ``draw()`` after each chunk, for a stated reason:

    // Chunked so the canvas fills in progressively instead of showing nothing
    // for ~30 s while 1152 SOR files are parsed on first visit to a folder.

It showed nothing for ~30 s.  Three facts in three different functions:

    clearAll()      gView = null
    draw()          if (gTraces.length === 0 || !gView) return;
    addFibers()     if (gAutoFit) fit();      <-- AFTER loadOverview() resolves

``fit()`` is the only thing that fills ``gView``, and it runs after the loop.
So on a cold page, and again after every Clear All, every per-chunk ``draw()``
hit the early return and did nothing.  ``renderChips()`` has the same shape --
it runs only after the loop -- so the strip printed the literal string "no
traces loaded" beside a readout counting up to 1152.

MEASURED IN THE BROWSER, same span (Sacramento, 1152 fibers), same 8 s mark,
288 traces resident in gTraces both times:

                        before          after
    gView               null            set
    non-white px        0               245,837
    chip strip          "no traces      "F1-F288 A->B  288 traces,
                         loaded"          ~2000 pts"
    readout             loading 288/1152 traces...   (identical)

COST.  dataBounds() over the full 1152 measures 10.7 ms, and the loop runs it
on 1/8, 2/8 ... 8/8 of the traces -- 48 ms added to a ~30 s load, 0.16%.  Total
load 30.2 s fixed vs 27.3-29.5 s unfixed, inside run-to-run spread.  So the
cheap form (re-fit from scratch each chunk) is what ships; widening gView
incrementally from only the new chunk would save ~40 ms and cost 15 lines.

renderEventTable() is deliberately NOT in the loop: it rebuilds all ~10,371
rows, and eight passes would cost more than the redraw buys.
"""
from __future__ import annotations

import os
import re

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
VIEWER_HTML = os.path.join(ROOT, 'viewer', 'viewer.html')


def _viewer_src():
    return open(VIEWER_HTML, encoding='utf-8').read()


def _strip_comments(src):
    """Drop // line comments so the prose ABOUT gView is not mistaken for code
    that touches it.  Same crude rule as test_viewer_boot_race.py."""
    return '\n'.join(re.sub(r'//.*$', '', ln) for ln in src.split('\n'))


def _js_func(src, name):
    """Body of `async function name(...)` / `function name(...)`, brace-matched."""
    m = re.search(r'(?:async\s+)?function\s+' + re.escape(name) + r'\s*\([^)]*\)\s*\{', src)
    assert m, 'viewer.html no longer defines %s()' % name
    i, depth = m.end(), 1
    while i < len(src) and depth:
        if src[i] == '{':
            depth += 1
        elif src[i] == '}':
            depth -= 1
        i += 1
    return src[m.end():i - 1]


def _chunk_loop_body(src=None):
    """The body of loadOverview's `for (let i = 0; ...)` chunk loop."""
    body = _js_func(_strip_comments(src or _viewer_src()), 'loadOverview')
    m = re.search(r'for\s*\(\s*let\s+i\s*=\s*0\s*;[^)]*\)\s*\{', body)
    assert m, 'loadOverview no longer has its chunk loop'
    i, depth = m.end(), 1
    while i < len(body) and depth:
        if body[i] == '{':
            depth += 1
        elif body[i] == '}':
            depth -= 1
        i += 1
    return body[m.end():i - 1]


# ─── the premise: why a per-chunk draw() needs gView seeded ──────────────────

def test_draw_still_bails_when_the_view_is_unset():
    """The whole reason the seed is required.  If this early return ever goes
    away the fix is merely harmless rather than load-bearing, and whoever
    removes it should be told by a failing test, not discover it in the field."""
    body = _js_func(_strip_comments(_viewer_src()), 'draw')
    assert re.search(r'if\s*\(\s*gTraces\.length\s*===\s*0\s*\|\|\s*!\s*gView\s*\)\s*return', body), (
        "draw() no longer early-returns on !gView — re-read "
        "test_viewer_overview_progressive.py before deleting the seed")


def test_clear_all_still_nulls_the_view():
    """The other half of the premise: Clear All is how a tech reaches the bug
    a second time, after the first load has already run fit()."""
    body = _js_func(_strip_comments(_viewer_src()), 'clearAll')
    assert re.search(r'gView\s*=\s*null', body), 'clearAll() no longer nulls gView'


# ─── structure: the seed and the chips are INSIDE the loop, before draw() ────

def test_the_chunk_loop_seeds_the_view_before_it_draws():
    loop = _chunk_loop_body()
    seed = re.search(r'gView\s*=\s*b\b|gView\s*=\s*dataBounds\(\)', loop)
    assert seed, (
        'loadOverview\'s chunk loop never fills gView — every per-chunk draw() '
        'is a no-op and the canvas stays blank for the whole load')
    draw = re.search(r'\bdraw\s*\(\s*\)', loop)
    assert draw, 'loadOverview\'s chunk loop no longer draws'
    assert seed.start() < draw.start(), (
        'gView is seeded AFTER draw() inside the chunk loop — the first chunk '
        'still paints nothing')


def test_the_seed_is_gated_on_autofit():
    """A tech who has zoomed owns the viewport; chunk 5 must not yank it back."""
    loop = _chunk_loop_body()
    assert re.search(r'if\s*\(\s*gAutoFit\s*\)\s*\{[^}]*dataBounds\(\)', loop), (
        're-fitting per chunk is not gated on gAutoFit — a load would stomp a '
        'manual zoom every 144 fibers')


def test_the_chunk_loop_renders_chips():
    assert re.search(r'\brenderChips\s*\(\s*\)', _chunk_loop_body()), (
        'the chip strip still reads "no traces loaded" for the whole load')


def test_the_event_table_stays_out_of_the_chunk_loop():
    """Deliberate omission, not an oversight — see the module docstring."""
    assert not re.search(r'\brenderEventTable\s*\(\s*\)', _chunk_loop_body()), (
        'renderEventTable() moved into the chunk loop — it rebuilds ~10k rows '
        'and eight passes cost more than the progressive redraw buys')


# ─── behaviour: a mirror of the three functions, driven from source ──────────

class _Viewer:
    """The parts of viewer.html this invariant lives in: gView, draw()'s early
    return, and loadOverview's chunk loop.  `seeds` mirrors whether the loop
    fills gView, which is read off the real source by the caller."""

    CHUNK = 144

    def __init__(self, seeds, autofit=True):
        self.traces = []            # gTraces
        self.view = None            # gView — null on a cold page / after clear
        self.autofit = autofit      # gAutoFit
        self.seeds = seeds
        self.painted = []           # traces on screen at each chunk boundary

    def data_bounds(self):
        return {'n': len(self.traces)} if self.traces else None

    def draw(self):
        if not self.traces or self.view is None:      # draw()'s early return
            self.painted.append(0)
            return
        self.painted.append(len(self.traces))

    def load_overview(self, total):
        for i in range(0, total, self.CHUNK):
            self.traces.extend(range(i, min(i + self.CHUNK, total)))
            if self.seeds and self.autofit:
                b = self.data_bounds()
                if b:
                    self.view = b
            self.draw()
        if self.autofit:                               # addFibers()' fit()
            self.view = self.data_bounds()
        self.draw()
        return self.painted


def _loop_seeds_view():
    """Read the behaviour out of viewer.html rather than hard-coding it, so the
    mirror cannot drift away from what ships."""
    return bool(re.search(r'gView\s*=\s*b\b|gView\s*=\s*dataBounds\(\)',
                          _chunk_loop_body()))


def test_the_canvas_is_not_blank_while_1152_fibers_load():
    """THE INVARIANT.  Every chunk boundary paints something."""
    painted = _Viewer(seeds=_loop_seeds_view()).load_overview(1152)
    assert len(painted) == 9, 'expected 8 chunk draws + the final fit draw'
    assert all(p > 0 for p in painted[:-1]), (
        'blank canvas at chunk boundaries %r — the tech sees nothing for the '
        'whole load' % [i for i, p in enumerate(painted[:-1]) if p == 0])
    assert painted[0] == 144, 'the first chunk must paint its 144 traces'
    assert painted[-2] == 1152


def test_the_mirror_reproduces_the_blank_canvas_without_the_seed():
    """Teeth check.  With the seed removed the mirror must go blank for all
    eight chunks — the behaviour measured in the browser — otherwise the test
    above is passing for the wrong reason."""
    painted = _Viewer(seeds=False).load_overview(1152)
    assert painted[:-1] == [0] * 8, (
        'the unseeded mirror no longer reproduces the blank canvas (%r) — it '
        'has stopped modelling draw()\'s early return' % painted)
    assert painted[-1] == 1152, 'the post-loop fit() must still paint'


def test_a_zoomed_tech_keeps_their_viewport():
    """gAutoFit off: the loop must not re-fit, and must not paint over a view
    the tech chose.  (Nothing is on screen in the mirror because it starts
    cold; what matters is that the loop leaves `view` alone.)"""
    v = _Viewer(seeds=_loop_seeds_view(), autofit=False)
    v.view = {'n': 'the tech\'s own zoom'}
    v.load_overview(1152)
    assert v.view == {'n': 'the tech\'s own zoom'}, (
        'the chunk loop overwrote a manual zoom')

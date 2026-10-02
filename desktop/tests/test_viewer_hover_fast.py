"""A mouse move repaints only the overlay (Viewer 432 load audit, 2026-10-01).

With a whole 432-fiber cable on screen (864 traces) draw() takes 35-50 ms
and every mouse move paid it, so hovering lagged.  draw() now keeps the
chart it painted, before the overlay (keepBase); a mouse move puts that copy
back and draws only the overlay -- box-zoom rectangle, crosshair, hover box
(drawHover).  A pan still draws the chart, and a copy that no longer matches
what is on screen (baseSig: size, traces, view, pick, markers, theme) falls
back to draw().

The page runs in JavaScriptCore where present.
"""
from __future__ import annotations

import json
import os
import re
import subprocess

import pytest

from conftest import VIEWER_DIR

SRC = (VIEWER_DIR / 'viewer.html').read_text(encoding='utf-8')
JSC = ('/System/Library/Frameworks/JavaScriptCore.framework/'
       'Versions/Current/Helpers/jsc')
needs_jsc = pytest.mark.skipif(not os.path.exists(JSC),
                               reason='JavaScriptCore shell (macOS) not present')


def _js_func(name):
    m = re.search(r'(?:async\s+)?function\s+' + re.escape(name) + r'\s*\([^)]*\)\s*\{', SRC)
    assert m, 'viewer.html no longer defines %s()' % name
    i, depth = m.end(), 1
    while i < len(SRC) and depth:
        depth += {'{': 1, '}': -1}.get(SRC[i], 0)
        i += 1
    return SRC[m.start():i]


_STUBS = r"""
var log = [];
function ctx2d(name) {
  return {
    save: function () {}, restore: function () {}, setTransform: function () {},
    clearRect: function () {}, fillRect: function () {}, strokeRect: function () {},
    setLineDash: function () {},
    drawImage: function (img) { log.push(name + ':copy:' + img.name); },
  };
}
var canvas = { name: 'screen', width: 1600, height: 600 };
var ctx = ctx2d('screen');
var theme = null;
var document = {
  documentElement: { getAttribute: function () { return theme; } },
  createElement: function () {
    var c = { name: 'base', width: 0, height: 0 };
    var cx = ctx2d('base');
    c.getContext = function () { return cx; };
    return c;
  },
};
var gBase = null, gBaseImg = null, gExportDpr = 0;
var gTraces = [{ key: 'a-1', visible: true }, { key: 'b-1', visible: true }];
var gView = { x0: 0, x1: 55, y0: -30, y1: 15 };
var gPickKey = null, gMarkers = { a: null, b: null };
var gMouse = { px: 10, py: 10 }, gDragging = null, gHoverKey = null;
function plotRect() { return { x: 0, y: 0, w: 100, h: 100 }; }
function hoverTrace() { return null; }
function drawCrosshair() { log.push('crosshair'); }
function drawHoverBox() { log.push('box'); }
function drawLabelTip() { log.push('tip'); }
// draw() as the page has it, cut to what this test watches: the chart is
// painted, kept, then the overlay goes on top
function draw() { log.push('chart'); gBase = null; keepBase(); drawOverlay(plotRect()); }
function step(name, fn) { log = []; fn(); out[name] = log; }
var out = {};
"""

_CASES = r"""
step('first_draw', function () { draw(); });
step('move', function () { gMouse = { px: 20, py: 20 }; drawHover(); });
step('move_again', function () { gMouse = { px: 30, py: 20 }; drawHover(); });
step('box_drag', function () { gDragging = { kind: 'box', startPx: 5, curPx: 40 }; drawHover(); });
gDragging = null;
step('zoomed', function () { gView = { x0: 40, x1: 46, y0: -30, y1: 15 }; drawHover(); });
step('after_zoom', function () { drawHover(); });
step('picked', function () { gPickKey = 'a-1'; drawHover(); });
step('hidden', function () { gTraces[1].visible = false; drawHover(); });
step('marker', function () { gMarkers.a = 43.9; drawHover(); });
step('dark', function () { theme = 'dark'; drawHover(); });
step('resized', function () { canvas.width = 1200; drawHover(); });
step('new_traces', function () { gTraces = gTraces.slice(); drawHover(); });
step('steady', function () { drawHover(); });
step('export', function () { gExportDpr = 1.5; gBase = null; draw(); });
out.export_kept = gBase !== null;
gExportDpr = 0;
step('after_export', function () { drawHover(); });
print('OUT ' + JSON.stringify(out));
"""


@pytest.fixture(scope='module')
def res(tmp_path_factory):
    funcs = '\n'.join(_js_func(n) for n in ('drawOverlay', 'baseSig', 'keepBase', 'drawHover'))
    path = tmp_path_factory.mktemp('hover_fast') / 'hf.js'
    path.write_text(_STUBS + funcs + '\n' + _CASES, encoding='utf-8')
    r = subprocess.run([JSC, str(path)], capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    assert r.returncode == 0 and 'OUT ' in out, out[-2000:]
    return json.loads(out.split('OUT ', 1)[1].strip())


CHART = ['chart', 'base:copy:screen', 'crosshair', 'tip']
OVERLAY = ['screen:copy:base', 'crosshair', 'tip']


@needs_jsc
def test_a_mouse_move_puts_the_kept_chart_back_and_draws_the_overlay(res):
    assert res['first_draw'] == CHART
    assert res['move'] == OVERLAY
    assert res['move_again'] == OVERLAY
    assert res['box_drag'] == OVERLAY        # the zoom box is overlay too
    assert res['steady'] == OVERLAY


@needs_jsc
def test_a_chart_that_changed_under_the_copy_is_drawn(res):
    for k in ('zoomed', 'picked', 'hidden', 'marker', 'dark', 'resized', 'new_traces'):
        assert res[k] == CHART, k
    assert res['after_zoom'] == OVERLAY


@needs_jsc
def test_a_report_snapshot_is_never_kept_as_the_screen(res):
    assert res['export'] == ['chart', 'crosshair', 'tip']
    assert res['export_kept'] is False
    assert res['after_export'][0] == 'chart'


def test_mouse_moves_use_the_overlay_and_a_pan_draws_the_chart():
    move = SRC.split("canvas.addEventListener('mousemove'", 1)[1].split("window.addEventListener('mouseup'", 1)[0]
    assert "if (gDragging && gDragging.kind === 'pan') draw();\n  else drawHover();" in move
    # dragging a marker moves the chart's markers: still a full draw
    assert "gMarkers[gDragMarker] = pxToX(ev.offsetX, r);\n    draw();" in move
    draw = _js_func('draw')
    assert draw.index('gBase = null;') < draw.index('drawGrid(r);')
    assert draw.index('drawMarkers(r);') < draw.index('keepBase();') < draw.index('drawOverlay(r);')
    # declared up top: draw() runs at boot (resizeCanvas) before the chart code
    assert SRC.index('let gBase = null;') < SRC.index('function resizeCanvas()')

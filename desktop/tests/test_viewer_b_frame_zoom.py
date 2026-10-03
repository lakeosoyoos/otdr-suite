"""Viewer: a zoom lands on the event when B is drawn in a frame of its own.

Found 2026-10-01 on a tie panel (1.0383 km reel from A, 1.0000 km from B,
62 m between the panels).  The chart is A's frame whenever an A trace is
loaded, B mirrored onto it; with no A trace B is drawn as it was shot.  Two
zooms took a km from one frame and used it in the other:

  * the one-direction OTDR Suite table's km is in that direction's own frame.
    With A loaded but hidden, B is still mirrored, and the "0.000 km" column
    (B's own panel, B-raw 1.000 km) zoomed to 1.000 km while that connector
    is drawn at 1.1006 km.  The drawer's tags for the table sat at the same
    wrong place.  The table now turns its km into chart positions through
    the trace.
  * a Splice Report link carries A-frame km (app.py's `_vkm`).  When the
    fibre's A file is missing only B loads, drawn as shot, and the link zoomed
    to the A-frame km on B's axis: on a 50 km span with 1 km reels a splice
    at report km 10 sits at B-raw 41 km, and the view opened at 11 km.
    linkDispKm now converts a link's km for what is drawn (a Unidirectional
    report link is in its own direction's frame).

Also here: a B-folder file set to A->B with Direction, loaded alone, read its
distances from the A folder's reel (38 m out on that panel); its zero is now
the B folder's reel, as #471 did for the other way round.

Checked by running the real functions in JavaScriptCore when the Mac's jsc
is there.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from conftest import VIEWER_DIR

SRC = (VIEWER_DIR / "viewer.html").read_text(encoding="utf-8")
JSC = Path("/System/Library/Frameworks/JavaScriptCore.framework/Versions/"
           "Current/Helpers/jsc")


def _fn(name):
    """The whole function, one-liners included (brace matching)."""
    m = re.search(r"^(?:async )?function %s\([^)]*\)\s*\{" % name, SRC, re.M)
    assert m, "viewer.html no longer defines %s" % name
    i, depth = m.end(), 1
    while depth:
        depth += {"{": 1, "}": -1}.get(SRC[i], 0)
        i += 1
    return SRC[m.start():i] + "\n"


def _const(name):
    m = re.search(r"^const " + name + r" = [^\n]*;", SRC, re.M)
    assert m, name
    return m.group(0)


# ─── the source ──────────────────────────────────────────────────────────

def test_report_link_goes_through_link_disp_km():
    # ±2.5 km, except FEC shots' ±0.3 km on a 5 km facility-entrance shot
    assert ("zoomToKm(linkDispKm(km, t.src, t.dir || 'both', fiber), gFecMode ? 0.3 : 2.5)"
            in _fn("applyTarget"))


def test_one_direction_table_km_reach_the_chart_through_the_trace():
    fn = _fn("paintSuiteBidiGrid")
    assert "const toView = (km, t) => (oneDir && (t || leadT)) ? dispKm(t || leadT, km) : km;" in fn
    assert "viewKm: toView((Number(c.km) || 0) + launchA)," in fn
    assert "toView(x.km + launchA, oneDir && legOf(have[fi]))" in fn
    # oneDir is known before the columns are laid out
    assert fn.index("const oneDir =") < fn.index("viewKm: toView(")


# ─── the real functions, in JavaScriptCore ───────────────────────────────

needs_jsc = pytest.mark.skipif(not JSC.exists(), reason="no JavaScriptCore shell here")


def _run(tmp_path, body):
    prelude = r"""
var gTraces = [], gSpanDecl = {a: null, b: null}, gInfo = null, gLaunchA = 0;
var gStacked = true, gHaveA = false, gMirrorDelta = 0, gFecMode = false;
function T(dir, src, fiber, ev, far) {
  return {key: src + '-' + fiber, dir: dir, src: src, fiber: fiber, visible: true,
          data: {events: ev.map(function (k) { return {dist_km: k, is_end: false}; }),
                 dist_km: [0, ev[ev.length - 1]], far_conn_km: far}};
}
function load(info, traces) {
  gInfo = info; gLaunchA = Number(info.launch_a_km) || 0;
  gTraces = traces; gHaveA = traces.some(function (t) { return t.dir === 'a'; });
}
function r4(v) { return Math.round(v * 1e4) / 1e4; }
var out = {};
"""
    code = "\n".join([prelude, _const("SPAN_SNAP_KM"), "function unpairedB() { return new Set(); }  // every B paired: a frame test, not a pairing one"] + [
        _fn(n) for n in ("eofKm", "isFlipped", "dispKm", "reelOriginKm", "declaredEdgeKm",
                         "declaredOriginKm", "mirrorOriginKm", "linkDispKm", "axisZeroKm")
    ] + [body, "print(JSON.stringify(out));"])
    p = tmp_path / "frame.js"
    p.write_text(code, encoding="utf-8")
    res = subprocess.run([str(JSC), str(p)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr + res.stdout
    return json.loads(res.stdout.strip().splitlines()[-1])


PANEL = r"""
// the tie panel: A's reel 1.0383 km, B's 1.0000 km, 62 m between panels
var INFO = {launch_a_km: 1.0383, launch_b_km: 1.0001};
var A103 = function () { return T('a', 'a', 103, [0, 1.0383, 1.1001, 2.1017], 1.1001); };
var B103 = function () { return T('b', 'b', 103, [0, 1.0001, 1.0617, 2.1133], 1.0623); };
"""


@needs_jsc
def test_splice_report_link_with_only_b_loaded(tmp_path):
    out = _run(tmp_path, PANEL + r"""
// the B-end panel is at A-raw 1.1001 km in the report's frame
load(INFO, [B103()]);
out.panel_b_alone = r4(linkDispKm(1.1001, 'sr', 'both', '103'));
out.panel_no_src = r4(linkDispKm(1.1001, undefined, 'both', '103'));
// 50 km span, 1 km reels at both ends: report km 10 = A-raw 11.0 = B-raw 41
load({launch_a_km: 1.0, launch_b_km: 1.0}, [T('b', 'b', 7, [0, 1.0, 41.0, 51.0, 52.0], 51.0)]);
out.long_b_alone = r4(linkDispKm(11.0, 'sr', 'both', '7'));
// with A loaded the chart IS A's frame: the km is used as it comes
load(INFO, [A103(), B103()]);
out.with_a = r4(linkDispKm(1.1001, 'sr', 'both', '103'));
""")
    assert abs(out["panel_b_alone"] - 1.0001) < 0.001     # was 1.1001: 100 m out
    assert out["panel_no_src"] == out["panel_b_alone"]
    assert out["long_b_alone"] == 41.0                    # was 11.0: 30 km out
    assert out["with_a"] == 1.1001


@needs_jsc
def test_unidirectional_link_is_in_its_own_direction(tmp_path):
    out = _run(tmp_path, PANEL + r"""
// B's own panel, B-raw 1.0001 km
load(INFO, [B103()]);
out.b_alone = r4(linkDispKm(1.0001, 'uni', 'b', '103'));       // drawn as shot
load(INFO, [A103(), B103()]);
var b = gTraces[1];
out.b_mirrored = r4(linkDispKm(1.0001, 'uni', 'b', '103'));
out.b_drawn_at = r4(dispKm(b, 1.0001));
out.a_link = r4(linkDispKm(1.1001, 'uni', 'a', '103'));
""")
    assert out["b_alone"] == 1.0001
    assert out["b_mirrored"] == out["b_drawn_at"]          # lands on the drawn connector
    assert abs(out["b_drawn_at"] - 1.1006) < 0.001
    assert out["a_link"] == 1.1001


@needs_jsc
def test_b_folder_file_set_to_a_reads_from_its_own_reel(tmp_path):
    out = _run(tmp_path, PANEL + r"""
load(INFO, [T('a', 'b', 103, [0, 1.0001, 1.0617, 2.1133], 1.0623)]);
out.b_file_as_a = r4(axisZeroKm());
load({launch_a_km: 1.0383, launch_b_km: null}, [T('a', 'b', 103, [0, 1.0617], 1.0617)]);
out.b_file_as_a_no_b_reel = r4(axisZeroKm());
load(INFO, [A103()]);
out.a_alone = r4(axisZeroKm());
load(INFO, [A103(), B103()]);
out.a_and_b = r4(axisZeroKm());
load(INFO, []);
out.empty = r4(axisZeroKm());
""")
    assert out["b_file_as_a"] == 1.0001          # was 1.0383: 38 m out
    assert out["b_file_as_a_no_b_reel"] == 0
    assert out["a_alone"] == 1.0383
    assert out["a_and_b"] == 1.0383
    assert out["empty"] == 1.0383

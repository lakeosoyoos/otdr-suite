"""Viewer: the A/B marker loss reads positive for B, and colours follow the
direction a trace is drawn as.

Found 2026-10-01 in an audit of where the Viewer treats A and B differently.

Marker loss.  The A/B marker readout took Delta Loss as the signal at the
LEFT marker minus the signal at the right one: right for A, which runs left
to right.  A B trace is mirrored onto the chart whenever an A trace is
loaded, so it runs right to left, and every B row printed its loss and dB/km
negative (0.2 dB/km fibre, markers 10 km apart: A 2.000, B -2.000).  The
same B on its own, drawn as shot, printed +2.000.  The Summary Report copies
this table.  A mirrored B now reads from its upstream end, the right marker.

Colours.  With "fiber colors" off, traces take FastReporter's scheme: A blue,
B gray.  A trace was coloured by its FOLDER when it loaded, by its direction
only after the checkbox was toggled, and not at all when the Direction menu
changed it, so an A-folder file set to B->A stayed blue (or a file whose own
stamp says A was drawn as A in B's gray) until the next toggle.  A trace is
now coloured by the direction it is drawn as, at load and on every change.

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
from conftest import with_copy_helpers  # noqa: E402

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


def _line(prefix):
    m = re.search(r"^" + re.escape(prefix) + r"[^\n]*", SRC, re.M)
    assert m, prefix
    return m.group(0)


# ─── the source ──────────────────────────────────────────────────────────

def test_mirrored_b_reads_from_its_upstream_marker():
    fn = _fn("updateMarkerReadout")
    assert "(isFlipped(t) ? vhi - vlo : vlo - vhi)" in fn


def test_every_colour_goes_by_drawn_direction():
    assert "function traceColor(t) { return nextColor(`${t.dir}-${t.fiber}`); }" in SRC
    assert "color: nextColor(key)" not in SRC               # never by folder again
    assert SRC.count("color = traceColor(") >= 4            # 2 loads, toggle, Direction


# ─── the real functions, in JavaScriptCore ───────────────────────────────

needs_jsc = pytest.mark.skipif(not JSC.exists(), reason="no JavaScriptCore shell here")


def _run(tmp_path, body):
    prelude = r"""
var gStacked = true, gHaveA = false, gMarkerMode = false, gDistUnit = 'km', gPickKey = null,
    gFecMode = false;
var gMarkers = {a: null, b: null}, gTraces = [], gFiberColors = false, gAutoFit = false;
var gDirOverride = {}, gStoredDir = {};
var PALETTE = ['#0072ce'], COLORS_USED = new Map();
var MARKER_MAX_ROWS = 8;
var box = {style: {}, innerHTML: ''};
var document = {getElementById: function () { return box; }};
function axisZeroKm() { return 0; }
function mirrorOriginKm(t) { return 20; }       // a 20 km fibre, B mirrored onto A
function fmtDist(v) { return v.toFixed(4); }
function renderChips() {} function fit() {} function draw() {}
function renderEventTable() {} function setReadout() {}
// a 0.2 dB/km fibre, 20 km, sampled every 10 m, seen from its own port
function shot(dir, src) {
  var xs = [], ys = [];
  for (var i = 0; i <= 2000; i++) { xs.push(i / 100); ys.push(-0.2 * i / 100); }
  return {key: (src || dir) + '-1', fiber: 1, dir: dir, src: src || dir, visible: true,
          color: '#000', data: {dist_km: xs, trace_db: ys}};
}
// Delta Loss and dB/km of each readout row, by trace key order
function rows() {
  var out = [], re = /<tr><td>.*?F1 (A→B|B→A)<\/td><td>[^<]*<\/td><td>[^<]*<\/td><td>([^<]*)<\/td><td>([^<]*)<\/td><\/tr>/g, m;
  while ((m = re.exec(box.innerHTML))) out.push([m[1], m[2], m[3]]);
  return out;
}
var out = {};
"""
    code = "\n".join([prelude, _line("const FR_COLORS = ")] + [
        _fn(n) for n in ("isFlipped", "dataKmFromDisp", "lowerBound", "traceDbAtDispKm",
                         "capReadoutRows", "updateMarkerReadout", "nextColor", "traceColor",
                         "effDir", "splitFileKey", "setFilesDirection")
    ] + [body, "print(JSON.stringify(out));"])
    p = tmp_path / "marker.js"
    p.write_text(with_copy_helpers(code), encoding="utf-8")
    res = subprocess.run([str(JSC), str(p)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr + res.stdout
    return json.loads(res.stdout.strip().splitlines()[-1])


@needs_jsc
def test_marker_loss_is_positive_for_b_mirrored_or_not(tmp_path):
    out = _run(tmp_path, r"""
gMarkers = {a: 5, b: 15};
gTraces = [shot('a'), shot('b')]; gHaveA = true;          // A+B: B mirrored
updateMarkerReadout(); out.a_and_b = rows();
gTraces = [shot('b')]; gHaveA = false;                     // B alone: drawn as shot
updateMarkerReadout(); out.b_alone = rows();
gMarkers = {a: 15, b: 5};                                  // markers the other way round
gTraces = [shot('a'), shot('b')]; gHaveA = true;
updateMarkerReadout(); out.swapped = rows();
""")
    assert out["a_and_b"] == [["A→B", "2.000", "0.200"], ["B→A", "2.000", "0.200"]]   # B was -2.000
    assert out["b_alone"] == [["B→A", "2.000", "0.200"]]
    assert out["swapped"] == out["a_and_b"]


@needs_jsc
def test_fr_colours_follow_the_drawn_direction(tmp_path):
    out = _run(tmp_path, r"""
var a = shot('a'), b = shot('b', 'b');
a.color = traceColor(a); b.color = traceColor(b);
out.loaded = [a.color, b.color];
gTraces = [a];
setFilesDirection(['a-1'], 'b');                           // A-folder file set to B->A
out.set_to_b = [a.dir, a.color];
setFilesDirection(['a-1'], 'a');                           // and back
out.back = [a.dir, a.color];
var stamped = shot('a', 'b');                              // B-folder file stamped A
stamped.color = traceColor(stamped);
out.stamped = stamped.color;
gFiberColors = true; COLORS_USED.clear();                  // the 12-colour code: by fibre only
out.fibre_code = [traceColor(shot('a')), traceColor(shot('b'))];
""")
    blue, gray = "#7b96d8", "#8a8a8a"
    assert out["loaded"] == [blue, gray]
    assert out["set_to_b"] == ["b", gray]                  # stayed blue before
    assert out["back"] == ["a", blue]
    assert out["stamped"] == blue                          # drawn as A, coloured as A
    assert out["fibre_code"] == ["#0072ce", "#0072ce"]

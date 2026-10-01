"""Viewer: B loaded on its own reads its distances from B's first event.

Found 2026-10-01 on a tie panel shot through a 1.0383 km reel from the A end
and a 1.0000 km reel from the B end.  B loaded alone is drawn the way it was
shot (isFlipped needs an A trace loaded), but axisZeroKm() still returned the
A folder's launch reel.  Every number on the chart came out 38 m short: B's
own launch connector sat at -0.038 km on the axis while the event table under
it said 0.0000 km, and the A/B marker readout and the Summary Report caption
were off by the same 38 m.  A B reel with no A reel would put B a whole reel
out.

axisZeroKm() now uses B's declared start, or else the B folder's launch reel,
whenever B is loaded and A is not.  A alone, A+B (stacked or not) and an
empty chart keep A's zero.

Checked by running the real axisZeroKm / declaredEdgeKm in JavaScriptCore
when the Mac's jsc is there.
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
    i = SRC.index("function " + name + "(")
    return SRC[i:SRC.index("\n}\n", i) + 3]


def _const(name):
    m = re.search(r"^const " + name + r" = [^\n]*;", SRC, re.M)
    assert m, name
    return m.group(0)


# ─── the source ──────────────────────────────────────────────────────────

def test_b_alone_zero_comes_from_b():
    fn = _fn("axisZeroKm")
    assert "gInfo.launch_b_km" in fn
    assert "gSpanDecl.b" in fn
    # the B branch runs before A's declared start or A's reel is looked at
    assert fn.index("gInfo.launch_b_km") < fn.index("return gLaunchA")


def test_every_printed_distance_reads_the_one_zero():
    # axis ticks, the A/B marker readout and the report caption
    assert SRC.count("const z = axisZeroKm();") >= 3


# ─── the real function, in JavaScriptCore ────────────────────────────────

needs_jsc = pytest.mark.skipif(not JSC.exists(), reason="no JavaScriptCore shell here")


def _zero(tmp_path, cases):
    prelude = r"""
var gTraces = [], gSpanDecl = {a: null, b: null}, gInfo = null, gLaunchA = 0;
var gStacked = true, gHaveA = false;
function isFlipped(t) { return gStacked && gHaveA && t.dir === 'b'; }
function trace(dir, evKm) {
  return {key: dir + '-1', dir: dir, fiber: 1, visible: true,
          data: {events: evKm.map(function (k) { return {dist_km: k}; })}};
}
var out = {};
function run(name, c) {
  gInfo = {launch_a_km: c.la, launch_b_km: c.lb};
  gLaunchA = Number(c.la) || 0;
  gSpanDecl = c.decl || {a: null, b: null};
  gTraces = (c.dirs || []).map(function (d) {
    return d === 'a' ? trace('a', [0, 1.0383, 1.1001, 2.1017])
                     : trace('b', [0, 1.0001, 1.0615]);
  });
  gHaveA = gTraces.some(function (t) { return t.dir === 'a'; });
  out[name] = Math.round(axisZeroKm() * 1e5) / 1e5;
}
"""
    calls = "\n".join("run(%s, %s);" % (json.dumps(k), json.dumps(v))
                      for k, v in cases.items())
    code = "\n".join([prelude, _const("SPAN_SNAP_KM"), _fn("declaredEdgeKm"),
                      _fn("axisZeroKm"), calls, "print(JSON.stringify(out));"])
    p = tmp_path / "zero.js"
    p.write_text(code, encoding="utf-8")
    res = subprocess.run([str(JSC), str(p)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr + res.stdout
    return json.loads(res.stdout.strip().splitlines()[-1])


@needs_jsc
def test_b_alone_launch_connector_is_zero(tmp_path):
    z = _zero(tmp_path, {
        "b_alone": {"dirs": ["b"], "la": 1.0383, "lb": 1.00005},
        "b_alone_no_b_reel": {"dirs": ["b"], "la": 1.0383, "lb": None},
        "b_alone_declared": {"dirs": ["b"], "la": 1.0383, "lb": 1.00005,
                             "decl": {"a": {"start_km": 1.0383}, "b": {"start_km": 1.0003}}},
    })
    assert z["b_alone"] == 1.00005          # was 1.0383: B's connector at -38 m
    assert z["b_alone_no_b_reel"] == 0      # drawn as shot, port at 0 km
    assert z["b_alone_declared"] == 1.0001  # snapped to this fiber's own event


@needs_jsc
def test_a_loaded_keeps_a_zero(tmp_path):
    z = _zero(tmp_path, {
        "a_alone": {"dirs": ["a"], "la": 1.0383, "lb": 1.00005},
        "a_and_b": {"dirs": ["a", "b"], "la": 1.0383, "lb": 1.00005},
        "empty": {"dirs": [], "la": 1.0383, "lb": 1.00005},
        "a_declared": {"dirs": ["a", "b"], "la": 1.0383, "lb": 1.00005,
                       "decl": {"a": {"start_km": 1.1}, "b": None}},
    })
    assert z["a_alone"] == 1.0383
    assert z["a_and_b"] == 1.0383
    assert z["empty"] == 1.0383
    assert z["a_declared"] == 1.1001

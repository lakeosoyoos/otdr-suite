"""Viewer: a fibre loaded B only reads its table from A's declared start.

Found 2026-10-01 in an audit of where the Viewer treats A and B differently.
When a fibre is loaded one way among paired fibres, its own single-direction
table sits under the A+B table.  Once the tech declares A's span start (the
far connector of A's launch reel), the A+B table and the chart's axis read
their distances from that start.  The single-direction table took its zero
from the first UNFLIPPED trace's own declared start; a B trace is mirrored
onto A's frame while A is loaded, so a B-only fibre had none and read from
A's OTDR port instead.  On a tie panel with A's start declared on its
1.0383 km reel, the A+B table printed a column at 0.0617 km and the B-only
table under it printed the same column 1.0383 km further on.

oneDirTableZeroKm() now gives a mirrored B A's declared start, as the axis
reads it.  With nothing declared the zero stays the port (FR's own table);
an A-only fibre keeps its own declared start; B loaded alone, drawn as shot,
keeps B's own.

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


def test_the_single_direction_table_takes_its_zero_from_the_helper():
    assert "const zeroKm = oneDirTableZeroKm(traces);" in _fn("renderFastReporterGrid")


needs_jsc = pytest.mark.skipif(not JSC.exists(), reason="no JavaScriptCore shell here")


def _zeros(tmp_path, cases):
    prelude = r"""
var gTraces = [], gSpanDecl = {a: null, b: null}, gInfo = {launch_a_km: 1.0383, launch_b_km: 1.0001};
var gLaunchA = 1.0383, gStacked = true, gHaveA = false, gMirrorDelta = 0;
function T(dir, fiber, ev, far) {
  return {key: dir + '-' + fiber, dir: dir, src: dir, fiber: fiber, visible: true,
          data: {events: ev.map(function (k) { return {dist_km: k}; }),
                 dist_km: [0, ev[ev.length - 1]], far_conn_km: far}};
}
// a tie panel: A's reel 1.0383 km, B's 1.0000 km, 62 m between the panels
function A(f) { return T('a', f, [0, 1.0383, 1.1001, 2.1017], 1.1001); }
function B(f) { return T('b', f, [0, 1.0001, 1.0617, 2.1133], 1.0623); }
var out = {};
function run(name, c) {
  gSpanDecl = c.decl || {a: null, b: null};
  gTraces = c.loaded.map(function (k) { return k[0] === 'a' ? A(k[1]) : B(k[1]); });
  gHaveA = gTraces.some(function (t) { return t.dir === 'a'; });
  var table = gTraces.filter(function (t) { return c.table.indexOf(t.key) >= 0; });
  out[name] = Math.round(oneDirTableZeroKm(table) * 1e4) / 1e4;
}
"""
    calls = "\n".join("run(%s, %s);" % (json.dumps(k), json.dumps(v)) for k, v in cases.items())
    code = "\n".join([prelude, _const("SPAN_SNAP_KM")] + [
        _fn(n) for n in ("eofKm", "isFlipped", "dispKm", "reelOriginKm", "declaredEdgeKm",
                         "declaredOriginKm", "mirrorOriginKm", "ownSpanWindow", "axisZeroKm",
                         "oneDirTableZeroKm")
    ] + [calls, "print(JSON.stringify(out));"])
    p = tmp_path / "zero.js"
    p.write_text(code, encoding="utf-8")
    res = subprocess.run([str(JSC), str(p)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr + res.stdout
    return json.loads(res.stdout.strip().splitlines()[-1])


A_START = {"a": {"start_km": 1.0383, "end_km": None}, "b": None}


@needs_jsc
def test_b_only_fibre_under_the_a_b_table_reads_from_a_s_declared_start(tmp_path):
    z = _zeros(tmp_path, {
        # F102 loaded both ways, F103 B only: F103's table holds its mirrored B
        "b_only_declared": {"loaded": [["a", 102], ["b", 102], ["b", 103]],
                            "table": ["b-103"], "decl": A_START},
        "b_only_nothing_declared": {"loaded": [["a", 102], ["b", 102], ["b", 103]],
                                    "table": ["b-103"]},
    })
    assert z["b_only_declared"] == 1.0383        # was 0: A's OTDR port
    assert z["b_only_nothing_declared"] == 0     # FR's own table: the port


@needs_jsc
def test_other_single_direction_tables_keep_their_zero(tmp_path):
    z = _zeros(tmp_path, {
        # F103 loaded A only among paired fibres: its own declared start
        "a_only_declared": {"loaded": [["a", 102], ["b", 102], ["a", 103]],
                            "table": ["a-103"], "decl": A_START},
        # B loaded on its own, drawn as shot: A's declaration is not its frame
        "b_alone_a_declared": {"loaded": [["b", 103]], "table": ["b-103"], "decl": A_START},
        "b_alone_b_declared": {"loaded": [["b", 103]], "table": ["b-103"],
                               "decl": {"a": None, "b": {"start_km": 1.0003, "end_km": None}}},
    })
    assert z["a_only_declared"] == 1.0383
    assert z["b_alone_a_declared"] == 0
    assert z["b_alone_b_declared"] == 1.0001     # snapped to B's own event

"""Viewer: the "B never reaches the end" warning shows only while a short B
is drawn lined up against A.

Found 2026-10-01 in an audit of where the Viewer treats A and B differently.
When the B folder is a short shot (no end-of-fibre event, so the cable's
length is unknown), A and B cannot be put on one frame.  The warning exists
for the accident worth catching -- the short-shot folder loaded as B beside
a full-length A (test_viewer_ab_frame.py) -- but it followed the folder
alone, so it sat on every readout and on the Summary Report of an A-only
load and of a short shot loaded by itself and drawn as shot, where nothing
is aligned.

Robert 2026-10-01 picked "only fix when the warning shows": it now shows
while a visible trace from the B folder is drawn lined up against A
(stacked, an A trace loaded), and the short B is still drawn as before.

Checked by running the real frameWarn and isFlipped in JavaScriptCore when
the Mac's jsc is there.
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
    m = re.search(r"^function %s\([^)]*\)\s*\{" % name, SRC, re.M)
    assert m, "viewer.html no longer defines %s" % name
    i, depth = m.end(), 1
    while depth:
        depth += {"{": 1, "}": -1}.get(SRC[i], 0)
        i += 1
    return SRC[m.start():i] + "\n"


def test_readout_and_report_read_the_warning_through_frame_warn():
    readout = _fn("setReadout")
    assert "frameWarn().trim()" in readout and "gFrameWarn" not in readout
    assert "if (frameWarn()) meta.push(['Note', frameWarn()]);" in SRC
    assert "if (gFrameWarn) meta.push" not in SRC


needs_jsc = pytest.mark.skipif(not JSC.exists(), reason="no JavaScriptCore shell here")


@needs_jsc
def test_the_warning_shows_only_while_a_short_b_is_lined_up_against_a(tmp_path):
    code = "\n".join([r"""
var WARN = '   ⚠ B never reaches the end of the fiber';
var gFrameWarn = '', gTraces = [], gStacked = true, gHaveA = false, gFecMode = false;
function T(dir, src, visible) { return {key: src + '-1', dir: dir, src: src, fiber: 1, visible: visible !== false}; }
var out = {};
function run(name, warn, stacked, traces) {
  gFrameWarn = warn ? WARN : ''; gStacked = stacked; gTraces = traces;
  gHaveA = traces.some(function (t) { return t.dir === 'a'; });
  out[name] = frameWarn() ? 'warn' : '';
}
""", _fn("isFlipped"), _fn("frameWarn"), r"""
run('a_and_short_b', true, true, [T('a', 'a'), T('b', 'b')]);
run('a_only', true, true, [T('a', 'a')]);
run('short_b_alone', true, true, [T('b', 'b')]);
run('b_hidden', true, true, [T('a', 'a'), T('b', 'b', false)]);
run('unstacked', true, false, [T('a', 'a'), T('b', 'b')]);
run('full_length_b', false, true, [T('a', 'a'), T('b', 'b')]);
run('a_file_set_to_b', true, true, [T('a', 'a'), T('b', 'a')]);
print(JSON.stringify(out));
"""])
    p = tmp_path / "warn.js"
    p.write_text(code, encoding="utf-8")
    res = subprocess.run([str(JSC), str(p)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr + res.stdout
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert out == {
        'a_and_short_b': 'warn',          # the accident it exists for
        'a_only': '',                     # was 'warn'
        'short_b_alone': '',              # was 'warn': drawn as shot, nothing aligned
        'b_hidden': '',
        'unstacked': '',
        'full_length_b': '',
        'a_file_set_to_b': '',            # not the B folder's shot
    }

"""Viewer stacked overlay: one common dB scale, like FastReporter's default.

Field report (Robert's boss, looking at a bidirectional overlay):

    "they weren't matched up -- they had different floors."

He was right, and it was ours.  Stacked mode used to push every B trace down a
hard-coded 8 dB so the two curves would not overlap:

    const STACK_Y_OFFSET_DB = -8;

Purely cosmetic, drawing-only -- but it moves the whole B curve, so the gap
between an A and a B trace at a given metre stopped being a physical quantity
and the crossing point landed 8 dB away from where the glass puts it.

WHAT FASTREPORTER ACTUALLY DOES.  Measured off the boss's own FR session on
this same span (WSC <-> SUI):

  * F350, window 62.5-66.5 km: blue ~29.5 dB, black ~17 dB.  That 12.5 dB is
    the SPAN LOSS (0.19 dB/km x 65 km ~ 12.4), not an offset -- at that x, A
    has traversed the whole cable and B has traversed almost none.
  * 8-file view: black launches ~30 and falls to ~16 by 65 km; blue sits ~16.5
    at the left and reaches ~30 at the right.  Same 16->30 range, mirrored.
    One Y axis.

Our own files agree the data shares a floor -- F71 launch backscatter reads
A 35.220 dB, B 35.119 dB, 0.1 dB apart.

FR *can* separate traces: its OTDR ribbon carries a "Y Spacing" stepper.  It
is off by default, and the boss's session shows the default.  So: default 0 to
match FR out of the box, and expose the separation as a control the tech turns
on, rather than baking a constant into the drawing code.

RENDERED PROOF (F350, Sacramento <-> Susisun, canvas pixels read back and
converted through pxToY, not source inspection):

    display km      gap @ spacing 0     gap @ spacing 8     delta
       62.0            10.937               2.896           8.041
       63.0            11.309               3.304           8.005
       64.2            12.091               4.119           7.972
       64.5            12.239               4.209           8.030

12.2 dB just inside A's far connector (65.09 km) -- the span loss, matching
FR's 12.5 -- and the control moves it by exactly 8, nothing more.  Over the
same change the FR event table, the A/B marker readout and the hover readout
came back byte-identical.
"""
from __future__ import annotations

import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _viewer_src() -> str:
    return open(os.path.join(ROOT, 'viewer', 'viewer.html'), encoding='utf-8').read()


# ─── the default ───────────────────────────────────────────────────────

def test_the_stacked_default_is_a_true_common_scale():
    """0 dB, so an A/B gap on screen is the loss between them and nothing else."""
    src = _viewer_src()
    m = re.search(r'const\s+STACK_Y_SPACING_DEFAULT_DB\s*=\s*([-\d.]+)\s*;', src)
    assert m, 'the default spacing must stay a named constant, not a literal'
    assert float(m.group(1)) == 0.0, (
        'FastReporter ships Y Spacing off; anything else re-introduces the '
        '"different floors" the boss reported'
    )


def test_the_old_hard_coded_eight_db_shift_is_gone():
    src = _viewer_src()
    assert 'STACK_Y_OFFSET_DB' not in src
    off = src[src.index('function yOffsetFor'):][:200]
    assert '-8' not in off and '- 8' not in off, (
        'the B offset must come from the control, not from a literal'
    )


def test_the_live_value_starts_at_the_default():
    src = _viewer_src()
    m = re.search(r'let\s+gStackSpacingDb\s*=\s*([A-Za-z_0-9]+)\s*;', src)
    assert m and m.group(1) == 'STACK_Y_SPACING_DEFAULT_DB'


def test_the_input_ships_showing_the_same_default():
    """A value= in the markup that disagrees with the constant would leave the
    box reading one number while the chart drew another."""
    src = _viewer_src()
    tag = re.search(r'<input[^>]*id="num-yspace"[^>]*>', src)
    assert tag, 'no Y-spacing input in the toolbar'
    val = re.search(r'value="([-\d.]+)"', tag.group(0))
    assert val and float(val.group(1)) == 0.0


# ─── the control exists, and is wired ──────────────────────────────────

def test_the_toolbar_carries_a_y_spacing_control():
    src = _viewer_src()
    tag = re.search(r'<input[^>]*id="num-yspace"[^>]*>', src).group(0)
    assert 'type="number"' in tag, 'FR uses a stepper; give the tech one too'
    assert 'step=' in tag
    # it belongs beside the stack checkbox, in the same toolbar group
    grp = src[src.index('id="cb-stack"'):src.index('id="cb-events"')]
    assert 'num-yspace' in grp, 'the control must sit with stack A/B'


def test_adjusting_it_redraws_immediately():
    src = _viewer_src()
    i = src.index("getElementById('num-yspace').addEventListener")
    handler = src[i:i + 500]
    assert "'input'" in handler, 'must be live, not commit-on-blur'
    assert 'gStackSpacingDb' in handler
    assert 'draw()' in handler or 'fit()' in handler


def test_the_handler_survives_an_emptied_box():
    """Clearing a number input hands back '' -> NaN.  Left unguarded that NaN
    reaches yOffsetFor and every B pixel becomes NaN: the trace vanishes."""
    src = _viewer_src()
    i = src.index("getElementById('num-yspace').addEventListener")
    handler = src[i:i + 500]
    assert 'Number.isFinite' in handler or 'isNaN' in handler


def test_the_spacing_only_applies_while_the_two_share_a_chart():
    """Unstacked, A and B are not being compared, so an offset means nothing --
    and a stale non-zero value must not silently move an unstacked B trace."""
    src = _viewer_src()
    off = src[src.index('function yOffsetFor'):][:200]
    assert 'gStacked' in off, 'the offset must be gated on stacked mode'
    assert 'gStackSpacingDb' in off

    # ...and the box itself follows the checkbox, so the tech can see why.
    sync = src[src.index('function syncYSpacingEnabled'):][:400]
    assert 'disabled' in sync and 'gStacked' in sync
    stack = src[src.index("getElementById('cb-stack').onchange"):][:300]
    assert 'syncYSpacingEnabled' in stack
    # and once at boot, or the box starts out of step with the checkbox
    assert re.search(r'^syncYSpacingEnabled\(\);', src, re.M)


# ─── nothing that carries a NUMBER may see the spacing ─────────────────

def test_the_measurement_read_strips_the_offset():
    """traceDbAtDispKm is what the marker table and every dB readout quote.  It
    must return the trace's own dB -- never dispDb, at any spacing."""
    src = _viewer_src()
    fn = src[src.index('function traceDbAtDispKm'):]
    fn = fn[:fn.index('\n}\n') + 3]
    assert 'dispDb' not in fn and 'yOffsetFor' not in fn
    assert 'gStackSpacingDb' not in fn
    assert 't.data.trace_db[' in fn, 'it must read the raw samples'


def test_the_hover_readout_quotes_the_raw_trace():
    src = _viewer_src()
    i = src.index('const dKm = pxToX(gMouse.px, r);')
    block = src[i:i + 700]
    assert 'dispDb' not in block and 'yOffsetFor' not in block
    assert 't.data.trace_db[' in block


@pytest.mark.parametrize('fname', ['renderEventTable'])
def test_the_fastreporter_table_never_sees_the_spacing(fname):
    """The FR-layout grid quotes the file's own event losses; a drawing offset
    must not reach it."""
    src = _viewer_src()
    i = src.index('function %s' % fname)
    # to the end of the function block (next top-level function)
    j = src.index('\nfunction ', i + 10)
    body = src[i:j]
    assert 'dispDb' not in body and 'gStackSpacingDb' not in body
    assert 'yOffsetFor' not in body


def test_only_drawing_and_viewport_code_may_use_the_offset():
    """Whitelist every dispDb caller by name.  A new one showing up in a
    measurement path is exactly the regression this pins."""
    src = _viewer_src()
    allowed = {'dataBounds', 'dataYBounds', 'drawTrace', 'drawEventMarkers',
               'zoomToKm'}
    # map each dispDb( occurrence back to its enclosing top-level function
    seen = set()
    for m in re.finditer(r'\bdispDb\(', src):
        head = src.rfind('\nfunction ', 0, m.start())
        assert head != -1
        name = re.match(r'\nfunction\s+([A-Za-z0-9_]+)', src[head:head + 80]).group(1)
        seen.add(name)
    seen.discard('dispDb')          # its own definition
    extra = seen - allowed
    assert not extra, (
        'dispDb reached %s -- if that path reports a number to the tech, the '
        'Y-spacing control now corrupts it' % sorted(extra)
    )


def test_the_markers_are_positioned_in_x_only():
    """Marker placement and dragging are distance-axis only, so no spacing
    value can move a marker or change what it grabs."""
    src = _viewer_src()
    assert 'let gMarkers = { a: null, b: null };' in src
    for fn in ('function drawMarkerLine', 'function hitMarker'):
        if fn not in src:
            continue
        i = src.index(fn)
        j = src.index('\n}\n', i)
        assert 'dispDb' not in src[i:j]

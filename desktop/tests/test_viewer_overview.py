"""Viewer: whole-cable overview (1152 fibers, one direction).

The trace cap was 48 because a cable-scale load is two separate walls:
1152 x 39,173 points is 340 MB of JSON, and one fetch per trace is thousands
of serial round trips against a single-threaded server.  Both are solved
server-side — bulk endpoint + spike-preserving decimation — so the client can
raise the cap.

The decimation is the load-bearing part and is what these tests pin.  Plain
striding would silently delete the very features the overview exists to show:
measured on WSC_SUIsh F19, whose real glint is 0.943 dB deep and ~4 samples
wide, striding to ~1000 points kept 0.111 dB of it while bucketed min/max
kept 0.957.
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, 'viewer'))

import trace_server as TS  # noqa: E402

from conftest import FIXTURE_A_DIR  # noqa: E402

VIEWER_HTML = os.path.join(ROOT, 'viewer', 'viewer.html')


def _spiky(n=12000, spike_at=None, depth=8.0):
    y = np.full(n, 60.0)
    if spike_at is None:
        spike_at = n // 2
    y[min(spike_at, n - 1)] = 60.0 - depth          # one-sample spike
    x = (np.arange(n) * 0.08) / 1000.0
    return list(x), list(y)


def test_decimation_preserves_a_one_sample_spike():
    x, y = _spiky()
    for target in (2000, 1000, 500):
        dx, dy = TS.decimate_minmax(x, y, target)
        assert min(dy) == 52.0, f"spike lost at target {target}: min {min(dy)}"


def test_decimation_keeps_x_monotonic():
    """The two extremes per bucket are emitted in the order they occur, so the
    polyline never doubles back on itself."""
    x, y = _spiky()
    dx, _ = TS.decimate_minmax(x, y, 1000)
    assert all(dx[i] <= dx[i + 1] for i in range(len(dx) - 1))


def test_decimation_respects_the_budget():
    x, y = _spiky(n=40000)
    for target in (2000, 1000):
        _, dy = TS.decimate_minmax(x, y, target)
        assert len(dy) <= target + 2, (target, len(dy))


def test_short_trace_is_returned_untouched():
    x, y = _spiky(n=300)
    dx, dy = TS.decimate_minmax(x, y, 2000)
    assert dx is x and dy is y


def test_no_decimation_when_maxpts_absent():
    x, y = _spiky()
    for bad in (None, 0, -5):
        dx, dy = TS.decimate_minmax(x, y, bad)
        assert len(dy) == len(y)


def test_bulk_route_and_ceiling_exist():
    src = open(os.path.join(ROOT, 'viewer', 'trace_server.py'), encoding='utf-8').read()
    assert "'/api/traces'" in src
    # a cable is 1152 fibers; a larger query must not be able to pin the server
    assert '[:1152]' in src
    assert 'max(200, min(max_pts, 20000))' in src


# ─── the ENTRY POINT, not just the maths ─────────────────────────────────
#
# Every decimation test above calls decimate_minmax() directly, and the cache
# test below used to grep load_trace's SOURCE for 't = dict(t)'.  Both kept
# passing when PR #65 inserted an early `return {**t, ...}` ABOVE the
# `if max_pts:` block: the text was still there, just unreachable, so
# /api/traces honoured maxpts in the URL and ignored it in the code and shipped
# 1152 fibers at FULL resolution -- the exact wall this feature exists to
# avoid.  These drive load_trace itself.

def _fixture_fiber():
    """(dir, fiber_num) for a real 39,173-point A-direction trace."""
    d = str(FIXTURE_A_DIR)
    TS.set_dirs(d, None)
    return d, TS.list_fibers(d)[0][0]


def test_load_trace_decimates_when_maxpts_is_given():
    """THE REGRESSION: the overview path must actually decimate."""
    d, f = _fixture_fiber()
    t = TS.load_trace('a', f, max_pts=2000)
    assert t['num_points'] <= 2100, t['num_points']
    assert t['decimated_from'] > 30000, t.get('decimated_from')


def test_load_trace_is_full_resolution_without_maxpts():
    """The detail path must be untouched — this is what zooming one fiber uses."""
    d, f = _fixture_fiber()
    t = TS.load_trace('a', f)
    assert t['num_points'] > 30000
    assert 'decimated_from' not in t


def test_the_frame_is_identical_decimated_or_not():
    """launch_km / far_conn_km are what stacked mode aligns A and B on, so the
    overview must not shift them.  _trace_frame falls back to dist_km[-1] when
    a trace carries no end event, so it is derived BEFORE decimation."""
    d, f = _fixture_fiber()
    full = TS.load_trace('a', f)
    dec = TS.load_trace('a', f, max_pts=2000)
    assert (full['launch_km'], full['far_conn_km']) == (dec['launch_km'], dec['far_conn_km'])


def test_cache_keeps_full_resolution():
    """Decimation must apply to a COPY — zooming into one fiber after an
    overview load has to still get every sample.  Driven, not grepped: the
    grep version survived the feature being unreachable for 100 engines."""
    d, f = _fixture_fiber()
    TS.load_trace('a', f, max_pts=2000)          # overview first
    after = TS.load_trace('a', f)                # then zoom in
    assert after['num_points'] > 30000, 'overview poisoned the cache'


def test_the_bulk_payload_is_json_serialisable():
    """decimate_minmax returns numpy internally; anything leaking out of
    load_trace would make json.dumps raise and kill the whole overview."""
    import json
    d, f = _fixture_fiber()
    t = TS.load_trace('a', f, max_pts=2000)
    assert isinstance(t['dist_km'], list) and isinstance(t['trace_db'], list)
    json.dumps(TS._finite(t))                    # must not raise


def test_client_raises_the_cap_for_either_direction_count():
    """This used to assert the opposite -- that overview was a SINGLE-direction
    regime, "because A+B at cable scale is 2304 traces and defeats the point".

    The real reason was the event grid: its column clustering was cubic, so
    2,304 traces would not render.  That is fixed, and the field asked for the
    cable in both directions, so the restriction went.  Measured afterwards on
    864 fibers both ways -- 1,728 traces, 22,767 events -- clustering takes
    348 ms and the fetch 36 s, which is transfer and is the tech's to spend.

    The cap is now per DIRECTION.  Capping on traces instead would quietly hand
    back half a cable the moment someone picked A+B.
    """
    html = open(VIEWER_HTML, encoding='utf-8').read()
    assert 'MAX_OVERVIEW_FIBERS = 1152' in html
    assert 'MAX_DETAIL_TRACES = 48' in html
    assert 'const overview = tasks.length > MAX_DETAIL_TRACES' in html
    assert 'MAX_OVERVIEW_FIBERS * dirs.length' in html


def test_client_collapses_the_chip_strip():
    """1152 chips push the canvas off-screen entirely."""
    html = open(VIEWER_HTML, encoding='utf-8').read()
    assert 'CHIP_COLLAPSE_AT' in html
    assert 'remove all' in html


def test_event_panel_is_virtualised():
    """A real <tr> per event built a 172,140 px table at cable scale."""
    html = open(VIEWER_HTML, encoding='utf-8').read()
    assert 'renderVirtualEventList' in html
    assert 'EVENT_ROW_H' in html
    assert 'evt-scroll' in html


def test_viewer_flag_defaults_track_the_engine():
    """The panel highlights what a uni report would flag, so its thresholds
    must be the engine's — a drift here would mislead the tech.

    Read the engine's constants from SOURCE, not by importing it.  viewer/ is
    already on sys.path in this module and it carries its own deliberately
    divergent sor_reader324802a; importing the splicereport engine here
    resolves that name to the WRONG copy and dies at its import line.  The
    three copies are isolated on purpose."""
    import re
    eng = open(os.path.join(ROOT, 'splicereport', 'splicereportmatchexfo.py'),
               encoding='utf-8').read()

    def const(name):
        m = re.search(rf'^{name}\s*=\s*(-?[\d.]+)', eng, re.M)
        assert m, f'{name} not found in the engine'
        return float(m.group(1))

    html = open(VIEWER_HTML, encoding='utf-8').read()
    line = next(l for l in html.splitlines() if 'const gViewerSettings' in l)
    assert f'lossDb: {const("UNI_BEND_THRESHOLD"):.3f}' in line, line
    assert f'reflLo: {const("UNI_REFL_FLOOR_DB"):.1f}' in line, line
    assert f'reflHi: {const("UNI_REFL_CEIL_DB"):.1f}' in line, line


def test_flagging_scope_is_documented_not_reimplemented():
    """Closure discovery and at-splice classification stay in the engine; a
    second copy of those rules in JavaScript would drift from it."""
    html = open(VIEWER_HTML, encoding='utf-8').read()
    assert 'per-event rules' in html
    assert 'would drift from it' in html

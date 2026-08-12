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


def test_cache_keeps_full_resolution():
    """Decimation must apply to a COPY — zooming into one fiber after an
    overview load has to still get every sample."""
    src = open(os.path.join(ROOT, 'viewer', 'trace_server.py'), encoding='utf-8').read()
    body = src.split('def load_trace', 1)[1].split('\ndef ', 1)[0]
    assert 't = dict(t)' in body, 'decimation must not mutate the cached trace'


def test_client_raises_the_cap_only_for_one_direction():
    """Overview is a single-direction regime: A+B at cable scale is 2304
    traces and defeats the point."""
    html = open(VIEWER_HTML, encoding='utf-8').read()
    assert 'MAX_OVERVIEW_TRACES = 1152' in html
    assert 'MAX_DETAIL_TRACES = 48' in html
    assert 'dirs.length === 1 && tasks.length > MAX_DETAIL_TRACES' in html


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

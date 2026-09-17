"""A declared span start renumbers the Viewer the way FastReporter does.

Field report (the boss, Tooele <-> Knolls, both directions shot through a 1 km
launch reel): he right-clicked event 2, the reel's far connector, chose "Span
START here", and expected it to become event 1.  Nothing changed.  The span
only moved the B mirror (test_viewer_span_decl says why that was all it did);
the FR grid kept numbering columns from the OTDR port and printing raw km.

FastReporter's rule once a span is set: the start event is event 1 at 0.0000
km, and everything ahead of it (port, launch reel) leaves the table.  So now:

  * events outside a direction's declared [start, end] window (each edge
    snapped to that fiber's own event) are dropped from the grid and drawn as
    dimmed, unnumbered ticks on the canvas;
  * the grid's distances read from the declared start;
  * marker labels count from the start, matching the columns.

Verified in the browser on TOOKNO0001 with start on event 2 (1.0401 km): the
grid reads Event 1 Reflective 0.0000 km, Event 2 Non-reflective 0.0740 km, the
port marker is unnumbered, and clearing the span restores the old view.

The viewer is plain JS with no runtime here, so this pins the source.
"""
from __future__ import annotations

import re

from conftest import VIEWER_DIR

SRC = (VIEWER_DIR / "viewer.html").read_text(encoding="utf-8")


def test_window_helpers_exist_and_snap_to_the_fibers_own_event():
    assert "function ownSpanWindow(t)" in SRC
    assert "function inDeclaredSpan(t, e)" in SRC
    assert "function spanEventNumbers(t)" in SRC
    body = SRC.split("function ownSpanWindow(t)")[1].split("function inDeclaredSpan")[0]
    assert "declaredEdgeKm(t, Number(d.start_km))" in body
    assert "declaredEdgeKm(t, Number(d.end_km))" in body


def test_grid_only_clusters_events_inside_the_declared_span():
    grid = SRC.split("function renderFastReporterGrid")[1]
    assert "if (inDeclaredSpan(t, e)) items.push({ km: dispKm(t, e.dist_km), ti, e });" in grid


def test_grid_distances_read_from_the_declared_start():
    grid = SRC.split("function renderFastReporterGrid")[1]
    assert "const zeroKm = zeroT ? dispKm(zeroT, ownSpanWindow(zeroT).lo) : 0;" in grid
    assert "${(c.km - zeroKm).toFixed(4)} km" in grid
    # Zoom targets stay raw: the canvas axis is raw and deep links land on it.
    assert 'data-km="${c.km}"' in grid


def test_markers_number_from_the_start_and_dim_the_rest():
    fn = SRC.split("function drawEventMarkers(t, r)")[1].split("\nfunction ")[0]
    assert "const nums = spanEventNumbers(t);" in fn
    assert "ctx.fillText(String(n), px, py - 11);" in fn
    assert "fillText(String(e.number)" not in fn
    assert re.search(r"globalAlpha = n == null \? 0\.35 : 0\.55", fn)

"""The Viewer starts empty when the software opens, keeps its chart across a
trip to another tool, and Clear Traces means it (Robert 2026-10-02).

The chart is kept on the trace server (viewer_state, #542) so a trip to
another tool finds it again.  The server outlives the page, though, so a hub
opened afresh took the last session's traces, and after Clear Traces the same
folders put back brought the cleared traces back.  Now:

  a hub session opened plainly (launch, a fresh page)   the kept chart goes
  a hub session opened by a report link (?nav=)         the chart stays (a trip)
  Clear Traces                                          the kept chart goes
  a cable load still landing                            kept whole, not the chunks in
"""
from __future__ import annotations

import re

import pytest

from conftest import (VIEWER_DIR, run_streamlit, import_trace_server, go_tab,
                      load_traces, clear_traces,
                      FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR)

A, B = str(FIXTURE_SPLICE_A_DIR), str(FIXTURE_SPLICE_B_DIR)
T = import_trace_server()
KEPT = {'by': 'other-window', 'keys': ['a-1', 'b-1', 'a-2'], 'removed': ['b-9'], 'add_dir': 'both'}


@pytest.fixture
def _span():
    was = (T.CONFIG.get('dir_a'), T.CONFIG.get('dir_b'))
    T.set_dirs(A, B)
    yield
    T.set_dirs(*was)
    T.reset_viewer_state()


def test_reset_forgets_the_chart_and_open_windows_follow(_span):
    v0 = T.set_viewer_state(KEPT)
    v1 = T.reset_viewer_state()
    st = T.viewer_state()
    assert v1 == v0 + 1                         # an open window sees a newer state
    assert st['keys'] == [] and st['removed'] == [] and st['add_dir'] == 'both'
    assert (st['dir_a'], st['dir_b']) == (A, B)  # made on these folders: it applies


def test_a_hub_opened_afresh_starts_with_an_empty_viewer(_span):
    T.set_viewer_state(KEPT)
    run_streamlit().run()
    assert T.viewer_state()['keys'] == []


def test_a_report_link_keeps_the_chart(_span):
    T.set_viewer_state(KEPT)
    at = run_streamlit()
    at.query_params['nav'] = 'viewer'
    at.query_params['fiber'] = '1'
    at.run()
    assert T.viewer_state()['keys'] == KEPT['keys']


def test_a_trip_inside_one_session_keeps_the_chart(_span):
    at = run_streamlit().run()
    T.set_viewer_state(KEPT)                    # the Viewer loaded, then the tech left
    go_tab(at, 'Splice Report')
    go_tab(at, 'Viewer')
    assert T.viewer_state()['keys'] == KEPT['keys']


def test_clear_traces_forgets_the_chart():
    at = run_streamlit().run()
    load_traces(at, a=A)
    load_traces(at, b=B)
    T.set_viewer_state(KEPT)
    clear_traces(at, allow=True)
    assert not at.exception, at.exception
    st = T.viewer_state()
    assert st['keys'] == []
    # the same folders put back do not bring the cleared traces back
    assert (st['dir_a'], st['dir_b']) != (A, B) or st['keys'] == []


def test_a_load_still_landing_is_kept_whole():
    src = (VIEWER_DIR / 'viewer.html').read_text(encoding='utf-8')
    i = src.index('function pushViewerState()')
    body = src[i:src.index('\n}\n', i)]
    assert re.search(r'const keys = \[\.\.\.new Set\(\[\.\.\.gTraces\.map\(t => t\.key\), \.\.\.gLoadingKeys\]\)\]', body)

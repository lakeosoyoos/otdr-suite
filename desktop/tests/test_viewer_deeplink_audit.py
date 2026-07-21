"""Deep-link audit fixes (2026-07-21) — locks for the cell-click→viewer path.

Three-agent audit of "boss clicks a Splice Report cell, viewer loads, trace
never appears" found, beyond the parser drift fixed separately:
  hub:    zip/one-folder staging dirs never reached the viewer; cell click
          is a fresh session so folder handoff needs to ride the URL; grid
          km is launch-normalized while the viewer plots the raw port frame;
          stale viewer_target / sr_result / zip-staging cache survive span
          changes.
  js:     cold-start deep link permanently dead after the retry window; hub
          poke never re-ran the load; every /api/trace failure silent to
          user AND Slack; boot wrapper swallowed (and de-reported) crashes.
  server: GenParams rescue full-file reads on every request of the exact
          folders needing rescue (uncached, single-threaded); /api/list
          unguarded; None parses memoized under coarse-mtime keys;
          duplicate fiber numbers resolved nondeterministically.
Source-lock style — no engine imports in the pytest process.
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))


def _src(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as f:
        return f.read()


# ── hub (app.py) ─────────────────────────────────────────────────────────

def test_cell_links_carry_span_dirs():
    s = _src('app.py')
    assert '&sra=' in s and '&srb=' in s
    assert "st.session_state['sr_dirs'] = (dir_a, dir_b)" in s


def test_nav_seeds_viewer_dirs_from_link():
    s = _src('app.py')
    body = s.split('def _handle_nav', 1)[1].split('\ndef ', 1)[0]
    assert "qp.get('sra')" in body
    assert "view_dir_a_input" in body


def test_grid_km_converted_to_viewer_frame():
    s = _src('app.py')
    assert '_vkm(c[' in s
    assert "_mani.get('launch_a_km'" in s


def test_span_load_invalidates_stale_state():
    s = _src('app.py')
    body = s.split('def _load_span', 1)[1].split('\ndef ', 1)[0]
    assert "pop('viewer_target'" in body
    assert "pop('sr_result'" in body


def test_zip_staging_cache_keys_on_mtime():
    s = _src('app.py')
    assert '_VIEWER_DIR_CACHE[p] = (_zsig, flat)' in s


# ── runner manifest ──────────────────────────────────────────────────────

def test_manifest_carries_launch_offset():
    s = _src('splicereport/run_splicereport.py')
    assert "'launch_a_km'" in s
    assert '_trace_offset_km' in s


# ── trace server ─────────────────────────────────────────────────────────

def test_listing_cached_and_rescue_bounded():
    s = _src('viewer/trace_server.py')
    assert '_LIST_CACHE' in s
    assert '_GENPARAMS_READ_CAP' in s
    # rescue must read a bounded head, never the whole file
    assert 'fh.read(_GENPARAMS_READ_CAP)' in s


def test_api_list_guarded_and_reported():
    s = _src('viewer/trace_server.py')
    assert '_api_list' in s
    assert "report_error('viewer /api/list'" in s


def test_none_parse_not_left_memoized():
    s = _src('viewer/trace_server.py')
    assert 'cache_clear()' in s


def test_duplicate_fibers_resolve_deterministically():
    s = _src('viewer/trace_server.py')
    assert 'out.sort(key=lambda t: (t[0], t[1]))' in s


# ── viewer JS ────────────────────────────────────────────────────────────

def test_focus_poke_refires_deeplink():
    s = _src('viewer/viewer.html')
    i = s.find("addEventListener('focus'")
    assert i > 0
    handler = s[i:i + 400]
    assert 'bootLoad' in handler


def test_trace_failures_surfaced_to_readout():
    s = _src('viewer/viewer.html')
    assert 'gLoadFailures' in s
    assert 'could not load' in s


def test_boot_crashes_reported_not_swallowed():
    s = _src('viewer/viewer.html')
    body = s.split('async function boot()', 1)[1]
    assert 'reportJsError' in body.split('})();')[0]

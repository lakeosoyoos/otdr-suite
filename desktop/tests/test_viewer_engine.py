"""Pytest suite for the OTDR Suite VIEWER engine (viewer/trace_server.py).

NAMESPACE ISOLATION (hard rule)
-------------------------------
The viewer engine and Secret Sauce ship DIFFERENT sor_reader324802a.py copies
that cannot coexist in one Python process.  Every test below touches ONLY the
viewer side via ``ts = import_trace_server()``.  Nothing here imports the
secretsauce package, report.py, report_sor.py, or run_secretsauce — doing so
would poison the import of the viewer's sor_reader copy for the whole process.

Fixtures: FIXTURE_A_DIR / FIXTURE_B_DIR each hold 4 SOR files (fibers 1-4).
Only a couple of fibers are actually parsed (the SOR parse is the slow part),
so the whole module runs well under the ~15 s budget.
"""
from __future__ import annotations

import shutil

import numpy as np
import pytest

from conftest import import_trace_server, FIXTURE_A_DIR, FIXTURE_B_DIR

# One module-level handle.  conftest.import_trace_server() puts VIEWER_DIR on
# sys.path so this resolves the viewer's sor_reader, never Secret Sauce's.
ts = import_trace_server()


@pytest.fixture(scope="module")
def dirs_set():
    """Point the engine at the A/B fixture spans for the trace-loading tests."""
    ts.set_dirs(str(FIXTURE_A_DIR), str(FIXTURE_B_DIR))
    return ts


@pytest.fixture(scope="module")
def trace_a1(dirs_set):
    """Fiber 1 of span_A, loaded once and shared (keeps the SOR parse cost to
    a single hit for every test that just needs a parsed trace)."""
    t = dirs_set.load_trace("a", 1)
    assert t is not None, "fixture span_A fiber 1 failed to load"
    return t


# ─── 1. fiber-number extraction ─────────────────────────────────────────
@pytest.mark.parametrize(
    "filename, expected",
    [
        ("ELMMIL0064_1550.sor", 64),
        ("MILELM1152_1550.sor", 1152),
        ("STRROM0001_1550.sor", 1),
    ],
)
def test_extract_fiber_num(filename, expected):
    assert ts.extract_fiber_num(filename) == expected


# ─── 2. list_fibers: sorted tuples + missing dir ────────────────────────
def test_list_fibers_returns_four_sorted_tuples():
    fibers = ts.list_fibers(str(FIXTURE_A_DIR))
    assert len(fibers) == 4
    # Each entry is a (fiber_num, filename) tuple.
    for fnum, fn in fibers:
        assert isinstance(fnum, int)
        assert isinstance(fn, str) and fn.lower().endswith(".sor")
    assert [fnum for fnum, _ in fibers] == [1, 2, 3, 4]


def test_list_fibers_nonexistent_dir_returns_empty():
    assert ts.list_fibers("/no/such/directory/anywhere") == []


# ─── 3. AppleDouble (._) files are skipped ──────────────────────────────
def test_list_fibers_skips_appledouble(tmp_path):
    span = tmp_path / "span_A_copy"
    shutil.copytree(FIXTURE_A_DIR, span)
    # An AppleDouble sidecar that Mac zips leave behind, named like a fiber.
    (span / "._ELMMIL0009_1550.sor").write_bytes(b"\x00\x05\x16\x07AppleDouble junk")
    fibers = ts.list_fibers(str(span))
    assert [fnum for fnum, _ in fibers] == [1, 2, 3, 4]
    assert not any(fn.startswith("._") for _, fn in fibers)


# ─── 4. load_trace payload shape ────────────────────────────────────────
def test_load_trace_payload_shape(trace_a1):
    t = trace_a1
    for key in ("num_points", "dx_km", "first_pos_km", "dist_km", "trace_db", "events"):
        assert key in t, f"missing key {key!r}"
    assert t["num_points"] > 1000
    assert len(t["dist_km"]) == t["num_points"]
    assert len(t["trace_db"]) == t["num_points"]
    assert t["dx_km"] > 0


# ─── 5. distance axis is monotonically non-decreasing ───────────────────
def test_dist_km_monotonic_non_decreasing(trace_a1):
    # Sampling every ~500th point keeps this cheap; a flipped/garbled axis
    # would still show up at this resolution.
    sampled = np.asarray(trace_a1["dist_km"][::500], dtype=np.float64)
    assert np.all(np.diff(sampled) >= 0.0)


# ─── 6. events: nonempty, launch near 0, has an end/reflective event ─────
def test_events_present_and_sane(trace_a1):
    events = trace_a1["events"]
    assert len(events) > 0
    # First event is the launch — within ~0.2 km of 0.
    assert abs(events[0]["dist_km"]) <= 0.2
    assert any(e["is_end"] or e["is_reflective"] for e in events)


# ─── 7. SIGN CONVENTION: descending = loss ──────────────────────────────
def test_sign_convention_descends_over_span(trace_a1):
    """After baseline removal the trace trends downward across the span:
    the median near the END must be clearly LESS than the median near the
    START (loss accumulates).  A sign flip in display_trace negation would
    invert this and fail."""
    tr = np.asarray(trace_a1["trace_db"], dtype=np.float64)
    start_med = float(np.median(tr[200:2200]))
    end_med = float(np.median(tr[-2000:]))
    assert end_med < start_med - 1.0, (
        f"expected descending-loss trace: end {end_med:.3f} dB should be "
        f">1 dB below start {start_med:.3f} dB"
    )


# ─── 8. missing fiber returns None ──────────────────────────────────────
def test_load_trace_missing_fiber_returns_none(dirs_set):
    assert dirs_set.load_trace("a", 99999) is None


# ─── 9. caching (lru_cache) returns consistent payloads ─────────────────
def test_load_trace_caching_consistent(dirs_set):
    first = dirs_set.load_trace("a", 1)
    second = dirs_set.load_trace("a", 1)
    assert first is not None and second is not None
    assert first["num_points"] == second["num_points"]
    assert len(first["events"]) == len(second["events"])


# ─── XFAIL: content-sniff to reject non-SOR files with a .sor extension ──
@pytest.mark.xfail(
    strict=True,
    reason="list_fibers filters on the .sor extension only; a junk file with a "
    ".sor extension leaks in and only fails later at parse time.",
)
def test_list_fibers_rejects_non_sor_content(tmp_path):
    """DESIRED-but-unimplemented: list_fibers should content-sniff so a file
    that merely *ends in* .sor (but isn't a real Telcordia SOR) is excluded,
    instead of being advertised as a loadable fiber that blows up on load.

    TODO(next engineer): to flip this green, have list_fibers (or a helper it
    calls) peek at the file header and skip files that don't carry the SOR
    'Map' block magic before appending them to ``out`` in
    viewer/trace_server.py.  Then delete this xfail marker.
    """
    span = tmp_path / "span_A_copy"
    shutil.copytree(FIXTURE_A_DIR, span)
    # A non-SOR payload wearing a .sor extension, named like fiber 9.
    (span / "ELMMIL0009_1550.sor").write_text("this is plainly not a SOR file")
    fibers = ts.list_fibers(str(span))
    assert [fnum for fnum, _ in fibers] == [1, 2, 3, 4], (
        "non-SOR .sor file should not be listed as a fiber"
    )


# ─── viewer.html deep-link contract ─────────────────────────────────────
def test_viewer_html_parses_multifiber_deeplink():
    """bootLoad() must read a comma-separated ?fibers= deep-link (the
    Duplicate Check 'Stay in app' pair click) and load BOTH fibers.  Guards
    the static contract the hub's pair link relies on."""
    from conftest import VIEWER_DIR
    html = (VIEWER_DIR / "viewer.html").read_text(encoding="utf-8")
    assert "p.get('fibers')" in html, "bootLoad doesn't read the ?fibers= param"
    # The fiber-list input + addFibers() already split on commas across dirs;
    # the deep-link routes the multi value through that same path.
    assert "/^\\d+(\\s*,\\s*\\d+)*$/" in html, "missing comma-separated fibers validation"

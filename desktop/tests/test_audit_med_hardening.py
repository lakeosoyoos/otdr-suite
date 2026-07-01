"""Regression tests for two safe MED hardening fixes (Fable audit).

Neither touches detection/scoring behavior:
  • Splice `--overrides` must reject a non-finite REBURN_THRESHOLD — a NaN made
    `abs(loss) >= nan` always False → zero reburns flagged, silently defeating
    the 0.160 invariant.
  • The viewer's /api/jserror endpoint must reject cross-origin POSTs — otherwise
    any website the tech visits while the hub runs could flood the shared Slack
    channel (dedup is keyed on the attacker-controlled message).
"""
import json
import urllib.error
import urllib.request

from conftest import (FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR,
                      import_trace_server, run_splicereport)


def test_splice_overrides_rejects_nonfinite_reburn_threshold(tmp_path):
    out = tmp_path / "r.xlsx"
    rc, man, err = run_splicereport(
        FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR, out,
        overrides={"REBURN_THRESHOLD": float("nan")})
    # The run must still succeed (baseline 0.160 kept), not silently zero reburns.
    assert rc == 0 and man and man.get("ok"), (rc, (err or "")[-600:])
    assert "not finite" in (err or ""), \
        "NaN REBURN_THRESHOLD override was not rejected (invariant unguarded)"


def test_splice_overrides_still_accepts_a_valid_threshold(tmp_path):
    """Guard against over-rejection: a normal positive float override still lands."""
    out = tmp_path / "r.xlsx"
    rc, man, err = run_splicereport(
        FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR, out,
        overrides={"REBURN_THRESHOLD": 0.05})
    assert rc == 0 and man and man.get("ok"), (rc, (err or "")[-600:])
    assert "skip override REBURN_THRESHOLD" not in (err or ""), \
        "a valid 0.05 threshold was wrongly rejected"


def _post(port, origin):
    body = json.dumps({"message": "x", "page": "p"}).encode()
    headers = {"Content-Type": "application/json"}
    if origin is not None:
        headers["Origin"] = origin
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/jserror",
                                 data=body, headers=headers)
    return urllib.request.urlopen(req, timeout=4)


def test_jserror_rejects_cross_origin_post():
    ts = import_trace_server()
    port = ts.start_in_thread(8797)          # idempotent; returns the live port
    # A real website's cross-origin POST always carries its Origin -> 403.
    try:
        _post(port, "https://evil.example")
        assert False, "cross-origin POST to /api/jserror was not rejected"
    except urllib.error.HTTPError as e:
        assert e.code == 403, e.code
    # A same-origin (loopback) POST is still accepted -> 200.
    with _post(port, f"http://127.0.0.1:{port}") as r:
        assert r.status == 200
    # A missing Origin (some same-origin fetches omit it) is still accepted.
    with _post(port, None) as r:
        assert r.status == 200

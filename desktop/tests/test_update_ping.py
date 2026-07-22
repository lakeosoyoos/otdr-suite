"""Update-ping guard tests (error_report.maybe_report_update).

The rollout ping must be SAFE the same way error reporting is: a silent no-op
without a webhook or in dev, exactly one post per build-identity change per
machine (marker file), an hourly in-process throttle when the marker can't be
written, and it must NEVER raise.  It must also NEVER match the Slack→issues
bridge's error header — a rollout note must not become a phantom error issue —
so the bridge's own HDR_RE is embedded here as a lock.
"""
import json
import os
import re
import time
import urllib.request

from conftest import REPO_ROOT  # noqa: F401  (ensures REPO_ROOT is on sys.path)
import error_report as R

DUMMY = "http://127.0.0.1:9/none"   # unreachable: background send fails silently

# Byte-for-byte copy of HDR_RE in otdr-suite-errors/scripts/slack_to_issues.py
# (with APP_NAME expanded).  If the ping ever matches this, the bridge would
# file every tech's update as an error issue.
BRIDGE_HDR_RE = re.compile(
    r":rotating_light:\s*\*OTDR Suite error\*\s*[—-]\s*(?P<where>.+)")

LABELS = ("build 79 (2026-07-17 11:57 PDT)", "update 89 applied 2026-07-21 15:05 PDT")


def _arm(monkeypatch, labels=LABELS):
    """Webhook set, labels stubbed to a frozen-build identity, throttle clear."""
    monkeypatch.setenv("SS_ERROR_WEBHOOK", DUMMY)
    monkeypatch.setattr(R, "version_labels", lambda *a, **k: labels)
    R._UPD_LAST.clear()


def _capture(monkeypatch, marker, timeout=4.0):
    """Run maybe_report_update with the network stubbed; return (posted, text)
    where text is the Slack payload the background thread would have POSTed
    (None when nothing was sent)."""
    captured = {}

    def fake_urlopen(req, timeout=4, **kwargs):
        captured["body"] = req.data.decode()
        class _Resp:
            status = 200
            def __enter__(self_):  # noqa: N805
                return self_
            def __exit__(self_, *a):  # noqa: N805
                return False
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    posted = R.maybe_report_update(marker_path=str(marker))
    if posted:
        deadline = time.time() + timeout
        while "body" not in captured and time.time() < deadline:
            time.sleep(0.02)
        assert "body" in captured, "maybe_report_update returned True but never sent"
        return True, json.loads(captured["body"])["text"]
    return False, None


def test_no_webhook_is_a_silent_noop(monkeypatch, tmp_path):
    monkeypatch.delenv("SS_ERROR_WEBHOOK", raising=False)
    monkeypatch.setattr(R, "version_labels", lambda *a, **k: LABELS)
    R._UPD_LAST.clear()
    marker = tmp_path / "ping.json"
    assert R.maybe_report_update(marker_path=str(marker)) is False
    assert not marker.exists()                     # no state left behind either


def test_dev_run_never_pings(monkeypatch, tmp_path):
    _arm(monkeypatch, labels=("dev", "dev"))
    posted, _ = _capture(monkeypatch, tmp_path / "ping.json")
    assert posted is False


def test_first_boot_pings_and_writes_marker(monkeypatch, tmp_path):
    _arm(monkeypatch)
    marker = tmp_path / "ping.json"
    posted, text = _capture(monkeypatch, marker)
    assert posted is True
    assert "update applied" in text
    assert LABELS[0] in text and LABELS[1] in text
    assert "first report from this machine" in text
    assert json.loads(marker.read_text()) == {"app": LABELS[0], "engine": LABELS[1]}


def test_ping_never_matches_the_bridge_error_header(monkeypatch, tmp_path):
    _arm(monkeypatch)
    posted, text = _capture(monkeypatch, tmp_path / "ping.json")
    assert posted is True
    assert not BRIDGE_HDR_RE.search(text)


def test_same_identity_is_reported_once(monkeypatch, tmp_path):
    _arm(monkeypatch)
    marker = tmp_path / "ping.json"
    assert _capture(monkeypatch, marker)[0] is True
    R._UPD_LAST.clear()          # marker alone must dedup, not the throttle
    assert _capture(monkeypatch, marker)[0] is False


def test_identity_change_pings_again_with_prev_line(monkeypatch, tmp_path):
    _arm(monkeypatch)
    marker = tmp_path / "ping.json"
    assert _capture(monkeypatch, marker)[0] is True
    newer = (LABELS[0], "update 90 applied 2026-07-22 09:00 PDT")
    _arm(monkeypatch, labels=newer)
    posted, text = _capture(monkeypatch, marker)
    assert posted is True
    assert newer[1] in text
    assert "prev: engine %s" % LABELS[1] in text   # old engine named, not lost
    assert json.loads(marker.read_text())["engine"] == newer[1]


def test_unwritable_marker_degrades_to_hourly_throttle(monkeypatch, tmp_path):
    _arm(monkeypatch)
    marker = tmp_path / "as_dir"
    marker.mkdir()                                  # open(marker, "w") fails
    posted, _ = _capture(monkeypatch, marker)
    assert posted is True                           # first report still goes out
    posted, _ = _capture(monkeypatch, marker)
    assert posted is False                          # throttle holds the line


def test_garbage_marker_never_raises_and_reports(monkeypatch, tmp_path):
    _arm(monkeypatch)
    marker = tmp_path / "ping.json"
    marker.write_text("{not json")
    posted, text = _capture(monkeypatch, marker)
    assert posted is True
    assert "first report from this machine" in text  # unreadable prev -> first

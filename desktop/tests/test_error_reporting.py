"""Error-reporting guard tests (error_report.report_error).

Reporting must be SAFE: a no-op when no webhook is configured, deduped to one
message per signature per hour, and it must NEVER raise (a reporting hiccup must
not break a tech's run).  The webhook URL is baked into the build from a CI
secret — never in source — so these tests use a dummy/unreachable URL and assert
the DECISION logic via the in-process dedup table, never the network.
"""
import os

from conftest import REPO_ROOT  # noqa: F401  (ensures REPO_ROOT is on sys.path)
import error_report as R

DUMMY = "http://127.0.0.1:9/none"   # unreachable: background send fails silently


def test_no_webhook_is_a_silent_noop():
    os.environ.pop("SS_ERROR_WEBHOOK", None)
    R._ERR_LAST.clear()
    R.report_error("unit", ValueError("boom"))     # must not raise
    assert R._ERR_LAST == {}                        # nothing queued without a webhook


def test_records_then_dedups_within_the_hour(monkeypatch):
    monkeypatch.setenv("SS_ERROR_WEBHOOK", DUMMY)
    R._ERR_LAST.clear()
    R.report_error("unit", ValueError("boom"), {"files": 3})
    assert len(R._ERR_LAST) == 1
    first = list(R._ERR_LAST.values())[0]
    R.report_error("unit", ValueError("boom"), {"files": 3})   # same signature
    assert list(R._ERR_LAST.values())[0] == first              # not re-sent


def test_distinct_errors_each_record(monkeypatch):
    monkeypatch.setenv("SS_ERROR_WEBHOOK", DUMMY)
    R._ERR_LAST.clear()
    R.report_error("unit", ValueError("a"))
    R.report_error("unit", KeyError("b"))
    R.report_error("other-where", ValueError("a"))   # same exc, different where
    assert len(R._ERR_LAST) == 3


def test_never_raises_on_weird_context(monkeypatch):
    monkeypatch.setenv("SS_ERROR_WEBHOOK", DUMMY)
    R._ERR_LAST.clear()
    R.report_error("unit", ValueError("x"), context={"obj": object()})
    R.report_error("unit2", RuntimeError("y"), context=None)
    # passes iff no exception escaped


def test_app_name_is_this_app():
    # One channel serves every app → the tag must identify THIS one.
    assert R.APP_NAME == "OTDR Suite"

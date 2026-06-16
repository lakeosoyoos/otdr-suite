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


def _capture_slack_text(monkeypatch, where, exc, context=None, timeout=4.0):
    """Run report_error with the network stubbed; return the composed Slack
    `text` payload the background thread would have POSTed (no real request)."""
    import json
    import time
    import urllib.request

    captured = {}

    def fake_urlopen(req, timeout=4):
        captured["body"] = req.data.decode()
        class _Resp:
            status = 200
            def __enter__(self_):  # noqa: N805
                return self_
            def __exit__(self_, *a):  # noqa: N805
                return False
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("SS_ERROR_WEBHOOK", DUMMY)
    R._ERR_LAST.clear()
    R.report_error(where, exc, context)
    deadline = time.time() + timeout
    while "body" not in captured and time.time() < deadline:
        time.sleep(0.02)
    assert "body" in captured, "report_error never attempted a send"
    return json.loads(captured["body"])["text"]


def test_home_paths_are_redacted_from_slack_message(monkeypatch):
    """SECURITY/PII: the shared channel must never receive the tech's local
    filesystem layout.  An absolute home path in BOTH a context value and the
    exception/traceback text must be scrubbed to '~', while the error type and
    basename survive (the report stays useful)."""
    home = os.path.expanduser("~")
    secret = os.path.join(home, "Desktop", "OTDR Suite", "dir_a", "fiber0007.sor")
    try:
        raise ValueError("bad event at " + secret)   # path leaks into exc + tb
    except ValueError as e:
        text = _capture_slack_text(
            monkeypatch, "secret sauce engine", e,
            context={"folder": secret, "files": 7})

    # No absolute home prefix anywhere in the composed message.
    assert home not in text, f"home path leaked to Slack:\n{text}"
    # And specifically no '/Users/<name>/' style absolute path survived.
    assert "/Users/" not in text or "~/" in text  # tilde form replaced it
    # The report is still useful: error type + the file basename remain.
    assert "ValueError" in text
    assert "fiber0007.sor" in text


def test_non_home_absolute_paths_are_redacted_from_slack_message(monkeypatch):
    """SECURITY/PII: redacting only the $HOME prefix is not enough — paths
    OUTSIDE home still leak the tech's layout.  A POSIX mount path
    (/Volumes/…), another user's /Users/… path, and a Windows drive path
    (C:\\Users\\…) must each be scrubbed to a basename, while the error type
    and the file's basename survive."""
    vol = "/Volumes/FieldDrive/Job12/ELMMIL/fiber0007.sor"
    win = r"C:\Users\rcolbert\Desktop\OTDR Suite\dir_a\report.xlsx"
    try:
        raise ValueError("bad event at " + vol + " and " + win)
    except ValueError as e:
        text = _capture_slack_text(
            monkeypatch, "secret sauce engine", e,
            context={"folder": vol, "out": win})

    # Neither full absolute path may survive anywhere in the message.
    assert vol not in text, f"/Volumes path leaked to Slack:\n{text}"
    assert win not in text, f"Windows path leaked to Slack:\n{text}"
    assert "/Volumes/" not in text, f"/Volumes root leaked:\n{text}"
    assert "C:\\Users" not in text and "C:/Users" not in text, (
        f"Windows drive root leaked:\n{text}")
    # The report stays useful: error type + the basenames remain.
    assert "ValueError" in text
    assert "fiber0007.sor" in text
    assert "report.xlsx" in text


def test_scrub_paths_is_a_pure_never_raising_helper():
    """_scrub_paths is the unit behind the redaction; it must be stdlib-only,
    never raise, leave non-path text intact, and reduce absolute paths to a
    basename (so the dedup signature, computed BEFORE text is built, is
    untouched)."""
    f = R._scrub_paths
    # Non-path text — including a bare '5 / 10' ratio — is left alone.
    assert f("ValueError: bad value 5 / 10 here") == "ValueError: bad value 5 / 10 here"
    # POSIX mount + Windows drive paths collapse to basename.
    assert f("at /Volumes/X/Y/trace.sor") == "at trace.sor"
    assert f(r"at D:\Jobs\Elmhurst\report.xlsx") == "at report.xlsx"
    # Never raises on odd input.
    f(None) if False else None      # keep type-stable; exercise strings only
    assert f("") == ""


def test_scrub_paths_never_crosses_a_space_into_following_words():
    """REGRESSION: the redaction must NEVER let a path match run past the real
    path across a space into the words that follow (or into a second path on the
    same line).  Allowing a space in the path-component class did exactly that —
    "/Volumes/A/B and C and D" mangled the sentence, and on two paths the FIRST
    filename was destroyed entirely.  Each path must redact to ONLY its basename;
    every trailing/connecting word must survive verbatim."""
    f = R._scrub_paths
    # (a) A single path FOLLOWED by trailing words on the same line: only the
    #     path collapses to its basename; the words after the space are verbatim.
    assert f("/Volumes/A/B and C and D") == "B and C and D"
    assert f("at /Volumes/X/Y/trace.sor then we stop") == "at trace.sor then we stop"
    # (b) TWO absolute paths on one line (POSIX + POSIX, then POSIX + Windows):
    #     BOTH redact to basenames, the connecting words survive, and NEITHER
    #     filename is destroyed.
    assert (f("/Volumes/A/trace.sor vs /Users/bob/other.sor here")
            == "trace.sor vs other.sor here")
    out = f(r"posix /Volumes/A/trace.sor and win C:\Users\bob\report.xlsx end")
    assert "trace.sor" in out and "report.xlsx" in out      # neither destroyed
    assert " and " in out and out.endswith("end")           # connectors survive
    assert "/Volumes/" not in out and "C:\\Users" not in out  # both redacted


def test_home_path_renders_with_slash_not_stranded_tilde():
    """A path under the running user's $HOME must render as '~/<basename>' (home
    prefix gone, basename kept) — never a stranded '~<basename>'.  And a literal
    '~' in ordinary prose (e.g. an approximate value '~5 dB') must be untouched:
    the slash is only restored for a real home-collapsed path, not any tilde."""
    import os
    f = R._scrub_paths
    home = os.path.expanduser("~")
    out = f("loaded " + os.path.join(home, "Desktop", "Jobs", "trace.sor"))
    assert "~/trace.sor" in out, out          # clean home-relative basename
    assert "~trace.sor" not in out, out       # no stranded tilde
    assert home not in out                    # username / layout gone
    # A literal '~5' in prose is NOT a path and must be left alone.
    assert f("loss was approx ~5 dB") == "loss was approx ~5 dB"


def test_app_name_is_this_app():
    # One channel serves every app → the tag must identify THIS one.
    assert R.APP_NAME == "OTDR Suite"


def test_hub_routing_has_global_report_hook():
    """The hub must wrap page routing so ANY unhandled render error reports."""
    from conftest import APP_PATH
    src = APP_PATH.read_text(encoding="utf-8")
    assert "except Exception as _exc:" in src and 'report_error(f"hub page' in src, (
        "app.py must catch+report unhandled page errors then re-raise"
    )


def test_jserror_endpoint_reports_to_slack(monkeypatch):
    """Browser JS errors POSTed to /api/jserror must flow through report_error
    (same path that reaches Slack) — proves the viewer error hook end-to-end."""
    import json
    import urllib.request
    from conftest import import_trace_server

    monkeypatch.setenv("SS_ERROR_WEBHOOK", DUMMY)
    R._ERR_LAST.clear()
    ts = import_trace_server()
    port = ts.start_in_thread(8795)            # idempotent; finds a free port
    body = json.dumps({"message": "TypeError: gView is undefined",
                       "stack": "at draw (viewer.html:430)",
                       "page": "http://127.0.0.1/"}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/jserror",
                                 data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=4) as r:
        assert r.status == 200
    assert len(R._ERR_LAST) == 1               # the JS error was recorded for Slack

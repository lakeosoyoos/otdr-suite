"""The run-time panel keeps counting while a report runs (2026-09-28).

A tech, running a report: "When it was showing seconds of run time it went
bright and dim and it stopped counting for a few seconds."

The panel was redrawn by re-running the WHOLE PAGE every 0.8 s.  Anything
slow on the page above it held the panel up: the sidebar's update check is a
web request (every 5 minutes, 3 s or more when the connection is poor), and
each pass re-checks the trace folders.  Streamlit fades whatever has not been
redrawn within half a second of a pass starting, so a slow pass showed as the
panel going dim, the count standing still, then both snapping back.

Measured on the 864-fiber span with the update check unable to connect: the
count went 5 s, nothing for 3.85 s, 9 s.  Every other pass reached the panel
in 0.03 s.

The panel is now a fragment: a redraw re-runs the panel and nothing else, so
nothing on the page can hold it up.  Checked on the messages the browser is
sent, update check still unable to connect: 42 redraws, the longest wait
0.58 s, no number skipped, and the page itself run twice (start and finish)
where it used to run 22 times.  The same with the browser's timer stopped
after its first tick, which is what a window that is not in front does.
"""
from __future__ import annotations

import ast
import copy
import sys
import time
import types

import pytest

from conftest import (REPO_ROOT, FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR,
                      finish_engine_run, run_streamlit)

SRC = (REPO_ROOT / "app.py").read_text(encoding="utf-8")
TREE = ast.parse(SRC)


def _fn_node(name):
    return next(n for n in TREE.body
                if isinstance(n, ast.FunctionDef) and n.name == name)


def _fn_code(name):
    """One app.py function as code, docstring and comments dropped, so a
    source pin reads what the function DOES and not what its prose says."""
    fn = copy.deepcopy(_fn_node(name))
    if (fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)
            and isinstance(fn.body[0].value.value, str)):
        fn.body.pop(0)
    return ast.unparse(fn)


def _tick_s():
    node = next(n for n in TREE.body if isinstance(n, ast.Assign)
                and getattr(n.targets[0], "id", None) == "ENGINE_TICK_S")
    return ast.literal_eval(node.value)


# A stand-in engine: says one line on stderr, waits, prints a manifest.
def _engine(seconds, manifest='{"ok": false, "error": "stand-in engine"}'):
    return [sys.executable, "-c",
            "import sys, time; sys.stderr.write('reading fiber 12\\n'); "
            f"sys.stderr.flush(); time.sleep({seconds}); print({manifest!r})"]


def _page_with_run(cmd):
    at = run_streamlit().run()
    at.session_state["view_dir_a_input"] = str(FIXTURE_SPLICE_A_DIR)
    at.session_state["view_dir_b_input"] = str(FIXTURE_SPLICE_B_DIR)
    at.sidebar.radio[0].set_value("Splice Report").run()
    assert not at.exception, list(at.exception)
    at.session_state["sr_pending_cmd"] = cmd
    return at


def _kill(at):
    job = at.session_state["sr_job"] if "sr_job" in at.session_state else None
    if job:
        try:
            job["proc"].kill()
            job["proc"].wait(timeout=5)
        except Exception:
            pass
        for fh in (job.get("fo"), job.get("fe")):
            try:
                fh.close()
            except Exception:
                pass


# ── the page is not re-run to move the count ─────────────────────────────
def test_one_pass_of_the_page_draws_the_panel_and_ends():
    """With a run going, a pass of the page draws the panel and finishes.
    Before the fix the pass re-ran itself every 0.8 s until the engine was
    done, so it could not come back while the run was still going."""
    at = _page_with_run(_engine(8))
    try:
        t0 = time.monotonic()
        at.run()
        took = time.monotonic() - t0
        assert not at.exception, list(at.exception)
        assert "sr_job" in at.session_state, "the run is over; the page looped"
        job = at.session_state["sr_job"]
        assert job["state"] == "running" and job["proc"].poll() is None
        assert took < 6, f"the page pass took {took:.1f}s of an 8 s run"
        assert any("s elapsed" in i.value for i in at.info)
        assert any(b.label == "Cancel run" for b in at.button)
    finally:
        _kill(at)


def test_the_panel_shows_the_engines_current_step():
    at = _page_with_run(_engine(8))
    try:
        at.run()
        deadline = time.monotonic() + 5
        while (not any("reading fiber 12" in c.value for c in at.caption)
               and time.monotonic() < deadline):
            time.sleep(0.1)
            at.run()
        assert any("reading fiber 12" in c.value for c in at.caption)
    finally:
        _kill(at)


def test_the_page_draws_no_report_under_a_run_but_keeps_its_sidebar():
    """No earlier report under a run that is replacing it, as it always has
    been.  The rest of the page is drawn as usual, down to the version line
    and 'Check for updates' at the foot of the sidebar."""
    at = _page_with_run(_engine(8))
    try:
        at.session_state["sr_result"] = {"ok": True, "grid": [], "xlsx": "x"}
        at.run()
        assert not at.exception, list(at.exception)
        assert "sr_job" in at.session_state
        labels = [b.label or "" for b in at.main.button]
        assert labels[-1] == "Cancel run", labels
        # (OTDR Suite App's pinned sidebar footer says "Check for Updates".)
        assert any("check for updates" in (b.label or "").lower()
                   for b in at.sidebar.button)
        assert any("OTDR Suite" in c.value for c in at.sidebar.caption)
    finally:
        _kill(at)


# ── the page takes over when the run ends ────────────────────────────────
def test_the_page_collects_the_result_when_the_run_ends():
    at = _page_with_run(_engine(0.2))
    try:
        finish_engine_run(at.run(), "sr", timeout=20)
        assert not at.exception, list(at.exception)
        assert "sr_job" not in at.session_state
        assert any("stand-in engine" in e.value for e in at.error)
        assert not any("s elapsed" in i.value for i in at.info)
    finally:
        _kill(at)


def test_cancel_stops_the_engine():
    at = _page_with_run(_engine(30))
    try:
        at.run()
        proc = at.session_state["sr_job"]["proc"]
        next(b for b in at.button if b.label == "Cancel run").click().run()
        assert not at.exception, list(at.exception)
        assert "sr_job" not in at.session_state
        assert proc.poll() is not None, "Cancel left the engine running"
        assert any("Run cancelled." in i.value for i in at.info)
    finally:
        _kill(at)


# ── one redraw of the panel, on its own ──────────────────────────────────
class _Rerun(BaseException):
    def __init__(self, scope):
        self.scope = scope


class _State(dict):
    pass


class _FakeSt:
    def __init__(self, state, did):
        self.session_state = _State(state)
        self.did = did

    def rerun(self, *, scope="app"):
        self.did.append(f"rerun {scope}")
        raise _Rerun(scope)

    def info(self, body, *a, **k):
        self.did.append(("info", body))

    def caption(self, body, *a, **k):
        self.did.append(("caption", body))

    def button(self, label, *a, **k):
        self.did.append(("button", label))
        return False


def _panel(state, poll, tail=("reading fiber 12",), now=100.0):
    """The panel's own body (decorator set aside), over a fake Streamlit
    and a fake clock.  Returns (what it did, in order; the function)."""
    fn = copy.deepcopy(_fn_node("_engine_live_panel"))
    fn.decorator_list = []
    did = []
    st = _FakeSt(state, did)
    mod = types.ModuleType("run_panel")
    mod.__dict__.update(
        st=st, ENGINE_TICK_S=_tick_s(),
        time=types.SimpleNamespace(
            monotonic=lambda: now,
            sleep=lambda s: did.append(f"wait {s}")),
        _engine_poll=lambda job, timeout_s: poll,
        _engine_tail=lambda job, n=1, stream="err": list(tail),
        _flag_cancel=lambda key: None)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "app.py", "exec"),
         mod.__dict__)
    return did, st, mod._engine_live_panel


DRAWN = [("info", "⏳ Generating the splice report: 41s elapsed. You can leave "
                  "this open or keep working; cancel below if needed."),
         ("caption", "current step · reading fiber 12"),
         ("button", "Cancel run")]


def test_a_redraw_on_its_own_draws_waits_then_asks_for_the_next():
    """Seconds, step, Cancel; THEN the wait; then the next redraw of the
    panel alone.  Waiting before drawing would leave the last redraw on
    screen to fade, which is the fault this fixes."""
    did, _st, panel = _panel({"sr_job": {"started": 58.4}}, "running")
    with pytest.raises(_Rerun) as asked:
        panel("sr", "Generating the splice report", 1200)
    assert asked.value.scope == "fragment"
    assert did == DRAWN + [f"wait {_tick_s()}", "rerun fragment"]


def test_drawn_by_the_page_it_draws_once_and_returns():
    """Streamlit does not allow a panel-only redraw to be asked for from a
    page pass; the browser's timer asks for the first one."""
    did, st, panel = _panel({"sr_job": {"started": 58.4},
                             "sr_panel_once": True}, "running")
    panel("sr", "Generating the splice report", 1200)
    assert did == DRAWN
    assert "sr_panel_once" not in st.session_state, "the mark is used once"


@pytest.mark.parametrize("state, poll", [
    ({"sr_job": {"started": 0.0}}, "done"),
    ({"sr_job": {"started": 0.0}}, "timeout"),
    ({"sr_job": {"started": 0.0}, "sr_cancel": True}, "running"),
    ({}, "running"),
    ({"sr_job": {"started": 0.0}, "sr_panel_once": True}, "done"),
], ids=["finished", "timed out", "cancel pressed", "run gone",
        "finished while the page was drawing"])
def test_a_redraw_hands_back_to_the_page(state, poll):
    """Finished, timed out, cancelled or gone: the panel draws nothing and
    asks for the whole page, which is where those are dealt with."""
    did, _st, panel = _panel(state, poll)
    with pytest.raises(_Rerun) as asked:
        panel("sr", "Generating the splice report", 1200)
    assert asked.value.scope == "app"
    assert did == ["rerun app"]


# ── how it is wired ──────────────────────────────────────────────────────
def test_the_panel_is_a_fragment_and_the_browser_timer_starts_it():
    deco = [ast.unparse(d) for d in _fn_node("_engine_live_panel").decorator_list]
    assert deco == ["st.fragment(run_every=ENGINE_TICK_S)"], deco


def test_the_seconds_cannot_skip_a_number():
    """The panel prints whole seconds.  Redrawn once a second, a late redraw
    would jump from 5 to 7; twice a second it cannot."""
    assert 0 < _tick_s() <= 0.5


def test_the_driver_does_not_rerun_the_page_to_move_the_count():
    code = _fn_code("run_engine_live")
    assert "time.sleep" not in code
    assert "st.rerun" not in code and "st.stop" not in code
    running = code.split("if state == 'running':", 1)[1]
    lines = [ln.strip() for ln in running.strip().split("\n")]
    assert lines[:3] == ["st.session_state[f'{prefix}_panel_once'] = True",
                         "_engine_live_panel(prefix, running_title, timeout_s)",
                         "return None"], lines[:4]


def test_all_three_report_pages_use_the_same_panel():
    assert SRC.count("run_engine_live(") >= 4      # the def + three pages
    assert SRC.count("_engine_live_panel(") == 2   # the def + the one call
    assert "'Cancel run'" in _fn_code("_engine_live_panel")
    assert SRC.count("'Cancel run'") == 1


@pytest.mark.parametrize("page, prefix", [
    ("page_splice_report", "_p"), ("page_unidirectional", "uni"),
    ("page_duplicate_check", "ss")])
def test_each_page_stops_at_the_panel_while_its_run_is_going(page, prefix):
    """run_engine_live hands back None while the run is going.  Each page
    returns on that, before anything that shows a report."""
    code = _fn_code(page)
    after = code.split("run_engine_live(", 1)[1]
    if page == "page_splice_report":
        guard = "if proc is None and f'{_p}_job' in st.session_state:\n"
        assert guard in after
        assert after.split(guard, 1)[1].lstrip().startswith("return")
        assert after.index(guard) < after.index("_sr_start_next_queued(")
    else:
        assert "if proc is None:\n" in after
        assert (after.split("if proc is None:\n", 1)[1].lstrip()
                .startswith("return"))
        assert after.index("if proc is None:") < after.index("_parse_manifest(")

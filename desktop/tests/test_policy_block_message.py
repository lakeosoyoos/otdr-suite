"""A file Windows REFUSED TO LOAD is not a file that went missing.

WHAT A TECH HIT.  Five reports off one machine, all one event:

    ImportError: DLL load failed while importing indexers:
    An Application Control policy has blocked this file.

Application Control blocked pandas' compiled `indexers.pyd` inside our bundle.
The same block then surfaced as `AttributeError: module 'pandas' has no
attribute 'DataFrame'`, because a pandas import that dies partway leaves a
half-built module behind for the next caller to trip over.  It landed on the
settings panels of three pages because a custom-component call makes Streamlit
run `is_dataframe_like()` on each argument, and that does `import pandas`.

WHY THIS FILE EXISTS.  #133 shipped one answer for every ImportError: "a file
this app needs is missing" plus a Repair and restart button.  For a blocked DLL
that advice is a loop with no exit — repair re-downloads ENGINE files, while the
blocked file lives inside the installed exe, which no update we publish ever
rewrites.  The two failures look alike and take opposite remedies, so the app
has to tell them apart before it offers anything.
"""
from __future__ import annotations

import ast
import types

from conftest import APP_PATH

APP_SRC = open(APP_PATH, encoding="utf-8").read()

REAL_BLOCK = (
    "ImportError: DLL load failed while importing indexers: "
    "An Application Control policy has blocked this file.")
REAL_MISSING = "ModuleNotFoundError: No module named 'sor_reader324802a'"


class _FakeExpander:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeStreamlit:
    """Records what the page would show, so the branch is tested by what a tech
    ends up reading rather than by grepping the source."""

    def __init__(self):
        self.shown = []
        self.buttons = []
        self.stopped = False

    def _say(self, *args):
        self.shown += [a for a in args if isinstance(a, str)]

    error = write = title = caption = code = markdown = _say

    def button(self, label, **kw):
        self.buttons.append(label)
        return False

    def expander(self, label, **kw):
        self._say(label)
        return _FakeExpander()

    def set_page_config(self, **kw):
        pass

    def stop(self):
        self.stopped = True


def _load(name, **namespace):
    """Exec the named app.py function, plus the module constants it reads, in a
    bare module — no Streamlit runtime, no engine import."""
    tree = ast.parse(APP_SRC)
    wanted = {name, "_blocked_by_policy"}
    body = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name in wanted)
            or (isinstance(n, ast.Assign)
                and any(getattr(t, "id", "").startswith("_POLICY")
                        for t in n.targets))]
    assert any(isinstance(n, ast.FunctionDef) and n.name == name for n in body), name
    mod = types.ModuleType("app_policy")
    mod.__dict__.update(namespace)
    exec(compile(ast.Module(body=body, type_ignores=[]), "app.py", "exec"),
         mod.__dict__)
    return mod


# ─── the classifier ──────────────────────────────────────────────────────

def test_the_real_block_is_recognised():
    blocked = _load("_blocked_by_policy")._blocked_by_policy
    assert blocked(REAL_BLOCK)
    assert blocked("An Application Control policy has blocked this file.")
    assert blocked("ImportError: DLL load failed while importing _multiarray")


def test_a_missing_engine_file_is_not_a_block():
    """The two must not collapse into each other: this one IS repairable."""
    blocked = _load("_blocked_by_policy")._blocked_by_policy
    assert not blocked(REAL_MISSING)
    assert not blocked("")
    assert not blocked(None)


# ─── what the tech reads ─────────────────────────────────────────────────

def test_a_blocked_file_is_not_offered_a_repair():
    """THE REGRESSION.  Repair rewrites engine files; the blocked file is in
    the installed exe.  Offering it sends the tech round a loop with no exit."""
    st = _FakeStreamlit()
    mod = _load("_engine_policy_block_page", st=st)
    mod._engine_policy_block_page(ImportError(REAL_BLOCK))

    text = " ".join(st.shown)
    assert "Repair and restart" not in st.buttons, st.buttons
    assert "blocked" in text.lower()
    assert "repairing or re-downloading will not clear it" in text.lower()
    assert "newest version" in text.lower(), "the tech is not told what to try"
    assert st.stopped


def test_the_blocked_page_hands_IT_something_to_act_on():
    st = _FakeStreamlit()
    mod = _load("_engine_policy_block_page", st=st)
    mod._engine_policy_block_page(ImportError(REAL_BLOCK))
    text = " ".join(st.shown)
    assert "CodeIntegrity" in text, "IT needs the log that names the file"
    assert "For your IT department" in text


def test_a_blocked_subprocess_gets_the_same_answer_not_a_repair():
    st = _FakeStreamlit()
    mod = _load("_engine_damaged_notice", st=st,
                _request_repair=lambda: True,
                _relaunch_and_exit=lambda: True,
                _render_restart_watchdog=lambda *a, **k: None)
    assert mod._engine_damaged_notice(f"Traceback...\n{REAL_BLOCK}", 'ss') is True
    assert "Repair and restart" not in st.buttons, st.buttons
    assert "blocked" in " ".join(st.shown).lower()


def test_a_missing_file_subprocess_still_gets_the_repair():
    """#133's behaviour must survive the split — this one IS repairable."""
    st = _FakeStreamlit()
    mod = _load("_engine_damaged_notice", st=st,
                _request_repair=lambda: True,
                _relaunch_and_exit=lambda: True,
                _render_restart_watchdog=lambda *a, **k: None)
    assert mod._engine_damaged_notice(f"Traceback...\n{REAL_MISSING}", 'ss') is True
    assert "Repair and restart" in st.buttons
    assert "missing from this computer" in " ".join(st.shown)


def test_an_unrelated_failure_is_left_alone():
    """Not every engine failure is ours to reinterpret."""
    st = _FakeStreamlit()
    mod = _load("_engine_damaged_notice", st=st)
    assert mod._engine_damaged_notice("ValueError: no fibers found", 'ss') is False
    assert st.shown == []


# ─── the boot path picks the right page ──────────────────────────────────

def test_the_boot_handler_checks_for_a_block_first():
    """Order matters: _engine_file_missing_page would otherwise claim a blocked
    file is missing and offer the repair that cannot work."""
    tree = ast.parse(APP_SRC)
    handler = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Try)
                and any(isinstance(n, ast.Import)
                        and any(a.name == "trace_server" for a in n.names)
                        for n in node.body)):
            handler = node.handlers[0]
    assert handler is not None, "the guarded boot import is gone"

    calls = [n.func.id for n in ast.walk(handler)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert calls.index("_blocked_by_policy") < calls.index("_engine_file_missing_page")
    assert "_engine_policy_block_page" in calls


# ─── the panels that fail open still name the cause ──────────────────────

def test_every_guarded_settings_panel_explains_a_block():
    """The screen the tech was actually looking at.  These panels are guarded
    so the page survives, which is right — but "unavailable, details sent to
    support" left him with nothing to do while support was us, hours later."""
    src = APP_SRC
    guards = [i for i, line in enumerate(src.splitlines())
              if 'settings panel render' in line and 'report_error' in line]
    assert len(guards) == 3, f"expected 3 guarded panels, found {len(guards)}"
    lines = src.splitlines()
    for i in guards:
        window = "\n".join(lines[max(0, i - 6):i + 1])
        assert '_policy_block_caption(_exc)' in window, (
            "a guarded settings panel does not explain a blocked file:\n" + window)


def test_the_caption_says_nothing_when_the_failure_is_ordinary():
    st = _FakeStreamlit()
    mod = _load("_policy_block_caption", st=st)
    mod._policy_block_caption(ValueError("no fibers found"))
    assert st.shown == []
    mod._policy_block_caption(ImportError(REAL_BLOCK))
    assert "blocked" in " ".join(st.shown).lower()

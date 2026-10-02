"""The trace-folder block (drop pickup, Clear Traces, the box labels, the
Secret Sauce folder build) and the top bar run from MODULE LEVEL, part-way
through the script, before any page draws.  Until 2026-10-01 this was the
sidebar's `with st.sidebar:` block, whose 'Load into all tools' click ran
`_load_span`.  Streamlit executes app.py top to bottom on every rerun, so any
name that code (or a helper it calls) resolves unconditionally must already be
defined ABOVE the statement that runs it -- otherwise every run is a
NameError.  PR #177 (2026-09-12) broke exactly this by calling
`_site_names_for`, defined ~1,300 lines lower.  This test walks the AST so the
next such regression fails in CI instead of in the field.
"""
import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
APP = REPO_ROOT / "app.py"


def _click_path_block(tree, src):
    """The module-level statements (not defs or classes) from the top bar's
    section to the Traces page: the code that runs on every click, part-way
    through the script, before the page is drawn."""
    first = src[:src.index("# ─── Top bar")].count("\n") + 1
    last = src[:src.index("\ndef page_traces(")].count("\n") + 1
    block = [n for n in tree.body if first <= n.lineno < last
             and not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    assert block, "module-level trace-folder block not found"
    # the work the sidebar block did is all still in it
    called = {n.func.id for s in block for n in ast.walk(s)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    for need in ("_render_top_nav", "_clear_traces", "_label_drop_boxes",
                 "_follow_viewer_folders", "_take_panel_ss_folder",
                 "_trace_folders_changed", "_panel_boxes"):
        assert need in called, need
    return block


def _module_defs(tree):
    """{name: lineno} for every module-level def / class / assignment."""
    defs = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            defs[node.name] = node.lineno
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        defs[n.id] = node.lineno
    return defs


def test_the_click_path_only_uses_names_defined_above_it():
    src = APP.read_text(encoding="utf-8")
    tree = ast.parse(src)
    defs = _module_defs(tree)
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    late = {}
    for stmt in _click_path_block(tree, src):
        # Every function this statement calls, transitively.
        todo = [n.id for n in ast.walk(stmt)
                if isinstance(n, ast.Name) and n.id in funcs]
        for n in ast.walk(stmt):
            if (isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                    and n.id in defs and defs[n.id] > stmt.lineno):
                late[n.id] = ("<module>", defs[n.id])
        seen = set()
        while todo:
            fn = todo.pop()
            if fn in seen:
                continue
            seen.add(fn)
            for n in ast.walk(funcs[fn]):
                if not (isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)):
                    continue
                if n.id in defs and defs[n.id] > stmt.lineno:
                    late[n.id] = (fn, defs[n.id], stmt.lineno)
                if n.id in funcs:
                    todo.append(n.id)
    assert not late, (
        f"names used on the click path (the module-level trace-folder block "
        f"under the top bar) but defined AFTER the statement that runs them "
        f"(a NameError on every run): {late}")

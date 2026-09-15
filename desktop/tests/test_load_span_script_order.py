"""The sidebar's 'Load into all tools' click runs `_load_span` from MODULE
LEVEL, part-way through the script.  Streamlit executes app.py top to bottom
on every rerun, so any name `_load_span` (or a helper it calls) resolves
unconditionally must already be defined ABOVE that module-level block —
otherwise every click is a NameError.  PR #177 (2026-09-12) broke exactly this
by calling `_site_names_for`, defined ~1,300 lines lower.  This test walks the
AST so the next such regression fails in CI instead of in the field.
"""
import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
APP = REPO_ROOT / "app.py"

def _sidebar_block(tree):
    """The module-level `with st.sidebar:` node — the code that runs on a
    sidebar click part-way through the script."""
    for node in tree.body:
        if isinstance(node, ast.With):
            for item in node.items:
                e = item.context_expr
                if (isinstance(e, ast.Attribute) and e.attr == "sidebar"
                        and isinstance(e.value, ast.Name) and e.value.id == "st"):
                    return node
    raise AssertionError("module-level `with st.sidebar:` block not found")


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


def test_sidebar_click_path_only_uses_names_defined_above_it():
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    block = _sidebar_block(tree)
    defs = _module_defs(tree)
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    # Every function the sidebar block calls, transitively.
    todo = [n.id for n in ast.walk(block)
            if isinstance(n, ast.Name) and n.id in funcs]
    seen, late = set(), {}
    while todo:
        fn = todo.pop()
        if fn in seen:
            continue
        seen.add(fn)
        for n in ast.walk(funcs[fn]):
            if not (isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)):
                continue
            if n.id in defs and defs[n.id] > block.lineno:
                late[n.id] = (fn, defs[n.id])
            if n.id in funcs:
                todo.append(n.id)
    assert not late, (
        f"names used on the sidebar click path but defined AFTER the "
        f"module-level `with st.sidebar:` block at line {block.lineno} "
        f"(a NameError on every click): {late}")

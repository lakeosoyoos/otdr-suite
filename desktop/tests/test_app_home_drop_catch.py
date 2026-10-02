"""OTDR App: a file dropped on the home screen is refused, not saved to
Downloads (Robert, 2026-10-01).

main #466 put the hub's drop catcher (_install_hub_drop_catch) on every page.
The App's home screen (Quick Analysis / Start New Project / Open Recent
Project) stops the script before that call, so a drop there went to the
browser's Downloads.  The home screen now installs it too, ahead of its
st.stop().  With no Viewer frame on the page, the catcher only refuses the
drop (dropEffect none).
"""
import ast

from conftest import REPO_ROOT

SRC = (REPO_ROOT / 'app.py').read_text(encoding='utf-8')
TREE = ast.parse(SRC)


def _home_gate():
    """The module-level `if` that draws the home screen and stops."""
    for node in TREE.body:
        if isinstance(node, ast.If):
            calls = [n.func.id for n in ast.walk(node)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
            if '_render_home' in calls:
                return node
    raise AssertionError('the home screen gate (_render_home) is not in app.py')


def _calls_in_order(stmts):
    """Top-level call names of these statements, in order (st.stop as 'st.stop')."""
    out = []
    for s in stmts:
        if isinstance(s, ast.Expr) and isinstance(s.value, ast.Call):
            f = s.value.func
            if isinstance(f, ast.Name):
                out.append(f.id)
            elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                out.append(f'{f.value.id}.{f.attr}')
    return out


def test_the_home_screen_installs_the_drop_catcher_before_it_stops():
    order = _calls_in_order(_home_gate().body)
    assert 'st.stop' in order, order
    assert '_install_hub_drop_catch' in order, order
    assert order.index('_install_hub_drop_catch') < order.index('st.stop'), order
    # After the page is drawn, so its zero-height frame adds nothing above it.
    assert order.index('_render_home') < order.index('_install_hub_drop_catch'), order


def test_the_catcher_is_defined_before_the_home_screen_runs():
    gate = _home_gate()
    defs = [n for n in TREE.body
            if isinstance(n, ast.FunctionDef) and n.name == '_install_hub_drop_catch']
    assert defs and defs[0].lineno < gate.lineno


def test_every_other_page_still_installs_it():
    """main's call after the gate stays (Quick Analysis and a project)."""
    gate = _home_gate()
    after = [n for n in TREE.body if n.lineno > gate.lineno]
    assert '_install_hub_drop_catch' in _calls_in_order(after)

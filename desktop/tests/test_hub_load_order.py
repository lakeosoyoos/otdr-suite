"""The hub is a Streamlit script: it runs top to bottom on every rerun, and
the sidebar's span loader (_load_span) is CALLED at module level about a
third of the way down.  Anything that call reaches must already be defined
above it.

#177 broke this: _load_span called _site_names_for, defined 1,300 lines
later next to the profile tables, so every span load raised
    NameError: name '_site_names_for' is not defined
and the fleet shipped it (runs 424-447).  The unit tests import app as a
module and never trigger the loader, so nothing caught it.

This test walks the loader's source and checks that every module-level
name it uses is defined ABOVE the module-level call.  It is a static check
on app.py, so it runs in CI without Streamlit.
"""
from __future__ import annotations

import ast
import os

from conftest import SPLICEREPORT_DIR

APP = os.path.join(os.path.dirname(str(SPLICEREPORT_DIR)), 'app.py')


def _module():
    with open(APP, encoding='utf-8') as fh:
        return ast.parse(fh.read(), APP)


def _top_level_defs(mod):
    """name -> line where the module defines it (functions, classes,
    assignments), first definition wins."""
    out = {}
    for node in mod.body:
        names = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [node.name]
        elif isinstance(node, (ast.Assign,)):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.append(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [(a.asname or a.name).split('.')[0] for a in node.names]
        for n in names:
            out.setdefault(n, node.lineno)
    return out


def _first_module_level_call(mod, fname):
    """Line of the first call to `fname` that is NOT inside a def/class."""
    calls = []

    class V(ast.NodeVisitor):
        def visit_FunctionDef(self, node):   # do not descend into defs
            return
        visit_AsyncFunctionDef = visit_FunctionDef
        visit_ClassDef = visit_FunctionDef

        def visit_Call(self, node):
            if isinstance(node.func, ast.Name) and node.func.id == fname:
                calls.append(node.lineno)
            self.generic_visit(node)
    V().visit(mod)
    return min(calls) if calls else None


def _names_used_in(func_node):
    return {n.id for n in ast.walk(func_node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}


def test_span_loader_only_uses_names_defined_above_its_module_level_call():
    mod = _module()
    defs = _top_level_defs(mod)
    loader = next(n for n in mod.body if isinstance(n, ast.FunctionDef) and n.name == '_load_span')
    call_line = _first_module_level_call(mod, '_load_span')
    assert call_line, "the loader is expected to be called from the sidebar at module level"
    late = sorted((name, defs[name]) for name in _names_used_in(loader)
                  if name in defs and defs[name] > call_line)
    assert not late, ("_load_span reaches names defined AFTER its module-level call at line %d; "
                      "they raise NameError on every span load: %s" % (call_line, late))


def test_site_names_are_still_derived_on_the_report_page():
    """The identifier-based names moved to the Splice Report page; make sure
    that path still exists and the loader no longer pins the signature that
    would stop it from running."""
    src = open(APP, encoding='utf-8').read()
    assert "_ila_a, _ila_b = _site_names_for(dir_a, dir_b)" in src
    loader_src = src[src.index("def _load_span("):src.index("def ", src.index("def _load_span(") + 10)]
    assert "_site_names_for(" not in loader_src          # a CALL; the comment may name it
    assert "st.session_state['sr_site_src'] = (dir_a, dir_b)" not in loader_src


def test_site_names_survive_the_viewer_shadowing_json_reader(tmp_path, monkeypatch):
    """In the hub process the Viewer's json_reader.py (no span_site_names)
    sits in sys.modules first.  A by-name import then fails and the site
    boxes silently fall back to "A"/"B" -- which is what the hub showed for
    every IIG span until the click-through of 2026-09-15.  The engine's
    reader must be loaded by path so the shadow cannot reach it."""
    import importlib, json, sys
    sys.path.insert(0, str(SPLICEREPORT_DIR))
    importlib.import_module('splicereportmatchexfo')  # engine first, as the other hub tests do
    app = importlib.import_module('app')
    # In the hub process the Viewer's trace_server imports ITS json_reader by
    # name before the report page runs.  The test process loads the engine's
    # copy first, so stand the shadow up by hand: a `json_reader` with no
    # span_site_names, exactly what the hub had in sys.modules.
    import types
    monkeypatch.setitem(sys.modules, 'json_reader', types.ModuleType('json_reader'))
    a, b = tmp_path / 'AB', tmp_path / 'BA'
    for d, name in ((a, 'AB'), (b, 'BA')):
        d.mkdir()
        doc = {"brief": {"Identifiers": [
            {"Name": "Cable ID", "Value": "MSO401-MSO402-OSP-0432F-01"},
            {"Name": "Segment", "Value": "Project=P|Span=Span 17|Segment=Superior, MT to Frenchtown, MT"},
            {"Name": "A End", "Value": "LOC=MSO401|FTP=42"},
            {"Name": "Z End", "Value": "LOC=MSO402|FTP=42"}]},
               "FiberInformation": {}}
        for i in (1, 2, 3):
            (d / f"MSO401-MSO402-OSP-0432F-01-{i:04d}-{name}.json").write_text(json.dumps(doc), encoding='utf-8')
    names = app._site_names_for(str(a), str(b), profile_name="AWS / IIG MT.1085")
    assert names == ('Superior MSO401', 'Frenchtown MSO402'), names

"""In-app 'Check for updates' — helper contracts.

The button is deliberately thin: it compares the LIVE manifest's version to
the running engine (display only, nothing trusted) and restarts the app so
the frozen launcher's signed boot path — the only code that ever applies an
update — does the actual work.  These tests lock the version-parse helper and
the security invariant that no fetch-and-apply logic exists app-side.
"""
import ast
import os
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))


def _load_helper(name):
    src = open(os.path.join(ROOT, 'app.py'), encoding='utf-8').read()
    tree = ast.parse(src)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
              and n.name == name)
    mod = types.ModuleType('upd')
    exec(compile(ast.Module(body=[fn], type_ignores=[]), 'app.py', 'exec'),
         mod.__dict__)
    return getattr(mod, name)


def test_parse_engine_version_applied_update():
    p = _load_helper('_parse_engine_version')
    assert p('build 79 (2026-07-17 11:57 PDT)',
             'update 94 applied 2026-07-22 16:40 PDT') == 94


def test_parse_engine_version_bundled_falls_back_to_app_build():
    p = _load_helper('_parse_engine_version')
    assert p('build 79 (2026-07-17 11:57 PDT)', 'bundled') == 79
    assert p('build 93 (2026-07-22)', 'bundled (auto-update disabled)') == 93


def test_parse_engine_version_dev_is_none():
    p = _load_helper('_parse_engine_version')
    assert p('dev', 'dev') is None
    assert p(None, None) is None


def test_no_apply_logic_app_side():
    """Security invariant: the app may READ the manifest version for display,
    but signature verification and update application live ONLY in the frozen
    launcher (it holds the trust anchor).  No Ed25519 / signature machinery
    may appear in app.py."""
    src = open(os.path.join(ROOT, 'app.py'), encoding='utf-8').read()
    for banned in ('Ed25519', 'update_manifest.json.sig', 'UPDATE_PUBLIC_KEY',
                   '_try_auto_update'):
        assert banned not in src, f'{banned} must not exist app-side'

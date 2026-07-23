"""Uni settings box — regression tests.

The SR settings panel's rows are all bidirectional thresholds; the uni
engine reads none of them.  The uni page gets its own settings box driven
by _UNI_SETTINGS (UNI_* engine globals + RIBBON_SIZE).  Locks: every spec
row maps to a real engine global, spec DEFAULTS match the engine values
(drift lock — a panel showing stale defaults would lie to the tech), and
the page passes the overrides into uni_cmd.
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, 'splicereport'))

import splicereportmatchexfo as E  # noqa: E402


def _spec():
    """Extract _UNI_SETTINGS from app.py WITHOUT importing app.py (it
    boots Streamlit).  The spec is a pure literal, so ast.literal_eval
    on the assignment works."""
    src = open(os.path.join(ROOT, 'app.py'), encoding='utf-8').read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(getattr(t, 'id', '') == '_UNI_SETTINGS'
                        for t in node.targets)):
            return ast.literal_eval(node.value)
    raise AssertionError('_UNI_SETTINGS not found in app.py')


def test_every_spec_row_is_a_real_engine_global():
    for g, *_ in _spec():
        assert hasattr(E, g), f'{g} not an engine global'


def test_spec_defaults_match_engine_values():
    """Drift lock: the panel's displayed defaults must BE the engine's
    current defaults, or the box lies about what an untouched run does."""
    for g, _label, default, *_ in _spec():
        assert getattr(E, g) == default, (g, getattr(E, g), default)


def test_spec_types_and_bounds_sane():
    for g, _label, default, lo, hi, step, is_int, help_ in _spec():
        assert lo <= default <= hi, (g, lo, default, hi)
        if is_int:
            assert isinstance(default, int), g
        assert isinstance(help_, str) and help_


def test_page_wires_panel_into_uni_cmd():
    src = open(os.path.join(ROOT, 'app.py'), encoding='utf-8').read()
    assert 'uni_overrides = _render_uni_settings_panel()' in src
    assert 'overrides=uni_overrides' in src
    # panel renders on the uni page BEFORE the Run button
    body = src.split('def page_unidirectional', 1)[1]
    assert body.index('_render_uni_settings_panel') < body.index(
        "st.button('Run unidirectional report'")

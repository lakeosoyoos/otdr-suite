"""Uni settings panel — regression tests.

Most OTDR Settings rows are bidirectional thresholds the uni engine never
reads.  The uni page gets its own rows, driven by _UNI_ROWS (UNI_* engine
globals + RIBBON_SIZE) and rendered by the SAME custom component as the
Splice Report / FR panels, in 'knobs' mode.  The three UNI_* globals that
mean the same thing as an OTDR Settings row (one-direction connector gate,
mid-span reflectance band and ceiling) are driven from there instead
(Robert 2026-09-28), so each global still has exactly one control.

Locks: every spec slot maps to a real engine global, spec DEFAULTS match
the engine values (drift lock — a panel showing stale defaults would lie
to the tech), band rows are genuinely ordered, and the page passes the
overrides into uni_cmd.
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, 'splicereport'))

import splicereportmatchexfo as E  # noqa: E402

APP = os.path.join(ROOT, 'app.py')


def _literal(name):
    """Extract a pure-literal assignment from app.py WITHOUT importing it
    (importing boots Streamlit)."""
    src = open(APP, encoding='utf-8').read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(getattr(t, 'id', '') == name for t in node.targets)):
            return ast.literal_eval(node.value)
    raise AssertionError(f'{name} not found in app.py')


def _rows():
    return _literal('_UNI_ROWS')


def test_every_slot_is_a_real_engine_global():
    for row in _rows():
        for slot, g in row['globals'].items():
            assert hasattr(E, g), f'{g} ({row["key"]}.{slot}) not an engine global'


def test_defaults_match_engine_values():
    """Drift lock: the panel's displayed defaults must BE the engine's
    current defaults, or the panel lies about what an untouched run does."""
    for row in _rows():
        for slot, g in row['globals'].items():
            assert getattr(E, g) == row['defaults'][slot], (
                g, getattr(E, g), row['defaults'][slot])


def _from_otdr_settings():
    """UNI_* globals the OTDR Settings drive: {global: (row key, slot)}."""
    m = _literal('_OTDR_KEY_TO_UNI_GLOBAL')
    return {g: (key, slot) for key, (slot, g) in m.items()}


def test_every_global_is_exposed_exactly_once():
    """No knob wired to two rows, and none silently dropped in the rewrite.
    The Uni box and the OTDR Settings rows Uni reads count together: a global
    in both would have two controls fighting over one --overrides key."""
    seen = [g for row in _rows() for g in row['globals'].values()]
    assert len(seen) == len(set(seen)), 'a global is exposed twice'
    shared = set(_from_otdr_settings())
    assert not (set(seen) & shared), set(seen) & shared
    # Every UNI_* the engine defines and the two boxes are meant to carry.
    expected = {
        'UNI_BEND_THRESHOLD', 'UNI_MIN_POP_SPLICE', 'UNI_CLOSURE_MATCH_KM',
        'UNI_REFL_FLOOR_DB', 'UNI_REFL_CEIL_DB', 'UNI_BREAK_MIN_KM',
        'UNI_BREAK_PREMATURE_KM', 'UNI_END_REGION_KM',
        'UNI_DAMAGE_ZONE_BREAK_KM', 'UNI_PREBREAK_CONFIRM_DB',
        'UNI_PREBREAK_MEMBER_DB', 'UNI_PREBREAK_STORED_DB',
        'UNI_LANDMARK_MATCH_KM', 'UNI_LANDMARK_DEMOTE_KM', 'RIBBON_SIZE',
        'UNI_CONN_LOSS_DB',
    }
    assert set(seen) | shared == expected
    assert shared == {'UNI_CONN_LOSS_DB', 'UNI_REFL_FLOOR_DB', 'UNI_REFL_CEIL_DB'}


def test_row_kinds_and_slots_agree():
    for row in _rows():
        slots = set(row['globals'])
        assert set(row['defaults']) == slots, row['key']
        if row['kind'] == 'range':
            assert slots == {'low', 'high'}, row['key']
        else:
            assert row['kind'] == 'scalar' and slots == {'value'}, row['key']


def test_bands_are_ordered_low_then_high():
    """A band's low end must not sit above its high end, or the two number
    boxes under Low / High mean the opposite of what they say.  A high of 0
    is the 'no ceiling' sentinel and is exempt."""
    for row in _rows():
        if row['kind'] != 'range':
            continue
        lo, hi = row['defaults']['low'], row['defaults']['high']
        if hi == 0:
            continue
        assert lo <= hi, (row['key'], lo, hi)


def test_bounds_and_types_sane():
    for row in _rows():
        for slot, d in row['defaults'].items():
            assert row['min'] <= d <= row['max'], (row['key'], slot, d)
        if row['int']:
            assert all(isinstance(d, int) for d in row['defaults'].values()), row['key']
        assert isinstance(row['help'], str) and row['help']
        assert isinstance(row['label'], str) and row['label']
        assert row['unit'] in ('dB', 'dB/km', 'km', 'fibers', ''), row['unit']


def test_reflectance_band_comes_from_the_otdr_settings_band():
    """The knob the boss reaches for after a span with many weak
    reflectances: the Uni band's floor is the OTDR Settings band's weak end
    (the same floor the bidirectional report flags from) and its ceiling is
    the ceiling row.  Fail on the band row is the bidirectional FAIL/WARN
    split, which Uni does not have."""
    m = _from_otdr_settings()
    assert m['UNI_REFL_FLOOR_DB'] == ('midspan_reflectance', 'warning')
    assert m['UNI_REFL_CEIL_DB'] == ('midspan_refl_ceiling', 'fail')
    assert m['UNI_CONN_LOSS_DB'] == ('unidir_connector_loss', 'fail')


def test_page_wires_panel_into_uni_cmd():
    src = open(APP, encoding='utf-8').read()
    assert 'uni_overrides = _render_uni_settings_panel()' in src
    assert 'overrides=uni_overrides' in src
    body = src.split('def page_unidirectional', 1)[1]
    assert body.index('_render_uni_settings_panel') < body.index(
        "st.button('Run Unidirectional Report'")


def test_panel_uses_the_shared_component_in_knobs_mode():
    """The whole point of the rewrite: one component, three report pages."""
    src = open(APP, encoding='utf-8').read()
    panel = src.split('def _render_uni_settings_panel', 1)[1].split('\ndef ', 1)[0]
    assert 'otdr_settings_component' in panel
    assert "mode='knobs'" in panel
    assert 'st.number_input' not in panel, 'back to bare Streamlit widgets'


def test_component_supports_both_modes():
    comp = open(os.path.join(ROOT, 'components', 'otdr_settings', '__init__.py'),
                encoding='utf-8').read()
    assert 'mode' in comp and 'knobs' in comp
    html = open(os.path.join(ROOT, 'components', 'otdr_settings', 'index.html'),
                encoding='utf-8').read()
    # threshold layout must survive untouched alongside the new one
    for token in ('Description', 'Apply', 'Fail', 'Warning',
                  'Setting', 'Low', 'High'):
        assert token in html, token

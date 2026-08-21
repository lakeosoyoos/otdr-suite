"""Connector & launch settings panel — regression tests.

Task #110: every knob on the launch / box-connector path has to be reachable
from the Splice Report settings panel, and each one has to actually WIRE —
a row that renders but changes nothing is worse than no row at all.  These
tests pin each row to its engine global, lock the displayed defaults against
engine drift, and prove the page passes the panel's values into the run.

They also cover the three engine behaviours the panel now exposes:
the tailbox outlier margin, the bidirectional-average gate, and the guard
that used to let one knob switch another one off.
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, 'splicereport'))

import pytest  # noqa: E402
import splicereportmatchexfo as E  # noqa: E402

APP = os.path.join(ROOT, 'app.py')


def _literal(name):
    """Pull a pure-literal assignment out of app.py WITHOUT importing it
    (importing boots Streamlit)."""
    tree = ast.parse(open(APP, encoding='utf-8').read())
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(getattr(t, 'id', '') == name for t in node.targets)):
            return ast.literal_eval(node.value)
    raise AssertionError(f'{name} not found in app.py')


def _rows():
    return _literal('_CONN_ROWS')


# ── the panel spec ───────────────────────────────────────────────────

def test_every_slot_is_a_real_engine_global():
    for row in _rows():
        for slot, g in row['globals'].items():
            assert hasattr(E, g), f'{g} ({row["key"]}.{slot}) is not an engine global'


def test_defaults_match_engine_values():
    """Drift lock: a panel showing stale defaults lies about what an
    untouched run does."""
    for row in _rows():
        for slot, g in row['globals'].items():
            engine = getattr(E, g)
            shown = row['defaults'][slot]
            if engine is None:      # a rule that ships disabled reads 0 here
                assert shown == 0, (g, shown)
            else:
                assert engine == shown, (g, engine, shown)


def test_every_global_is_exposed_exactly_once():
    seen = [g for row in _rows() for g in row['globals'].values()]
    assert len(seen) == len(set(seen)), 'a global is exposed twice'
    assert set(seen) == {
        'LAUNCH_CONN_LOSS_MIN_DB', 'LAUNCH_CONN_UNI_MIN_DB',
        'LAUNCH_CONN_AVG_MIN_DB', 'LAUNCH_CONN_CONFIRM_TOL_DB',
        'TAILBOX_OUTLIER_DB', 'LAUNCH_CONN_FAR_WINDOW_KM',
        'LAUNCH_CONN_REEL_SLACK_KM', 'LAUNCH_STEP_GUARD_KM',
        'LAUNCH_HIGH_LOSS_DB',
    }


def test_no_row_points_at_a_dead_constant():
    """LAUNCH_REFL_OUTLIER_DB and LAUNCH_NO_FIRST_SPLICE_TOL_KM are defined
    by the engine and read by nothing.  Giving either a row would ship a
    knob that does nothing — the exact failure this panel must not have."""
    src = open(os.path.join(ROOT, 'splicereport',
                            'splicereportmatchexfo.py'), encoding='utf-8').read()
    exposed = {g for row in _rows() for g in row['globals'].values()}
    for dead in ('LAUNCH_REFL_OUTLIER_DB', 'LAUNCH_NO_FIRST_SPLICE_TOL_KM'):
        reads = [ln for ln in src.splitlines()
                 if dead in ln and not ln.lstrip().startswith('#')
                 and not ln.startswith(dead)]
        assert not reads, f'{dead} is read now — it may deserve a row: {reads}'
        assert dead not in exposed, f'{dead} is dead code; it must not get a row'


def test_row_kinds_and_slots_agree():
    for row in _rows():
        slots = set(row['globals'])
        assert set(row['defaults']) == slots, row['key']
        assert row['kind'] == 'scalar' and slots == {'value'}, row['key']


def test_bounds_and_help_sane():
    for row in _rows():
        for slot, d in row['defaults'].items():
            assert row['min'] <= d <= row['max'], (row['key'], slot, d)
        assert isinstance(row['help'], str) and len(row['help']) > 40, row['key']
        assert isinstance(row['label'], str) and row['label']
        assert row['unit'] in ('dB', 'km'), (row['key'], row['unit'])


def test_no_global_is_controlled_by_two_panels():
    """The EXFO table above must not still own a knob this panel took over,
    or two controls write one global and the last one rendered wins."""
    table = set(_literal('_OTDR_KEY_TO_ENGINE_GLOBAL').values())
    knobs = {g for row in _rows() for g in row['globals'].values()}
    assert not (table & knobs), table & knobs


# ── the page wiring ──────────────────────────────────────────────────

def test_page_renders_the_panel_and_passes_it_to_the_run():
    src = open(APP, encoding='utf-8').read()
    assert '_render_conn_settings_panel()' in src
    # the run reads the COMMITTED session slot, never the component return
    assert "st.session_state.get('conn_settings')" in src
    assert 'overrides.update(' in src


def test_panel_uses_the_shared_component_in_knobs_mode():
    src = open(APP, encoding='utf-8').read()
    panel = src.split('def _render_conn_settings_panel', 1)[1].split('\ndef ', 1)[0]
    assert "mode='knobs'" in panel


def test_settings_block_is_defined_once():
    """app.py carried a 159-line byte-identical duplicate of the settings
    tables; the second copy won at runtime, so editing the first silently
    did nothing.  One definition, forever."""
    src = open(APP, encoding='utf-8').read()
    for name in ('OTDR_ROWS', 'CUSTOMER_PROFILES', '_OTDR_KEY_TO_ENGINE_GLOBAL',
                 '_CONN_ROWS'):
        n = sum(1 for ln in src.splitlines() if ln.startswith(name + ' ='))
        assert n == 1, f'{name} defined {n} times in app.py'


# ── the engine behaviours the panel exposes ──────────────────────────

SPAN = 60.0
LAUNCH = 1.01


def _rec(own_launch_loss, far_conn_loss, tailbox_refl=-57.0, span=SPAN):
    """One direction's record, shaped exactly as pass 0 leaves it: port
    reflection, its OWN launch connector at LAUNCH, a mid-span splice, the
    OTHER end's connector one reel back, EOF.  `_raw_events` keeps the
    untrimmed geometry (what the connector gate and the tailbox scan read)
    and `events` is the launch-normalized copy (what the launch-reflectance
    and launch-loss checks read) — the same two views main() builds."""
    raw = [
        {'dist_km': 0.0, 'splice_loss': 0.0, 'is_end': False,
         'is_reflective': True, 'reflection': -60.0, 'type': '1F',
         'time_of_travel': 0},
        {'dist_km': LAUNCH, 'splice_loss': own_launch_loss, 'is_end': False,
         'is_reflective': True, 'reflection': -55.0, 'type': '1F',
         'time_of_travel': 1000},
        {'dist_km': 25.0, 'splice_loss': 0.05, 'is_end': False,
         'is_reflective': False, 'reflection': 0.0, 'type': '0F',
         'time_of_travel': 2000},
        {'dist_km': span - LAUNCH, 'splice_loss': far_conn_loss, 'is_end': False,
         'is_reflective': True, 'reflection': tailbox_refl, 'type': '1F',
         'time_of_travel': 3000},
        {'dist_km': span, 'splice_loss': 0.0, 'is_end': True,
         'is_reflective': True, 'reflection': -60.0, 'type': '1E',
         'time_of_travel': 4000},
    ]
    return {'_raw_events': raw, 'events': E._normalize_untrimmed_events(list(raw))}


@pytest.fixture
def engine_defaults():
    """Restore every global this module pokes."""
    names = ('LAUNCH_CONN_LOSS_MIN_DB', 'LAUNCH_CONN_UNI_MIN_DB',
             'LAUNCH_CONN_AVG_MIN_DB', 'TAILBOX_OUTLIER_DB',
             'LAUNCH_HIGH_LOSS_DB')
    saved = {n: getattr(E, n) for n in names}
    yield
    for n, v in saved.items():
        setattr(E, n, v)


def _tags(A, B):
    iss = E.detect_launch_issues({1: A}, {1: B})
    d = iss.get(1) or {'a_tags': [], 'b_tags': []}
    return d['a_tags'], d['b_tags']


def test_average_gate_flags_what_min_and_uni_both_miss(engine_defaults):
    """Sacramento↔Suisun F1013: near .318 / far 1.088 -> average .703, the
    value the field sheet carries.  min = .318 never reached .62."""
    E.LAUNCH_CONN_LOSS_MIN_DB = 0.62
    E.LAUNCH_CONN_UNI_MIN_DB = 1.50      # high enough that the uni gate is silent
    E.LAUNCH_CONN_AVG_MIN_DB = 0.0       # average gate OFF -> the old blind spot
    a, b = _tags(_rec(0.318, 0.05), _rec(0.05, 1.088))
    assert not any('LAUNCH' in t for t in a + b), (a, b)

    E.LAUNCH_CONN_AVG_MIN_DB = 0.62      # …and ON, it fires
    a, b = _tags(_rec(0.318, 0.05), _rec(0.05, 1.088))
    assert any('LAUNCH' in t for t in a), (a, b)
    # It speaks for the PAIR, so it prints the pair's number, not the worst side
    tag = next(t for t in a if 'LAUNCH' in t)
    assert '1-WAY' not in tag, tag
    assert tag.startswith('.70'), tag


def test_zeroing_the_min_gate_leaves_the_other_gates_running(engine_defaults):
    """The guard used to test the min gate alone, so setting 'connector loss
    (bidirectional)' to 0 in the panel silently switched the 1-direction gate
    off too — one knob turning off another."""
    E.LAUNCH_CONN_LOSS_MIN_DB = 0.0      # OFF
    E.LAUNCH_CONN_UNI_MIN_DB = 0.65
    E.LAUNCH_CONN_AVG_MIN_DB = 0.0
    a, b = _tags(_rec(0.05, 0.05), _rec(0.90, 0.05))
    assert any('LAUNCH' in t for t in b), (a, b)


def test_all_three_gates_off_reports_no_connector_fault(engine_defaults):
    E.LAUNCH_CONN_LOSS_MIN_DB = 0.0
    E.LAUNCH_CONN_UNI_MIN_DB = 0.0
    E.LAUNCH_CONN_AVG_MIN_DB = 0.0
    a, b = _tags(_rec(0.90, 0.95), _rec(0.92, 0.94))
    assert not any('LAUNCH' in t for t in a + b), (a, b)


def test_tailbox_margin_is_a_reachable_global(engine_defaults):
    """It used to be a function-local constant, so no panel value could
    reach it.  At 10.0 the Sacramento↔Suisun fibers were dropped; at 8.0
    they are reported."""
    assert isinstance(E.TAILBOX_OUTLIER_DB, float)
    # population median -57.171, this fiber -49.218 -> +7.95 dB out
    fibers_a = {i: _rec(0.05, 0.05, tailbox_refl=-57.171) for i in range(1, 21)}
    fibers_b = {i: _rec(0.05, 0.05, tailbox_refl=-57.171) for i in range(1, 21)}
    fibers_b[1] = _rec(0.05, 0.05, tailbox_refl=-49.218)

    # The CELL no longer names a place — both reflectance rules print a bare
    # 'REFL-49.2dB'.  Ask the engine which RULE fired instead of reading the
    # printed string: 'refl_rules' is internal (never printed, never coloured
    # on), and asking it is strictly no weaker than the old text match — a
    # launch-rule REFL tag does NOT satisfy 'tailbox'.
    def _tailbox_fired(iss):
        return 'tailbox' in (((iss.get(1) or {}).get('refl_rules') or {})
                             .get('B', []))

    E.TAILBOX_OUTLIER_DB = 10.0
    iss = E.detect_launch_issues(fibers_a, fibers_b)
    assert not _tailbox_fired(iss), 'should be dropped at 10.0'

    # 8.0 is NOT enough — F12's real margin is +7.953, which is why the
    # shipped default is 7.5 and not the rounder number.
    E.TAILBOX_OUTLIER_DB = 8.0
    iss = E.detect_launch_issues(fibers_a, fibers_b)
    assert not _tailbox_fired(iss), 'F12 needs < 7.953'

    E.TAILBOX_OUTLIER_DB = 7.5
    iss = E.detect_launch_issues(fibers_a, fibers_b)
    assert _tailbox_fired(iss), 'should report at 7.5'
    # ... and the tag it printed is the bare data form.
    assert (iss[1]['b_tags'] == ['REFL-49.2dB']), iss[1]['b_tags']


def test_launch_loss_rule_ships_off_and_zero_means_off(engine_defaults):
    """The panel sends a number for a rule whose shipped state is disabled,
    so 0.0 has to mean OFF — not 'flag every positive launch loss'."""
    assert E.LAUNCH_HIGH_LOSS_DB in (None, 0, 0.0)
    E.LAUNCH_HIGH_LOSS_DB = 0.0
    a, b = _tags(_rec(0.30, 0.05), _rec(0.30, 0.05))
    assert not any('LAUNCH_LOSS' in t for t in a + b), (a, b)
    E.LAUNCH_HIGH_LOSS_DB = 0.20
    a, b = _tags(_rec(0.30, 0.05), _rec(0.30, 0.05))
    assert any('LAUNCH_LOSS' in t for t in a + b), (a, b)

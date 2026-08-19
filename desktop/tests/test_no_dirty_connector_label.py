"""The report prints the DATA, not a diagnosis — no 'DIRTY CONNECTOR' suffix.

Robert's call (2026-08-19): a reflective in-line event that also drops a real
loss step already prints everything the tech needs —

    388 ref .349 (refl -32dB)

— reflective, loss, reflectance.  The old ``DIRTY CONNECTOR`` suffix asserted a
CAUSE (contamination vs. mechanical splice vs. bad polish vs. a stressed
mating) that an OTDR trace cannot distinguish, so it comes off.

Scope is exactly that adjective.  The tests below lock three things:

  * the two label-emitting sites no longer decorate the label (behavioural,
    driven through ``analyze_all`` and ``scan_a_standalone_events``);
  * the flag decision, category and numbers are unchanged — the suffix removal
    is text-only, exactly as the recategorization's own comments claimed;
  * the INTERNAL ``event_source='dirty_connector'`` category and the
    ``_is_dirty_connector`` gates stay alive.  That category is never rendered
    as a word to any reader (Excel writes no event_source text, the hub grid
    uses it only as a colour key, and the Reburn Summary counts only the
    'bidir*' sources), but ``build_ribbon_data`` groups a cell's fibers by
    ``event_source`` equality — so dropping it WOULD move workbook cell text.
    That is why it stays.

Measured blast radius when the suffix was removed (10 spans on disk, full
workbook diff of every sheet / cell / fill / font / merge): four cells total,
all on Santa Rosa–Duran (fibers 539, 583, 659, 849).  Nothing else moved.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SPLICE_DIR = os.path.join(ROOT, 'splicereport')
sys.path.insert(0, SPLICE_DIR)

import splicereportmatchexfo as E  # noqa: E402

BANNED = 'DIRTY CONNECTOR'
SPAN_KM = 40.0


def _ev(km, loss=0.0, typ='0F9999', refl=-60.0, end=False):
    return {'dist_km': km, 'splice_loss': loss, 'type': typ,
            'reflection': refl, 'is_end': end}


# ── Site 1: analyze_all, A+B bidirectional reflective at a closure ───────
# A reflective (1F) event with a weak Fresnel (-32 dB) mid-span, the trace
# carrying on past it, and a 0.30 dB loss step — the exact shape the
# recategorization fires on.

def _bidir_case(refl_km=10.0, loss=0.30, refl_db=-32.0):
    a = {'events': [_ev(0.0, 0.0, '1F9999', -35.0),
                    _ev(refl_km, loss, '1F9999', refl_db),
                    _ev(30.0, 0.03),
                    _ev(SPAN_KM, 0.0, end=True)],
         'wavelength': 1550.0}
    b = {'events': [_ev(0.0, 0.0, '1F9999', -35.0),
                    _ev(SPAN_KM - 30.0, 0.03),
                    _ev(SPAN_KM - refl_km, loss, '1F9999', refl_db),
                    _ev(SPAN_KM, 0.0, end=True)],
         'wavelength': 1550.0}
    return E.analyze_all({1: a}, {1: b}, [{'position_km': refl_km}],
                         E.REBURN_THRESHOLD)


def test_bidir_reflective_with_loss_prints_data_only():
    res = _bidir_case()
    assert (1, 0) in res, "fixture stopped producing the in-line reflective cell"
    cell = res[(1, 0)]
    assert cell['label'] == '1 ref .300 (refl -32dB)', cell['label']
    assert BANNED not in cell['label']


def test_bidir_flag_category_and_numbers_are_untouched():
    """The suffix removal is text-only: same flag, same category, same loss."""
    cell = _bidir_case()[(1, 0)]
    assert cell['is_ref'] is True
    assert cell['is_flagged'] is True
    assert cell['event_source'] == 'dirty_connector'   # internal, never printed
    assert cell['bidir_loss'] == 0.30
    assert cell['fresnel'] == -32.0


# ── Site 2: scan_a_standalone_events, off-splice A-only reflective ───────
# Same event shape, but sitting 5 km off its closure so Pass 1 never saw it.

def _standalone_case(refl_km=25.0, loss=0.40, refl_db=-30.0):
    a = {'events': [_ev(0.0, 0.0, '1F9999', -35.0),
                    _ev(20.0, 0.05),                    # the fiber's own splice
                    _ev(refl_km, loss, '1F9999', refl_db),
                    _ev(32.0, 0.03),
                    _ev(SPAN_KM, 0.0, end=True)],
         'wavelength': 1550.0}
    return E.scan_a_standalone_events({2: a}, [{'position_km': 20.0}],
                                      {}, SPAN_KM)


def test_standalone_reflective_with_loss_prints_data_only():
    res = _standalone_case()
    assert (2, 0) in res, "fixture stopped producing the standalone reflective"
    cell = res[(2, 0)]
    assert cell['label'] == '2 ref .400 (refl -30dB)', cell['label']
    assert BANNED not in cell['label']


def test_standalone_flag_category_and_numbers_are_untouched():
    cell = _standalone_case()[(2, 0)]
    assert cell['is_ref'] is True
    assert cell['is_flagged'] is True
    assert cell['event_source'] == 'dirty_connector'
    assert cell['bidir_loss'] == 0.40


# ── The internal category itself must stay alive ─────────────────────────
# It is load-bearing for build_ribbon_data's per-cell fiber grouping, so it
# must NOT be quietly dropped along with the label.

def test_predicate_gates_still_fire():
    # reflective + past launch + strong enough + real loss step → True
    assert E._is_dirty_connector(10.0, -32.0, 0.30)
    # the end of the fiber is never this
    assert not E._is_dirty_connector(10.0, -32.0, 0.30, is_end=True)
    # inside the launch-exclusion zone
    assert not E._is_dirty_connector(E.DIRTY_CONN_LAUNCH_EXCL_KM, -32.0, 0.30)
    # reflectance weaker than the gate
    assert not E._is_dirty_connector(10.0, E.DIRTY_CONN_REFL_GATE_DB - 1.0, 0.30)
    # no real loss step
    assert not E._is_dirty_connector(10.0, -32.0,
                                     E.DIRTY_CONN_LOSS_GATE_DB / 2.0)
    # missing readings never fire
    assert not E._is_dirty_connector(None, -32.0, 0.30)
    assert not E._is_dirty_connector(10.0, None, 0.30)
    assert not E._is_dirty_connector(10.0, -32.0, None)


def test_knobs_are_still_wired_to_the_predicate():
    """A knob that renders but does nothing is worse than none — prove each
    one actually moves the gate."""
    old = (E.DIRTY_CONN_LAUNCH_EXCL_KM, E.DIRTY_CONN_REFL_GATE_DB,
           E.DIRTY_CONN_LOSS_GATE_DB)
    try:
        E.DIRTY_CONN_LAUNCH_EXCL_KM = 20.0
        assert not E._is_dirty_connector(10.0, -32.0, 0.30)
        E.DIRTY_CONN_LAUNCH_EXCL_KM = old[0]

        E.DIRTY_CONN_REFL_GATE_DB = -20.0
        assert not E._is_dirty_connector(10.0, -32.0, 0.30)
        E.DIRTY_CONN_REFL_GATE_DB = old[1]

        E.DIRTY_CONN_LOSS_GATE_DB = 1.0
        assert not E._is_dirty_connector(10.0, -32.0, 0.30)
    finally:
        (E.DIRTY_CONN_LAUNCH_EXCL_KM, E.DIRTY_CONN_REFL_GATE_DB,
         E.DIRTY_CONN_LOSS_GATE_DB) = old


def test_category_survives_to_the_manifest_but_only_as_a_category():
    """run_splicereport still emits the refined category (the hub grid keys a
    colour off it) — but it is a key, never text handed to a reader."""
    sys.path.insert(0, SPLICE_DIR)
    import run_splicereport as R
    assert R._category({'event_source': 'dirty_connector',
                        'is_ref': True}) == 'dirty_connector'


def test_ribbon_grouping_still_separates_the_subcategory():
    """Why the category stays: build_ribbon_data merges a cell's fibers only
    when event_source matches, so collapsing 'dirty_connector' back into 'ref'
    would move workbook cell text."""
    def _res(fnum, src):
        return {'fiber': fnum, 'bidir_loss': 0.30, 'a_loss': 0.30,
                'b_loss': 0.30, 'is_break': False, 'is_broke': False,
                'is_bend': False, 'is_ref': True, 'is_bfill': False,
                'is_a_only': False, 'is_b_only': False, 'is_flagged': True,
                'event_source': src, 'label': f'{fnum} ref .300 (refl -32dB)'}

    mixed, _, _ = E.build_ribbon_data({(1, 0): _res(1, 'dirty_connector'),
                                       (2, 0): _res(2, 'ref')},
                                      n_fibers=12, ribbon_size=12, n_splices=1)
    same, _, _ = E.build_ribbon_data({(1, 0): _res(1, 'ref'),
                                      (2, 0): _res(2, 'ref')},
                                     n_fibers=12, ribbon_size=12, n_splices=1)
    assert mixed[(0, 0)]['text'] != same[(0, 0)]['text']
    # and whatever it renders, it never names a cause
    assert BANNED not in mixed[(0, 0)]['text']


# ── Source lock ──────────────────────────────────────────────────────────

def test_engine_source_carries_no_diagnosis_suffix():
    eng = open(os.path.join(SPLICE_DIR, 'splicereportmatchexfo.py'),
               encoding='utf-8').read()
    assert BANNED not in eng, \
        "the report must print the data, not a cause — no DIRTY CONNECTOR suffix"
    # ...and the two call sites still tag the category
    assert eng.count("_is_dirty_connector(") == 3   # 1 def + 2 call sites
    # both call sites still tag the internal category (the two assignments;
    # any further hits are prose in the comments above them)
    assert eng.count("= 'dirty_connector'") == 2

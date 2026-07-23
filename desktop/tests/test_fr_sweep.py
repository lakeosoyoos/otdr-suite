"""Phase-3 (FR mode) trace-sweep discovery — regression tests.

The sweep finds real losses the stored tables never marked (prototype:
62 bidirectionally-confirmed unmarked losses on SEANOR clustering at the
boss's uni-map damage zones).  Locks: detection on a synthetic unmarked
step, the near-field lobe killer, table-dedup, the B-mirror requirement,
additive-only key semantics, and the FR_MODE gate (classic = no-op).
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SPLICE_DIR = os.path.join(ROOT, 'splicereport')
sys.path.insert(0, SPLICE_DIR)

import splicereportmatchexfo as E  # noqa: E402

SP = 5e-08
M0 = SP * (299792458.0 / 1.468) / 2.0        # ~5.1 m/sample


def _rec(step_km=None, step_db=0.0, n=4000, span_km=None, seed=3):
    """Synthetic SOR-like record: launch reflective @1.0 km, EOF at the
    far end, clean ascending backscatter, optional unmarked step."""
    span_km = span_km if span_km is not None else n * M0 / 1000.0 - 0.5
    rng = np.random.RandomState(seed)
    x = np.arange(n)
    tr = 5.0 + 0.19 * (x * M0 / 1000.0) + rng.normal(0, 0.004, n)
    if step_km is not None:
        tr[int(step_km * 1000 / M0):] += step_db
    events = [
        {'dist_km': 1.0, 'splice_loss': 0.0, 'type': '1F', 'is_end': False,
         'is_reflective': True, 'reflection': -45.0, 'time_of_travel': 100},
        {'dist_km': span_km, 'splice_loss': 0.0, 'type': '1E', 'is_end': True,
         'is_reflective': True, 'reflection': -40.0, 'time_of_travel': 900},
    ]
    return {'trace': tr, 'exfo_sampling_period': SP, 'events': events}


def _mirror(rec_a, span_km, **kw):
    """B-direction twin of rec_a: same span, step mirrored to span-x."""
    return _rec(**kw)


SPLICES = [{'position_km': 5.0, 'position_km_refined': 5.0},
           {'position_km': 15.0, 'position_km_refined': 15.0}]


def test_sweep_finds_unmarked_step(monkeypatch):
    r = _rec(step_km=10.0, step_db=0.25)
    ev, res = E._fr_sweep_events(r)
    assert res is not None
    assert any(abs(d - 10.0) < 0.15 and 0.15 < h < 0.40 for d, h, _ in ev), ev


def test_sweep_clean_glass_is_quiet():
    ev, _ = E._fr_sweep_events(_rec())
    assert ev == []


def test_near_step_reads_real_step_and_kills_lobes():
    r = _rec(step_km=10.0, step_db=0.25)
    ns = E._fr_near_step(r, 10.0)
    assert ns is not None and 0.18 < ns < 0.32
    # 700 m past the step (where two-window lobes live) the near-field
    # read must NOT confirm a positive candidate — the confirm gate
    # requires >= CONFIRM_FRAC x the sweep step.
    ns_lobe = E._fr_near_step(r, 10.7)
    assert ns_lobe is not None
    assert ns_lobe < E.FR_SWEEP_CONFIRM_FRAC * 0.25


def test_pass_discovers_bidir_confirmed_cell(monkeypatch):
    monkeypatch.setattr(E, 'FR_MODE', True)
    span = 4000 * M0 / 1000.0 - 0.5
    ra = _rec(step_km=10.0, step_db=0.25, span_km=span)
    # B twin: same launch @1.0, EOF at span; step at launch_b + eof_a - d
    rb = _rec(step_km=1.0 + span - 10.0, step_db=0.25, span_km=span, seed=4)
    out = E.fr_sweep_pass({1: ra}, {1: rb}, SPLICES, {}, span)
    assert len(out) == 1
    (key, cell), = out.items()
    assert cell['event_source'] == 'sweep'
    assert cell['is_bend'] is True                 # 10.0 is off-column
    assert 0.15 < cell['bidir_loss'] < 0.35
    assert abs(cell['bidir_dist'] - 10.0) < 0.15
    assert 'glass' in cell['label']


def test_pass_requires_mirror(monkeypatch):
    monkeypatch.setattr(E, 'FR_MODE', True)
    span = 4000 * M0 / 1000.0 - 0.5
    ra = _rec(step_km=10.0, step_db=0.25, span_km=span)
    rb = _rec(span_km=span, seed=4)                # B glass clean → no confirm
    out = E.fr_sweep_pass({1: ra}, {1: rb}, SPLICES, {}, span)
    assert out == {}


def test_pass_dedupes_table_owned_events(monkeypatch):
    monkeypatch.setattr(E, 'FR_MODE', True)
    span = 4000 * M0 / 1000.0 - 0.5
    ra = _rec(step_km=10.0, step_db=0.25, span_km=span)
    ra['events'].insert(1, {'dist_km': 10.1, 'splice_loss': 0.25,
                            'type': '0F', 'is_end': False,
                            'is_reflective': False, 'reflection': 0,
                            'time_of_travel': 500})
    rb = _rec(step_km=1.0 + span - 10.0, step_db=0.25, span_km=span, seed=4)
    out = E.fr_sweep_pass({1: ra}, {1: rb}, SPLICES, {}, span)
    assert out == {}          # the table marks it — Phases 1-2 own it


def test_pass_is_additive_only(monkeypatch):
    monkeypatch.setattr(E, 'FR_MODE', True)
    span = 4000 * M0 / 1000.0 - 0.5
    ra = _rec(step_km=10.0, step_db=0.25, span_km=span)
    rb = _rec(step_km=1.0 + span - 10.0, step_db=0.25, span_km=span, seed=4)
    existing = {(1, 1): {'anything': True}}       # nearest column to 10.0? si by min-dist
    out_free = E.fr_sweep_pass({1: ra}, {1: rb}, SPLICES, {}, span)
    (key, _), = out_free.items()
    out = E.fr_sweep_pass({1: ra}, {1: rb}, SPLICES, {key: {'x': 1}}, span)
    assert out == {}          # existing cell wins; never overwritten


def test_fr_off_pass_is_noop():
    span = 4000 * M0 / 1000.0 - 0.5
    ra = _rec(step_km=10.0, step_db=0.30, span_km=span)
    rb = _rec(step_km=1.0 + span - 10.0, step_db=0.30, span_km=span, seed=4)
    assert E.fr_sweep_pass({1: ra}, {1: rb}, SPLICES, {}, span) == {}


def test_source_locks_sweep_wiring():
    eng = open(os.path.join(SPLICE_DIR, 'splicereportmatchexfo.py'),
               encoding='utf-8').read()
    run = open(os.path.join(SPLICE_DIR, 'run_splicereport.py'),
               encoding='utf-8').read()
    assert eng.count('fr_sweep_pass(fibers_a, fibers_b, splices, all_results, span_km)') == 1
    assert run.count('fr_sweep_pass(fa, fb, splices, all_results, span_km)') == 1
    assert "== 'sweep'" in run and "'sweep'" in open(
        os.path.join(ROOT, 'app.py'), encoding='utf-8').read()


def test_split_preserves_sweep_provenance(monkeypatch):
    """A sweep discovery moved into its own damage-zone column must keep
    event_source='sweep' (the grid's 'glass' category), not be relabeled
    bend_column like table-driven bends."""
    monkeypatch.setattr(E, 'FR_MODE', True)
    splices = [{'position_km': 5.0, 'position_km_refined': 5.0}]
    cell = {'fiber': 7, 'splice_idx': 0, 'bidir_loss': 0.12,
            'a_loss': 0.12, 'b_loss': 0.12, 'bidir_dist': 9.0,
            'is_break': False, 'is_broke': False, 'is_bend': True,
            'is_flagged': True, 'event_source': 'sweep',
            'label': '7 glass .120 (+4000m)'}
    out, spl = E.split_offsplice_events_into_own_columns(
        {(7, 0): dict(cell)}, list(splices), total_span_km=30.0)
    moved = [v for v in out.values() if v['fiber'] == 7]
    assert len(moved) == 1
    assert moved[0]['event_source'] == 'sweep'
    assert len(spl) == 2                       # own column created
    assert 'glass' in moved[0]['label']

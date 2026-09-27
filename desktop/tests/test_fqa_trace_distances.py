"""
FQA Builder: Event Log distances from the Splice Report's closures.

fqa.event_chain.splice_distances maps the Splice Report manifest's closure
columns onto the production sheet's splice worksheets, one distance from
Site A each, so the tech no longer types them.  The production sheets
here are built in memory (Location / CableRow), with footage marks placed
where the test says the vaults are.

Real-data check, not a test (the traces are not in the repo): a 98 km,
864-fibre span with 12 vaults and 2 entry splices, whose submitted FQA
package and production sheet are both on hand.  The engine found 12
closures, mapped by order; against the package's Event Log the 12 vaults
land -6..+117 m (median |diff| 17 m; the package holds values rounded to
10 m off whichever fibre the tech opened, while the engine gives the
population centre), the entries sit exactly on the entry offsets, and the
span length is 98,420 m against the package's 98,410 m.  An injected
repair column 20 km from any vault was dropped by the footage marks; one
500 m from a vault came back None as ambiguous; one closure removed came
back None.
"""
from __future__ import annotations

import json
import os
import sys
import types

import pytest

from conftest import (FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR, REPO_ROOT,
                      run_splicereport)

sys.path.insert(0, str(REPO_ROOT))

from fqa.event_chain import (FT_TO_M, METHOD_FOOTAGE, METHOD_ORDER,  # noqa: E402
                             build_chain, splice_distances,
                             splice_distances_from_manifest)
from fqa.production_sheet import (SPLICE, TERMINATION, CableRow,      # noqa: E402
                                  Location, ProductionSheet)
from test_engine_self_verify import _load_app_helper                  # noqa: E402


def _ft(m):
    return f'{m / FT_TO_M:07.0f}ft'


def _sheet(vaults_m, east_marks_m=None, *, entry_offset=60, entries=True):
    """A production sheet: termination, entry, vaults, entry, termination.

    `vaults_m` places each vault, and both its cables' footage marks say so;
    `east_marks_m` overrides the mark on the cable leaving each vault toward
    Z -- give a different value to plant a bad mark.  Every cable is on one
    reel whose footage reads metres from Site A.
    """
    marks = list(vaults_m)
    east = list(east_marks_m if east_marks_m is not None else vaults_m)
    span_mark = marks[-1] + 5000
    locs = [Location(sheet='A ILA', kind=TERMINATION, index=0)]
    if entries:
        locs.append(Location(sheet='A Entry', kind=SPLICE, index=1, cables=[
            CableRow(direction='East', seq_at_node=_ft(entry_offset))]))
    for k, (mk, ek) in enumerate(zip(marks, east)):
        locs.append(Location(sheet=f'Splice {k + 1}', kind=SPLICE,
                             index=len(locs), cables=[
            CableRow(direction='West', seq_at_node=_ft(mk)),
            CableRow(direction='East', seq_at_node=_ft(ek))]))
    if entries:
        locs.append(Location(sheet='Z Entry', kind=SPLICE, index=len(locs),
                             cables=[CableRow(direction='West',
                                              seq_at_node=_ft(span_mark))]))
    locs.append(Location(sheet='Z ILA', kind=TERMINATION, index=len(locs)))
    return ProductionSheet(path='synthetic.xlsx', locations=locs)


def _manifest(closures_km, span_km, kinds=None, **extra):
    kinds = kinds or ['splice'] * len(closures_km)
    return {'ok': True, 'analysis_mode': 'suite', 'span_km': span_km,
            'columns': [{'index': i, 'km': km, 'kind': k, 'is_repair': False,
                         'num': None}
                        for i, (km, k) in enumerate(zip(closures_km, kinds))],
            **extra}


VAULTS = [5010, 7650, 13540, 18700, 23500]
SPAN_KM = 28.5


# ── counts agree ───────────────────────────────────────────────────────────

def test_count_match_maps_by_order_and_entries_take_the_offset():
    prod = _sheet(VAULTS)
    m = _manifest([v / 1000 + 0.012 for v in VAULTS], SPAN_KM)
    r = splice_distances_from_manifest(m, prod)
    assert r['method'] == METHOD_ORDER
    assert r['warnings'] == []
    assert len(r['distances_m']) == len(prod.splices) == 7
    assert r['distances_m'][0] == 60
    assert r['distances_m'][1:-1] == [v + 12 for v in VAULTS]
    assert r['distances_m'][-1] == SPAN_KM * 1000 - 60
    assert r['span_length_m'] == SPAN_KM * 1000
    assert [x['source'] for x in r['matches']] == (
        ['entry offset'] + ['trace'] * 5 + ['entry offset'])
    # and build_chain takes it as trace distances, not footage
    chain = build_chain(prod, trace_distances_m=r['distances_m'],
                        span_length_m=r['span_length_m'])
    assert chain.distance_source == 'trace'


def test_z_entry_offset_can_differ_from_a():
    r = splice_distances_from_manifest(
        _manifest([v / 1000 for v in VAULTS], SPAN_KM), _sheet(VAULTS),
        entry_offset_z_m=50)
    assert r['distances_m'][-1] == SPAN_KM * 1000 - 50


def test_a_closure_inside_the_entry_window_is_the_entry_measured():
    m = _manifest([0.085] + [v / 1000 for v in VAULTS], SPAN_KM)
    r = splice_distances_from_manifest(m, _sheet(VAULTS))
    assert r['method'] == METHOD_ORDER
    assert r['distances_m'][0] == 85
    assert r['matches'][0]['source'] == 'trace'


def test_a_vault_near_the_frame_is_not_stolen_by_the_entry():
    # First vault 800 m out, entry in the dead zone: the near closure has
    # to go back to the vaults rather than leave them one short.
    vaults = [800] + VAULTS
    r = splice_distances_from_manifest(
        _manifest([v / 1000 for v in vaults], SPAN_KM), _sheet(vaults))
    assert r['distances_m'] is not None
    assert r['distances_m'][:2] == [60, 800]


def test_off_splice_columns_are_not_closures():
    kms = [5.01, 6.2, 7.65, 13.54, 18.7, 21.0, 23.5]
    kinds = ['splice', 'bend', 'splice', 'splice', 'splice', 'damage', 'splice']
    r = splice_distances_from_manifest(_manifest(kms, SPAN_KM, kinds),
                                       _sheet(VAULTS))
    assert r['method'] == METHOD_ORDER
    assert r['closures_found_m'] == VAULTS


def test_a_sheet_without_entry_tabs_maps_every_splice_to_a_closure():
    prod = _sheet(VAULTS, entries=False)
    r = splice_distances_from_manifest(
        _manifest([v / 1000 for v in VAULTS], SPAN_KM), prod)
    assert r['distances_m'] == VAULTS


# ── traces found more closures than the sheet has vaults ──────────────────

def test_extra_closure_is_dropped_by_the_footage_marks():
    kms = [v / 1000 for v in VAULTS] + [10.6]      # a repair between 2 and 3
    r = splice_distances_from_manifest(_manifest(sorted(kms), SPAN_KM),
                                       _sheet(VAULTS))
    assert r['method'] == METHOD_FOOTAGE
    assert r['distances_m'][1:-1] == VAULTS
    assert r['dropped_m'] == [10600]
    assert any('10.60 km' in w for w in r['warnings'])


def test_extra_closure_is_placed_past_a_bad_footage_mark():
    # Vault 3's outgoing mark is 1.5 km wrong, so the chain from A is off
    # past it; the chain from Z still places vaults 4 and 5.
    marks = [5010, 7650, 12040, 18700, 23500]
    assert build_chain(_sheet(VAULTS, marks)).events[5].footage_from_a_m != 18700
    kms = sorted([v / 1000 for v in VAULTS] + [21.1])
    r = splice_distances_from_manifest(_manifest(kms, SPAN_KM),
                                       _sheet(VAULTS, marks))
    assert r['method'] == METHOD_FOOTAGE
    assert r['dropped_m'] == [21100]


def test_ambiguous_extra_closure_returns_none():
    # 150 m past vault 3: either could be the one the sheet means.
    kms = sorted([v / 1000 for v in VAULTS] + [13.69])
    r = splice_distances_from_manifest(_manifest(kms, SPAN_KM), _sheet(VAULTS))
    assert r['distances_m'] is None
    assert r['method'] == 'none'
    assert 'more than one choice fits' in r['warnings'][-1]
    assert len(r['closures_found_m']) == 6


def test_no_footage_marks_means_no_guess():
    prod = _sheet(VAULTS)
    for loc in prod.locations:
        loc.cables = []
    kms = sorted([v / 1000 for v in VAULTS] + [10.6])
    r = splice_distances_from_manifest(_manifest(kms, SPAN_KM), prod)
    assert r['distances_m'] is None


# ── traces found fewer ────────────────────────────────────────────────────

def test_missing_closure_returns_none_with_the_counts():
    kms = [v / 1000 for v in VAULTS[:2] + VAULTS[3:]]
    r = splice_distances_from_manifest(_manifest(kms, SPAN_KM), _sheet(VAULTS))
    assert r['distances_m'] is None
    assert r['closures_found_m'] == VAULTS[:2] + VAULTS[3:]
    assert 'found 4 closures' in r['warnings'][-1]
    assert '5 splice worksheets' in r['warnings'][-1]


def test_failed_or_fastreporter_reports_give_no_distances():
    prod = _sheet(VAULTS)
    r = splice_distances_from_manifest({'ok': False, 'error': 'boom'}, prod)
    assert r['distances_m'] is None and 'boom' in r['warnings'][0]
    fr = _manifest([v / 1000 for v in VAULTS], SPAN_KM, analysis_mode='fr')
    r = splice_distances_from_manifest(fr, prod)
    assert r['distances_m'] is None and 'FastReporter' in r['warnings'][0]


def test_splice_distances_never_raises_for_a_bad_run():
    def boom(a, b):
        raise RuntimeError('engine fell over')
    r = splice_distances('A', 'B', _sheet(VAULTS), get_manifest=boom)
    assert r['distances_m'] is None
    assert 'engine fell over' in r['warnings'][0]


# ── end to end: the real engine on the repo's fixture traces ──────────────

def test_real_splice_report_manifest_maps_onto_a_sheet(tmp_path):
    rc, manifest, err = run_splicereport(FIXTURE_SPLICE_A_DIR,
                                         FIXTURE_SPLICE_B_DIR,
                                         tmp_path / 'sr.xlsx')
    assert rc == 0 and manifest and manifest['ok'], err[-2000:]
    closures = [c['km'] * 1000 for c in manifest['columns']
                if c['kind'] == 'splice']
    assert len(closures) >= 2
    # a sheet with one vault per closure the engine validated, marks at them
    prod = _sheet([round(c) for c in closures])
    r = splice_distances('unused', 'unused', prod,
                         get_manifest=lambda a, b: manifest)
    assert r['method'] == METHOD_ORDER, r['warnings']
    assert r['distances_m'][1:-1] == [round(c, 1) for c in closures]
    assert r['span_length_m'] == pytest.approx(manifest['span_km'] * 1000)
    assert r['distances_m'][-1] == pytest.approx(manifest['span_km'] * 1000 - 60)


# ── the hub side: which manifest the FQA Builder gets ─────────────────────

def _hub_manifest_fn(tmp_path, cached, engine_calls):
    def run_engine(cmd):
        engine_calls.append(cmd)
        return types.SimpleNamespace(
            stdout=json.dumps(_manifest([1.0], 2.0, analysis_mode='suite',
                                        fresh=True)),
            stderr='')

    def splicereport_cmd(dir_a, dir_b, out, sa, sb, analysis=None):
        return ['engine', dir_a, dir_b, '--analysis', analysis]

    cache = tmp_path / 'sr_grid_cache.json'
    if cached is not None:
        cache.write_text(json.dumps(cached), encoding='utf-8')

    def _parse_manifest(stdout):
        return json.loads(stdout)

    import tempfile
    return _load_app_helper(
        '_fqa_sr_manifest', json=json, os=os, tempfile=tempfile,
        _hub_cache_path=lambda name, folder: str(cache),
        run_engine=run_engine, splicereport_cmd=splicereport_cmd,
        _parse_manifest=_parse_manifest)


def test_hub_reuses_the_splice_report_grid_for_the_same_folders(tmp_path):
    calls = []
    grid = _manifest([5.0], 9.0)
    fn = _hub_manifest_fn(tmp_path, {'manifest': grid, '_dirs': ['/a', '/b']},
                          calls)
    assert fn('/a', '/b') == grid
    assert calls == []


@pytest.mark.parametrize('cached', [
    None,
    {'manifest': _manifest([5.0], 9.0, analysis_mode='fr'), '_dirs': ['/a', '/b']},
    {'manifest': _manifest([5.0], 9.0), '_dirs': ['/a', '/other-b']},
])
def test_hub_runs_the_engine_in_otdr_mode_otherwise(tmp_path, cached):
    calls = []
    fn = _hub_manifest_fn(tmp_path, cached, calls)
    m = fn('/a', '/b')
    assert m.get('fresh') is True
    assert calls and calls[0][-1] == 'suite'

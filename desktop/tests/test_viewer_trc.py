"""
.trc in the Viewer.

The Viewer runs on its own copy of the .sor reader, so it carries its own
cut-down .trc reader (viewer/sor_reader324802a.py).  These tests pin:

  1. It says what the Splice Report engine's reader says, on every field the
     Viewer draws -- the two copies must not drift.
  2. Run on a real .sor's own EXFO block it gives what the Viewer's
     parse_sor_full gives for that .sor, including FR's event kind, status
     bits and "no loss reading" flag the event grid prints.
  3. trace_server lists and loads a .trc folder, puts a declared-span file
     back in the raw frame, reads the stored direction, splits a dropped
     .trc pair by its stored sites, and refuses to edit one with a reason.

The .trc fixtures are the scrubbed ones in fixtures/trc (see its README).
"""
import importlib.util
import os
import shutil

import numpy as np
import pytest

from conftest import VIEWER_DIR, import_trace_server
from test_trc_intake import _with_sites

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, 'fixtures')
TRC = os.path.join(FIX, 'trc')
SPAN = os.path.join(TRC, 'TRCSPAN0001_131015501625.trc')     # 3 wavelengths, no span
DECL = os.path.join(TRC, 'TRCDECL0001_155016251310.trc')     # span declared 1 km in


def _viewer_reader():
    spec = importlib.util.spec_from_file_location(
        'viewer_sor_reader_trc', str(VIEWER_DIR / 'sor_reader324802a.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


V = _viewer_reader()


# ── 1. The two readers agree ─────────────────────────────────────────

@pytest.mark.parametrize('path', [SPAN, DECL], ids=os.path.basename)
def test_viewer_reader_matches_engine_reader(path):
    import sor_reader324802a as engine                    # the engine's copy
    for v, e in zip(V.parse_trc(path), engine.parse_trc(path, trim=False)):
        assert np.array_equal(v['trace'], e['trace'])
        # Not exfo_res_m: the Viewer's block parser has no stated-IOR fallback
        # for a file with under three markers (it is None there, for .sor
        # too), and the Viewer draws on IOR x sampling period anyway.
        for k in ('wavelength', '_trc_nominal_nm', 'user_offset_km', 'ior',
                  'fxd_pulse_ns', 'gen_fiber_id', 'gen_loc_a', 'gen_loc_b',
                  'exfo_sampling_period'):
            assert v[k] == e[k], k
        assert len(v['events']) == len(e['events'])
        for a, b in zip(v['events'], e['events']):
            for k in ('number', 'time_of_travel', 'dist_km', 'splice_loss',
                      'reflection', 'slope', 'type', 'is_reflective', 'is_end'):
                assert a[k] == b[k], (k, b['dist_km'])


def test_wavelength_the_viewer_draws():
    assert V.parse_trc_wavelength(SPAN)['_trc_nominal_nm'] == 1550
    assert V.parse_trc_wavelength(DECL)['_trc_nominal_nm'] == 1550


# ── 2. The .sor twin, on the Viewer's own reader ─────────────────────

TWIN_DIRS = ('span_A', 'refl', 'satrefl', 'launchreel', 'paneljumper/A', 'negtot')
TWINS = sorted(os.path.join(FIX, d, f) for d in TWIN_DIRS
               for f in os.listdir(os.path.join(FIX, d)) if f.lower().endswith('.sor'))


@pytest.mark.parametrize('path', TWINS, ids=lambda p: os.path.relpath(p, FIX))
def test_sor_block_read_as_trc_matches_viewer_sor(path):
    with open(path, 'rb') as fh:
        data = fh.read()
    stream = V._decompress_proprietary(data, V._parse_block_directory(data))
    subs = V._trc_substreams(stream)
    assert len(subs) == 1
    got, want = V._trc_record(subs[0], path), V.parse_sor_full(path, trim=False)
    assert np.array_equal(got['trace'], want['trace'])
    assert got['user_offset_km'] == pytest.approx(want['user_offset_km'], abs=5e-4)
    assert got['fxd_pulse_ns'] == pytest.approx(want['fxd_pulse_ns'])
    assert got['ior'] == pytest.approx(want['ior'], abs=1e-9)
    assert len(got['events']) == len(want['events'])
    # The .sor path takes FR's float values and flags from the block only
    # when its list lines up 1:1 with KeyEvents, and that list drops records
    # more than 1 m before the span start -- so a tie panel's -15 m jumper
    # joint leaves the whole .sor file on KeyEvents' 1 mdB integers with no FR
    # flags.  The .trc reads FR's records directly and keeps them.  Where the
    # .sor path did upgrade, the two must agree exactly.
    upgraded = any('fr_type' in w for w in want['events'])
    for g, w in zip(got['events'], want['events']):
        for k in ('number', 'type', 'is_reflective', 'is_end'):
            assert g[k] == w[k], (k, w['dist_km'])
        if upgraded:
            for k in ('fr_type', 'fr_status', 'fr_has_loss', 'loss_full_precision'):
                assert g.get(k) == w.get(k), (k, w['dist_km'])
        tol = 5e-4 if upgraded else 1.5e-3
        for k in ('dist_km', 'splice_loss', 'reflection', 'slope'):
            assert g[k] == pytest.approx(w[k], abs=tol), (k, w['dist_km'])
        assert abs(g['time_of_travel'] - w['time_of_travel']) <= 1


# ── 3. trace_server ──────────────────────────────────────────────────

@pytest.fixture
def ts():
    mod = import_trace_server()
    saved = dict(mod.CONFIG)
    mod._LIST_CACHE.clear()
    mod._load_trace_cached.cache_clear()
    yield mod
    mod.CONFIG.clear()
    mod.CONFIG.update(saved)
    mod._LIST_CACHE.clear()
    mod._load_trace_cached.cache_clear()


def test_trc_folder_lists_and_loads(ts, tmp_path):
    for i in (1, 2, 3):
        shutil.copy(SPAN, tmp_path / f'TRCSPAN{i:04d}_131015501625.trc')
    assert [n for n, _fn in ts.list_fibers(str(tmp_path))] == [1, 2, 3]
    ts.CONFIG['dir_a'] = str(tmp_path)
    t = ts.load_trace('a', 2)
    assert t is not None
    ends = [e for e in t['events'] if e['is_end']]
    assert len(ends) == 1 and ends[0]['dist_km'] == pytest.approx(68.51, abs=0.05)


def _sor_as_trc(sor_path, dst):
    """A .sor's EXFO block IS a one-trace .trc container: rewrap it behind a
    real .trc's outer header, so the same shot can be loaded both ways."""
    data = open(sor_path, 'rb').read()
    blocks = V._parse_block_directory(data)
    name = next(k for k in blocks if 'ExfoNewProprietaryBlock' in k)
    body = data[blocks[name]['body']:blocks[name]['offset'] + blocks[name]['size']]
    assert body.startswith(b'AppReg Format Ex')
    wrap = open(SPAN, 'rb').read()
    with open(dst, 'wb') as fh:
        fh.write(wrap[:wrap.find(b'AppReg Format Ex', 1)] + body)


def _loaded(ts, folder, fiber):
    ts.CONFIG['dir_a'] = str(folder)
    ts._load_trace_cached.cache_clear()
    return ts.load_trace('a', fiber)


def test_tie_panel_trc_loads_like_its_sor(ts, tmp_path):
    """Declared span with a jumper joint before the span start: the Viewer
    puts the events back in the raw frame.  The same shots as .sor and as
    .trc must come back on the same metres, with the same frame facts."""
    src = os.path.join(FIX, 'paneljumper', 'A')
    sor_dir, trc_dir = tmp_path / 'sor', tmp_path / 'trc'
    sor_dir.mkdir()
    trc_dir.mkdir()
    for fn in sorted(os.listdir(src))[:4]:
        shutil.copy(os.path.join(src, fn), sor_dir / fn)
        _sor_as_trc(os.path.join(src, fn), trc_dir / (fn[:-4] + '.trc'))
    fs, ft = ts.frame_facts(str(sor_dir)), ts.frame_facts(str(trc_dir))
    assert fs['span_launch_km'] and ft['span_launch_km'] == pytest.approx(
        fs['span_launch_km'], abs=5e-4)
    ts_, tt = _loaded(ts, sor_dir, 1), _loaded(ts, trc_dir, 1)
    assert tt['span_launch_km'] == pytest.approx(ts_['span_launch_km'], abs=5e-4)
    assert len(tt['events']) == len(ts_['events'])
    for a, b in zip(tt['events'], ts_['events']):
        assert a['dist_km'] == pytest.approx(b['dist_km'], abs=5e-4)
        assert a['type'] == b['type']


def test_declared_span_without_pre_start_events_frames_like_its_sor(ts, tmp_path):
    """A declared span whose events all start at 0 is framed the way a .sor
    shot like it is (fixtures/endlaunch): whatever the Viewer does with the
    stored launch length for one, it does for the other.  When it applies it,
    the .trc's own stored length (1.006 km) is the one used."""
    for i in (1, 2, 3):
        shutil.copy(DECL, tmp_path / f'TRCDECL{i:04d}_155016251310.trc')
    got = ts.frame_facts(str(tmp_path))['span_launch_km']
    sor = ts.frame_facts(os.path.join(FIX, 'endlaunch'))['span_launch_km']
    assert (got is None) == (sor is None)
    if got is not None:
        assert got == pytest.approx(1.0059, abs=0.0005)


def test_a_few_trc_reshoots_do_not_hide_a_sor_span(ts, tmp_path):
    for fn in os.listdir(os.path.join(FIX, 'span_A')):
        if fn.endswith('.sor'):
            shutil.copy(os.path.join(FIX, 'span_A', fn), tmp_path / fn)
    shutil.copy(SPAN, tmp_path / 'ELMMIL0001_131015501625.trc')
    names = [fn for _n, fn in ts.list_fibers(str(tmp_path))]
    assert names and all(fn.endswith('.sor') for fn in names)


def test_stored_direction_is_read(ts):
    with open(SPAN, 'rb') as fh:
        assert ts.read_direction(fh.read()) == 'a'        # LocationsDirection 1
    with open(DECL, 'rb') as fh:
        assert ts.read_direction(fh.read()) == 'b'        # LocationsDirection 2


def test_dropped_trc_pair_splits_by_stored_sites(ts, tmp_path):
    for i in (1, 2):
        _with_sites(DECL, tmp_path / f'{i:04d}_155016251310a.trc', 'SITA', 'SITB')
        _with_sites(DECL, tmp_path / f'{i:04d}_155016251310b.trc', 'SITB', 'SITA')
    paths = sorted(str(p) for p in tmp_path.iterdir())
    groups = ts.split_by_location_pair(paths)
    assert sorted(groups) == ['SITA → SITB', 'SITB → SITA']


def test_trc_is_not_editable_and_says_why(ts, tmp_path):
    shutil.copy(SPAN, tmp_path / 'TRCSPAN0001_131015501625.trc')
    out = ts.trace_settings('a', 1, dir_a=str(tmp_path))
    assert not out.get('editable')
    assert '.trc' in out['why']


def test_a_trc_stamp_does_not_pick_the_drop_side(ts, tmp_path):
    """.trc stamps are not trusted to choose A or B for a dropped folder (two
    of seven named .trc directions on hand are stamped from the wrong end);
    the folder name and what is already loaded decide."""
    paths = []
    for i in (1, 2):
        p = tmp_path / f'TRCDECL{i:04d}_155016251310.trc'
        shutil.copy(DECL, p)
        paths.append(str(p))
    assert ts._declared_direction(paths) is None

"""Editing Backscatter from the Viewer's Trace Settings dialog (Robert
2026-10-01: "we need to be able to edit Backscatter in IOR settings").

What an edit must write was read off two files FastReporter 3 saved after its
Backscatter cell was changed (see the comment above set_backscatter): the
expected numbers below are FR's own, taken from those saved files, so each
test replays FR's edit on the untouched fixture and checks we land on FR.
"""
import hashlib
import math
import os
import struct
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, 'viewer'))

import trace_server as TS                       # noqa: E402
from test_sor_writer import make_sor            # noqa: E402

FIX = os.path.join(HERE, 'fixtures')


def _by_hash(folder, prefix):
    """The fixture in `folder` whose sha256 starts with `prefix`: the two
    files FR was run on, picked by content so no file name is repeated here."""
    d = os.path.join(FIX, *folder.split('/'))
    for f in sorted(os.listdir(d)):
        if f.endswith('.sor'):
            with open(os.path.join(d, f), 'rb') as fh:
                if hashlib.sha256(fh.read()).hexdigest().startswith(prefix):
                    return os.path.join(d, f)
    raise FileNotFoundError(prefix)


ENDLAUNCH = _by_hash('endlaunch', 'bc7cba61726a2090')         # 2 reflective events
SPLICES = _by_hash('portoutlier/A', '79883f37721e5df4')       # 17 events, 15 splices


def _read(path):
    with open(path, 'rb') as f:
        return f.read()


def _blocks(data):
    return {b.name: b for b in TS.split(data)[1]}


def _stream(data):
    for b in TS.split(data)[1]:
        if b.name.startswith(b'ExfoNewProprietaryBlock'):
            return b''.join(d for _, d in TS._prop_chunks(b.body)[1])
    return b''


def _prop(data, name):
    s = _stream(data)
    return [(v, s[r['pay']:r['pay'] + 8]) for r, v in TS._prop_typed(s)
            if r['name'] == name and r['tc'] == 3]


def _fxd_bs(data):
    fx = _blocks(data)[b'FxdParams'].body
    return struct.unpack_from('<H', fx, TS.fxdparams_offsets(fx)['backscatter'])[0]


def _kev(data):
    return TS._kev_parse(_blocks(data)[b'KeyEvents'].body)


def _same(a, b):
    return (a != a and b != b) or a == pytest.approx(b, abs=1e-9)


# ── FR's two saves, replayed ─────────────────────────────────────────────

def test_end_launch_shot_minus_83_to_minus_73_lands_on_frs_file():
    orig = _read(ENDLAUNCH)
    out = TS.set_backscatter(orig, -73.0)
    assert _fxd_bs(orig) == 830 and _fxd_bs(out) == 730
    evs, summary = _kev(out)
    assert [e['refl'] for e in evs] == [-18836, -6757]          # FR: -28836, -16757 before
    assert summary[3] == 18877                                  # FR: 28877 before
    assert [v for v, _ in _prop(out, 'Rbs')] == [-73.0]
    refl = [v for v, _ in _prop(out, 'Reflectance')]
    for got, fr in zip(refl, [math.nan, -18.836253544661368, -6.756978750543993]):
        assert _same(got, fr)
    assert _prop(out, 'TotalOrl')[0][0] == pytest.approx(-18.877487657734967, abs=1e-12)
    assert TS.read_backscatter(out) == -73.0


def test_splice_shot_minus_83_to_minus_80_5_lands_on_frs_file():
    orig = _read(SPLICES)
    out = TS.set_backscatter(orig, -80.5)
    assert _fxd_bs(out) == 805
    evs, summary = _kev(out)
    refl = [e['refl'] for e in evs]
    assert refl[0] == -53058 and refl[-1] == -64150
    assert refl[1:-1] == [0] * 15                               # splices stay non-reflective
    assert summary[3] == 30411                                  # FR: 32911 before
    refl = [v for v, _ in _prop(out, 'Reflectance') if v == v]
    assert refl == [pytest.approx(-53.057727255457536, abs=1e-12),
                    pytest.approx(-64.15048521005264, abs=1e-12)]
    # A positive TotalOrl stays positive and shrinks: the magnitude moves.
    assert _prop(out, 'TotalOrl')[0][0] == pytest.approx(30.411077894631433, abs=1e-12)


def test_peak_reflection_to_rbs_is_cleared_with_frs_own_nan_bytes():
    orig = _read(SPLICES)
    assert any(v == v for v, _ in _prop(orig, 'PeakReflectionToRbs'))
    out = TS.set_backscatter(orig, -80.5)
    after = _prop(out, 'PeakReflectionToRbs')
    assert after and all(raw == b'\x00' * 6 + b'\xf8\xff' for _, raw in after)


@pytest.mark.parametrize('path', [ENDLAUNCH, SPLICES])
def test_the_trace_does_not_move(path):
    # DataPts and the RawSamples payload are byte-identical in FR's saves.
    orig = _read(path)
    out = TS.set_backscatter(orig, -73.0)
    assert _blocks(out)[b'DataPts'].body == _blocks(orig)[b'DataPts'].body
    so, sn = _stream(orig), _stream(out)
    lo, hi = TS._rawsamples_span(so)
    assert sn[lo:hi] == so[lo:hi]
    for name in (b'GenParams', b'SupParams'):
        assert _blocks(out)[name].body == _blocks(orig)[name].body
    assert TS.roundtrip_ok(out)


def test_nothing_else_in_the_proprietary_block_moves():
    orig = _read(SPLICES)
    out = TS.set_backscatter(orig, -73.0)
    moved = {r['name'] for (r, a), (_, b) in zip(TS._prop_typed(_stream(orig)),
                                                 TS._prop_typed(_stream(out)))
             if not _same(a, b) if a is not None}
    assert moved == {'Rbs', 'Reflectance', 'TotalOrl', 'PeakReflectionToRbs'}


def test_setting_it_back_restores_every_number():
    orig = _read(ENDLAUNCH)
    back = TS.set_backscatter(TS.set_backscatter(orig, -73.0), -83.0)
    assert _fxd_bs(back) == 830
    assert _kev(back) == _kev(orig)
    for name in ('Rbs', 'Reflectance', 'TotalOrl'):
        for (a, _), (b, _) in zip(_prop(back, name), _prop(orig, name)):
            assert _same(a, b)


# ── input rules ──────────────────────────────────────────────────────────

def test_the_value_is_kept_to_tenths_the_field_s_resolution():
    out = TS.set_backscatter(_read(ENDLAUNCH), -80.54)
    assert TS.read_backscatter(out) == -80.5 and _fxd_bs(out) == 805


@pytest.mark.parametrize('bad', [-120.0, -30.0, 0.0])
def test_a_value_outside_the_band_is_refused(bad):
    with pytest.raises(ValueError):
        TS.set_backscatter(_read(ENDLAUNCH), bad)


def _with_bellcore_bs(raw, tenths):
    mv, bl = TS.split(raw)
    fx = TS._find(bl, b'FxdParams')
    body = bytearray(fx.body)
    struct.pack_into('<H', body, TS.fxdparams_offsets(fx.body)['backscatter'], tenths)
    fx.body = bytes(body)
    return TS.build(mv, bl)


def test_a_file_with_no_rbs_record_uses_the_bellcore_field():
    raw = _with_bellcore_bs(make_sor(ior=1.47), 820)
    assert TS.read_backscatter(raw) == -82.0
    out = TS.set_backscatter(raw, -73.0)
    assert TS.read_backscatter(out) == -73.0 and _fxd_bs(out) == 730


def test_a_file_that_records_no_backscatter_is_refused():
    raw = _with_bellcore_bs(make_sor(ior=1.47), 0)
    assert TS.read_backscatter(raw) is None
    with pytest.raises(ValueError):
        TS.set_backscatter(raw, -73.0)


# ── the dialog and the route ─────────────────────────────────────────────

@pytest.fixture
def _downloads_is_tmp(tmp_path, monkeypatch):
    dl = tmp_path / 'Downloads'
    dl.mkdir()
    monkeypatch.setenv('OTDR_DOWNLOADS_DIR', str(dl))
    return dl


def _folder(tmp_path):
    d = tmp_path / 'DNN1DNN2 A'
    d.mkdir()
    (d / 'DNN1DNN20001.sor').write_bytes(_read(ENDLAUNCH))
    return str(d)


def test_edit_traces_writes_copies_and_leaves_the_original(tmp_path, _downloads_is_tmp):
    d = _folder(tmp_path)
    fiber = TS.list_fibers(d)[0][0]
    out = TS.edit_traces('a', 'all', backscatter=-73.0, dir_a=d, dest_name='bs')
    assert out['written'] == [fiber] and not out['skipped']
    copy = _read(os.path.join(out['dest'], 'DNN1DNN20001.sor'))
    assert TS.read_backscatter(copy) == -73.0
    assert _read(os.path.join(d, 'DNN1DNN20001.sor')) == _read(ENDLAUNCH)


def test_a_backscatter_outside_the_band_is_refused_before_any_file_is_touched(tmp_path, _downloads_is_tmp):
    d = _folder(tmp_path)
    with pytest.raises(ValueError):
        TS.edit_traces('a', 'all', backscatter=-10.0, dir_a=d)
    assert not os.listdir(str(_downloads_is_tmp))


def test_the_settings_route_reports_the_backscatter(tmp_path):
    d = _folder(tmp_path)
    s = TS.trace_settings('a', TS.list_fibers(d)[0][0], dir_a=d)
    assert s['backscatter'] == -83.0


def _html():
    return _read(os.path.join(ROOT, 'viewer', 'viewer.html')).decode('utf-8')


def test_the_dialog_backscatter_row_is_an_input():
    body = _html().split('function frPanelHtml', 1)[1].split('function closeEditDialog', 1)[0]
    assert 'id="edit-bs"' in body and 'step="0.1"' in body


def test_the_dialog_sends_only_a_changed_backscatter():
    h = _html()
    sub = h.split('async function submitEdit', 1)[1].split('\n}\n', 1)[0]
    assert 'backscatter: bsChanged ? bs : null' in sub
    assert '!bsChanged' in sub


def test_the_route_forwards_the_backscatter():
    src = _read(os.path.join(ROOT, 'viewer', 'trace_server.py')).decode('utf-8')
    route = src.split("if u.path == '/api/trace_edit':", 1)[1].split('self._send_json({\'ok\'', 1)[0]
    assert "backscatter=data.get('backscatter')" in route

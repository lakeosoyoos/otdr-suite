"""Editing a trace's settings from the Viewer: copies only, whole direction
or one fiber, and the dialog is wired to the two routes.

The writer itself is tested in test_sor_writer.py; this file covers the
layer between the dialog and the writer: where the copies go, what is
refused, and that one bad file never stops the batch.
"""
import os
import re
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, 'viewer'))

import trace_server as TS                       # noqa: E402
from test_sor_writer import make_sor            # noqa: E402


def _folder(tmp_path, n=3, name='DNN1DNN2 A'):
    d = tmp_path / name
    d.mkdir()
    for i in range(1, n + 1):
        (d / f'DNN1DNN2{i:04d}.sor').write_bytes(make_sor(ior=1.47))
    return str(d)


# ── reading what the dialog pre-fills ────────────────────────────────────

def test_settings_prefill_from_the_file(tmp_path):
    d = _folder(tmp_path)
    s = TS.trace_settings('a', 2, dir_a=d)
    assert s['editable'] is True
    assert s['filename'] == 'DNN1DNN20002.sor'
    assert abs(s['ior'] - 1.47) < 1e-9
    assert 'cable_id' in s['identifiers']
    assert s['dest_default'] == 'DNN1DNN2 A edited'


def test_a_json_export_is_reported_not_editable(tmp_path):
    d = tmp_path / 'j'
    d.mkdir()
    (d / 'DNN1DNN20001.json').write_text('{}', encoding='utf-8')
    s = TS.trace_settings('a', 1, dir_a=str(d))
    assert s['editable'] is False and 'JSON' in s['why']


def test_a_file_that_does_not_round_trip_is_refused_with_a_reason(tmp_path):
    d = tmp_path / 'bad'
    d.mkdir()
    raw = bytearray(make_sor())
    raw[-1] ^= 0xFF                              # stale checksum
    (d / 'DNN1DNN20001.sor').write_bytes(bytes(raw))
    s = TS.trace_settings('a', 1, dir_a=str(d))
    assert s['editable'] is False and 'byte-exact' in s['why']


def test_an_unknown_fiber_is_an_error(tmp_path):
    with pytest.raises(ValueError):
        TS.trace_settings('a', 99, dir_a=_folder(tmp_path))


# ── writing copies ───────────────────────────────────────────────────────

def test_all_fibers_go_to_a_sibling_folder_and_the_originals_are_untouched(tmp_path):
    d = _folder(tmp_path)
    before = {f: open(os.path.join(d, f), 'rb').read() for f in os.listdir(d)}
    out = TS.edit_traces('a', 'all', ior=1.467, dir_a=d)
    assert out['written'] == [1, 2, 3] and out['skipped'] == []
    assert out['dest'] == os.path.join(str(tmp_path), 'DNN1DNN2 A edited')
    assert sorted(os.listdir(out['dest'])) == sorted(before)
    for f, raw in before.items():
        assert open(os.path.join(d, f), 'rb').read() == raw, 'original changed'
        edited = open(os.path.join(out['dest'], f), 'rb').read()
        assert abs(TS.read_ior(edited) - 1.467) < 1e-9
        assert TS.roundtrip_ok(edited)


def test_one_fiber_writes_one_copy(tmp_path):
    d = _folder(tmp_path)
    out = TS.edit_traces('a', [2], fields={'cable_id': 'NEWCABLE'}, dir_a=d)
    assert out['written'] == [2]
    assert os.listdir(out['dest']) == ['DNN1DNN20002.sor']
    edited = open(os.path.join(out['dest'], 'DNN1DNN20002.sor'), 'rb').read()
    assert TS.read_identifiers(edited)['cable_id'] == 'NEWCABLE'


def test_the_tech_names_the_folder_but_cannot_point_it_anywhere(tmp_path):
    d = _folder(tmp_path)
    out = TS.edit_traces('a', [1], ior=1.467, dest_name='fixed IOR', dir_a=d)
    assert out['dest'] == os.path.join(str(tmp_path), 'fixed IOR')
    for bad in ('../x', 'a/b', '..', '/tmp/x'):
        with pytest.raises(ValueError, match='folder name'):
            TS.edit_traces('a', [1], ior=1.467, dest_name=bad, dir_a=d)


def test_the_source_folder_is_never_the_destination(tmp_path):
    d = _folder(tmp_path)
    with pytest.raises(ValueError, match='copies only'):
        TS.edit_traces('a', [1], ior=1.467, dest_name='DNN1DNN2 A', dir_a=d)


def test_an_existing_copy_is_skipped_not_overwritten(tmp_path):
    d = _folder(tmp_path)
    first = TS.edit_traces('a', [1], ior=1.467, dir_a=d)
    marker = open(os.path.join(first['dest'], 'DNN1DNN20001.sor'), 'rb').read()
    again = TS.edit_traces('a', 'all', ior=1.46, dir_a=d)
    assert again['written'] == [2, 3]
    assert [s['fiber'] for s in again['skipped']] == [1]
    assert 'already exists' in again['skipped'][0]['reason']
    assert open(os.path.join(first['dest'], 'DNN1DNN20001.sor'), 'rb').read() == marker


def test_one_bad_file_does_not_stop_the_batch(tmp_path):
    d = _folder(tmp_path)
    raw = bytearray(make_sor())
    raw[-1] ^= 0xFF
    open(os.path.join(d, 'DNN1DNN20002.sor'), 'wb').write(bytes(raw))
    out = TS.edit_traces('a', 'all', ior=1.467, dir_a=d)
    assert out['written'] == [1, 3]
    assert out['skipped'] == [{'fiber': 2, 'reason': 'does not rebuild byte-exact'}]


# ── what is refused up front ─────────────────────────────────────────────

def test_a_fiber_id_cannot_be_stamped_on_every_fiber(tmp_path):
    d = _folder(tmp_path)
    with pytest.raises(ValueError, match='per-fiber'):
        TS.edit_traces('a', 'all', fields={'fiber_id': '7'}, dir_a=d)
    assert not os.path.exists(os.path.join(str(tmp_path), 'DNN1DNN2 A edited'))


def test_nothing_to_change_is_refused_and_blank_fields_count_as_nothing(tmp_path):
    d = _folder(tmp_path)
    with pytest.raises(ValueError, match='nothing to change'):
        TS.edit_traces('a', 'all', fields={'cable_id': '   '}, dir_a=d)


def test_an_ior_outside_the_band_is_refused_before_any_file_is_touched(tmp_path):
    d = _folder(tmp_path)
    with pytest.raises(ValueError, match='sane band'):
        TS.edit_traces('a', 'all', ior=1.2, dir_a=d)
    assert not os.path.exists(os.path.join(str(tmp_path), 'DNN1DNN2 A edited'))


def test_an_unknown_field_is_refused(tmp_path):
    with pytest.raises(ValueError, match='not editable'):
        TS.edit_traces('a', 'all', fields={'wavelength': '1550'}, dir_a=_folder(tmp_path))


def test_the_b_direction_uses_the_b_folder(tmp_path):
    db = _folder(tmp_path, n=1, name='DNN2DNN1 B')
    out = TS.edit_traces('b', 'all', ior=1.467, dir_a='/nonexistent', dir_b=db)
    assert out['written'] == [1]
    assert out['dest'].endswith('DNN2DNN1 B edited')


# ── the page is wired to the routes ──────────────────────────────────────

def _html():
    return open(os.path.join(ROOT, 'viewer', 'viewer.html'), encoding='utf-8').read()


def _server_src():
    return open(os.path.join(ROOT, 'viewer', 'trace_server.py'), encoding='utf-8').read()


def test_the_span_menu_offers_the_edit_dialog():
    h = _html()
    assert 'Edit trace settings' in h
    assert 'showEditDialog(dir, fiber)' in h


def test_the_dialog_talks_to_both_routes():
    h = _html()
    assert '/api/trace_settings?dir=' in h
    assert "fetch('/api/trace_edit'" in h
    s = _server_src()
    assert "u.path == '/api/trace_settings'" in s
    assert "u.path == '/api/trace_edit'" in s


def test_the_edit_route_is_local_only():
    s = _server_src()
    body = s.split("u.path == '/api/trace_edit'", 1)[1].split('return', 1)[0]
    assert '_origin_is_local' in body


def test_the_dialog_greys_out_fiber_id_for_all_fibers():
    h = _html()
    m = re.search(r'fid\.disabled = all;', h)
    assert m, 'fiber id must be disabled in the all-fibers scope'


# ── the dialog shows what FastReporter shows ─────────────────────────────

def _sor_with_fr_records():
    """A .sor whose proprietary stream carries FR's own identifier records,
    including two (CustomerName, CompanyName) that GenParams has no slot for."""
    import struct
    from test_sor_writer import _genparams, _fxdparams, _rec, _u16, _prop_from_stream
    st = bytearray(b'\x00' * 16)
    _rec(st, b'UserNameA', 4, _u16('G. Kolok'))
    _rec(st, b'CustomerName', 4, _u16('Lumen'))
    _rec(st, b'CompanyName', 4, _u16('ZerodB'))
    _rec(st, b'Comment', 4, _u16('West 144f'))
    _rec(st, b'Name', 4, _u16('Cable ID'))
    _rec(st, b'Value', 4, _u16('FRCABLE'))
    blocks = [
        TS.Block(b'GenParams', 2, _genparams()),
        TS.Block(b'FxdParams', 2, _fxdparams(ior=1.47)),
        TS.Block(b'KeyEvents', 2, struct.pack('<H', 0)),
        TS.Block(b'DataPts', 2, b'\x00' * 64),
        TS.Block(b'ExfoNewProprietaryBlock 01', 1, _prop_from_stream(bytes(st))),
        TS.Block(b'Cksum', 2, b'\x00\x00'),
    ]
    return TS.build(2, blocks)


def test_fr_only_identifiers_are_read_from_the_proprietary_stream():
    got = TS.read_fr_identifiers(_sor_with_fr_records())
    assert got['customer'] == 'Lumen' and got['company'] == 'ZerodB'
    assert got['operator'] == 'G. Kolok' and got['comment'] == 'West 144f'
    assert got['cable_id'] == 'FRCABLE'          # the Identifiers list's Value


def test_a_file_without_a_proprietary_block_has_no_fr_identifiers():
    import struct
    from test_sor_writer import _genparams, _fxdparams
    raw = TS.build(2, [TS.Block(b'GenParams', 2, _genparams()),
                       TS.Block(b'FxdParams', 2, _fxdparams()),
                       TS.Block(b'KeyEvents', 2, struct.pack('<H', 0)),
                       TS.Block(b'Cksum', 2, b'\x00\x00')])
    assert TS.read_fr_identifiers(raw) == {}


def test_the_dialog_prefill_merges_fr_fields_under_genparams(tmp_path):
    """A tech must not be shown a blank Customer on a file whose Customer FR
    displays; and where both sides carry a field, GenParams (what our readers
    speak) is the value shown."""
    d = tmp_path / 'X A'
    d.mkdir()
    (d / 'X0001.sor').write_bytes(_sor_with_fr_records())
    ids = TS.trace_settings('a', 1, dir_a=str(d))['identifiers']
    assert ids['customer'] == 'Lumen' and ids['company'] == 'ZerodB'
    assert ids['cable_id'] == 'CABLE1'           # GenParams wins over FRCABLE


# ── the declared span goes into the copies ───────────────────────────────

def _real_folder(tmp_path, n=2):
    src = os.path.join(HERE, 'fixtures', 'frspan', 'DNN1DNN20001.sor')
    d = tmp_path / 'DNN1DNN2 A'
    d.mkdir()
    for i in range(1, n + 1):
        (d / f'DNN1DNN2{i:04d}.sor').write_bytes(open(src, 'rb').read())
    return str(d)


def test_a_declared_span_is_written_into_every_copy(tmp_path):
    d = _real_folder(tmp_path)
    out = TS.edit_traces('a', 'all', span={'start_km': 1.0361, 'end_km': 2.0415}, dir_a=d)
    assert out['written'] == [1, 2] and out['skipped'] == []
    fr = open(os.path.join(HERE, 'fixtures', 'frspan', 'DNN1DNN20001_fr_span_e3_e4.sor'), 'rb').read()
    for n in (1, 2):
        e = open(os.path.join(out['dest'], f'DNN1DNN2{n:04d}.sor'), 'rb').read()
        s = TS.read_span(e)
        assert s['start_km'] == 0.0 and abs(s['offset_km'] - 1.0361) < 0.0005
        _, a = TS.split(e); _, b = TS.split(fr)
        assert TS._find(a, b'GenParams').body == TS._find(b, b'GenParams').body


def test_a_span_that_misses_every_event_skips_that_fiber_with_the_reason(tmp_path):
    d = _real_folder(tmp_path)
    out = TS.edit_traces('a', 'all', span={'start_km': 0.5}, dir_a=d)
    assert out['written'] == []
    assert len(out['skipped']) == 2 and 'from the nearest event' in out['skipped'][0]['reason']


def test_span_and_ior_compose_in_one_save(tmp_path):
    d = _real_folder(tmp_path, n=1)
    out = TS.edit_traces('a', [1], ior=1.467, span={'end_km': 2.0415}, dir_a=d)
    e = open(os.path.join(out['dest'], 'DNN1DNN20001.sor'), 'rb').read()
    assert abs(TS.read_ior(e) - 1.467) < 1e-9 and TS.read_span(e)['end_km'] is not None


def test_the_route_forwards_the_span_and_the_dialog_offers_it():
    s = _server_src()
    assert "span=dict(data.get('span') or {})" in s
    h = _html()
    assert 'id="edit-span"' in h and 'write it into the copies' in h


# ── the feature says it is there ─────────────────────────────────────────

def test_the_event_table_tells_the_tech_that_right_click_exists():
    """Right-click is the only way into the span menu and the settings
    editor.  Until this line the page never said so, and a tech who does not
    think to try it concludes the feature is missing."""
    h = _html()
    assert 'RIGHT_CLICK_HINT' in h
    hint = h.split('const RIGHT_CLICK_HINT =', 1)[1].split(';', 1)[0].lower()
    assert 'right-click' in hint
    # BOTH jobs the menu does are named.  "more options" would be shorter
    # still, but a generic label is the kind of thing eyes skip, and a tech
    # hunting a wrong IOR has to see that this menu is where settings live.
    assert 'span' in hint and 'settings' in hint


def test_every_event_table_heading_carries_the_hint():
    """One fiber, a pair, or a whole folder — all three headings get it."""
    h = _html()
    block = h.split('const RIGHT_CLICK_HINT =', 1)[1].split('const fmt ', 1)[0]
    assert block.count('click a column to zoom') == 3, 'the three headings moved'
    assert ') + RIGHT_CLICK_HINT;' in block, 'the hint must apply to all three'

"""
.trc through the hub's folder intake (folder_intake, stdlib only).

A .trc is one direction at several wavelengths.  Intake has to treat it like
a .sor: find it loose and in zips, split a mixed folder into directions --
by filename, and by the sites stored in the file when the names carry none --
and let it vote in the foreign-file audit.  The header fields are read
without the engine's reader, so they are pinned against it here.
"""
import os
import shutil
import struct
import sys
import zipfile
import zlib

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, 'fixtures')
TRC = os.path.join(FIX, 'trc')
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, 'splicereport'))

import folder_intake as fi                                 # noqa: E402
import sor_reader324802a as sr                            # noqa: E402

DECL = os.path.join(TRC, 'TRCDECL0001_155016251310.trc')
SPAN = os.path.join(TRC, 'TRCSPAN0001_131015501625.trc')


def _with_sites(src, dst, loc_a, loc_b):
    """Copy a .trc with its stored LocationA/LocationB rewritten (same length),
    re-chunked at the original boundaries."""
    data = open(src, 'rb').read()
    inner = data.find(b'AppReg Format Ex', 1)
    off, chunks = inner + 36, []
    while off + 4 <= len(data):
        n = struct.unpack_from('<I', data, off)[0]
        if n < 2 or off + 4 + n > len(data):
            break
        chunks.append(zlib.decompress(data[off + 4:off + 4 + n]))
        off += 4 + n
    stream = bytearray(b''.join(chunks))
    for name, value in (('LocationA', loc_a), ('LocationB', loc_b)):
        i = stream.find(b'\x00' + name.encode() + b'\x00') + 1
        size = struct.unpack_from('<I', stream, i - 8)[0]
        voff = i + len(name) + 1
        raw = (value + '\x00').encode('utf-16-le')
        assert len(raw) <= size
        stream[voff:voff + size] = raw.ljust(size, b'\x00')
    out, p = bytearray(data[:inner + 36]), 0
    for c in chunks:
        z = zlib.compress(bytes(stream[p:p + len(c)]))
        out += struct.pack('<I', len(z)) + z
        p += len(c)
    out += data[off:]
    with open(dst, 'wb') as fh:
        fh.write(out)


def test_found_loose_and_in_zips(tmp_path):
    shutil.copy(DECL, tmp_path / 'TRCDECL0001_155016251310.trc')
    with zipfile.ZipFile(tmp_path / 'more.zip', 'w') as z:
        z.write(SPAN, 'TRCSPAN0001_131015501625.trc')
    loose = fi.find_otdr_files(str(tmp_path))
    assert [os.path.basename(p) for p in loose] == ['TRCDECL0001_155016251310.trc']
    both = fi.find_otdr_files_with_zips(str(tmp_path), str(tmp_path / 'x'))
    assert sorted(os.path.basename(p) for p in both) == [
        'TRCDECL0001_155016251310.trc', 'TRCSPAN0001_131015501625.trc']


@pytest.mark.parametrize('path', [DECL, SPAN], ids=os.path.basename)
def test_header_agrees_with_the_engine_reader(path):
    """The stdlib read and the engine's parse_trc name the same sites, pulse
    and data spacing, so direction and foreign-file votes see what the
    engine will load."""
    h = fi.trc_header(path)
    r = sr.parse_trc(path)[0]
    assert h['loc_ordered'] == (r['gen_loc_a'].upper(), r['gen_loc_b'].upper())
    assert h['loc_pair'] == tuple(sorted(h['loc_ordered']))
    assert h['pulse_ns'] == round(r['fxd_pulse_ns'])
    assert h['acq_range'] == r['acq_range']
    assert fi.sor_location_pair(path) == h['loc_ordered']
    assert fi.sor_header(path) == {k: h[k] for k in ('loc_pair', 'pulse_ns', 'acq_range')}
    # The shot time, as the engine reads it (UTC).
    import calendar
    import datetime
    assert calendar.timegm(datetime.datetime.strptime(
        h['date_utc'], '%Y-%m-%dT%H:%M:%S').timetuple()) == r['date_time']


def test_not_a_trc_reads_empty(tmp_path):
    bad = tmp_path / 'x.trc'
    bad.write_bytes(b'nothing here' * 50)
    assert fi.trc_header(str(bad)) == {}
    assert fi.sor_location_pair(str(bad)) is None


def test_site_less_names_split_by_stored_sites(tmp_path):
    """0001/0002 names say nothing about direction; the sites stored in each
    .trc do, and a reversed pair is two directions of one span."""
    for i in (1, 2):
        _with_sites(DECL, tmp_path / f'{i:04d}_155016251310a.trc', 'SITA', 'SITB')
        _with_sites(DECL, tmp_path / f'{i:04d}_155016251310b.trc', 'SITB', 'SITA')
    paths = fi.find_otdr_files(str(tmp_path))
    groups, how = fi.resolve_direction_groups(paths)
    assert how == 'location'
    assert sorted(groups) == ['SITA → SITB', 'SITB → SITA']
    assert all(len(v) == 2 for v in groups.values())


def test_named_trc_split_by_prefix(tmp_path):
    for i in (1, 2):
        shutil.copy(DECL, tmp_path / f'SITASITB{i:03d}_155016251310.trc')
        shutil.copy(DECL, tmp_path / f'SITBSITA{i:03d}_155016251310.trc')
    groups, how = fi.resolve_direction_groups(fi.find_otdr_files(str(tmp_path)))
    assert how == 'prefix' and sorted(groups) == ['SITASITB', 'SITBSITA']


def test_direction_token_with_a_multi_wavelength_suffix():
    assert fi.direction_prefix('MSO401-MSO402-0001-AB_155016251310.trc') == 'MSO-AB'
    assert fi.direction_prefix('MSO401-MSO402-0001-BA_155016251310.trc') == 'MSO-BA'
    assert fi.direction_prefix('MSO401-MSO402-0001-BA_1550.sor') == 'MSO-BA'


def test_stray_trc_from_another_job_is_foreign():
    """A .trc shot elsewhere with another setup, dropped among a span's .sor,
    is voted out like a stray .sor would be."""
    sors = sorted(os.path.join(FIX, 'span_A', f) for f in os.listdir(os.path.join(FIX, 'span_A'))
                  if f.endswith('.sor'))
    sors += sorted(os.path.join(FIX, 'span_B', f) for f in os.listdir(os.path.join(FIX, 'span_B'))
                   if f.endswith('.sor'))
    kept, foreign = fi.audit_foreign_files(sors + [DECL])
    assert [f['name'] for f in foreign] == [os.path.basename(DECL)]
    assert kept == sors

"""The .zfc / .zdb share-file container (folder_intake.share_*): the manifest decides what a file is,
old extensions keep opening, and bad files get a message a tech can act on."""
import json
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import folder_intake as _fi  # noqa: E402


class bf:  # the share-file API under short names
    FORMAT, MANIFEST, KINDS = _fi.SHARE_FORMAT, _fi.SHARE_MANIFEST, _fi.SHARE_KINDS
    ShareFileError = _fi.ShareFileError
    extensions, save_extension = staticmethod(_fi.share_extensions), staticmethod(_fi.share_save_extension)
    write, open_file = staticmethod(_fi.share_write), staticmethod(_fi.share_open)
    register, dispatch = staticmethod(_fi.share_register), staticmethod(_fi.share_dispatch)


def test_extensions_per_kind():
    assert bf.save_extension('field-capture') == '.zfc'
    assert bf.save_extension('project') == '.zdb'
    assert set(bf.extensions()) == {'.zfc', '.zdb'}


def test_round_trip_and_extension_added(tmp_path):
    src = tmp_path / 'photo.jpg'
    src.write_bytes(b'\xff\xd8jpeg')
    out = bf.write(tmp_path / 'Span 4 A', 'field-capture',
                   {'capture.json': b'{"a": 1}', 'photos/1.jpg': src}, meta={'span': 4})
    assert out.name == 'Span 4 A.zfc'
    assert not list(tmp_path.glob('*.part'))
    sf = bf.open_file(out)
    assert (sf.kind, sf.version, sf.label) == ('field-capture', 1, 'Field Capture')
    assert sf.manifest['meta'] == {'span': 4}
    assert sorted(sf.names) == ['capture.json', 'photos/1.jpg']
    assert sf.read('photos/1.jpg') == b'\xff\xd8jpeg'
    dest = sf.extract_to(tmp_path / 'x')
    assert (dest / 'capture.json').read_bytes() == b'{"a": 1}'
    assert not (dest / bf.MANIFEST).exists()


def test_manifest_not_extension_decides(tmp_path):
    out = bf.write(tmp_path / 'p', 'project', {'project.json': b'{}'})
    renamed = out.rename(tmp_path / 'wrongly named.zfc')
    assert bf.open_file(renamed).kind == 'project'
    with pytest.raises(bf.ShareFileError, match='is a Project file, not a Field Capture'):
        bf.open_file(renamed, expect='field-capture')


def test_renamed_extension_keeps_old_files_opening(tmp_path, monkeypatch):
    old = bf.write(tmp_path / 'old', 'project', {'project.json': b'{}'})
    monkeypatch.setitem(_fi.SHARE_KINDS, 'project', dict(_fi.SHARE_KINDS['project'], exts=('.zb', '.zdb')))
    assert bf.save_extension('project') == '.zb'
    assert '.zdb' in bf.extensions('project')
    assert bf.open_file(old).kind == 'project'
    assert bf.write(tmp_path / 'new', 'project', {}).suffix == '.zb'


def _raw(tmp_path, manifest, name='f.zdb'):
    p = tmp_path / name
    with zipfile.ZipFile(p, 'w') as z:
        if manifest is not None:
            z.writestr(bf.MANIFEST, manifest if isinstance(manifest, str) else json.dumps(manifest))
        z.writestr('x.txt', 'x')
    return p


@pytest.mark.parametrize('manifest, msg', [
    (None, 'not an OTDR Suite file'),
    ('{not json', 'damaged'),
    ({'format': 'other', 'kind': 'project', 'version': 1}, 'not an OTDR Suite file'),
    ({'format': bf.FORMAT, 'kind': 'report', 'version': 1}, 'does not know'),
    ({'format': bf.FORMAT, 'kind': 'project', 'version': 99}, 'newer OTDR Suite'),
    ({'format': bf.FORMAT, 'kind': 'project'}, 'no format version'),
])
def test_bad_files_explain_themselves(tmp_path, manifest, msg):
    with pytest.raises(bf.ShareFileError, match=msg):
        bf.open_file(_raw(tmp_path, manifest))


def test_not_a_zip_and_missing(tmp_path):
    p = tmp_path / 'half.zfc'
    p.write_bytes(b'PK\x03\x04truncated')
    with pytest.raises(bf.ShareFileError, match='damaged'):
        bf.open_file(p)
    with pytest.raises(bf.ShareFileError, match='not found'):
        bf.open_file(tmp_path / 'gone.zfc')


@pytest.mark.parametrize('bad', ['../evil.txt', '/abs.txt', 'C:/x.txt', 'a/../../b', 'a//b'])
def test_unsafe_member_names_refused(tmp_path, bad):
    p = tmp_path / 'e.zdb'
    with zipfile.ZipFile(p, 'w') as z:
        z.writestr(bf.MANIFEST, json.dumps({'format': bf.FORMAT, 'kind': 'project', 'version': 1}))
        z.writestr(bad, 'x')
    with pytest.raises(bf.ShareFileError, match='Unsafe'):
        bf.open_file(p)
    with pytest.raises(bf.ShareFileError, match='Unsafe'):
        bf.write(tmp_path / 'w', 'project', {bad: b'x'})


def test_dispatch(tmp_path, monkeypatch):
    monkeypatch.setattr(_fi, '_SHARE_HANDLERS', {})
    fc = bf.write(tmp_path / 'c', 'field-capture', {})
    with pytest.raises(bf.ShareFileError, match='not available in this version'):
        bf.dispatch(fc)
    bf.register('field-capture', lambda sf: ('imported', sf.kind))
    assert bf.dispatch(fc) == ('imported', 'field-capture')
    with pytest.raises(ValueError):
        bf.register('nope', print)


def test_failed_write_leaves_nothing(tmp_path):
    with pytest.raises(ValueError):
        bf.write(tmp_path / 'c', 'project', {bf.MANIFEST: b'{}'})
    assert list(tmp_path.iterdir()) == []

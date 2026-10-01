"""A staged file is written whole or not at all.

A dropped file used to be written straight to its final name and its name
marked as arrived before the write finished.  A write cut off partway (a zip
member that fails its CRC check, a full disk) then left half a file loaded as
a trace, and the page's retry with the whole file (#425) was taken for a
DIFFERENT file under the same name: kept aside as a repeat (#443) and
reported as not loaded, while the half file stayed on the chart.

Now each file is written under a temporary name and moved into place once
complete, its name is marked only after that, and the bytes a failed write
was charged are given back to the drop's size budget.
"""
from __future__ import annotations

import io
import os
import sys
import zipfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, 'viewer'))
import trace_server as TS                       # noqa: E402
from conftest import FIXTURE_SPLICE_A_DIR       # noqa: E402


@pytest.fixture(autouse=True)
def _own_temp(tmp_path, monkeypatch):
    monkeypatch.setenv('TMPDIR', str(tmp_path))
    import tempfile
    tempfile.tempdir = None
    yield
    tempfile.tempdir = None
    TS.set_dirs(None, None)
    TS.CONFIG.pop('dropped_at', None)


def _real(n):
    return [(p.name, p.read_bytes()) for p in sorted(FIXTURE_SPLICE_A_DIR.glob('*.sor'))[:n]]


def _in_dir(tok):
    return os.path.join(TS._DROPS[tok]['dir'], 'in')


def test_a_write_cut_off_partway_leaves_nothing_and_the_retry_stages_normally():
    files = _real(4)
    name, data = files[2]
    tok = TS.drop_begin()
    for n, d in files[:2]:
        TS.drop_file(tok, n, d)
    before = TS._DROPS[tok]['bytes']

    def half_then_fail(fh):
        fh.write(data[:len(data) // 2])
        raise OSError('no space left on device')

    with pytest.raises(OSError):
        TS._drop_take(TS._DROPS[tok], len(data))
        TS._stage_write(TS._DROPS[tok], _in_dir(tok), name, half_then_fail, size=len(data))
    assert name not in os.listdir(_in_dir(tok))             # no half file
    assert name.lower() not in TS._DROPS[tok]['seen']       # not marked arrived
    assert TS._DROPS[tok]['bytes'] == before                # budget given back
    assert not [f for f in os.listdir(TS._DROPS[tok]['dir']) if f.startswith('.part_')]
    # the page's retry of that file, then the rest
    assert TS.drop_file(tok, name, data, retry=True)['files'] == 1
    TS.drop_file(tok, *files[3])
    out = TS.drop_end(tok)
    assert out['a_count'] == 4
    assert out['repeated'] == [] and out['repeats_placed'] == []
    with open(os.path.join(out['dir_a'], name), 'rb') as fh:
        assert fh.read() == data                            # the whole file


def _zip(files, corrupt=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_STORED) as zf:
        for n, d in files:
            zf.writestr('span/' + n, d)
    raw = bytearray(buf.getvalue())
    if corrupt:
        # flip a byte inside that member's data: it extracts fully, then
        # fails its CRC check at the end of the read
        d = files[corrupt][1]
        off = next(o for o in range(len(d) // 2, len(d) - 64, 64)
                   if bytes(raw).count(d[o:o + 64]) == 1)
        raw[bytes(raw).index(d[off:off + 64])] ^= 0xFF
    return bytes(raw)


def test_a_zip_member_that_fails_its_check_leaves_nothing_and_the_retry_loads_it():
    files = _real(3)
    tok = TS.drop_begin()
    with pytest.raises(zipfile.BadZipFile):
        TS.drop_file(tok, 'span.zip', _zip(files, corrupt=1))
    staged = os.listdir(_in_dir(tok))
    assert files[1][0] not in staged                        # the bad member is not there
    assert files[0][0] in staged                            # the one before it is
    # the page retries the whole zip, sound this time
    r = TS.drop_file(tok, 'span.zip', _zip(files), retry=True)
    assert r['files'] == 3
    out = TS.drop_end(tok)
    assert out['a_count'] == 3
    assert out['repeated'] == [] and out['repeats_placed'] == []
    with open(os.path.join(out['dir_a'], files[1][0]), 'rb') as fh:
        assert fh.read() == files[1][1]

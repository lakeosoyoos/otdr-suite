"""Files > Direction > A->B / B->A, saved the way FastReporter saves it.

Ground truth: desktop/tests/fixtures/frdir/MILELM0001_1550_fr_saved_ab.sor is
span_B/MILELM0001_1550.sor after FR 3 switched it to A->B and saved.  The
only VALUE that changed was the proprietary int32 `LocationsDirection` 2 -> 1
(FR also inserted a MinimumCumulativeLoss record and an ExfoAdditionalInfo
block, which it does on any save)."""
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'viewer'))
import trace_server as T  # noqa: E402

FIX = os.path.join(ROOT, 'desktop', 'tests', 'fixtures')
ORIG = os.path.join(FIX, 'span_B', 'MILELM0001_1550.sor')
FR_AB = os.path.join(FIX, 'frdir', 'MILELM0001_1550_fr_saved_ab.sor')
A_FILE = os.path.join(FIX, 'span_A', 'ELMMIL0001_1550.sor')


def _raw(p):
    with open(p, 'rb') as f:
        return f.read()


def test_read_direction_follows_the_folder_on_real_files():
    assert T.read_direction(_raw(A_FILE)) == 'a'
    assert T.read_direction(_raw(ORIG)) == 'b'


def test_fr_saved_file_reads_as_the_new_direction():
    assert T.read_direction(_raw(FR_AB)) == 'a'


def test_set_direction_matches_what_fr_writes():
    out = T.set_direction(_raw(ORIG), 'a')
    assert T.read_direction(out) == 'a'
    # Everything else in the proprietary stream is untouched: same records,
    # same payloads, apart from the one int32.
    def stream(data):
        mv, bl = T.split(data)
        b = next(x for x in bl if x.name.startswith(b'ExfoNewProprietaryBlock'))
        _, ch, _ = T._prop_chunks(b.body)
        return b''.join(d for _, d in ch)
    s0, s1 = stream(_raw(ORIG)), stream(out)
    assert len(s0) == len(s1)
    off = T._prop_locdir(s0)
    assert s0[:off] == s1[:off] and s0[off + 4:] == s1[off + 4:]
    assert struct.unpack_from('<i', s1, off)[0] == 1
    # And the FR-saved file agrees on the value.
    sfr = stream(_raw(FR_AB))
    assert struct.unpack_from('<i', sfr, T._prop_locdir(sfr))[0] == 1


def test_set_direction_round_trips_and_leaves_bellcore_alone():
    out = T.set_direction(_raw(ORIG), 'a')
    assert T.roundtrip_ok(out)
    for name in (b'GenParams', b'FxdParams', b'KeyEvents', b'DataPts'):
        assert T._find(T.split(out)[1], name).body == T._find(T.split(_raw(ORIG))[1], name).body
    # Re-deflating the proprietary chunks is not byte-identical to FR's
    # compressor, so compare CONTENT: the decompressed stream comes back exact.
    back = T.set_direction(out, 'b')
    def stream(data):
        b = next(x for x in T.split(data)[1] if x.name.startswith(b'ExfoNewProprietaryBlock'))
        return b''.join(d for _, d in T._prop_chunks(b.body)[1])
    assert stream(back) == stream(_raw(ORIG))


def test_edit_traces_writes_a_copy_with_the_new_direction(tmp_path):
    # span_A, not span_B: the span_B fixtures carry a stale stored checksum,
    # so the writer's byte-exact guard (rightly) refuses to edit them.
    import shutil
    src = tmp_path / 'span_A'
    src.mkdir()
    shutil.copy(A_FILE, src / 'ELMMIL0001_1550.sor')
    res = T.edit_traces('a', [1], dir_a=str(src), new_direction='b')
    assert res['written'] == [1] and not res['skipped']
    dst = os.path.join(res['dest'], 'ELMMIL0001_1550.sor')
    assert T.read_direction(_raw(dst)) == 'b'
    assert T.read_direction(_raw(str(src / 'ELMMIL0001_1550.sor'))) == 'a', 'original untouched'


def test_viewer_wires_save_and_honours_the_stored_direction():
    html = open(os.path.join(ROOT, 'viewer', 'viewer.html'), encoding='utf-8').read()
    assert 'async function saveFileDirection(' in html
    fn = html[html.index('async function saveFileDirection('):][:700]
    assert "new_direction: dir" in fn and "'/api/trace_edit'" in fn
    assert 'gStoredDir[key] = data.stored_dir' in html
    eff = html[html.index('function effDir('):][:160]
    assert 'gDirOverride[k] || gStoredDir[k] || src' in eff
    srv = open(os.path.join(ROOT, 'viewer', 'trace_server.py'), encoding='utf-8').read()
    assert "'stored_dir': stored" in srv

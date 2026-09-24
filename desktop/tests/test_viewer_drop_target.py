"""The Viewer's FILES panel is a drop target: files, a folder, or a zip.

The hub's pages take drag-and-drop through Streamlit's uploader; the Viewer is
its own page and had none (the boss asked for one).  The browser gives bytes,
never paths, so each file is POSTed to the local trace server, staged into a
temp folder, split into A and B by filename prefix with the hub's own rule
(folder_intake.direction_prefix), and the server points itself at the result.
A fresh drop also re-seeds the hub sidebar's folder boxes, so a hub rerun does
not put the old paths back.

A drop holding ONE direction fills whichever side is EMPTY.  2026-09-18, the
field: "if we drag in our a side into files in viewer and then we try to drag
in our b side, it seems to override the a side that were in there" — both
drops became A and set_dirs overwrote both folders, so the second drop threw
the A traces away and loaded the B folder as the A side.

And the files are asked first.  2026-09-19: "can we not determine direction
when theyre dropped in? so if we drop in B first we know it?"  EXFO stamps
LocationsDirection into every .sor and FastReporter's Direction column reads
it, so a folder can name its own side — B dropped into an empty Viewer lands
on B.  The positional rule stays underneath it, for files that do not carry
the field, folders that hold both directions, and the one real span whose two
directions are both stamped A.
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
from test_sor_writer import make_sor            # noqa: E402


@pytest.fixture(autouse=True)
def _own_temp(tmp_path, monkeypatch):
    monkeypatch.setenv('TMPDIR', str(tmp_path))
    import tempfile
    tempfile.tempdir = None
    yield
    tempfile.tempdir = None
    TS.set_dirs(None, None)
    TS.CONFIG.pop('dropped_at', None)


def test_two_prefixes_split_into_a_and_b_and_the_server_points_at_them():
    tok = TS.drop_begin()
    for name in ('ROMTUC001_1550.sor', 'ROMTUC002_1550.sor', 'TUCROM001_1550.sor'):
        assert TS.drop_file(tok, name, make_sor(ior=1.47))['files'] == 1
    out = TS.drop_end(tok)
    assert out['a_prefix'] == 'ROMTUC' and out['a_count'] == 2
    assert out['b_prefix'] == 'TUCROM' and out['b_count'] == 1
    assert TS.CONFIG['dir_a'] == out['dir_a'] and TS.CONFIG['dir_b'] == out['dir_b']
    assert [n for n, _ in TS.list_fibers(out['dir_a'])] == [1, 2]
    assert [n for n, _ in TS.list_fibers(out['dir_b'])] == [1]
    assert TS.CONFIG['dropped_at'] > 0


def test_one_prefix_with_nothing_loaded_is_a_alone():
    tok = TS.drop_begin()
    TS.drop_file(tok, 'SEANOR001_1550.sor', make_sor(ior=1.47))
    out = TS.drop_end(tok)
    assert out['added'] == 'A'
    assert out['dir_b'] is None and out['b_count'] == 0
    assert TS.CONFIG['dir_b'] is None


def _drop(*names):
    tok = TS.drop_begin()
    for n in names:
        TS.drop_file(tok, n, make_sor(ior=1.47))
    return TS.drop_end(tok)


def test_the_a_side_then_the_b_side_loads_both():
    a = _drop('ROMTUC001_1550.sor', 'ROMTUC002_1550.sor')
    assert a['added'] == 'A' and a['dir_b'] is None
    b = _drop('TUCROM001_1550.sor', 'TUCROM002_1550.sor')
    assert b['added'] == 'B'                       # the empty side, not A again
    assert b['dir_a'] == a['dir_a']                # the A folder is untouched
    assert b['a_prefix'] == 'ROMTUC' and b['a_count'] == 2
    assert b['b_prefix'] == 'TUCROM' and b['b_count'] == 2
    assert TS.CONFIG['dir_a'] == a['dir_a'] and TS.CONFIG['dir_b'] == b['dir_b']
    assert [n for n, _ in TS.list_fibers(TS.CONFIG['dir_a'])] == [1, 2]
    assert [n for n, _ in TS.list_fibers(TS.CONFIG['dir_b'])] == [1, 2]


def test_the_b_side_is_staged_in_a_folder_named_b():
    """/api/list labels each side with its folder's basename, so a B-side drop
    staged into an 'A' folder would read 'B: A' on the page."""
    _drop('ROMTUC001_1550.sor', 'ROMTUC002_1550.sor')
    b = _drop('TUCROM001_1550.sor', 'TUCROM002_1550.sor')
    assert os.path.basename(b['dir_b']) == 'B'
    assert os.path.basename(b['dir_a']) == 'A'


def test_the_same_folder_again_refreshes_its_own_side():
    """Re-dropping a folder is a refresh of the side it is already on — it
    must not turn into the other direction and give the span two A sides."""
    a = _drop('ROMTUC001_1550.sor', 'ROMTUC002_1550.sor')
    b = _drop('TUCROM001_1550.sor', 'TUCROM002_1550.sor')
    again = _drop('ROMTUC001_1550.sor', 'ROMTUC002_1550.sor')
    assert again['added'] == 'A'
    assert again['dir_a'] not in (a['dir_a'], None)      # the fresh staging
    assert again['dir_b'] == b['dir_b']                  # B kept as it was
    assert again['a_prefix'] == 'ROMTUC' and again['b_prefix'] == 'TUCROM'
    once_more = _drop('TUCROM001_1550.sor', 'TUCROM002_1550.sor')
    assert once_more['added'] == 'B'
    assert once_more['dir_a'] == again['dir_a']


def test_a_single_drop_with_both_sides_full_starts_a_new_span():
    """Both sides loaded and an unrelated folder arrives: it is a different
    span, not a third direction — and this is the way back to a clean slate."""
    _drop('ROMTUC001_1550.sor', 'ROMTUC002_1550.sor')
    _drop('TUCROM001_1550.sor', 'TUCROM002_1550.sor')
    new = _drop('SEANOR001_1550.sor', 'SEANOR002_1550.sor')
    assert new['added'] == 'A'
    assert new['a_prefix'] == 'SEANOR'
    assert new['dir_b'] is None and new['b_prefix'] is None and new['b_count'] == 0
    assert TS.CONFIG['dir_b'] is None


def test_both_directions_in_one_drop_still_replace_both_sides():
    _drop('SEANOR001_1550.sor', 'SEANOR002_1550.sor')
    out = _drop('ROMTUC001_1550.sor', 'TUCROM001_1550.sor')
    assert out['added'] == 'AB'
    assert out['a_prefix'] == 'ROMTUC' and out['b_prefix'] == 'TUCROM'
    assert TS.CONFIG['dir_a'] == out['dir_a'] and TS.CONFIG['dir_b'] == out['dir_b']


def test_a_side_whose_folder_has_gone_is_not_treated_as_loaded(tmp_path):
    """A path left in CONFIG for a folder that has since moved is not a loaded
    side: the drop replaces it instead of filling B and leaving the dead one
    on screen."""
    gone = tmp_path / 'moved_away'
    gone.mkdir()
    TS.set_dirs(str(gone), None)
    gone.rmdir()
    out = _drop('ROMTUC001_1550.sor', 'ROMTUC002_1550.sor')
    assert out['added'] == 'A'
    assert out['dir_a'] != str(gone) and out['dir_b'] is None


def test_a_drop_fills_an_empty_a_side_under_a_hub_picked_b():
    TS.set_dirs(None, None)
    b_only = _drop('TUCROM001_1550.sor', 'TUCROM002_1550.sor')
    TS.set_dirs(None, b_only['dir_a'])              # hub picked B and no A
    out = _drop('ROMTUC001_1550.sor', 'ROMTUC002_1550.sor')
    assert out['added'] == 'A'
    assert out['dir_b'] == b_only['dir_a']          # the B pick survives


def test_a_zip_is_unpacked_flat_and_guarded():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('span/ROMTUC001_1550.sor', make_sor(ior=1.47))
        zf.writestr('span/notes.txt', 'ignored')
        zf.writestr('../../evil/TUCROM009_1550.sor', make_sor(ior=1.47))   # zip-slip name
        zf.writestr('__MACOSX/._ROMTUC001_1550.sor', b'junk')
    tok = TS.drop_begin()
    r = TS.drop_file(tok, 'span.zip', buf.getvalue())
    assert r['files'] == 2
    out = TS.drop_end(tok)
    assert out['a_prefix'] == 'ROMTUC' and out['b_prefix'] == 'TUCROM'
    assert sorted(os.listdir(out['dir_b'])) == ['TUCROM009_1550.sor']   # flattened, inside


def test_names_are_sanitised_and_non_traces_are_skipped():
    tok = TS.drop_begin()
    r = TS.drop_file(tok, '../../etc/ROMTUC001_1550.sor', make_sor(ior=1.47))
    assert r['name'] == 'ROMTUC001_1550.sor'
    assert TS.drop_file(tok, 'photo.jpg', b'x')['skipped'] == 'not a trace file'
    with pytest.raises(ValueError):
        TS.drop_file(tok, '.hidden.sor', b'x')
    out = TS.drop_end(tok)
    assert out['a_count'] == 1


def test_a_drop_with_nothing_usable_is_refused_and_the_token_dies():
    tok = TS.drop_begin()
    TS.drop_file(tok, 'photo.jpg', b'x')
    with pytest.raises(ValueError, match='nothing dropped'):
        TS.drop_end(tok)
    with pytest.raises(ValueError, match='unknown'):
        TS.drop_end(tok)


# ── the files say which side they are ───────────────────────────────────
#
# 2026-09-19, the field: "can we not determine direction when theyre dropped
# in? so if we drop in B first we know it?"  Yes -- EXFO stamps
# LocationsDirection into every .sor, and it is what FastReporter's own
# Direction column reads.  These use the REAL fixture spans, because a
# synthetic .sor carries no such field (which is also the fallback test).

from conftest import FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR   # noqa: E402


def _real(side, n=3, rename=None):
    """n real fixture files from the A or B direction, as (name, bytes)."""
    d = FIXTURE_SPLICE_A_DIR if side == 'a' else FIXTURE_SPLICE_B_DIR
    out = []
    for p in sorted(d.glob('*.sor'))[:n]:
        name = rename(p.name) if rename else p.name
        out.append((name, p.read_bytes()))
    return out


def _drop_real(*groups):
    tok = TS.drop_begin()
    for g in groups:
        for name, data in g:
            TS.drop_file(tok, name, data)
    return TS.drop_end(tok)


def test_the_fixture_spans_really_carry_the_direction():
    """If this fails the tests below are not testing what they say."""
    assert TS.read_direction(_real('a')[0][1]) == 'a'
    assert TS.read_direction(_real('b')[0][1]) == 'b'


def test_dropping_the_b_side_first_lands_on_b():
    """The whole point: an empty Viewer, B dropped first, and it does NOT
    become the A side."""
    out = _drop_real(_real('b'))
    assert out['added'] == 'B' and out['added_by'] == 'file'
    assert out['dir_a'] is None and out['a_count'] == 0
    assert out['b_prefix'] == 'MILELM' and out['b_count'] == 3
    assert TS.CONFIG['dir_a'] is None and TS.CONFIG['dir_b'] == out['dir_b']
    # and the A side afterwards fills the side that is left
    a = _drop_real(_real('a'))
    assert a['added'] == 'A' and a['added_by'] == 'file'
    assert a['dir_b'] == out['dir_b']
    assert a['a_prefix'] == 'ELMMIL' and a['b_prefix'] == 'MILELM'


def test_a_folder_that_declares_a_taken_side_takes_the_empty_one():
    """TOOKNO and KNOTOO both stamp A.  Believing the second one would put it
    straight back over the first, which is the bug this all started from."""
    first = _drop_real(_real('a', 3))
    assert first['added'] == 'A'
    # another folder, also stamped A (real A-direction bytes under other names)
    second = _drop_real(_real('a', 3, rename=lambda n: 'KNOTOO' + n[6:]))
    assert second['added'] == 'B'                  # the empty side, not over A
    assert second['added_by'] == 'position'
    assert second['dir_a'] == first['dir_a']       # the first folder is untouched
    assert second['b_prefix'] == 'KNOTOO'


def test_both_directions_in_one_drop_take_their_sides_from_the_files():
    """A and B used to go by whichever prefix sorted first -- NILWNH before
    WNHNIL puts the B side on A.  Named here so the alphabet gets it wrong."""
    out = _drop_real(_real('b', 3, rename=lambda n: 'AAAAAA' + n[6:]),   # B bytes
                     _real('a', 3, rename=lambda n: 'ZZZZZZ' + n[6:]))   # A bytes
    assert out['added'] == 'AB' and out['added_by'] == 'file'
    assert out['a_prefix'] == 'ZZZZZZ'             # the A-direction files
    assert out['b_prefix'] == 'AAAAAA'             # the B-direction files


def test_the_same_folder_again_still_refreshes_its_own_side():
    """The signature check outranks the declaration, so re-dropping the A
    folder cannot be read as 'this is the A side' twice over and clear B."""
    _drop_real(_real('a'))
    b = _drop_real(_real('b'))
    again = _drop_real(_real('a'))
    assert again['added'] == 'A'
    assert again['dir_b'] == b['dir_b']


def test_files_that_do_not_declare_still_fill_the_empty_side():
    """A synthetic .sor has no proprietary block, so the positional rule is
    what answers -- the behaviour the rest of this file pins."""
    assert TS.read_direction(make_sor(ior=1.47)) is None
    a = _drop('ROMTUC001_1550.sor', 'ROMTUC002_1550.sor')
    assert a['added'] == 'A' and a['added_by'] == 'position'
    b = _drop('TUCROM001_1550.sor', 'TUCROM002_1550.sor')
    assert b['added'] == 'B' and b['added_by'] == 'position'
    assert b['dir_a'] == a['dir_a']


def test_a_folder_holding_both_directions_declares_nothing():
    """A tie-panel folder carries both, so it cannot name a side and the
    positional rule takes over."""
    import os as _os
    tok = TS.drop_begin()
    # same prefix so the prefix rule sees ONE group, different fiber numbers so
    # the B files do not simply overwrite the A files in the staging folder
    for name, data in _real('a', 2) + _real('b', 2, rename=lambda n: 'ELMMIL9' + n[7:]):
        TS.drop_file(tok, name, data)
    staged = _os.path.join(TS._DROPS[tok]['dir'], 'in')
    paths = [_os.path.join(staged, f) for f in sorted(_os.listdir(staged))]
    assert TS._declared_direction(paths) is None
    out = TS.drop_end(tok)
    assert out['added'] == 'A' and out['added_by'] == 'position'


def test_the_page_forgets_only_the_side_the_drop_replaced():
    h = open(os.path.join(ROOT, 'viewer', 'viewer.html'), encoding='utf-8').read()
    fn = h.split('async function handleFilesDrop(dt) {', 1)[1].split('\n}', 1)[0]
    assert "const before = { a: (gInfo && gInfo.dir_a) || '', b: (gInfo && gInfo.dir_b) || '' };" in fn
    assert "if ((j['dir_' + d] || '') !== before[d]) forgetSide(d);" in fn
    assert fn.index('const before =') < fn.index('/api/drop_end')
    fs = h.split('function forgetSide(dir) {', 1)[1].split('\n}', 1)[0]
    assert "gTraces = gTraces.filter(t => t.src !== dir);" in fs
    # and the hint names the side the next drop fills
    panel = h.split('function renderFilesPanel() {', 1)[1].split('\n}\n', 1)[0]
    assert "'drop the other direction here, A stays loaded'" in panel
    assert "'drop the other direction here, B stays loaded'" in panel
    # and the readout says when the FILES named the side, not the drop order
    assert "j.added_by === 'file'" in fn
    assert 'the files name this folder the ${j.added} side' in fn


def test_the_page_and_hub_are_wired():
    h = open(os.path.join(ROOT, 'viewer', 'viewer.html'), encoding='utf-8').read()
    assert "panel.addEventListener('drop'" in h
    assert "/api/drop_begin" in h and "/api/drop_file?token=" in h and "/api/drop_end?token=" in h
    assert 'webkitGetAsEntry' in h                      # folders, not just files
    assert 'id="files-drop-hint"' in h
    s = open(os.path.join(ROOT, 'viewer', 'trace_server.py'), encoding='utf-8').read()
    body = s.split("u.path in ('/api/drop_begin', '/api/drop_file', '/api/drop_end')", 1)[1].split('return', 1)[0]
    assert '_origin_is_local' in body
    a = open(os.path.join(ROOT, 'app.py'), encoding='utf-8').read()
    assert "trace_server.CONFIG.get('dropped_at')" in a


# ── one flat staging folder: the first file of a name wins ──────────────
#
# 2026-09-19: drop_file wrote every dropped file to <drop>/in/<name>, so two
# files of one name collided and the second overwrote the first in silence.
# That is not a corner case.  The browser hands a dragged PARENT folder over
# FLAT, without its subfolders, and a crew that puts each direction in its own
# folder has no reason to put the site in the file names — both directions
# arrive as 0001_1550.sor and up (folder_intake.resolve_direction_groups calls
# those names 'unnamed'), so the whole first direction could disappear under
# the second with nothing on the readout.
#
# The hub solved this for its own drop target: the first file of a name is
# kept and the repeat is REPORTED (app.py _stage_dropped ->
# folder_intake.stage_uploads, pinned by test_drop_stage.py
# ::test_repeated_name_is_reported_not_overwritten).  The Viewer does the same,
# and hands the repeats back from drop_end for the drop readout to say so.


def _sized(fill, pad):
    """A synthetic .sor of its own SIZE, so (name, size) signatures differ."""
    return make_sor(raw_payload=fill * pad)


def _unnamed(files):
    """The same files under the names a crew that keeps each direction in its
    own folder uses: 0001_1550.sor and up, no site code."""
    return [('%04d_1550.sor' % (i + 1), data) for i, (_n, data) in enumerate(files)]


def _staged(out):
    """{name: bytes} of everything a drop actually put on a side."""
    got = {}
    for d in (out['dir_a'], out['dir_b']):
        for f in (sorted(os.listdir(d)) if d else []):
            with open(os.path.join(d, f), 'rb') as fh:
                got[f] = fh.read()
    return got


def test_a_repeated_name_keeps_the_first_file_and_is_reported():
    tok = TS.drop_begin()
    first, second = _sized(b'\xa1', 40), _sized(b'\xb2', 90)
    assert TS.drop_file(tok, 'ROMTUC001_1550.sor', first)['files'] == 1
    again = TS.drop_file(tok, 'ROMTUC001_1550.sor', second)
    assert again['files'] == 0                        # not written
    assert 'already dropped' in again['skipped']
    TS.drop_file(tok, 'ROMTUC002_1550.sor', first)
    out = TS.drop_end(tok)
    assert out['repeated'] == ['ROMTUC001_1550.sor']
    assert out['a_count'] == 2                        # one file of each name
    assert _staged(out)['ROMTUC001_1550.sor'] == first   # kept, not clobbered


def test_the_repeat_is_caught_whatever_case_the_name_arrives_in():
    """The hub matches names case-insensitively (folder_intake.stage_uploads
    keys on name.lower()), and so must this: on a case-insensitive filesystem
    the two names are ONE file, so a case-sensitive check would go back to
    overwriting on the very machines the boss and the techs run."""
    tok = TS.drop_begin()
    TS.drop_file(tok, 'ROMTUC001_1550.sor', _sized(b'\xa1', 40))
    assert TS.drop_file(tok, 'RomTuc001_1550.SOR', _sized(b'\xb2', 90))['files'] == 0
    out = TS.drop_end(tok)
    assert out['repeated'] == ['RomTuc001_1550.SOR']
    assert out['a_count'] == 1


def test_a_dragged_parent_folder_keeps_the_first_direction():
    """The reported case, with the real fixture spans: both direction folders
    name their traces alike, so the B files arrive under the A files' names.
    Every survivor is an A file, and every name that came twice is reported.

    (What the SPLIT then makes of letterless names is its own question, below:
    nothing can tell them apart, so the survivors stay together on one side.
    This pins only that the first direction's bytes are still there.)"""
    a_side = _unnamed(_real('a', 3))
    b_side = _unnamed(_real('b', 3))
    assert [n for n, _ in a_side] == [n for n, _ in b_side]      # one name set
    out = _drop_real(a_side, b_side)
    assert sorted(set(out['repeated'])) == ['0001_1550.sor', '0002_1550.sor',
                                            '0003_1550.sor']
    assert len(out['repeated']) == 3                  # one per file not staged
    staged = _staged(out)
    assert staged                                     # something landed
    # the A bytes, file for file, and nothing from the B folder
    by_name = dict(a_side)
    assert all(v == by_name[n] for n, v in staged.items())
    assert {TS.read_direction(v) for v in staged.values()} == {'a'}


def test_a_zip_of_a_parent_folder_keeps_the_first_of_each_name():
    """A zip is flattened into the same one folder, so its two direction
    subfolders collide exactly as a dragged folder's do."""
    buf = io.BytesIO()
    first, second = _sized(b'\xa1', 40), _sized(b'\xb2', 90)
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('span/A/ROMTUC001_1550.sor', first)
        zf.writestr('span/A/ROMTUC002_1550.sor', first)
        zf.writestr('span/B/ROMTUC001_1550.sor', second)
    tok = TS.drop_begin()
    assert TS.drop_file(tok, 'span.zip', buf.getvalue())['files'] == 2
    out = TS.drop_end(tok)
    assert out['repeated'] == ['ROMTUC001_1550.sor']
    assert out['a_count'] == 2
    assert _staged(out)['ROMTUC001_1550.sor'] == first


def test_a_clean_drop_reports_no_repeats():
    out = _drop('ROMTUC001_1550.sor', 'ROMTUC002_1550.sor', 'TUCROM001_1550.sor')
    assert out['repeated'] == []


def test_the_same_folder_again_is_recognised_after_a_collided_drop():
    """_single_drop_side asks whether a drop is the SAME folder a side already
    holds, by the (name, size) signature of the staged files — so which copy
    of a repeated name survives decides what the drop signs as.  Keeping the
    FIRST keeps that honest: the parent folder whose A subfolder was
    enumerated first signs as the A folder and REFRESHES the A side, leaving B
    loaded.  (Keeping the last used to leave a signature matching neither
    side, which with both sides full starts a new span and drops B.)"""
    a = _drop('ROMTUC001_1550.sor', 'ROMTUC002_1550.sor')
    b = _drop('TUCROM001_1550.sor', 'TUCROM002_1550.sor')
    tok = TS.drop_begin()
    for name in ('ROMTUC001_1550.sor', 'ROMTUC002_1550.sor'):
        TS.drop_file(tok, name, make_sor(ior=1.47))          # the A folder again
    for name in ('ROMTUC001_1550.sor', 'ROMTUC002_1550.sor'):
        TS.drop_file(tok, name, _sized(b'\x55', 200))        # the other subfolder
    again = TS.drop_end(tok)
    assert sorted(again['repeated']) == ['ROMTUC001_1550.sor', 'ROMTUC002_1550.sor']
    assert again['added'] == 'A'
    assert again['dir_a'] not in (a['dir_a'], None)          # the fresh staging
    assert again['dir_b'] == b['dir_b']                      # B still loaded


def test_the_readout_says_which_names_arrived_twice():
    h = open(os.path.join(ROOT, 'viewer', 'viewer.html'), encoding='utf-8').read()
    fn = h.split('async function handleFilesDrop(dt) {', 1)[1].split('\n}', 1)[0]
    assert 'if (j.repeated && j.repeated.length) {' in fn
    assert 'arrived under a name already dropped' in fn
    assert 'Drop one direction at a time' in fn
    # named once each, and a 288-fiber cable cannot flood the one-line readout
    assert '[...new Set(j.repeated)].sort()' in fn
    assert 'uniq.slice(0, 6)' in fn and 'more)`' in fn
    assert fn.index('j.ignored') < fn.index('j.repeated') < fn.index('setReadout(msg)')


# ── names that carry no site at all ─────────────────────────────────────
#
# 2026-09-19: the drop target split by file name with direction_prefix, whose
# re.match(r'[A-Za-z]+') returns None on '0001_1550.sor', so the key fell back
# to the WHOLE file name and every fiber became its own direction group.  Four
# files of ONE direction came back as
#
#     A = 0001_1550.SOR (1 file) | B = 0002_1550.SOR (1 file)
#     ignored = ['0003_1550.SOR', '0004_1550.SOR']
#
# -- two fibers of the same direction loaded as the two sides of a span, and
# the rest of the folder thrown away.  Crews that keep each direction in its
# OWN folder really do name traces this way (there is nothing to disambiguate
# inside the folder), so the hub already refuses this: folder_intake
# .resolve_direction_groups calls those names 'unnamed', does not trust the
# prefix result for them, and falls back to the file headers and the site
# codes first.  The Viewer now runs the same rules, and when none of them can
# split the drop it keeps the drop WHOLE and says so.


def _synth(loc_a=None, loc_b=None, fill=b'\xa1', pad=40):
    """A synthetic .sor (no direction stamp), optionally shot between two
    named locations."""
    d = make_sor(raw_payload=fill * pad)
    return TS.set_identifiers(d, loc_a=loc_a, loc_b=loc_b) if loc_a else d


def test_letterless_names_are_not_split_one_fiber_per_direction():
    """The reported case: one direction's folder, no site in the names."""
    out = _drop_real(_unnamed(_real('a', 4)))
    assert out['split_by'] == 'unnamed'
    assert out['added'] == 'A' and out['a_count'] == 4     # whole, not 1 vs 1
    assert out['dir_b'] is None and out['b_count'] == 0
    assert out['ignored'] == []                            # nothing thrown away
    assert sorted(os.listdir(out['dir_a'])) == ['0001_1550.sor', '0002_1550.sor',
                                                '0003_1550.sor', '0004_1550.sor']
    assert {TS.read_direction(v) for v in _staged(out).values()} == {'a'}


def test_a_letterless_drop_is_given_no_direction_name():
    """'0001_1550.SOR' is a file name, not a site, so the side has none: the
    readout prints the count alone rather than naming the direction after
    whichever fiber sorted first."""
    out = _drop_real(_unnamed(_real('a', 3)))
    assert out['a_prefix'] is None and out['a_count'] == 3
    # and the same folder read back later (the OTHER side's facts come from
    # the folder, not from the drop) still has no name
    assert TS._dir_facts(out['dir_a']) == (None, 3)


def test_each_letterless_direction_folder_lands_on_its_own_side():
    """The field flow: one folder per direction, dropped one after the other.
    The files carry EXFO's direction stamp even when the names say nothing, so
    the B folder lands on B."""
    a = _drop_real(_unnamed(_real('a', 3)))
    assert a['added'] == 'A' and a['added_by'] == 'file'
    b = _drop_real(_unnamed(_real('b', 3)))
    assert b['added'] == 'B' and b['added_by'] == 'file'
    assert b['split_by'] == 'unnamed'
    assert b['dir_a'] == a['dir_a']                     # the A side is untouched
    assert b['a_count'] == 3 and b['b_count'] == 3
    assert {TS.read_direction(open(os.path.join(b['dir_b'], f), 'rb').read())
            for f in os.listdir(b['dir_b'])} == {'b'}


def test_letterless_files_that_declare_nothing_still_fill_the_empty_side():
    """No site in the names and no direction in the files: the positional rule
    is all that is left, and it still must not split the drop."""
    tok = TS.drop_begin()
    for i in range(4):
        TS.drop_file(tok, '%04d_1550.sor' % (i + 1), _synth())
    out = TS.drop_end(tok)
    assert out['split_by'] == 'unnamed'
    assert out['added'] == 'A' and out['added_by'] == 'position'
    assert out['a_count'] == 4 and out['dir_b'] is None


def test_numeric_site_codes_split_into_a_and_b():
    """MTG4 ↔ MTG5, Montgomery TX: the prefix rule keys the leading ALPHA run,
    so both directions came back 'MTG' — one group, one side.  The site-code
    fallback reads the leading alphanumeric run instead."""
    tok = TS.drop_begin()
    for name in ('MTG4_0001_1550.sor', 'MTG4_0002_1550.sor',
                 'MTG5_0001_1550.sor', 'MTG5_0002_1550.sor'):
        TS.drop_file(tok, name, _synth())
    out = TS.drop_end(tok)
    assert out['split_by'] == 'sitecode'
    assert out['added'] == 'AB'
    assert out['a_prefix'] == 'MTG4' and out['a_count'] == 2
    assert out['b_prefix'] == 'MTG5' and out['b_count'] == 2
    assert sorted(os.listdir(out['dir_a'])) == ['MTG4_0001_1550.sor', 'MTG4_0002_1550.sor']


def test_reversed_location_pairs_split_letterless_names():
    """Nothing in these names, but the crew set origin and far end per
    direction, so the headers say which way each file was shot."""
    tok = TS.drop_begin()
    for i, (a, b) in enumerate([('MTG4', 'MTG5'), ('MTG4', 'MTG5'),
                                ('MTG5', 'MTG4'), ('MTG5', 'MTG4')]):
        TS.drop_file(tok, '%04d_1550.sor' % (i + 1), _synth(loc_a=a, loc_b=b))
    out = TS.drop_end(tok)
    assert out['split_by'] == 'location'
    assert out['added'] == 'AB'
    assert out['a_prefix'] == 'MTG4 → MTG5' and out['a_count'] == 2
    assert out['b_prefix'] == 'MTG5 → MTG4' and out['b_count'] == 2
    assert sorted(os.listdir(out['dir_a'])) == ['0001_1550.sor', '0002_1550.sor']


def test_one_location_pair_both_ways_is_not_a_split():
    """Most OTDRs stamp the SAME pair in both directions, which says nothing
    about which way a file was shot — that is why the reverse check is there,
    and why these files stay together instead of splitting two-and-two."""
    tok = TS.drop_begin()
    for i in range(4):
        TS.drop_file(tok, '%04d_1550.sor' % (i + 1), _synth(loc_a='MTG4', loc_b='MTG5'))
    out = TS.drop_end(tok)
    assert out['split_by'] == 'unnamed'
    assert out['added'] == 'A' and out['a_count'] == 4


def test_a_named_folder_is_still_split_by_its_names():
    """The fallbacks run only when the names cannot do the job: a folder that
    names its two directions is split exactly as before."""
    out = _drop('ROMTUC001_1550.sor', 'ROMTUC002_1550.sor', 'TUCROM001_1550.sor')
    assert out['split_by'] == 'prefix'
    assert out['a_prefix'] == 'ROMTUC' and out['b_prefix'] == 'TUCROM'


def test_a_letterless_folder_dropped_again_refreshes_its_own_side():
    """The (name, size) signature is what recognises the same folder, and the
    unnamed drop must not lose that: re-dropping the A folder cannot become a
    second B side and clear the real one."""
    a = _drop_real(_unnamed(_real('a', 3)))
    b = _drop_real(_unnamed(_real('b', 3)))
    again = _drop_real(_unnamed(_real('a', 3)))
    assert again['added'] == 'A'
    assert again['dir_b'] == b['dir_b']                # B left as it was
    assert again['dir_a'] not in (a['dir_a'], None)    # the fresh staging


def test_the_readout_says_when_nothing_could_split_the_drop():
    h = open(os.path.join(ROOT, 'viewer', 'viewer.html'), encoding='utf-8').read()
    fn = h.split('async function handleFilesDrop(dt) {', 1)[1].split('\n}', 1)[0]
    assert "if (j.split_by === 'unnamed') {" in fn
    assert 'the names carry no site, so the drop was kept whole' in fn
    assert "Drop each direction's folder on its own" in fn
    # the other two fallbacks name themselves as well
    assert "j.split_by === 'location'" in fn and 'locations in the file headers' in fn
    assert "j.split_by === 'sitecode'" in fn and 'split by site code' in fn
    # and a side with no direction key prints its count, not a file name
    assert "const side = (name, count) => (name ? name + ' ' : '') + `(${count} files)`;" in fn
    assert fn.index('j.split_by') < fn.index('j.ignored')


def test_the_viewer_splits_a_folder_exactly_as_the_hub_does(tmp_path):
    """The Viewer must run standalone, so these rules are a COPY of
    folder_intake's.  A copy drifts -- this whole bug was the Viewer still
    splitting on names the hub had stopped trusting -- so every rule is run
    side by side here on the shapes that tell them apart."""
    import folder_intake as FI                          # noqa: PLC0415

    cases = {
        'named two ways': {'ROMTUC001_1550.sor': _synth(), 'ROMTUC002_1550.sor': _synth(),
                           'TUCROM001_1550.sor': _synth()},
        'named one way': {'SEANOR001_1550.sor': _synth(), 'SEANOR002_1550.sor': _synth()},
        'no site in the name': {'0001_1550.sor': _synth(), '0002_1550.sor': _synth(),
                                '0003_1550.sor': _synth()},
        'numeric site codes': {'MTG4_0001_1550.sor': _synth(), 'MTG4_0002_1550.sor': _synth(),
                               'MTG5_0001_1550.sor': _synth(), 'MTG5_0002_1550.sor': _synth()},
        'locations both ways': {'0001_1550.sor': _synth('MTG4', 'MTG5'),
                                '0002_1550.sor': _synth('MTG4', 'MTG5'),
                                '0003_1550.sor': _synth('MTG5', 'MTG4'),
                                '0004_1550.sor': _synth('MTG5', 'MTG4')},
        'one location pair': {'0001_1550.sor': _synth('MTG4', 'MTG5'),
                              '0002_1550.sor': _synth('MTG4', 'MTG5')},
    }

    def _verdict(fn, paths):
        groups, how = fn(paths)
        return how, {k: sorted(os.path.basename(p) for p in v) for k, v in groups.items()}

    for label, files in cases.items():
        d = tmp_path / label.replace(' ', '_')
        d.mkdir()
        paths = []
        for name, data in files.items():
            (d / name).write_bytes(data)
            paths.append(str(d / name))
        assert _verdict(TS.resolve_direction_groups, paths) \
            == _verdict(FI.resolve_direction_groups, paths), label

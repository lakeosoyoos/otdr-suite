"""Dragging a span folder onto the Splice Report box.

Montgomery TX follow-up (2026-09-17): the Splice Report's one-folder box had a
drop target beside it that only accepted .zip / .bdr, so dragging the folder of
.sor traces in was rejected by the file-type filter.  It now takes the span's
own traces, and the staging that flattens a dropped folder no longer lets two
same-named files overwrite each other.
"""
import ast
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
import folder_intake as fi  # noqa: E402

APP = (REPO_ROOT / 'app.py').read_text(encoding='utf-8')


class _Fake:
    """A Streamlit UploadedFile: bytes and a NAME, never a path."""
    def __init__(self, name, data=b'trace'):
        self.name = name
        self._data = data
        self.size = len(data)

    def getbuffer(self):
        return self._data


def test_app_parses():
    ast.parse(APP)


def test_splice_report_drop_box_takes_traces():
    assert "type=['zip', 'bdr', 'sor', 'json']" in APP, \
        'SR drop box still rejects a dragged span folder'


def test_resolver_stages_dropped_traces():
    """The resolver must branch on dropped traces, not only on a .zip."""
    assert 'trace_uploads' in APP
    assert 'elif zip_uploads or trace_uploads:' in APP


def test_stage_uploads_keeps_both_same_named_files(tmp_path):
    """The two direction folders of a dragged parent both hold 0001_1550.sor.
    Nested, both survive with their names intact and the recursive intake walk
    finds both — which is what the A/B split needs."""
    ups = [_Fake('0001_1550.sor', b'A'), _Fake('0001_1550.sor', b'B'),
           _Fake('0002_1550.sor', b'A')]
    dest = str(tmp_path / 'dropped')
    paths, dupes = fi.stage_uploads(ups, dest)
    assert dupes == ['0001_1550.sor']
    assert len(paths) == 3                                  # nothing lost
    assert len(fi.find_otdr_files(dest)) == 3               # walk finds all 3
    assert {os.path.basename(p) for p in paths} == {'0001_1550.sor',
                                                    '0002_1550.sor'}
    with open(os.path.join(dest, '0001_1550.sor'), 'rb') as fh:
        assert fh.read() == b'A'                            # first not clobbered


def test_stage_uploads_can_skip_duplicates(tmp_path):
    """For a folder handed straight to an engine (os.listdir, no recursion) an
    unread copy on disk is worse than none."""
    ups = [_Fake('0001_1550.sor', b'A'), _Fake('0001_1550.sor', b'B')]
    dest = str(tmp_path / 'engine_input')
    paths, dupes = fi.stage_uploads(ups, dest, nest_duplicates=False)
    assert len(paths) == 1 and dupes == ['0001_1550.sor']
    assert os.listdir(dest) == ['0001_1550.sor']


def test_duplicate_names_message_names_the_files():
    names = ['0001_1550.sor', '0002_1550.sor']
    kept = fi.duplicate_names_message(names)
    assert '0001_1550.sor' in kept and '0002_1550.sor' in kept
    assert 'every copy was kept' in kept                 # nested, nothing lost
    skipped = fi.duplicate_names_message(names, kept=False)
    assert 'only the first file of each name was used' in skipped
    assert fi.duplicate_names_message([]) == ''


def test_dropped_span_splits_into_two_directions(tmp_path):
    """End to end on Chris's shape: drag the parent, get both directions."""
    ups = []
    for i in range(1, 7):
        ups.append(_Fake(f'MTG4-{i:04d}_1550.sor'))
        ups.append(_Fake(f'MTG5-{i:04d}_1550.sor'))
    dest = str(tmp_path / 'dropped')
    fi.stage_uploads(ups, dest)
    files = fi.find_otdr_files(dest, fi.OTDR_EXTS_WITH_BDR)
    _da, _db, info = fi.materialize_two_directions(files, str(tmp_path / 'work'))
    assert info['split_by'] == 'sitecode'
    assert (info['a_prefix'], info['a_count']) == ('MTG4', 6)
    assert (info['b_prefix'], info['b_count']) == ('MTG5', 6)


def test_letterless_names_are_not_read_as_directions(tmp_path):
    """Two direction FOLDERS dropped at once arrive flat, and a crew that puts
    the site in the folder rather than the file names them 0001_1550.sor on
    both sides.  The old prefix rule keyed on the whole name and handed back
    fiber 0001 vs fiber 0002 as the two 'directions' — a report pairing two
    different fibers.  The header decides instead, and when it can't, the
    intake says so rather than guessing."""
    def _sor(p, a, b):
        p.write_bytes(b'\x00\x02Map\x00GenParams\x00GenParams\x00EN'
                      b'CABLE\x00FIBER\x00' + b'\x00' * 4
                      + a.encode() + b'\x00' + b.encode() + b'\x00' + b'\xff' * 64)
        return str(p)

    d = tmp_path / 'flat'
    d.mkdir()
    (d / '_dup2').mkdir()
    readable = [_sor(d / '0001_1550.sor', 'MTG4', 'MTG5'),
                _sor(d / '0002_1550.sor', 'MTG4', 'MTG5'),
                _sor(d / '_dup2' / '0001_1550.sor', 'MTG5', 'MTG4'),
                _sor(d / '_dup2' / '0002_1550.sor', 'MTG5', 'MTG4')]
    groups, how = fi.resolve_direction_groups(readable)
    assert how == 'location'                            # not fiber 1 vs fiber 2
    assert set(groups) == {'MTG4 → MTG5', 'MTG5 → MTG4'}

    # Same names, but the OTDR stamped one pair both ways: undecidable, say so.
    blind = tmp_path / 'blind'
    blind.mkdir()
    (blind / '_dup2').mkdir()
    same = [_sor(blind / '0001_1550.sor', 'MTG4', 'MTG5'),
            _sor(blind / '0002_1550.sor', 'MTG4', 'MTG5'),
            _sor(blind / '_dup2' / '0001_1550.sor', 'MTG4', 'MTG5'),
            _sor(blind / '_dup2' / '0002_1550.sor', 'MTG4', 'MTG5')]
    assert fi.resolve_direction_groups(same) == ({}, 'unnamed')
    import pytest
    with pytest.raises(ValueError) as exc:
        fi.materialize_two_directions(same, str(tmp_path / 'work'))
    assert 'Two folders (A + B)' in str(exc.value)

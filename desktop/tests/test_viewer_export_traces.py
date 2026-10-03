"""FILES > right-click > Export Marked Traces (Robert 2026-10-02).

"We need a way we can export traces from viewer after we have made changes
to them, either by changing their direction or some other setting."  Save
Direction and Trace Settings each wrote a copy made from the ORIGINAL, so a
file given both kept only one: the second save was "skipped: already
exists".  Export writes every marked file once, with the direction shown and
any IOR / backscatter / span picked in the dialog together, and copies the
untouched files as they are so the folder holds the whole marked set."""
import os
import shutil
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'viewer'))
import trace_server as T  # noqa: E402

FIX = os.path.join(ROOT, 'desktop', 'tests', 'fixtures')
A_DIR = os.path.join(FIX, 'span_A')
NAMES = ['ELMMIL0001_1550.sor', 'ELMMIL0002_1550.sor', 'ELMMIL0003_1550.sor']


def _raw(p):
    with open(p, 'rb') as f:
        return f.read()


@pytest.fixture
def span(tmp_path, monkeypatch):
    # Downloads pointed at the test's own dir: a real one would collect
    # copies, and every run after the first would be "already exists".
    monkeypatch.setenv('OTDR_DOWNLOADS_DIR', str(tmp_path / 'Downloads'))
    (tmp_path / 'Downloads').mkdir()
    src = tmp_path / 'span_A'
    src.mkdir()
    for n in NAMES:
        shutil.copy(os.path.join(A_DIR, n), src / n)
    return src, tmp_path / 'out'


def test_export_copies_an_untouched_file_byte_for_byte(span):
    src, out = span
    res = T.edit_traces('a', [1, 2], dir_a=str(src), dest_name=str(out), export=True)
    assert res['written'] == [1, 2] and not res['skipped']
    for n in NAMES[:2]:
        assert _raw(str(out / n)) == _raw(str(src / n))


def test_export_writes_direction_and_ior_into_one_copy(span):
    """The bug Export exists for: both changes land in the same file."""
    src, out = span
    res = T.edit_traces('a', [1], dir_a=str(src), dest_name=str(out),
                        new_direction='b', ior=1.4685, export=True)
    assert res['written'] == [1]
    got = _raw(str(out / NAMES[0]))
    assert T.read_direction(got) == 'b'
    assert T.read_direction(_raw(str(src / NAMES[0]))) == 'a', 'original untouched'
    assert _raw(str(src / NAMES[0])) == _raw(os.path.join(A_DIR, NAMES[0]))
    assert abs(T.read_ior(got) - 1.4685) < 1e-9


def test_export_never_overwrites(span):
    src, out = span
    T.edit_traces('a', [1], dir_a=str(src), dest_name=str(out), export=True)
    res = T.edit_traces('a', [1, 2], dir_a=str(src), dest_name=str(out), export=True)
    assert res['written'] == [2]
    assert res['skipped'][0]['fiber'] == 1
    assert res['skipped'][0]['reason'].startswith('already exists')


def test_export_never_writes_into_the_source_folder(span):
    src, _ = span
    with pytest.raises(ValueError):
        T.edit_traces('a', [1], dir_a=str(src), dest_name=str(src), export=True)


def test_a_plain_save_with_nothing_to_change_is_still_refused(span):
    src, out = span
    with pytest.raises(ValueError, match='nothing to change'):
        T.edit_traces('a', [1], dir_a=str(src), dest_name=str(out))


def test_export_copies_a_non_sor_file_but_refuses_to_change_it(tmp_path, monkeypatch):
    monkeypatch.setenv('OTDR_DOWNLOADS_DIR', str(tmp_path))
    src = tmp_path / 'json_A'
    src.mkdir()
    body = b'{"not": "parsed by an export"}'
    (src / 'ABCDEF0001_1550.json').write_bytes(body)
    (src / 'ABCDEF0002_1550.json').write_bytes(body)
    out = tmp_path / 'out'
    res = T.edit_traces('a', [1], dir_a=str(src), dest_name=str(out), export=True)
    assert res['written'] == [1]
    assert (out / 'ABCDEF0001_1550.json').read_bytes() == body
    res = T.edit_traces('a', [2], dir_a=str(src), dest_name=str(out),
                        new_direction='b', export=True)
    assert res['written'] == [] and res['skipped'] == [{'fiber': 2, 'reason': 'not a .sor'}]


def test_api_hands_the_export_flag_through():
    srv = _read('viewer', 'trace_server.py')
    handler = srv.split("if u.path == '/api/trace_edit':", 1)[1].split('self._send_json({\'ok\': True', 1)[0]
    assert "export=bool(data.get('export'))" in handler


# ── The Viewer side ─────────────────────────────────────────────────────

def _read(*p):
    return open(os.path.join(ROOT, *p), encoding='utf-8').read()


def _menu(html):
    return html.split('function showFileDirMenu(', 1)[1].split('\nasync function ', 1)[0]


def test_file_menu_offers_export_on_the_marked_set():
    menu = _menu(_read('viewer', 'viewer.html'))
    assert 'data-export="1"' in menu
    assert "Export ${marks.length > 1 ? marks.length + ' Marked Traces' : 'This Trace'}…" in menu
    assert 'showExportDialog(marks)' in menu


def test_export_sends_the_shown_direction_and_the_export_flag():
    html = _read('viewer', 'viewer.html')
    grp = html.split('function exportGroups(', 1)[1].split('\n}\n', 1)[0]
    # only a direction changed HERE is stamped; the rest go out as they are
    assert 'const nd = gDirOverride[key] ? eff : null;' in grp
    sub = html.split('async function submitExport(', 1)[1].split('\n}\n', 1)[0]
    assert "'/api/trace_edit'" in sub and 'export: true' in sub
    assert 'new_direction: g.dir' in sub and 'ior, backscatter: bs' in sub

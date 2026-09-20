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


def test_edit_traces_writes_a_copy_with_the_new_direction(tmp_path, monkeypatch):
    # span_A, not span_B: the span_B fixtures carry a stale stored checksum,
    # so the writer's byte-exact guard (rightly) refuses to edit them.
    # Copies default to the tech's Downloads folder; point that at the test's
    # own dir, or the first run leaves a file in the real one and every run
    # after is "skipped: already exists".
    import shutil
    monkeypatch.setenv('OTDR_DOWNLOADS_DIR', str(tmp_path / 'Downloads'))
    (tmp_path / 'Downloads').mkdir()
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
    assert 'async function saveFilesDirection(' in html
    fn = html.split('async function saveFilesDirection(', 1)[1].split('\n}\n', 1)[0]
    assert "new_direction: dir" in fn and "'/api/trace_edit'" in fn
    assert 'gStoredDir[key] = data.stored_dir' in html
    eff = html[html.index('function effDir('):][:160]
    assert 'gDirOverride[k] || gStoredDir[k] || src' in eff
    srv = open(os.path.join(ROOT, 'viewer', 'trace_server.py'), encoding='utf-8').read()
    assert "'stored_dir': stored" in srv


def test_file_menu_offers_the_trace_settings_editor():
    """Boss: reach the IOR / names editor from the file's right-click menu,
    not only from the event table's span menu."""
    html = open(os.path.join(ROOT, 'viewer', 'viewer.html'), encoding='utf-8').read()
    # To the end of the function, not a fixed slice: the menu grows (Rename
    # files landed under the editor) and a character count goes stale silently.
    fn = html.split('function showFileDirMenu(', 1)[1].split('\nasync function ', 1)[0]
    assert 'Edit trace settings' in fn and 'showEditDialog(src, fiber)' in fn


# ── Direction acts on the marked files ─────────────────────────────────
#
# Rename and Remove in this menu have always acted on the whole marked range;
# Direction alone acted on the one row under the cursor, so re-labelling a
# folder meant right-clicking it a fiber at a time.  That is the job a drop
# the names cannot split leaves behind: the folder lands whole on one side
# (viewer/trace_server.py resolve_direction_groups), and half of it may be the
# other direction.  Mark the rows, set them once.

def _menu(html):
    return html.split('function showFileDirMenu(', 1)[1].split('\nasync function ', 1)[0]


def _html():
    return open(os.path.join(ROOT, 'viewer', 'viewer.html'), encoding='utf-8').read()


def test_direction_acts_on_every_marked_file():
    """The marked set when the clicked row is in it, that row alone otherwise
    -- the same rule Rename and Remove follow."""
    menu = _menu(_html())
    assert "const marks = gSelectedFiles.has(k) ? [...gSelectedFiles] : [k];" in menu
    # both A->B and B->A hand the whole set over, not (src, fiber)
    assert "setFilesDirection(marks, b.dataset.d)" in menu
    assert "setFileDirection(src, fiber" not in menu           # the old single-file call
    # and the header says how many are about to move
    assert "Direction${marked ? ` (${marked} marked)` : ''}" in menu


def test_the_tick_needs_every_marked_file_to_agree():
    """A ✓ against a mixed set would claim a direction the files do not share,
    and the next click would look like a no-op on half of them."""
    menu = _menu(_html())
    assert "const allAre = d => marks.every(key => effDir(...splitFileKey(key)) === d);" in menu
    assert "${allAre(d) ? '✓ ' : '\\u2003'}" in menu


def test_save_writes_every_marked_file_that_changed():
    html = _html()
    menu = _menu(html)
    # only the ones actually changed are written, and the label counts them
    assert "const dirty = marks.filter(key => gDirOverride[key]);" in menu
    assert "saveFilesDirection(dirty)" in menu
    assert "dirty.length > 1 ? dirty.length + ' directions to edited copies'" in menu
    assert "${dirty.length ? '' : ' disabled" in menu          # nothing changed, nothing to save
    fn = html.split('async function saveFilesDirection(', 1)[1].split('\n}\n', 1)[0]
    # one POST per (side, direction): the endpoint takes one of each, and a
    # marked range can hold files from both sides changed to either
    assert "const g = `${src}-${dir}`;" in fn
    assert "groups.get(g).fibers.push(fiber);" in fn
    assert "for (const { src, dir, fibers } of groups.values()) {" in fn
    assert "fibers, new_direction: dir" in fn
    # and the readout carries the count, the destination and what was skipped
    assert "written.push(...(j.written || []));" in fn
    assert "skipped.push(...(j.skipped || []));" in fn
    assert "skipped.length ? ` · ${skipped.length} not written: ${skipped[0].reason}`" in fn


def test_setting_the_direction_a_file_already_declares_still_shows_it():
    """The override is cleared only against what the file would read as
    WITHOUT it.  Cleared against the folder alone, a file sitting on the A
    side whose own stamp says B snapped back to B the moment it was set to A,
    and the row and the chart disagreed from then on."""
    fn = _html().split('function setFilesDirection(keys, dir) {', 1)[1].split('\n}', 1)[0]
    assert "if (dir === (gStoredDir[key] || src)) delete gDirOverride[key];" in fn
    assert "else gDirOverride[key] = dir;" in fn
    # the loaded trace is redrawn as what effDir now says, not as the raw pick
    assert "if (t) t.dir = effDir(src, fiber);" in fn
    assert "`${keys.length} files set to ${arrow}`" in fn


def test_a_files_panel_key_splits_back_into_side_and_fiber():
    """'a-12' -> ['a', 12]: the fiber has to come back a NUMBER, since the
    trace edit endpoint and gTraces both key on it as one."""
    fn = _html().split('function splitFileKey(key) {', 1)[1].split('\n}', 1)[0]
    assert "const i = key.indexOf('-');" in fn
    assert "return [key.slice(0, i), +key.slice(i + 1)];" in fn

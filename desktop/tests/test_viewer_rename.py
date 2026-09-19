"""Renaming files from the Viewer's FILES panel.

The boss asked for PowerRename's flow against a span folder: mark files,
right-click, find and replace with a live preview, and the ORIGINALS renamed
in place.  This is the one route in the Viewer that changes the tech's own
files, so most of what is pinned here is what it REFUSES to do.

Two halves:
  * trace_server.rename_files / rename_check — run for real against a temp
    folder, because the failure that matters is a file that went missing.
  * viewer.html — pinned as source (no JS runtime in this suite), including
    the two mirrors of server rules the dialog carries so its preview can be
    truthful before the POST: the fiber-number reader and the name check.
"""
from __future__ import annotations

import os
import re

import pytest

from conftest import VIEWER_DIR, import_trace_server

TS = import_trace_server()
SRC = (VIEWER_DIR / "viewer.html").read_text(encoding="utf-8")
PY_SRC = (VIEWER_DIR / "trace_server.py").read_text(encoding="utf-8")


@pytest.fixture
def folder(tmp_path):
    """A span folder: twelve traces and one report that must never move."""
    d = tmp_path / "TOOKNO A"
    d.mkdir()
    for i in range(1, 13):
        (d / f"TOOKNO{i:04d}.sor").write_bytes(b"trace-%04d" % i)
    (d / "splice_report.xlsx").write_bytes(b"not a trace")
    TS.CONFIG["dir_a"] = str(d)
    TS.CONFIG["dir_b"] = None
    return d


def _pairs(*names):
    return [{"from": a, "to": b} for a, b in names]


# ── the rename itself ────────────────────────────────────────────────────

def test_a_find_and_replace_renames_in_place_and_leaves_the_bytes_alone(folder):
    out = TS.rename_files("a", _pairs(*((f"TOOKNO{i:04d}.sor", f"TK_{i:04d}.sor")
                                        for i in range(1, 13))))
    assert len(out["renamed"]) == 12 and out["skipped"] == []
    assert out["folder"] == str(folder)
    assert sorted(os.listdir(folder)) == (
        [f"TK_{i:04d}.sor" for i in range(1, 13)] + ["splice_report.xlsx"])
    assert (folder / "TK_0001.sor").read_bytes() == b"trace-0001"


def test_a_shift_by_one_does_not_collide_with_itself(folder):
    """Renumbering a whole cable renames F0002 onto F0001's name.  In one
    pass half the batch lands on a name still in use; the writer stages every
    source under a temp name first, so the shift goes through."""
    out = TS.rename_files("a", _pairs(*((f"TOOKNO{i:04d}.sor", f"TOOKNO{i + 1:04d}.sor")
                                        for i in range(1, 13))))
    assert len(out["renamed"]) == 12 and out["skipped"] == []
    assert sorted(f for f in os.listdir(folder) if f.endswith(".sor")) == [
        f"TOOKNO{i:04d}.sor" for i in range(2, 14)]
    assert (folder / "TOOKNO0002.sor").read_bytes() == b"trace-0001"


def test_two_files_can_swap_names(folder):
    TS.rename_files("a", _pairs(("TOOKNO0001.sor", "TOOKNO0002.sor"),
                                ("TOOKNO0002.sor", "TOOKNO0001.sor")))
    assert (folder / "TOOKNO0001.sor").read_bytes() == b"trace-0002"
    assert (folder / "TOOKNO0002.sor").read_bytes() == b"trace-0001"


def test_changing_only_the_case_of_a_name_goes_through(folder):
    """On Windows and on a Mac the folder is case-insensitive, so .SOR -> .sor
    reads as "already in the folder" to a naive collision check."""
    (folder / "MIX0001.SOR").write_bytes(b"mixed")
    out = TS.rename_files("a", _pairs(("MIX0001.SOR", "mix0001.sor")))
    assert out["renamed"] == [{"from": "MIX0001.SOR", "to": "mix0001.sor"}]
    assert "mix0001.sor" in os.listdir(folder)


def test_no_temp_files_are_left_behind(folder):
    TS.rename_files("a", _pairs(("TOOKNO0001.sor", "A0001.sor"),
                                ("TOOKNO0002.sor", "CON.sor")))     # one of each
    assert not [f for f in os.listdir(folder) if "otdr-rename" in f]


# ── what it refuses ──────────────────────────────────────────────────────

def test_a_refused_pair_does_not_free_its_name_for_another_pair(folder):
    """The regression this file exists for.  Reading "what the batch frees"
    off the SUBMITTED pairs made a pair that gets refused still look like it
    was vacating its name, and the pair aimed at that name renamed straight
    over a customer's trace.  A name is only free if the file holding it is
    actually moving."""
    out = TS.rename_files("a", _pairs(
        ("TOOKNO0003.sor", "TOOKNO0004.sor"),    # wants 0004's name
        ("TOOKNO0004.sor", "note.txt"),          # refused: extension
    ))
    assert out["renamed"] == []
    assert (folder / "TOOKNO0004.sor").read_bytes() == b"trace-0004"
    assert (folder / "TOOKNO0003.sor").exists()
    assert any("already in the folder" in s["reason"] for s in out["skipped"])


def test_two_files_cannot_be_given_the_same_name(folder):
    out = TS.rename_files("a", _pairs(("TOOKNO0001.sor", "SAME.sor"),
                                      ("TOOKNO0002.sor", "SAME.sor")))
    assert len(out["renamed"]) == 1
    assert [s["reason"] for s in out["skipped"]] == ["two files would be named SAME.sor"]
    assert (folder / "TOOKNO0002.sor").exists()


@pytest.mark.parametrize("bad, why", [
    ("../escape.sor", "not a path"),
    ("sub/deep.sor", "not a path"),
    ("note.txt", "extension must stay"),
    ("CON.sor", "reserved name on Windows"),
    ("lpt3.sor", "reserved name on Windows"),
    ("a:b.sor", "not allowed in a file name"),
    ("trailing.sor ", "space or a dot"),
    (".hidden.sor", "is hidden"),
    ("", "empty"),
    ("x" * 260 + ".sor", "longer than 255"),
])
def test_names_the_writer_will_not_produce(folder, bad, why):
    out = TS.rename_files("a", _pairs(("TOOKNO0001.sor", bad)))
    assert out["renamed"] == []
    assert why in out["skipped"][0]["reason"]
    assert (folder / "TOOKNO0001.sor").exists()


def test_a_name_differing_only_in_case_never_overwrites(folder):
    """On Windows and on a Mac the folder is case-insensitive but
    os.path.normcase is not: on a Mac it is the identity, so "A.SOR" and
    "a.sor" compare as different names while the filesystem says they are the
    same file.  The check right before each rename is what catches it, and
    what it must never do is destroy the file already sitting there."""
    out = TS.rename_files("a", _pairs(("TOOKNO0002.sor", "TOOKNO0001.SOR")))
    survivors = {(folder / f).read_bytes() for f in os.listdir(folder)
                 if f.lower().endswith(".sor")}
    assert b"trace-0001" in survivors and b"trace-0002" in survivors
    if not out["renamed"]:                       # case-insensitive filesystem
        assert "already in the folder" in out["skipped"][0]["reason"]


def test_only_trace_files_move(folder):
    """A span folder also holds reports and caches.  A rule that happens to
    match one must not touch it — at either end of the rename."""
    out = TS.rename_files("a", _pairs(("splice_report.xlsx", "other.xlsx")))
    assert out["renamed"] == []
    assert (folder / "splice_report.xlsx").exists()


def test_a_missing_source_is_skipped_not_raised(folder):
    out = TS.rename_files("a", _pairs(("gone.sor", "here.sor"),
                                      ("TOOKNO0001.sor", "kept.sor")))
    assert out["renamed"] == [{"from": "TOOKNO0001.sor", "to": "kept.sor"}]
    assert out["skipped"][0]["reason"] == "not in the folder any more"


def test_a_name_that_is_already_right_is_neither_renamed_nor_skipped(folder):
    out = TS.rename_files("a", _pairs(("TOOKNO0001.sor", "TOOKNO0001.sor")))
    assert out["renamed"] == [] and out["skipped"] == []


def test_a_folder_that_is_not_loaded_or_gone_is_an_error(folder, tmp_path):
    with pytest.raises(ValueError):
        TS.rename_files("b", _pairs(("TOOKNO0001.sor", "x.sor")))
    TS.CONFIG["dir_a"] = str(tmp_path / "not there")
    with pytest.raises(ValueError):
        TS.rename_files("a", _pairs(("TOOKNO0001.sor", "x.sor")))


def test_an_empty_or_oversized_batch_is_refused(folder):
    with pytest.raises(ValueError):
        TS.rename_files("a", [])
    too_many = _pairs(*(("a%d.sor" % i, "b%d.sor" % i) for i in range(TS.RENAME_MAX + 1)))
    with pytest.raises(ValueError):
        TS.rename_files("a", too_many)


def test_the_folder_listing_is_dropped_so_the_panel_re_reads_it(folder):
    TS.list_fibers(str(folder))                      # warm the cache
    assert str(folder) in TS._LIST_CACHE
    TS.rename_files("a", _pairs(("TOOKNO0001.sor", "TK0001.sor")))
    assert str(folder) not in TS._LIST_CACHE
    assert ("TK0001.sor" in [fn for _, fn in TS.list_fibers(str(folder))])


# ── the route ────────────────────────────────────────────────────────────

def test_the_route_is_origin_checked_and_capped():
    route = PY_SRC.split("if u.path == '/api/rename':", 1)[1].split("if u.path == '/api/trace_edit':", 1)[0]
    assert "if not self._origin_is_local():" in route
    assert "if n > RENAME_BODY_MAX:" in route
    assert "report_error('viewer /api/rename', e)" in route
    # The server writes the names it is handed; it never re-derives them from
    # the tech's rule, which would be a second regex dialect and a second
    # answer to what the preview already promised.
    for word in ("find", "search", "replace", "regex"):
        assert word not in route.lower(), word


# ── the dialog (source-pinned: no JS runtime in this suite) ──────────────

def test_the_files_row_carries_its_own_name():
    """The fiber number is not unique in a multi-wavelength folder, so a
    rename has to key on the file NAME, not on the row's fiber."""
    row = SRC.split("function renderFilesPanel()", 1)[1].split("list.innerHTML", 1)[0]
    assert 'data-name="${esc(files[i] || \'\')}"' in row


def test_the_right_click_menu_offers_rename_and_undo():
    menu = SRC.split("function showFileDirMenu(", 1)[1].split("\nasync function ", 1)[0]
    assert "function showFileDirMenu(x, y, src, fiber, name)" in SRC
    assert "showRenameDialog(src, fiber, name)" in menu
    # Marked files come along, and the count is on the menu item before the
    # dialog opens.
    assert "marked > 1 ? marked + ' marked files' : 'file'" in menu
    assert "gRenameUndo.length" in menu and "undoLastRename()" in menu
    assert "row.dataset.name);" in SRC


def test_the_dialog_posts_the_names_it_previewed():
    fn = SRC.split("async function submitRename() {", 1)[1].split("\n}", 1)[0]
    assert "from: r.name, to: r.newName" in fn
    # A row that is unticked, unchanged, or red is not in the batch.
    assert "if (!r.cb.checked || r.problem || r.newName === r.name) continue;" in fn
    post = SRC.split("async function applyRenameBatches(", 1)[1].split("\n}", 1)[0]
    assert "'/api/rename'" in post
    assert "JSON.stringify({ dir: b.dir, pairs: b.pairs })" in post


def test_undo_replays_what_actually_moved():
    """Not the inverse of what was ASKED for: a batch where the server
    refused four files must put back eight, not twelve."""
    post = SRC.split("async function applyRenameBatches(", 1)[1].split("\n}", 1)[0]
    assert "const done = j.renamed || [];" in post
    assert "done.map(p => ({ from: p.to, to: p.from }))" in post
    undo = SRC.split("async function undoLastRename() {", 1)[1].split("\n}\n", 1)[0]
    assert "gRenameUndo.push(last);" in undo        # a failed undo stays undoable


def test_a_rename_that_moves_the_fiber_number_drops_what_was_keyed_on_it():
    """Every trace key, direction override and removed-file mark in the page
    is "<dir>-<fiber>".  Renumbering makes all of them lie."""
    fn = SRC.split("async function afterRename(", 1)[1].split("\n}\n", 1)[0]
    assert "gSelectedFiles.clear();" in fn
    for store in ("gRemovedFiles", "gDirOverride", "gStoredDir"):
        assert store in fn, store
    assert "gTraces = gTraces.filter(t => t.src !== dir);" in fn
    assert "await loadInfo();" in fn
    # and whatever survives that must still be in the new listing
    assert "gTraces = gTraces.filter(t => live.has(t.key));" in fn


def test_a_half_typed_regex_greys_the_preview_instead_of_lying():
    fn = SRC.split("function renamePreview() {", 1)[1].split("\n}\n", 1)[0]
    bad = fn.split("} catch (e) {", 1)[1].split("return;", 1)[0]
    assert "classList.add('rn-stale')" in bad and "go.disabled = true;" in bad
    assert "classList.remove('rn-stale')" in fn
    assert "#rn-list.rn-stale { opacity: .4; }" in SRC


def test_a_literal_search_does_not_eat_dollar_signs():
    """With the regex box off, "$1" in the replacement is the tech's own
    text; String.replace would read it as a group reference."""
    fn = SRC.split("function renameRule() {", 1)[1].split("\n}", 1)[0]
    assert "rep: useRe ? rep : rep.replace(/\\$/g, '$$$$')," in fn
    assert "find.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&')" in fn


# ── the two mirrors: the preview has to agree with the server ───────────

def test_the_name_check_gives_one_answer_on_every_machine():
    """It went out once reading the separator test through os.path.basename,
    which on Windows treats "a:b.sor" as a drive-relative path: the server
    said "not a path" there and the dialog said "not allowed in a file name"
    everywhere, so the preview and the writer disagreed depending on whose
    laptop it was.  Caught by the Windows CI run, pinned here so it fails on
    a Mac too."""
    py = PY_SRC.split("def rename_check(name):", 1)[1].split("\ndef ", 1)[0]
    code = "\n".join(l for l in py.splitlines() if not l.lstrip().startswith("#"))
    assert "os.path" not in code, "the name rules must not go through os.path"
    assert "'/' in n or '\\\\' in n" in py
    # The character set the two sides refuse has to be the same set, and must
    # not hold the separators — those belong to the path test, so that a path
    # is told it is a path.
    assert """_NAME_BAD_CHARS = set('<>:"|?*')""" in PY_SRC
    assert """'<>:"|?*'.includes(c)""" in SRC
    assert TS.rename_check("a:b.sor").startswith("not allowed in a file name")
    assert TS.rename_check("sub/deep.sor") == "must be a plain file name, not a path"


def test_the_dialogs_name_check_says_what_the_server_says():
    """nameProblem() exists so a name the server would refuse is red in the
    preview, before the click.  Every reason the server can give has to be
    one the dialog can give too, or the tech meets it only afterwards."""
    server = TS.rename_check
    for name in ("", "a/b.sor", "../x.sor", ".hidden.sor", "a:b.sor",
                 "trailing.sor ", "trailing.sor.", "CON.sor", "note.txt",
                 "x" * 260 + ".sor"):
        assert server(name) is not None, name
    assert server("TOOKNO0001.sor") is None
    js = SRC.split("function nameProblem(n) {", 1)[1].split("\n}", 1)[0]
    py = PY_SRC.split("def rename_check(name):", 1)[1].split("\ndef ", 1)[0]
    for phrase in ("the new name is empty", "must be a plain file name, not a path",
                   'a name starting with "." is hidden', "control characters in the name",
                   "not allowed in a file name: ", "a name cannot end in a space or a dot",
                   "name longer than 255 characters", "is a reserved name on Windows",
                   "the extension must stay .sor, .json or .trc"):
        assert phrase in js, phrase
        assert phrase in py, phrase


def test_the_dialogs_fiber_reader_is_the_engines_fiber_reader():
    """fiberNumOf() in viewer.html mirrors extract_fiber_num so the preview
    can warn, per keystroke, that a rename is moving the number the whole
    suite keys fibers on.  Pin the two rules that make it non-obvious, so
    editing the Python here shows up as a failure rather than as a preview
    that quietly stops warning."""
    py = PY_SRC.split("def extract_fiber_num(fn):", 1)[1].split("\ndef ", 1)[0]
    js = SRC.split("function fiberNumOf(name) {", 1)[1].split("\n}", 1)[0]
    waves = re.search(r"\(\?:((?:\d+\|)+\d+)\)\+\$", py).group(1)
    assert waves in SRC.split("const RN_WAVELENGTHS =", 1)[1].split("\n", 1)[0]
    assert r"[\s_\-.]" in py and r"[\s_\-.]" in SRC.split("const RN_WAVELENGTHS =", 1)[1][:20]
    assert r"0\d{3}$" in py and r"0\d{3}$" in js          # the tie-panel port rule
    assert 'startswith("._")' in py and "startsWith('._')" in js

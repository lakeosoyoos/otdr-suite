"""FILES' P/F is the event table's (Robert 2026-10-02).

"P/F should always [be the] event table": the FILES list no longer judges a
file on its own (it used the single-direction gate, so a direction file went
F while the table beside it flagged nothing).  Each table records each file's
verdict as it judges its rows (noteTableVerdict), and FILES and Select Same
Pass/Fail read that (tableFileFails):

  - A+B tables (Suite and FR layout): a file fails when its fiber's Average
    fails or its own direction row does ("F on both" when the Average fails);
  - a one-direction table: when its row does;
  - the FEC table: when its row does;
  - a file no table has judged (not added): no P/F, blank.

The column stays optional, on or off from the FILES header's right-click.
No Node here, so the JS is checked at the source, like the other Viewer tests.
"""
from conftest import REPO_ROOT

SRC = (REPO_ROOT / 'viewer' / 'viewer.html').read_text(encoding='utf-8')


def _fn(name):
    return SRC.split('function %s(' % name, 1)[1].split('\n}\n', 1)[0]


def test_the_table_records_and_files_reads():
    note = _fn('noteTableVerdict')
    assert 'gTableFails.set(t.key, !!fail);' in note
    # a new render of the tables starts the map afresh on its first verdict
    assert 'if (gTableFailsPass !== gTablePass) { gTableFails = new Map(); gTableFailsPass = gTablePass; }' in note
    assert 'gTablePass++;' in _fn('renderEventTable')
    assert "if (gTableFails.size) { gTableFails = new Map(); filesPfChanged(); }" in _fn('renderEventTable')
    assert 'return gTableFails.has(key) ? gTableFails.get(key) : null;' in SRC
    # FILES repaints only when its P/F shows or sorts
    assert "gFileCols.has('pf') || (gFileSort && gFileSort.col === 'pf')" in _fn('filesPfChanged')


def test_every_table_records_its_rows():
    pair = ("    const avg = legFails(fi, 'avg');\n"
            "    noteTableVerdict(p.ta, avg || legFails(fi, 'a'));\n"
            "    noteTableVerdict(p.tb, avg || legFails(fi, 'b'));")
    assert pair in _fn('paintFrBidiGrid')
    suite = _fn('paintSuiteBidiGrid')
    assert pair in suite
    assert 'if (oneDir) { noteTableVerdict(legOf(p), legFails(fi, oneDir)); return; }' in suite
    assert 'traces.forEach((t, ti) => noteTableVerdict(t, rowFails[ti]));' in _fn('renderFastReporterGrid')
    assert ('rows.forEach(({ t, g }) => { if (g && g.found) '
            'noteTableVerdict(t, g.fail_loss || g.fail_refl); });') in _fn('renderFecGrid')


def test_files_shows_blank_until_judged_and_the_column_stays_optional():
    panel = _fn('renderFilesPanel')
    assert "const pf = tableFileFails(`${dir}-${f}`);" in panel
    assert "${pf == null ? '' : pfMark(pf)}</span>" in panel
    assert "['pf', 'P/F', '30px'," in SRC                       # still a column
    assert "const FILE_COLS_DEFAULT = ['dir'];" in SRC           # off until turned on
    assert "case 'pf': return r.pf == null ? null : (r.pf ? 1 : 0);" in SRC
    # nothing judges a file on its own any more
    assert 'function fileFails(' not in SRC and 'function pairedFileKeys(' not in SRC

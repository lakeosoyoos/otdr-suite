"""P/F as FastReporter shows it, and FILES' P/F is the event table's.

Robert 2026-10-02, with FR on screen: "when we go Bidi we only show P/F on
the average row and then we don't show it at all in the files panel", and
"P/F should always [be the] event table".

  - an A+B table (Suite and FR layout): one P/F per fibre, on its Average
    row, for the whole fibre at the report's gates; the A->B / B->A rows'
    P/F cells are empty (their failing cells still print red);
  - a one-direction table: every row keeps its P/F;
  - FILES: a file's P/F is the one-direction table's verdict for its row
    (noteTableVerdict / tableFileFails); a file of a fibre loaded both ways,
    or one no table has judged (not added), shows none;
  - the FILES P/F column stays optional, on or off from its header's
    right-click.
No Node here, so the JS is checked at the source, like the other Viewer tests.
"""
from conftest import REPO_ROOT

SRC = (REPO_ROOT / 'viewer' / 'viewer.html').read_text(encoding='utf-8')


def _fn(name):
    return SRC.split('function %s(' % name, 1)[1].split('\n}\n', 1)[0]


def test_a_two_direction_table_shows_p_f_on_the_average_row_only():
    fr = _fn('paintFrBidiGrid')
    assert ("      which === 'avg'\n"
            "        ? `<td class=\"${pfClass(fibreFails(fi))}\"") in fr
    assert "        : '<td></td>'," in fr
    suite = _fn('paintSuiteBidiGrid')
    assert '    const fail = LEGS.some(w => legFails(fi, w));' in suite
    assert "      (!oneDir && which !== 'avg') ? '<td></td>'" in suite
    # the whole fibre: any of its rows failing
    assert "const fibreFails = fi => ['a', 'b', 'avg'].some(w => legFails(fi, w));" in fr
    assert 'const rowFails = have.map((_p, fi) => LEGS.some(w => legFails(fi, w)));' in suite


def test_only_one_direction_tables_hand_files_a_verdict():
    assert 'noteTableVerdict' not in _fn('paintFrBidiGrid')
    assert ('  if (oneDir) have.forEach((p, fi) => noteTableVerdict(legOf(p), rowFails[fi]));'
            in _fn('paintSuiteBidiGrid'))
    assert 'traces.forEach((t, ti) => noteTableVerdict(t, rowFails[ti]));' in _fn('renderFastReporterGrid')
    assert ('rows.forEach(({ t, g }) => { if (g && g.found) '
            'noteTableVerdict(t, g.fail_loss || g.fail_refl); });') in SRC


def test_the_store_starts_afresh_each_render():
    note = _fn('noteTableVerdict')
    assert 'gTableFails.set(t.key, !!fail);' in note
    assert 'if (gTableFailsPass !== gTablePass) { gTableFails = new Map(); gTableFailsPass = gTablePass; }' in note
    ret = _fn('renderEventTable')
    assert 'gTablePass++;' in ret
    assert "if (gTableFails.size) { gTableFails = new Map(); filesPfChanged(); }" in ret
    assert 'return gTableFails.has(key) ? gTableFails.get(key) : null;' in SRC
    # FILES repaints only when its P/F shows or sorts
    assert "gFileCols.has('pf') || (gFileSort && gFileSort.col === 'pf')" in _fn('filesPfChanged')


def test_files_reads_the_table_and_the_column_stays_optional():
    panel = _fn('renderFilesPanel')
    assert "const pf = tableFileFails(`${dir}-${f}`);" in panel
    assert "${pf == null ? '' : pfMark(pf)}</span>" in panel
    assert "['pf', 'P/F', '30px'," in SRC                       # still a column
    assert "const FILE_COLS_DEFAULT = ['dir'];" in SRC           # off until turned on
    assert "case 'pf': return r.pf == null ? null : (r.pf ? 1 : 0);" in SRC
    assert 'pf: tableFileFails(key)' in _fn('fileTraits')       # Select Same Pass/Fail too
    # nothing judges a file on its own any more
    assert 'function fileFails(' not in SRC and 'function pairedFileKeys(' not in SRC

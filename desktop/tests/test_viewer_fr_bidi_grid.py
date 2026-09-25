from pathlib import Path
"""The Viewer's A+B table: FR's bidirectional layout in BOTH analysis modes --
numbers from the engine in the app's mode, judged on each fibre's Average row.

No Node here, so the JS is checked at the source: the mode guard, the FR
grid's shape (three rows per fibre, A→B / B→A / Average, sections between,
synthesised legs in grey, FR's kinds, no reflectance on the Average row), and
that the single-direction grid every other mode renders is the function it
was, called the way it was.
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..'))
SRC = open(os.path.join(ROOT, 'viewer', 'viewer.html'), encoding='utf-8').read()


def _fn(name):
    i = SRC.index('function ' + name + '(')
    return SRC[i:SRC.index('\n}\n', i) + 3]


def test_both_modes_use_fr_s_bidirectional_layout_for_a_pair():
    """Robert 2026-09-25: the A+B table is laid out as FR's in BOTH analysis
    modes; the server runs the engine in the app's mode, so only the numbers
    differ."""
    body = _fn('renderEventTable')
    guard = "if (renderFrBidiGrid(visible, host, hint)) return;"
    assert guard in body
    after = body[body.index(guard) + len(guard):]
    assert after.lstrip().startswith('renderFastReporterGrid(visible, host, hint);'), after[:120]
    assert 'if (!pairs.length) return false;' in _fn('renderFrBidiGrid')
    srv = (Path(__file__).resolve().parents[2] / 'viewer' / 'trace_server.py').read_text(encoding='utf-8')
    assert "'--analysis', mode]" in srv
    assert "key = (mode, pa," in srv


def test_the_fr_grid_is_fr_s_bidirectional_table():
    body = _fn('paintFrBidiGrid')
    assert "fetch(`/api/fr_table?fibers=${pairs.map(p => p.fiber).join(',')}`)" in _fn('renderFrBidiGrid')
    # three rows per fibre, in FR's order
    assert "[[fi, 'a'], [fi, 'b'], [fi, 'avg']]" in body
    assert "which === 'a' ? 'A→B' : which === 'b' ? 'B→A' : 'Average'" in body
    # FR's kinds on the merged row's Type
    assert "r.type === 3 ? 'Reflective' : r.type === 1 ? 'Positive'" in body
    assert "r.type === 2 ? 'Non-reflective'" in body
    # the Average row prints no reflectance; the legs print theirs
    assert "lossCell(x.row.loss, false, ` data-col=\"${i}\"`)" in body
    # ... and its reflectance cell is empty (cellText blanks it again under
    # "only failing events", which is the only thing that wraps it)
    assert "+ `<td>${cellText('---')}</td>`" in body
    # synthesised legs are grey, in both the loss and the reflectance cell
    assert "if (synthetic) cls.push('fr-synth');" in body
    assert "leg.synthetic ? ' class=\"fr-synth\"' : ''" in body
    assert re.search(r"table\.fr-table td\.fr-synth\s*\{[^}]*color", SRC)
    # sections: loss and attenuation per direction, the merged pair on the Average row
    assert "const v = s ? (which === 'avg' ? s : s[which]) : null;" in body
    assert "fmt(v.att_db_km)" in body and "fmt(v.loss)" in body
    # the section is printed only where the fibre's next row is the next column
    assert "if (!c || !n || n.k !== c.k + 1) return null;" in body
    # the report's verdict, not FR's, drives P/F -- said so on the cell
    assert "this is OUR verdict, not FastReporter's" in body
    # rows carry what the span menu and the trace-label click need
    assert 'data-dir="${t.dir}" data-src="${t.src || t.dir}" data-fiber="${p.fiber}"' in body
    assert "tb.addEventListener('contextmenu'" in body and 'gGridGoTo = (t, e) =>' in body


def test_a_fibre_is_judged_on_its_average_row_only():
    """Robert 2026-09-25: the legs print plain; P/F and the flagged-rows
    filter follow the Average row."""
    body = _fn('paintFrBidiGrid')
    assert "const rowFails = have.map((_p, fi) => legFails(fi, 'avg'));" in body
    assert "const fail = which === 'avg' && legFails(fi, 'avg');" in body
    # a direction row always shows a pass mark, as FR's do
    assert "which !== 'avg' ? '<td class=\"fr-pf-pass\"" in body
    assert "data-km=\"${rawKm(leg.pos_m)}\"`, true)" in body


def test_min_max_average_strip_under_the_fibres():
    """FR prints Minimum / Maximum / Average under the fibres, taken over the
    fibres' Average rows; only that Average is gate-judged."""
    body = _fn('paintFrBidiGrid')
    assert "const ls = c.ev.filter(x => x).map(x => x.row.loss).filter(num);" in body
    assert "aggRow('Average', a => a.reduce((x, y) => x + y, 0) / a.length, true)," in body
    assert "`<tfoot>${aggRows.join('')}</tfoot>`" in body

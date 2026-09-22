"""FastReporter mode's Viewer table: FR's bidirectional table, verbatim -- and
OTDR Suite mode's table exactly as it was.

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


def test_suite_mode_renders_the_grid_it_always_did():
    body = _fn('renderEventTable')
    guard = "if (gAnalysisMode === 'fr' && renderFrBidiGrid(visible, host, hint)) return;"
    assert guard in body
    # ...and the classic grid is the very next statement, unconditional
    after = body[body.index(guard) + len(guard):]
    assert after.lstrip().startswith('renderFastReporterGrid(visible, host, hint);'), after[:120]
    # the FR path never runs outside FR mode: the only call site is behind the guard
    assert SRC.count('renderFrBidiGrid(') == 2          # definition + the guarded call
    # the FR grid steps aside when nothing is loaded in both directions
    assert 'if (!pairs.length) return false;' in _fn('renderFrBidiGrid')


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
    assert "lossCell(x.row.loss, false, ` data-col=\"${i}\"`) + '<td>---</td>'" in body
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

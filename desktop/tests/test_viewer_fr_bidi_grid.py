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
    assert "['a', 'b', 'avg']" in body and "w === 'avg'" in body
    assert "which === 'a' ? 'A→B' : which === 'b' ? 'B→A' : 'Average'" in body
    # FR's kinds on the merged row's Type
    assert "r.type === 3 ? 'Reflective' : r.type === 1 ? 'Positive'" in body
    assert "r.type === 2 ? 'Non-reflective'" in body
    # the Average row prints no reflectance; the legs print theirs
    assert "lossCell(x.row.loss, false, ` data-col=\"${i}\"`, gateFor(isRefl(x), false))" in body
    # ... and its reflectance cell is empty (cellText blanks it again under
    # "only failing events", which is the only thing that wraps it)
    assert "+ `<td>${cellText('---')}</td>`" in body
    # synthesised legs are grey, in both the loss and the reflectance cell
    assert "if (synthetic) cls.push('fr-synth');" in body
    assert "leg.synthetic ? 'fr-synth' : ''" in body   # a synthesised leg is greyed
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


def test_every_row_is_judged_at_the_report_s_own_gates():
    """Robert 2026-09-25: every threshold comes from the report that opened the
    Viewer.  As FR does (driven 2026-09-25), each row carries its own verdict:
    the Average at the bidirectional gates, a direction row at the
    single-direction gates plus reflectance; a connector at connector loss."""
    src = SRC
    assert "if (!reflective) return leg ? gThresholds.single_dir : activeGateDb();" in src
    assert "if (!leg) return gThresholds.connector;" in src
    # the report's mid-span reflectance rule: floor, optional ceiling, and the
    # dead zone at both ends (the fibre end's -29 dB is never judged)
    assert "const dead = Math.min(T.dead_km, T.dead_frac * eofKm);" in src
    assert "if (posKm < dead || posKm > eofKm - dead || eofKm - posKm < 1.0) return false;" in src
    assert "if (refl < T.refl_floor) return false;" in src
    body = _fn('paintFrBidiGrid')
    assert "if (which === 'avg') return clearsAt(x.row.loss, gateFor(isRefl(x), false));" in body
    assert "if (legReflFails(x, which)) return true;" in body
    assert "return clearsAt(leg.loss, gateFor(isRefl(x), true));" in body
    assert "const fail = legFails(fi, which);" in body
    assert "['a', 'b', 'avg'].some(w => legFails(fi, w))" in body
    # a launch level (status 0x08) or synthesised leg is not a reading
    assert "!leg.synthetic && !(Number(leg.status || 0) & 0x08)" in body


def test_the_server_hands_the_viewer_every_report_gate():
    srv = (Path(__file__).resolve().parents[2] / 'viewer' / 'trace_server.py').read_text(encoding='utf-8')
    assert "'connector': 'BIDIR_CONNECTOR_LOSS'" in srv and "'refl': 'LAUNCH_BAD_REFL_DB'" in srv
    run = (Path(__file__).resolve().parents[2] / 'splicereport' / 'run_splicereport.py').read_text(encoding='utf-8')
    for name in ('BIDIR_CONNECTOR_LOSS', 'LAUNCH_BAD_REFL_DB', 'MIDSPAN_REFL_WARN_DB',
                 'MIDSPAN_REFL_CEIL_DB', 'LAUNCH_FIBER_MAX', 'MIDSPAN_DEAD_SPAN_FRAC'):
        assert f"'{name}'" in run.split('def _effective_gates', 1)[1][:900], name


def test_min_max_average_strip_under_the_fibres():
    """FR prints Minimum / Maximum / Average under the fibres, taken over the
    fibres' Average rows; only that Average is gate-judged."""
    body = _fn('paintFrBidiGrid')
    assert "const ls = c.ev.filter(x => x).map(x => x.row.loss).filter(num);" in body
    assert "aggRow('Average', a => a.reduce((x, y) => x + y, 0) / a.length, true)," in body
    assert "`<tfoot>${aggRows.join('')}</tfoot>`" in body


def test_a_mixed_column_gets_fr_s_type_column():
    """FR (driven 2026-09-25, ELMMIL F21-F23 at 48.06 km): when the fibres'
    Average rows disagree on the type, the header drops the type and the event
    gains a Type column with each row's own type; a synthesised leg's is blank."""
    body = _fn('paintFrBidiGrid')
    assert "cols.forEach(c => { c.mixed = colKinds(c).length > 1; });" in body
    assert "const colKind = c => c.mixed ? '' : colKinds(c).join('');" in body
    assert "(c.mixed ? '<th class=\"fr-sub\">Type</th>' : '')" in body
    assert "typeCell(c, leg.synthetic ? '' : kind(leg))" in body
    assert "typeCell(c, kind(x.row))" in body
    # the spacer row and the Min/Max/Average strip count the extra cell
    assert "kept.filter(c => c.mixed).length" in body
    assert "cells.push((c.mixed ? '<td></td>' : '')" in body

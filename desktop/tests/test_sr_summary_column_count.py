"""The Splice Report page's summary line counts the report's columns the way
the workbook names them (2026-10-02).

A small job lays its closures out as Event columns.  The manifest's
n_splices counts only kind 'splice', so the green line read

    A -> B · 24 fibers · 0 splices · span 64.04 km · 12 flagged events

above a grid of twelve Event columns holding the flags.  The workbook's
Reburn Summary says "2 ribbons x 12 events" / "Real event columns 12": the
page now says "12 events" too.  A splice job still says "8 splices".
"""
from __future__ import annotations

import ast

import openpyxl

from conftest import (run_splicereport, APP_PATH,
                      FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR)


def _helpers():
    tree = ast.parse(APP_PATH.read_text(encoding='utf-8'))
    mod = ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef)
                           and n.name in ('_count', '_sr_column_count')],
                     type_ignores=[])
    ns = {}
    exec(compile(mod, 'app.py', 'exec'), ns)
    return ns['_sr_column_count']


def _cols(*kinds):
    return [{'index': i, 'kind': k, 'km': float(i)} for i, k in enumerate(kinds)]


def test_a_splice_job_counts_splices():
    count = _helpers()
    res = {'n_splices': 8, 'columns': _cols(*['splice'] * 8)}
    assert count(res) == '8 splices'


def test_one_splice_is_singular():
    count = _helpers()
    assert count({'n_splices': 1, 'columns': _cols('splice')}) == '1 splice'


def test_an_event_job_counts_its_event_columns():
    count = _helpers()
    res = {'n_splices': 0, 'columns': _cols(*['event'] * 12)}
    assert count(res) == '12 events'


def test_other_column_kinds_are_not_counted():
    count = _helpers()
    res = {'n_splices': 0, 'columns': _cols('event', 'event', 'connector', 'bend')}
    assert count(res) == '2 events'


def test_a_manifest_without_columns_keeps_n_splices():
    count = _helpers()
    assert count({'n_splices': 3}) == '3 splices'


def test_the_line_uses_the_count():
    src = APP_PATH.read_text(encoding='utf-8')
    tree = ast.parse(src)
    render = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                  and n.name == '_render_sr_result')
    body = ast.get_source_segment(src, render)
    assert '_sr_column_count(res)' in body
    assert "_count(res['n_splices'], 'splice')" not in body


def test_the_page_says_what_the_workbook_says(tmp_path):
    """A real run: the line's count and noun match the Reburn Summary's
    'Real <noun> columns' row of the same report."""
    out = tmp_path / 'sr.xlsx'
    rc, res, err = run_splicereport(FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR, out)
    assert rc == 0 and res and res.get('ok'), err[-800:]
    ws = openpyxl.load_workbook(out)['Reburn Summary']
    row = next(r for r in ws.iter_rows(values_only=True)
               if isinstance(r[0], str) and r[0].startswith('Real ')
               and r[0].endswith(' columns'))
    noun, n = row[0].split()[1], int(row[1])
    assert _helpers()(res) == f"{n} {noun}" + ('' if n == 1 else 's')

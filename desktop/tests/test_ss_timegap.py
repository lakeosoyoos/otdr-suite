"""Time-gap column everywhere a pair is displayed (Robert 2026-07-21).

The acquisition-time gap between the two shots of a pair is duplicate
evidence (seconds apart = re-shoot; days apart = caution), so every table
that names a pair carries it: the confirmed-duplicates detail (already
had it), both Top-30 tables, the per-file verdict (gap to the displayed
closest partner), the TRC closest-non-duplicate table, and the Excel
Top-30 sheets in both modes.  Source-locks only — secretsauce modules are
never imported into the pytest process (engine isolation).
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SS = os.path.join(ROOT, 'secretsauce')


def _src(name):
    with open(os.path.join(SS, name), encoding='utf-8') as f:
        return f.read()


def test_sor_pdf_tables_have_gap():
    s = _src('report_sor.py')
    # dup detail (pre-existing) + top-30 disagreement + top-30 similarity
    assert s.count('<th>Time gap</th>') >= 3
    # per-file verdict: gap to the displayed closest partner
    assert '<th>Time gap (closest)</th>' in s
    assert '_gap_str(f["name"], partner)' in s
    assert s.count("_gap_str(p[\"a\"], p[\"b\"])") >= 2


def test_trc_pdf_tables_have_gap():
    s = _src('report.py')
    # dup detail (pre-existing) + closest non-duplicate pairs
    assert s.count('<th>Time gap</th>') >= 2


def test_xlsx_top30_sheets_have_gap():
    for name in ('report_sor.py', 'report.py'):
        s = _src(name)
        xlsx_part = s.split('def build_xlsx', 1)[1]
        assert xlsx_part.count("'Time gap (s)'") >= 3, name  # dup + 2 top-30


def test_missing_timestamp_renders_dash():
    # the guard that blanks the gap when either timestamp is absent
    for name in ('report_sor.py', 'report.py'):
        s = _src(name)
        assert "if _ta and _tb else" in s or "if ta and tb else" in s, name

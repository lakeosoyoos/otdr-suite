"""Secret Sauce PDF cap + scaled Chrome timeout — regression tests.

Zach 2026-07-21 (otdr-suite-errors #9): an all_dups folder flagged 62,014
pairs >=50%; the unbounded confirmed-duplicates table blew headless
Chrome's fixed 180 s print budget and the run crashed.  Fixes under test:
  * PDF renders at most PDF_DUP_ROWS_CAP duplicate rows + an overflow note
    (both SOR and TRC modes); the Excel report always carries the full list
  * Chrome print timeout scales with HTML size (180 s base -> 480 s cap)

Engine isolation: secretsauce modules are exercised via SUBPROCESS only —
importing them in the pytest process would collide this suite's
splicereport sor_reader with secretsauce's copy on sys.path.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SS = os.path.join(ROOT, 'secretsauce')


def _ss_eval(expr):
    """Evaluate a python expression inside a clean secretsauce process."""
    code = (f"import sys; sys.path.insert(0, {SS!r}); "
            f"import report; print(repr({expr}))")
    p = subprocess.run([sys.executable, '-c', code],
                       capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr[-800:]
    return eval(p.stdout.strip().splitlines()[-1])


# ── _capped_rows ─────────────────────────────────────────────────────────

def test_under_cap_returns_all():
    assert _ss_eval("report._capped_rows([1, 2, 3], 5)") == ([1, 2, 3], 0)


def test_over_cap_truncates_and_counts():
    assert _ss_eval("report._capped_rows(list(range(10)), 3)") == ([0, 1, 2], 7)


def test_cap_none_disables():
    assert _ss_eval("report._capped_rows(list(range(9)), None)") == (list(range(9)), 0)


def test_default_cap_value():
    assert _ss_eval("report.PDF_DUP_ROWS_CAP") == 500


# ── _pdf_timeout_for ─────────────────────────────────────────────────────

def test_timeout_small_html_is_base():
    assert _ss_eval("report._pdf_timeout_for(200_000)") == 180


def test_timeout_scales_with_size():
    assert _ss_eval("report._pdf_timeout_for(2_000_000)") == 240


def test_timeout_capped():
    assert _ss_eval("report._pdf_timeout_for(50_000_000)") == 480


# ── wiring locks ─────────────────────────────────────────────────────────

def _src(name):
    with open(os.path.join(SS, name), encoding='utf-8') as f:
        return f.read()


def test_sor_pdf_uses_cap():
    s = _src('report_sor.py')
    assert '_capped_rows(dup_pairs_sorted' in s
    assert 'for p in dup_pairs_render:' in s
    assert 'more pairs at' in s          # overflow note


def test_trc_pdf_uses_cap():
    s = _src('report.py')
    assert '_capped_rows(dup_pairs_sorted' in s
    assert 'for p in dup_pairs_render:' in s


def test_xlsx_paths_stay_uncapped():
    # the Excel writers must never truncate the confirmed-duplicates list
    for name in ('report_sor.py', 'report.py'):
        s = _src(name)
        xlsx_part = s.split('def build_xlsx', 1)[1]
        assert '_capped_rows' not in xlsx_part


def test_chrome_timeout_is_scaled():
    s = _src('report.py')
    assert 'timeout=_pdf_timeout_for(len(html_str))' in s

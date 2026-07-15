"""Regression tests for the Secret Sauce robust analysis window +
suspected-break reporting (sandbox/ss-window-robust).

THE BUG: report_sor.py computed the folder's common analysis span as the
raw MINIMUM trace length over all files, so ONE broken fiber collapsed the
whole folder's window.  A-F West 145-288 (266 files, median EOF 2 037 m)
has port 198 physically broken ~1 km out (A-side EOF 1 005 m, B-side
1 036 m — the two sum to the span), which shrank the interior to 305 m,
hid the folder-wide similarity, misrouted the regime, and fed the
1 997-false-positive flood.

THE FIX (secretsauce/report_sor.py):
  * _robust_common_span — EOFs below 75 % of the folder median are
    suspected breaks (always reported); when the raw-min window has
    collapsed below the 2 km launch+connector floor, the window is rebuilt
    from the healthy population and the broken strands are excluded from
    pair metrics (they physically lack the glass being compared).
  * _ab_break_notes — when BOTH directions of the same port are suspected
    breaks and their EOFs sum to the folder median (±10 %), each gets
    "A+B lengths are consistent with a break ~<EOF> m from the <prefix>
    end".
  * Reporting — additive `short_traces` manifest key (present only when
    non-empty), a "Suspected broken / short fibers" PDF section, and a
    "Suspected short fibers" XLSX sheet + Summary row (all only when
    suspected breaks exist, so unaffected outputs stay byte-stable).

Calibration lock (2026-07-14, real folders): exclusion fires on A-F West
(break at 49/51 % of median, window collapsed to 1 005 m < 2 km) but NOT
on ELMMIL (ELMMIL0231 @ 22 288 m, 32 % of median) or SANDUR (20 144 m,
20 %), whose raw-min windows are >= 2 km — their long-standing pair
tables must stay byte-identical, with the short fibers merely REPORTED.

Namespace isolation rule: the Secret Sauce engine is only ever exercised
through subprocesses (run_secretsauce / sys.executable -c), never imported
into the test process.
"""
from __future__ import annotations

import json
import subprocess
import sys

from conftest import SECRETSAUCE_DIR, run_secretsauce, single_dir_fixture


def _run_engine_script(script, *args, timeout=240):
    p = subprocess.run([sys.executable, "-c", script, str(SECRETSAUCE_DIR),
                        *[str(a) for a in args]],
                       capture_output=True, text=True, timeout=timeout)
    assert p.returncode == 0, p.stderr[-2000:]
    return json.loads(p.stdout.strip().splitlines()[-1])


# ── _robust_common_span unit behavior ───────────────────────────────────────

_SPAN_UNIT_SCRIPT = r"""
import sys, json
sys.path.insert(0, sys.argv[1])
from report_sor import _robust_common_span

cases = {}

# Homogeneous folder: exact min preserved (byte-identity guarantee), no
# outliers, no exclusions.
cases['homog'] = _robust_common_span([2036.8, 2037.2, 2040.0, 2041.9])

# Homogeneous LONG folder (SEANOR-like): min is 99% of median — untouched.
cases['homog_long'] = _robust_common_span([108817.5] + [109954.0] * 9)

# A-F West shape: one A+B broken pair collapses the min below 2 km while
# the median says ~2 037 m -> robust window fires, both outliers excluded.
west = [1005.0, 1036.4] + [2037.0] * 264
cases['west'] = _robust_common_span(west)

# ELMMIL shape: extreme outlier (32% of median) but the raw-min window is
# still >= 2 km -> REPORTED, NOT excluded, span stays the raw min.
cases['elmmil'] = _robust_common_span([22288.1] + [69554.0] * 9)

# Two-file folder: only 1 healthy file would remain -> no exclusion
# (need >=2 files for any pair), span stays the raw min.
cases['twofile'] = _robust_common_span([1005.0, 2037.0])

# Short-but-homogeneous panel (Deming-style): no outliers, min preserved —
# the short_panel / short-common-span regime routes keep working on it.
cases['short_panel'] = _robust_common_span([150.0] * 60)

# Outlier just ABOVE the 75% cut is span-length jitter, not a break.
cases['jitter'] = _robust_common_span([1530.0] + [2000.0] * 9)

print(json.dumps(cases))
"""


def test_robust_common_span_units():
    out = _run_engine_script(_SPAN_UNIT_SCRIPT)

    span, med, outliers, excluded = out["homog"]
    assert span == 2036.8 and outliers == [] and excluded == []

    span, med, outliers, excluded = out["homog_long"]
    assert span == 108817.5 and outliers == [] and excluded == []

    span, med, outliers, excluded = out["west"]
    assert span == 2037.0, out["west"]
    assert med == 2037.0
    assert outliers == [0, 1] and excluded == [0, 1]

    span, med, outliers, excluded = out["elmmil"]
    assert span == 22288.1, "raw-min >= 2 km window must be preserved"
    assert outliers == [0], "the short fiber must still be REPORTED"
    assert excluded == [], "…but NOT excluded (pair table byte-identity)"

    span, med, outliers, excluded = out["twofile"]
    assert span == 1005.0 and excluded == []

    span, med, outliers, excluded = out["short_panel"]
    assert span == 150.0 and outliers == [] and excluded == []

    span, med, outliers, excluded = out["jitter"]
    assert outliers == [] and excluded == [] and span == 1530.0


# ── _ab_break_notes unit behavior ───────────────────────────────────────────

_AB_UNIT_SCRIPT = r"""
import sys, json
sys.path.insert(0, sys.argv[1])
from report_sor import _ab_break_notes

def entries(*pairs):
    return [{'file': f, 'eof_m': e} for f, e in pairs]

cases = {}

# The boss's West case: both directions of port 198, EOFs sum to ~median.
es = entries(('BCK1BCK60198', 1005.0), ('BCK6BCK10198', 1036.4))
_ab_break_notes(es, 2037.2)
cases['west'] = es

# Sum NOT consistent with the median span -> no note.
es = entries(('BCK1BCK60198', 1005.0), ('BCK6BCK10198', 1500.0))
_ab_break_notes(es, 2037.2)
cases['bad_sum'] = es

# Same prefix (same direction shot twice) -> not an A/B pair -> no note.
es = entries(('BCK1BCK60198', 1005.0), ('BCK1BCK60199', 1036.4))
_ab_break_notes(es, 2037.2)
cases['same_prefix'] = es

# Single-direction break (no partner) -> no note.
es = entries(('BCK1BCK60198', 1005.0),)
_ab_break_notes(es, 2037.2)
cases['single'] = es

# File without a trailing port number -> skipped, no crash.
es = entries(('NOPORT', 1005.0), ('BCK6BCK10198', 1036.4))
_ab_break_notes(es, 2037.2)
cases['no_port'] = es

print(json.dumps(cases))
"""


def test_ab_break_consistency_units():
    out = _run_engine_script(_AB_UNIT_SCRIPT)

    a, b = out["west"]
    assert a["break_note"] == ("A+B lengths are consistent with a break "
                               "~1005 m from the BCK1BCK6 end"), a
    assert b["break_note"] == ("A+B lengths are consistent with a break "
                               "~1036 m from the BCK6BCK1 end"), b

    for key in ("bad_sum", "same_prefix", "single", "no_port"):
        for e in out[key]:
            assert "break_note" not in e, (key, e)


# ── Pipeline e2e: _analyze_sor with a stubbed loader ────────────────────────
# Real fixtures are all healthy long fibers, so a broken strand is
# synthesized by stubbing report_sor.load_sor_file inside the engine
# subprocess — everything downstream of loading (robust window, exclusion,
# short_traces, pair metrics, regime, renderers) runs for real.

_STUB_PRELUDE = r"""
import sys, os, json, hashlib
import numpy as np
sys.path.insert(0, sys.argv[1])
import report_sor

def _stub_load(path):
    name = os.path.splitext(os.path.basename(path))[0]
    eof = float(open(path).read().strip())
    n = int(eof)                       # dz = 1 m
    seed = int(hashlib.md5(name.encode()).hexdigest()[:8], 16)  # deterministic
    rng = np.random.RandomState(seed)
    trace = (20.0 - 0.0002 * np.arange(n, dtype=np.float64)
             + rng.randn(n) * 0.05).astype(np.float32)
    return {
        'name': name, 'filepath': path,
        'trace': trace, 'pos': np.arange(n, dtype=np.float64) * 1.0,
        'length': eof, 'loss': None, 'max_splice_dB': None,
        'timestamp': None, 'wavelength': 1550, 'serial_number': None,
        'events': [],
    }

report_sor.load_sor_file = _stub_load

def make_folder(d, spec):
    os.makedirs(d, exist_ok=True)
    for name, eof in spec:
        with open(os.path.join(d, name + '.sor'), 'w') as fh:
            fh.write(str(eof))
    return d

HEALTHY = [('AAABBB%04d' % i, 2190.0 + 2 * i) for i in range(1, 9)]
BROKEN = [('AAABBB0198', 1000.4), ('BBBAAA0198', 1039.8)]
"""

_PIPELINE_SCRIPT = _STUB_PRELUDE + r"""
out = {}

folder = make_folder(os.path.join(sys.argv[2], 'broken'), HEALTHY + BROKEN)
a = report_sor._analyze_sor(folder)
out['min_L'] = a['min_L']
out['names'] = sorted(f['name'] for f in a['files'])
out['n_pairs'] = len(a['pairs'])
out['pair_names'] = sorted({n for p in a['pairs'] for n in (p['a'], p['b'])})
out['short_traces'] = a['short_traces']
out['flags'] = [a['n99'], a['n50']]

folder2 = make_folder(os.path.join(sys.argv[2], 'clean'), HEALTHY)
a2 = report_sor._analyze_sor(folder2)
out['clean_min_L'] = a2['min_L']
out['clean_short_traces'] = a2['short_traces']
out['clean_n_pairs'] = len(a2['pairs'])

print(json.dumps(out))
"""


def test_pipeline_excludes_broken_strands_and_reports_them(tmp_path):
    out = _run_engine_script(_PIPELINE_SCRIPT, tmp_path)

    # Window rebuilt from the healthy population (min healthy EOF = 2192).
    assert out["min_L"] == 2192.0, out["min_L"]

    # Broken strands are gone from pair metrics…
    assert len(out["names"]) == 8 and "BBBAAA0198" not in out["names"]
    assert out["n_pairs"] == 28                      # C(8,2)
    assert "AAABBB0198" not in out["pair_names"]
    assert "BBBAAA0198" not in out["pair_names"]

    # …but NEVER silently: both surface as suspected breaks, with the
    # cross-direction A+B consistency note anchored at each launch end.
    # (Median over all 10 EOFs = (2196 + 2198) / 2 = 2197.)
    st = out["short_traces"]
    assert [e["file"] for e in st] == ["AAABBB0198", "BBBAAA0198"]
    for e in st:
        assert e["excluded"] is True
        assert e["median_eof_m"] == 2197.0
        assert "suspected break" in e["note"]
    assert st[0]["note"] == ("ends at 1000 m (folder median 2197 m) "
                             "— suspected break")
    assert st[0]["break_note"] == ("A+B lengths are consistent with a break "
                                   "~1000 m from the AAABBB end")
    assert st[1]["break_note"] == ("A+B lengths are consistent with a break "
                                   "~1040 m from the BBBAAA end")

    # Independent synthetic fibers must not be called duplicates.
    assert out["flags"] == [0, 0]

    # Homogeneous folder: raw-min behavior byte-identical, nothing reported.
    assert out["clean_min_L"] == 2192.0
    assert out["clean_short_traces"] == []
    assert out["clean_n_pairs"] == 28


# ── Renderer e2e: XLSX sheet appears only when suspected breaks exist ───────

_XLSX_SCRIPT = _STUB_PRELUDE + r"""
out = {}

folder = make_folder(os.path.join(sys.argv[2], 'broken'), HEALTHY + BROKEN)
xb = os.path.join(sys.argv[2], 'broken.xlsx')
report_sor.build_xlsx_sor(folder, 'T', xb)

folder2 = make_folder(os.path.join(sys.argv[2], 'clean'), HEALTHY)
xc = os.path.join(sys.argv[2], 'clean.xlsx')
report_sor.build_xlsx_sor(folder2, 'T', xc)

out['broken_xlsx'] = xb
out['clean_xlsx'] = xc

# PDF section renderer (the HTML block the PDF embeds).
out['pdf_section'] = report_sor._short_trace_section_html([
    {'file': 'BCK1BCK60198', 'eof_m': 1005.0, 'median_eof_m': 2037.2,
     'excluded': True,
     'note': 'ends at 1005 m (folder median 2037 m) — suspected break',
     'break_note': ('A+B lengths are consistent with a break ~1005 m '
                    'from the BCK1BCK6 end')},
])
out['pdf_section_empty'] = report_sor._short_trace_section_html([])
print(json.dumps(out))
"""


def test_xlsx_and_pdf_sections_only_when_breaks_exist(tmp_path):
    out = _run_engine_script(_XLSX_SCRIPT, tmp_path)

    from openpyxl import load_workbook   # test process: no engine import

    wb = load_workbook(out["broken_xlsx"])
    assert "Suspected short fibers" in wb.sheetnames
    assert wb.sheetnames[1] == "Suspected short fibers"   # right after Summary
    ws = wb["Suspected short fibers"]
    rows = list(ws.values)
    assert rows[0] == ("File", "Ends at (m)", "Folder median (m)",
                       "Excluded from pairs", "Finding")
    body = {r[0]: r for r in rows[1:]}
    assert set(body) == {"AAABBB0198", "BBBAAA0198"}
    assert body["AAABBB0198"][3] == "Yes"
    assert "A+B lengths are consistent with a break ~1000 m" in body["AAABBB0198"][4]
    # Summary carries the count row only for affected folders.
    summary = [r for r in wb["Summary"].values if r and r[0]]
    assert ("Suspected short fibers", 2) in [(r[0], r[1]) for r in summary]

    wb2 = load_workbook(out["clean_xlsx"])
    assert "Suspected short fibers" not in wb2.sheetnames
    assert all(r[0] != "Suspected short fibers"
               for r in wb2["Summary"].values if r and r[0])

    # PDF HTML block: boss-facing sentences present; empty input renders ''.
    sec = out["pdf_section"]
    assert "Suspected broken / short fibers" in sec
    assert "BCK1BCK60198" in sec
    assert "suspected break — excluded from pair comparison" in sec
    assert "A+B lengths are consistent with a break ~1005 m from the BCK1BCK6 end" in sec
    assert out["pdf_section_empty"] == ""


# ── Manifest contract ───────────────────────────────────────────────────────

def test_manifest_short_traces_absent_on_clean_folder(tmp_path):
    """Unaffected folders must not grow the key at all (byte-stable
    manifests) — in pairs mode and in report mode."""
    d = single_dir_fixture(tmp_path)
    rc, m, stderr = run_secretsauce(d, tmp_path / "out", "pairs")
    assert rc == 0 and m and m.get("ok"), f"runner failed: {(stderr or '')[-800:]}"
    assert "short_traces" not in m, m.keys()

    rc, m2, stderr = run_secretsauce(d, tmp_path / "out2", "xlsx")
    assert rc == 0 and m2 and m2.get("ok"), f"runner failed: {(stderr or '')[-800:]}"
    assert "short_traces" not in m2, m2.keys()


def test_source_locks_window_and_manifest():
    """Pin the calibrated thresholds, the exclusion gate, and the additive
    manifest wiring (calibrated against A-F West / A-F East / SEANOR /
    SANDUR / ELMMIL real spans on 2026-07-14)."""
    src = (SECRETSAUCE_DIR / "report_sor.py").read_text(encoding="utf-8")

    # Calibrated thresholds.
    assert "_BREAK_FRAC_OF_MEDIAN = 0.75" in src
    assert "_BREAK_AB_SUM_TOL = 0.10" in src

    # Exclusion is gated on the 2 km collapse — the ELMMIL/SANDUR
    # byte-identity guarantee lives in this exact condition.
    assert "raw_min < _SHORT_COMMON_SPAN_M and n_healthy >= 2" in src

    # The robust span feeds the SAME min_L every downstream consumer reads
    # (interior window, regime rules, Common span row).
    assert "min_L, median_L, out_idx, excl_idx = _robust_common_span(" in src

    # Report surfaces exist.
    assert "Suspected broken / short fibers" in src        # PDF banner
    assert "'Suspected short fibers'" in src               # XLSX sheet
    assert "A+B lengths are consistent with a break" in src

    # Manifest wiring: additive, only-when-non-empty, in BOTH modes.
    runner = (SECRETSAUCE_DIR / "run_secretsauce.py").read_text(encoding="utf-8")
    assert runner.count("if short_traces_all:") == 2
    assert runner.count("payload['short_traces'] = short_traces_all") == 2

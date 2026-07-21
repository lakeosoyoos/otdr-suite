"""all_dups span floor (issue #9 root cause, Span 7 Tularosa-Orogrande).

864 UNIQUE short-shot fibers (~5 km common window) correlated broadband
over the short shared window: bulk r 0.88 landed INSIDE the all_dups
0.85-0.95 scoring ramp and 62,014 ordinary pairs walked over 50% with
ZERO at >=99% and frac_high_r 0.00 (the self-refuting signature).  The
all_dups gate now requires >= _ALLDUPS_MIN_SPAN_M of common window; short
high-r folders fall through to tie_panel (fingerprint + 0.999 ramp),
where true re-shoots still land and byte-copies stay caught by the
regime-independent raw-identity short-circuit.

Validated on the real folders (2026-07-21): OROTUL 62,014 -> 0 and
TULORO -> 0 (both tie_panel); ELMMIL (69.5 km, all_dups), SANDUR
(production), East/West (tie_panel) regimes and counts unchanged.
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))


def _src():
    with open(os.path.join(ROOT, 'secretsauce', 'report_sor.py'),
              encoding='utf-8') as f:
        return f.read()


def test_floor_value():
    s = _src()
    assert '_ALLDUPS_MIN_SPAN_M = 15000.0' in s


def test_alldups_gate_requires_span_floor():
    s = _src()
    assert ('bulk_r >= 0.7 and bulk_sigma < 0.10\n'
            '            and min_L >= _ALLDUPS_MIN_SPAN_M') in s


def test_short_high_r_falls_to_tie_panel():
    # the fall-through route must still exist unchanged
    s = _src()
    assert "elif bulk_r >= 0.7 or frac_high_r >= 0.30:" in s
    assert "regime = 'tie_panel'" in s

"""FastReporter's carry slope across a conflicting-reach stretch.

When the two fit windows cannot meet (CursorB - wb > CursorA + wa) FR reads
the after-line at its own limit and carries the before-line across the gap
(measure_fr_exact_loss).  The carry is the nominal 0.2 dB/km -- unless the
two windows AGREE on the fibre's attenuation, their raw fitted slopes within
10 % of their mean, in which case the carry is the magnitude of that mean,
sign dropped.

Pinned by clean-line probes: WSC<->SUI fibre 34's A->B trace rewritten over
the Splice 12 windows with exact lines (setline.py), paired with the real
B->A file and run through FastReporter's Create Bidirectional Files
(2026-09-22).  Seven of those keys are vendored here beside the real fibres
324 and 437 whose legs found the rule (4.7 and 15.6 mdB off under the
nominal carry).  test_fr_bidi_table covers all of them row for row; this
file states the rule on the numbers.

Engine tests run in a clean subprocess (three engines, three sor_reader
copies, never one process).
"""
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"
BDR_DIR = REPO_ROOT / "desktop" / "tests" / "fixtures" / "bdr"


def _run(body):
    header = ("import sys, os, math\n"
              "import numpy as np\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import sor_reader324802a as sr\n"
              f"BDR = {str(BDR_DIR)!r}\n")
    p = subprocess.run([sys.executable, "-c", header + textwrap.dedent(body)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


HELPER = """
        def s12_leg(name):
            a = sr.parse_bdr_side(os.path.join(BDR, name), 'a')
            fr = [m for m in a['_bdr_merged'] if 'Type' in m and not int(m.get('Status') or 0) & 0xC0][-1]['_ab']
            i1, i2, i3, i4 = fr['SubCursorA'], fr['CursorA'], fr['CursorB'], fr['SubCursorB']
            res = a['exfo_res_m']; y = 64.0 - a['exfo_raw'].astype(float) / 1024.0
            m1 = np.polyfit(np.arange(i1, i2 + 1), y[i1:i2 + 1], 1)[0] / res * 1000.0
            m2 = np.polyfit(np.arange(i3, i4 + 1), y[i3:i4 + 1], 1)[0] / res * 1000.0
            ext = (i3 - (i4 - i3)) - (i2 + (i2 - i1))
            ours = sr.measure_fr_exact_loss(a, i2 * res, i3 * res, i1 * res, i4 * res)
            return fr['Loss'], ours, m1, m2, ext, res
"""


def test_agreeing_slopes_carry_at_their_mean_magnitude_disagreeing_ones_at_the_nominal():
    _run(HELPER + """
        # (key, before slope, after slope, carry FR used)
        cases = [('WSC_SUI_0034_PROBE6.bdr',  -2.0,  -2.0,  'mean'),    # both uphill, alike
                 ('WSC_SUI_0034_PROBE17.bdr', -2.0,  -1.85, 'mean'),    # 7.6 % apart
                 ('WSC_SUI_0034_PROBE20.bdr', -2.0,  -1.81, 'mean'),    # 9.7 % of the mean
                 ('WSC_SUI_0034_PROBE16.bdr', -2.0,  -1.80, 'nominal'), # 10.4 % of the mean
                 ('WSC_SUI_0034_PROBE9.bdr',  -2.0,  -1.0,  'nominal'), # far apart
                 ('WSC_SUI_0324_1550.bdr',    None,  None,  'mean'),    # the real legs that found it
                 ('WSC_SUI_0437_1550.bdr',    None,  None,  'mean')]
        for name, want1, want2, carry in cases:
            fr, ours, m1, m2, ext, res = s12_leg(name)
            assert ext > 0, (name, ext)                                  # a conflicting reach
            if want1 is not None:
                assert abs(m1 - want1) < 0.02 and abs(m2 - want2) < 0.03, (name, m1, m2)
            assert abs(ours - fr) < 1e-6, (name, ours, fr)              # the rule reproduces FR
            # and the other carry would not have
            mean = (m1 + m2) / 2.0
            other = ours + (abs(mean) - 0.2) * ext * res / 1000.0 * (1 if carry == 'nominal' else -1)
            assert abs(other - fr) > 1e-4, (name, other, fr)
            agree = abs(m1 - m2) < sr.FR_CARRY_AGREE_FRAC * abs(mean)
            assert agree == (carry == 'mean'), (name, m1, m2, carry)
        print('OK')
    """)


def test_the_sign_is_dropped_and_the_band_does_not_matter():
    # slopes above the ceiling (+1/+1) or in band (+0.3/+0.3) carry at their
    # mean too; only the raw slopes' agreement decides
    _run(HELPER + """
        for name, want in (('WSC_SUI_0034_PROBE14.bdr', 0.29), ('WSC_SUI_0034_PROBE15.bdr', 1.0)):
            fr, ours, m1, m2, ext, res = s12_leg(name)
            assert abs((m1 + m2) / 2.0 - want) < 0.02, (name, m1, m2)
            assert abs(ours - fr) < 1e-6, (name, ours, fr)
            nominal = ours + (abs((m1 + m2) / 2.0) - 0.2) * ext * res / 1000.0
            assert abs(nominal - fr) > 1e-4, (name, nominal, fr)
        print('OK')
    """)

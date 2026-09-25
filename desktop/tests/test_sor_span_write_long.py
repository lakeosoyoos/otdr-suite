"""Span start written on a LONG span (56.7 km), checked against FastReporter's own file.

trace_server used 0.02998 m per time-of-travel unit.  The true constant is
c / 1e10 = 0.0299792458; the rounded one is 25 ppm long, 1.1 m at 44.7 km.
set_span matches every KeyEvent to FR's proprietary record within 1 m, so on
Lumen Span 7 (Monument <-> Grainfield) it refused all 2,304 files, and the
Viewer's "Span START here" could not be saved anywhere on the span.

The fixture pair is fiber 183 as shot and the same file after FR 3 set the
launch fiber length to 1.0095 km and saved it (fixtures/README.txt).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, 'viewer'))

import trace_server as TS                               # noqa: E402
from test_sor_span_write import _prop_values, _same     # noqa: E402

FX = os.path.join(HERE, 'fixtures', 'frspan_long')


def _orig():
    return open(os.path.join(FX, 'MONGRA0183_1550.sor'), 'rb').read()


def _fr():
    return open(os.path.join(FX, 'MONGRA0183_1550_fr_span_start.sor'), 'rb').read()


def test_the_constant_is_c_over_1e10():
    assert TS._TOT_M_PER_UNIT == 299_792_458.0 / 1e10


def test_a_long_span_accepts_a_span_start():
    # events at 44.76 and 50.81 km: 1.1-1.3 m off under the old constant
    TS.set_span(_orig(), start_km=1.0095)


def test_set_span_reproduces_fastreporters_long_span_file():
    out = TS.set_span(_orig(), start_km=1.0095)
    assert TS.roundtrip_ok(out)
    _, ours = TS.split(out)
    _, frs = TS.split(_fr())
    assert [b.name for b in ours] == [b.name for b in frs]
    for a, b in zip(ours, frs):
        if a.name in (b'GenParams', b'SupParams', b'FxdParams', b'DataPts', b'ExfoAdditionalInfo'):
            assert a.body == b.body, a.name
    ea, sa = TS._kev_parse(TS._find(ours, b'KeyEvents').body)
    eb, sb = TS._kev_parse(TS._find(frs, b'KeyEvents').body)
    assert ea == eb
    assert sa[:3] == sb[:3] and sa[4:] == sb[4:]
    # Two records differ, both on purpose: FR re-integrates TotalOrl (we do
    # not fake it), and FR's dialog also declares the span length, so it sets
    # IncludeSpanEnd where a start-only edit leaves it alone.
    diffs = [x[0] for x, y in zip(_prop_values(out), _prop_values(_fr()))
             if not _same(x[2], y[2])]
    assert diffs == ['TotalOrl', 'IncludeSpanEnd']

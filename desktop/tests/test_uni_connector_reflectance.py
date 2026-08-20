"""A connector column must report each fiber's OWN reflectance.

`uni_cluster_connectors` groups connector readings into columns.  On a
PRE-TRIMMED folder the launch reel is already stripped, so the `off > 0` half
of the `is_launch` test is False at BOTH ends and the entry connector and the
far connector land in a single group.

`conn_refl` was a plain dict comprehension, so for a fiber with two readings in
one group the LAST in file order won -- and that is the far end's
end-of-fiber marker, whose reflectance is pinned at the receiver ceiling
(~-15.6 dB, saturated).  The Flagged Events reason for the ENTRY connector then
printed that ceiling.

Real case, MILTOP_B F65:

    reading 1  @ 0.000 km   loss 0.669   refl -55.468   FLAGGED
    reading 2  @61.742 km   loss 0.000   refl -15.678   (end marker)
    conn_refl[65] = -15.678        <-- printed against the flagged reading

To a field crew -15.7 dB at a panel is a catastrophically dirty connector and
-55.5 dB is a healthy mated one, so this is a wrong number with a real
consequence -- on 141 rows across the spans on disk.

The root cause predates the saturated-reflective work: `1E` end markers were
always reflective and already did this on 126 rows.  Widening the alphabet to
the `2` class added 15 more.

Only `conn_refl` is corrected.  `conn_members` decides which cells shade and
`conn_all` supplies a count; neither may move on a reflectance-reporting fix.
"""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'splicereport'))

import splicereportmatchexfo as E  # noqa: E402


def _reading(fiber, km, refl, loss=0.0, flag=False, dark=False, is_launch=False):
    return {'fiber': fiber, 'position_km': km, 'refl': refl, 'loss': loss,
            'flag': flag, 'dark': dark, 'is_launch': is_launch}


def _column(cols, near_km):
    return min(cols, key=lambda c: abs(c['position_km_refined'] - near_km))


def test_the_entry_reading_supplies_the_entry_column_reflectance():
    """The MILTOP_B F65 shape: two readings, one group, far end last."""
    ev = [
        _reading(65, 0.000, -55.468, loss=0.669, flag=True),
        _reading(65, 61.742, -15.678, loss=0.000),
        # neighbours so the group has a sane median position
        _reading(66, 0.000, -54.9, loss=0.40),
        _reading(67, 0.000, -56.1, loss=0.38),
    ]
    cols = E.uni_cluster_connectors(ev)
    col = _column(cols, 0.0)
    assert col['conn_refl'][65] == pytest.approx(-55.468), (
        'the far end-marker ceiling was printed as the entry reflectance')


def test_file_order_does_not_decide():
    """Same readings, far end FIRST -- the answer must not change."""
    ev = [
        _reading(65, 61.742, -15.678, loss=0.000),
        _reading(65, 0.000, -55.468, loss=0.669, flag=True),
        _reading(66, 0.000, -54.9, loss=0.40),
    ]
    col = _column(E.uni_cluster_connectors(ev), 0.0)
    assert col['conn_refl'][65] == pytest.approx(-55.468)


def test_the_far_column_keeps_the_far_reading():
    """The fix is nearest-to-this-column, not always-the-first.  When the two
    ends DO separate into their own columns, each keeps its own reading."""
    ev = [
        _reading(65, 0.000, -55.468, loss=0.669, flag=True, is_launch=True),
        _reading(66, 0.000, -54.900, loss=0.400, is_launch=True),
        _reading(65, 61.742, -15.678, loss=0.000),
        _reading(66, 61.740, -16.100, loss=0.000),
    ]
    cols = E.uni_cluster_connectors(ev)
    assert _column(cols, 0.0)['conn_refl'][65] == pytest.approx(-55.468)
    assert _column(cols, 61.74)['conn_refl'][65] == pytest.approx(-15.678)


def test_shading_and_counts_are_untouched():
    """`conn_members` (shading) and `conn_all` (count) must not move."""
    ev = [
        _reading(65, 0.000, -55.468, loss=0.669, flag=True),
        _reading(65, 61.742, -15.678, loss=0.000),
        _reading(66, 0.000, -54.9, loss=0.40),
    ]
    col = _column(E.uni_cluster_connectors(ev), 0.0)
    assert col['conn_members'] == {65: 0.669}, 'only flagged readings shade'
    assert set(col['conn_all']) == {65, 66}
    assert col['conn_all'][65] == pytest.approx(0.000), (
        'conn_all still takes the last reading -- it is a count, not a verdict')


def test_a_single_reading_is_unaffected():
    ev = [_reading(1, 0.0, -52.3, loss=0.7, flag=True),
          _reading(2, 0.0, -51.9, loss=0.2)]
    col = _column(E.uni_cluster_connectors(ev), 0.0)
    assert col['conn_refl'] == {1: pytest.approx(-52.3), 2: pytest.approx(-51.9)}

"""A negative bidirectional loss must never render as a reburn.

Field report, KANLAN F176 (2026-08-18).  The tech read our cell as
`176 -0.178` in a pink "needs re-splice" fill and said it was wrong.  He was
right, and FastReporter agrees with our MEASUREMENTS: FR reads A -0.166 /
B 0.22 there and classes the event `Positive` — its gainer type — while our
per-direction numbers are -0.168 / +0.207, i.e. 2 and 13 mdB from FR.  Only
the category was wrong.

Mechanism: `_clears_threshold` gates on |loss| (it rounds to the PRINTED
value), so -0.178 flags exactly like +0.178.  `reburn` is the fallback
category, so any flagged cell the other rules decline lands there.  The
canonical gainer rule deliberately declines cells whose leg is
grey/reconstructed or whose two directions share a sign — and those dropped
straight through to reburn.

A passive splice cannot produce net gain, so such a cell is a gainer or a
measurement we do not trust; it is never something to re-splice.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, 'splicereport'))

import pytest  # noqa: E402
import splicereportmatchexfo as E  # noqa: E402


def _cell(bidir, a, b, **kw):
    d = {'fiber': 1, 'splice_idx': 0, 'bidir_loss': bidir, 'a_loss': a,
         'b_loss': b, 'bidir_dist': 40.0, 'is_flagged': True}
    d.update(kw)
    return d


SPAN = 100.0


def _run(cells):
    res = {(i, 0): c for i, c in enumerate(cells)}
    E.apply_field_gainer_rule(res, SPAN)
    return [res[(i, 0)] for i in range(len(cells))]


def test_canonical_gainer_still_claimed():
    """Opposite-sign A/B with a negative bidir is the real thing, and it must
    stay a corroborated gainer — not get swept up by the safety net."""
    c, = _run([_cell(-0.20, a=-0.55, b=0.15)])
    assert c['is_gainer'] is True
    assert not c.get('_gainer_uncorroborated'), 'should be a canonical gainer'


def test_negative_with_a_reconstructed_leg_is_not_a_reburn():
    """The KANLAN F176 shape: the canonical rule declines it (a leg is grey),
    and it used to fall through to `reburn` — pink, 'needs re-splice'."""
    c, = _run([_cell(-0.178, a=-0.168, b=-0.188, _b_is_grey=True)])
    assert c['is_gainer'] is True, 'a negative loss must not remain a reburn'
    assert c['_gainer_uncorroborated'] is True


def test_negative_with_same_sign_legs_is_not_a_reburn():
    """Both directions reading gain is physically impossible for a passive
    splice — a bad measurement, still not something to re-splice."""
    c, = _run([_cell(-0.25, a=-0.20, b=-0.30)])
    assert c['is_gainer'] is True
    assert c['_gainer_uncorroborated'] is True


def test_positive_loss_is_untouched():
    """The ordinary reburn path must not move."""
    c, = _run([_cell(0.240, a=0.22, b=0.26)])
    assert not c.get('is_gainer')
    assert not c.get('_gainer_uncorroborated')


@pytest.mark.parametrize('kind', ['is_break', 'is_broke', 'is_ref',
                                  'is_dead_zone', 'is_bend'])
def test_other_categories_keep_their_own_identity(kind):
    """Breaks, refs, dead zones and bends own their cells; the safety net is
    only for cells that would otherwise be called a reburn."""
    c, = _run([_cell(-0.30, a=-0.30, b=-0.30, **{kind: True})])
    assert not c.get('_gainer_uncorroborated'), kind


def test_safety_net_only_touches_flagged_cells():
    """The cable is full of quiet sub-threshold negatives (KANLAN: 264 of
    9,109 cells) and the safety net must not surface any of them — it only
    re-labels cells that ALREADY flagged and would otherwise read `reburn`.

    Note the CANONICAL rule is different and deliberately does claim
    sub-threshold opposite-sign gainers (that is its whole purpose), so this
    uses same-sign legs, which the canonical rule declines."""
    c, = _run([_cell(-0.02, a=-0.05, b=-0.01, is_flagged=False)])
    assert not c.get('is_gainer')
    assert not c.get('_gainer_uncorroborated')


def test_canonical_rule_still_surfaces_sub_threshold_gainers():
    """Guard the shipped behaviour the test above must not be read as
    contradicting: a weak opposite-sign gainer IS surfaced, by design."""
    c, = _run([_cell(-0.02, a=-0.05, b=0.01, is_flagged=False)])
    assert c['is_gainer'] is True
    assert not c.get('_gainer_uncorroborated')


def test_uncorroborated_gainer_does_not_earn_its_own_column():
    """A reading we declined to trust must not reshape the column layout.
    split_offsplice_events_into_own_columns keys off is_gainer, and it runs
    AFTER the gainer rule, so the safety-net flag has to be excluded there."""
    src = open(os.path.join(ROOT, 'splicereport',
                            'splicereportmatchexfo.py'), encoding='utf-8').read()
    body = src.split('def split_offsplice_events_into_own_columns', 1)[1]
    body = body.split('\ndef ', 1)[0]
    assert "_gainer_uncorroborated" in body, \
        'off-splice column rule must exclude uncorroborated gainers'


# ── The FastReporter-Average decision (deliberate divergence) ──────────
def test_we_average_the_two_directions_and_do_not_copy_frs_average():
    """DECISION, pinned so nobody 'corrects' it toward FR later.

    On KANLAN/WSC↔SUI, FastReporter's own bidirectional Average is computed in
    a broken frame: it merges in the A file's RAW frame with the A launch
    (1.0095 km) uncompensated, so every splice appears TWICE about a kilometre
    apart and each copy averages a real stored value against a silent-side
    sample ~1 km off the splice.  Reproduced on F481/F504 (2026-08-15): FR
    displayed 0.112 / 0.114 where FR's OWN two directions correctly average to
    0.152 / 0.160, and our engine gave 0.154 / 0.156.

    F176 is the same shape: FR reads A -0.166 / B 0.22 — which average to
    +0.027 — yet FR displays -0.095.  Matching FR exactly would mean copying
    the artifact, so we compute the straight mean of the two directions.
    """
    a, b = -0.166, 0.22
    assert abs(((a + b) / 2.0) - 0.027) < 0.001
    # …and that is what the engine's own combination produces.
    src = open(os.path.join(ROOT, 'splicereport',
                            'splicereportmatchexfo.py'), encoding='utf-8').read()
    assert '(float(a_loss) + float(b_loss)) / 2.0' in src or \
           '(a_loss + b_loss) / 2' in src, \
           'the bidirectional value must stay the mean of the two directions'

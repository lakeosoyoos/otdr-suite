"""ONE_SIDED_TRUST_STORED — grading a fiber whose other direction was never
delivered.

Span 29's last tube (fibers 401-432) has no B-direction files.  Such a fiber
already shows FILE_MISSING in its end column, and its A-direction events go
through the A-only path -- but that path grades a stored loss only when the
trace re-measure confirms it.  On iOLM exports the .sor trace does not carry
the samples the instrument fitted, so the gate is blind and silently drops
the reading: fiber 408's 0.411 dB at the first closure, which the customer's
review fails, printed nothing.

With the switch on, a fiber with NO record in the other direction is graded on
its stored value against SINGLE_DIR_THRESHOLD.  A fiber that HAS a B record
is untouched: the gate still applies to it exactly as before.
"""
from __future__ import annotations

import importlib
import sys

from conftest import SPLICEREPORT_DIR

sys.path.insert(0, str(SPLICEREPORT_DIR))
import splicereportmatchexfo as E  # noqa: E402

IIG = "AWS / IIG MT.1085"


def _fiber(events, eof_km=69.0):
    evs = [dict(dist_km=km, splice_loss=loss, reflection=0.0, type='0F9999LS',
                is_end=False, is_reflective=False) for km, loss in events]
    evs.append(dict(dist_km=eof_km, splice_loss=0.0, reflection=-30.0,
                    type='1E9999LS', is_end=True, is_reflective=True))
    return {'events': evs, '_raw_events': evs, 'trace': None, 'exfo_raw': None,
            'exfo_res_m': None, 'num_points': 0}


def _run(fibers_a, fibers_b, flag, monkeypatch):
    """Run analyze_all with the confirm gate BLIND (returns False), which is
    what an iOLM export's trace does to it; the switch is the only thing
    that can then let a one-sided stored loss through."""
    monkeypatch.setattr(E, '_local_step_confirms', lambda r, e: False)
    before = E.ONE_SIDED_TRUST_STORED
    E.ONE_SIDED_TRUST_STORED = flag
    try:
        splices = [dict(position_km=10.0, column_kind='splice', splice_display_num=1)]
        res = E.analyze_all(fibers_a, fibers_b, splices, 0.20)
    finally:
        E.ONE_SIDED_TRUST_STORED = before
    return {k: v for k, v in res.items() if v.get('is_flagged')}


def test_ships_off():
    assert E.ONE_SIDED_TRUST_STORED == 0


def test_a_fiber_with_no_b_record_is_graded_on_its_stored_loss(monkeypatch):
    fa = {408: _fiber([(10.0, 0.411)])}
    fb = {}
    assert _run(fa, fb, 0, monkeypatch) == {}, "off: the blind confirm gate drops it, as before"
    flagged = _run(fa, fb, 1, monkeypatch)
    assert (408, 0) in flagged, flagged
    assert '(A)' in flagged[(408, 0)]['label'], flagged[(408, 0)]['label']


def test_the_single_direction_limit_still_applies(monkeypatch):
    """0.227 is over the 0.20 splice limit but under the 0.25 single-direction
    limit -- the switch trusts the reading, it does not lower the bar."""
    fa = {409: _fiber([(10.0, 0.227)])}
    assert _run(fa, {}, 1, monkeypatch) == {}


def test_a_fiber_that_has_a_b_record_is_untouched(monkeypatch):
    """The switch is about a MISSING direction.  With a B record present the
    confirm gate applies as before, so an unconfirmable A-only reading stays
    unflagged whether the switch is on or off."""
    fa = {5: _fiber([(10.0, 0.411)])}
    fb = {5: _fiber([], eof_km=69.0)}
    assert _run(fa, fb, 0, monkeypatch) == _run(fa, fb, 1, monkeypatch) == {}


def test_profile_carries_the_switch_and_the_whitelist_allows_it():
    app = importlib.import_module('app')  # engine imported first, on purpose
    assert "ONE_SIDED_TRUST_STORED" in app._PROFILE_ENGINE_KEYS
    assert app.CUSTOMER_PROFILES[IIG]["engine"]["ONE_SIDED_TRUST_STORED"] == 1
    assert app._engine_extras_from_profile(IIG)["ONE_SIDED_TRUST_STORED"] == 1.0
    for name, prof in app.CUSTOMER_PROFILES.items():
        if name == IIG:
            continue
        assert "ONE_SIDED_TRUST_STORED" not in (prof.get("engine") or {}), name

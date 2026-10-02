"""B's view of A's launch connector is never B's own port.

_b_launch_conn_mirror looks one A-launch-reel back from B's end of fiber.
When B's shot stops AT A's panel (no receive reel) and the two launch reels
are about the same length, that lands on B's own 0 km port row, at the
other end of the cable.  Robert 2026-10-01: a 2 s B shot stopped at the A
panel (1.0615 km) with A's reel at 1.0383 km paired A's -0.495 with B's
port (0.000) and printed an A-end average of -0.247.
"""
import sys

from conftest import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "splicereport"))
import splicereportmatchexfo as E  # noqa: E402


def _ev(km, loss=0.0, end=False):
    return {'dist_km': km, 'splice_loss': loss, 'reflection': -55.0,
            'type': ('2E9999LS' if end else '1F9999LS'),
            'is_reflective': True, 'is_end': end}


def test_shot_stopped_at_a_panel_does_not_take_its_own_port():
    b = {'events': [_ev(0.0), _ev(1.0001, -0.081), _ev(1.0615, end=True)]}
    assert E._b_launch_conn_mirror(b, 1.0383) is None


def test_a_panel_seen_through_a_receive_reel_is_still_found():
    # B runs on through A's panel into A's reel: the panel is one A-reel
    # back from B's EOF, and that is the row taken, as before.
    b = {'events': [_ev(0.0), _ev(1.0001, -0.081), _ev(1.0615, 0.31),
                    _ev(2.1003, end=True)]}
    got = E._b_launch_conn_mirror(b, 1.0383)
    assert got is not None and got['dist_km'] == 1.0615


def test_a_port_row_away_from_zero_is_not_skipped():
    # Only the port row itself is passed over: a first event that is not at
    # the port (a trimmed or offset trace) is judged on distance as before.
    b = {'events': [_ev(0.05), _ev(1.1, end=True)]}
    got = E._b_launch_conn_mirror(b, 1.05)
    assert got is not None and got['dist_km'] == 0.05

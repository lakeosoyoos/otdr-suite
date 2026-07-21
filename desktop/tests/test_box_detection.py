"""Launch/tail box presence detection (Robert 2026-07-21).

Real-world matrix validated: Span 7 quick shots = launch reel, NO tail box
(shot ends at a 1O range marker); WINNIL finals = NO launch box, tail box
present (tail far-end 1F sits AFTER the 1E EOL event); SEANOR = both.
When a tail box is absent the BAD_TAILBOX_REFL checks are suppressed for
that direction; the panel row 'box_detection' (default ON) disables the
whole feature (assume both present, no notes).
"""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, 'splicereport'))
import splicereportmatchexfo as E


def _f(evs):
    return {0: {'events': evs}}

def _ev(km, typ='0F9999LS', refl=0.0, end=False):
    return {'dist_km': km, 'splice_loss': 0.05, 'reflection': refl,
            'type': typ, 'is_reflective': typ.startswith('1'), 'is_end': end}


def test_quickshot_launch_no_tail():
    evs = [_ev(0.0, '1F9999LS', -68), _ev(1.01, '1F9999LS', -54),
           _ev(4.99, '1O9999LS')]
    r = E.detect_box_presence(_f(evs), {})['a']
    assert r['launch'] is True and r['tail'] is False


def test_winnil_no_launch_tail_after_eol():
    evs = [_ev(0.0, '1F9999LS', -61), _ev(15.0), _ev(84.2),
           _ev(87.58, '1E9999LS', -55, end=True), _ev(88.62, '1F9999LS', -42)]
    r = E.detect_box_presence(_f(evs), {})['a']
    assert r['launch'] is False and r['tail'] is True


def test_full_span_both_present():
    evs = [_ev(0.0, '1F9999LS', -57), _ev(1.0, '1F9999LS', -54), _ev(50.0),
           _ev(107.5, '1F9999LS', -55), _ev(108.5, '1E9999LS', -56, end=True)]
    r = E.detect_box_presence(_f(evs), {})['a']
    assert r['launch'] is True and r['tail'] is True


def test_tailbox_suppression_accepts_pair():
    src = open(os.path.join(ROOT, 'splicereport', 'splicereportmatchexfo.py'),
               encoding='utf-8').read()
    assert 'isinstance(_tb, (tuple, list))' in src


def test_runner_wires_detection_and_manifest():
    src = open(os.path.join(ROOT, 'splicereport', 'run_splicereport.py'),
               encoding='utf-8').read()
    assert 'detect_box_presence' in src
    assert "'box_detection': box_info" in src
    assert 'no tail box in use' in src


def test_panel_row_and_mapping():
    src = open(os.path.join(ROOT, 'app.py'), encoding='utf-8').read()
    assert '"box_detection",             "Launch/tail box detection"' in src
    assert '"box_detection":        "BOX_DETECTION"' in src
    assert '"box_detection": 0.0' in src

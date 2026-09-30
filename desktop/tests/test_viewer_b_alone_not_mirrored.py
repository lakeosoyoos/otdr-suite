"""Viewer: B loaded on its own is drawn the way it was shot, not mirrored.

Field report (a tech's call, 2026-09-29, 24 fibres B->A only): "B traces are still
loading backward".  Stacked mode (the default) mirrored every B trace into
A's frame about far_conn_km + launch_a_km, even with no A trace loaded to
meet.  The whole trace came out reversed, the event table ran from the far
end (event 1 = "Continuous Fiber" at 0 km, the launch level last) and the
axis started kilometres in.

FastReporter opens one direction in its own frame, port at 0 km.  The mirror
now applies only while at least one A trace is loaded; refreshMirrorFrame,
which renderChips runs on every add, remove, clear and direction change,
keeps that flag current.

Checked by replaying the real viewer.html script in JavaScriptCore:
    main: B alone flipped=true,  event 3.93 km drawn at 1.060 km
    fix:  B alone flipped=false, event 3.93 km drawn at 3.930 km
          A+B     flipped=true,  1.060 km (unchanged)
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..'))
SRC = open(os.path.join(ROOT, 'viewer', 'viewer.html'), encoding='utf-8').read()


def _body(name):
    m = re.search(r'function %s\([^)]*\)\s*\{' % name, SRC)
    assert m, 'viewer.html no longer defines %s' % name
    i, depth = m.end(), 1
    while depth:
        depth += {'{': 1, '}': -1}.get(SRC[i], 0)
        i += 1
    return SRC[m.end():i - 1]


def test_flip_needs_an_a_trace():
    assert re.search(r'gStacked\s*&&\s*gHaveA\s*&&\s*t\.dir\s*===\s*\'b\'',
                     _body('isFlipped'))


def test_y_offset_needs_an_a_trace():
    assert 'gHaveA' in _body('yOffsetFor')


def test_have_a_refreshed_on_every_trace_change():
    body = _body('refreshMirrorFrame')
    assert re.search(r"gHaveA\s*=\s*gTraces\.some\(t\s*=>\s*t\.dir\s*===\s*'a'\)", body)
    # set before the early returns, so un-stacking or declaring a span
    # cannot leave it stale
    assert body.index('gHaveA =') < body.index('return')
    assert 'refreshMirrorFrame()' in _body('renderChips')

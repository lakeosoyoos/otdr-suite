"""A span declared on ONE side only, far past any reel, gets a warning.

Robert 2026-10-01, "add the warning".  A span START on A is read as "A's
launch reel ends here" (FastReporter's Launch fiber length) and B is mirrored
to meet it.  Picked on a splice in the middle of the cable, every B trace
slides along by that distance -- FastReporter does the same until B's own span
end (its Receive length) is set too.  The table still pairs the two
directions by where they really are, so chart and table disagree.

The Viewer now says so in the status line, and says where B's span end goes:
the point on B that is A's start, in the reel frame the mirror already uses,

    B's km = B's far connector + A's launch reel - A's km     (reelOriginKm)

On the audit fibre (64.04 km, no reels) a start on the 5.596 km splice asks
for B's end at about 58.44 km.  A start within 3 km of A's reel is a launch
reel and says nothing; with both halves set there is nothing to say.

The repo's tests are Python and the Windows runner has no JS engine, so the
rule is read out of viewer.html and mirrored here (the pattern of
test_viewer_boot_race.py); the teeth check removes it and watches it fail.
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SRC = open(os.path.join(ROOT, 'viewer', 'viewer.html'), encoding='utf-8').read()


def _body(src, head):
    i = src.index(head)
    depth, j = 0, src.index('{', i)
    for k in range(j, len(src)):
        if src[k] == '{':
            depth += 1
        elif src[k] == '}':
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    raise AssertionError(head)


def _threshold(src):
    m = re.search(r'const SPAN_HALF_WARN_KM = ([\d.]+);', src)
    assert m, 'SPAN_HALF_WARN_KM not found'
    return float(m.group(1))


def _mirror(src, a_start=None, b_end=None, far_b=64.0398, launch_a=0.0):
    """halfSpanWarning() for one A/B fibre, as read out of viewer.html:
    ('B', km) asks for B's end, ('A', km) for A's start, None says nothing."""
    body = _body(src, 'function halfSpanWarning() {')
    lim = _threshold(src)
    origin = far_b + launch_a                                   # reelOriginKm
    if (a_start is None) == (b_end is None):
        assert "if ((startRef == null) === (endRef == null)) return '';" in body
        return None
    if a_start is not None:
        assert 'if (start - gLaunchA <= SPAN_HALF_WARN_KM) return' in body
        assert '${fmtDist(origin - start)}' in body
        return None if a_start - launch_a <= lim else ('B', origin - a_start)
    assert 'if (origin - gLaunchA - end <= SPAN_HALF_WARN_KM) return' in body
    assert '${fmtDist(origin - end)}' in body
    return None if origin - launch_a - b_end <= lim else ('A', origin - b_end)


def test_a_start_on_a_mid_span_splice_asks_for_b_end():
    side, km = _mirror(SRC, a_start=5.5956)
    assert side == 'B' and abs(km - 58.4442) < 1e-3


def test_a_start_on_a_launch_reel_says_nothing():
    assert _mirror(SRC, a_start=1.0049) is None
    # a real 1 km reel, start nominated on its far connector
    assert _mirror(SRC, a_start=1.0049, launch_a=1.0049) is None


def test_the_reel_frame_is_used_for_the_answer():
    """With a 1 km reel on A the splice 5.596 km into the cable sits at
    6.6005 km on A, and still at 58.444 km on B."""
    side, km = _mirror(SRC, a_start=6.6005, launch_a=1.0049)
    assert side == 'B' and abs(km - 58.4442) < 1e-3


def test_b_end_alone_asks_for_a_start():
    side, km = _mirror(SRC, b_end=58.4442)
    assert side == 'A' and abs(km - 5.5956) < 1e-3
    assert _mirror(SRC, b_end=63.9) is None                     # B's own end


def test_both_halves_or_none_say_nothing():
    assert _mirror(SRC) is None
    assert _mirror(SRC, a_start=5.5956, b_end=58.4442) is None


def test_it_rides_the_mirror_note_on_the_status_line():
    body = _body(SRC, 'function refreshMirrorFrame() {')
    # it REPLACES the "B mirrored on the span" note: the status line is one
    # line cut at the window edge, and the warning is what must be read
    assert "gMirrorNote = '   ' + (halfSpanWarning()" in body
    assert "|| 'ⓘ B mirrored on the span you set" in body
    # the note is a warning on the status line (setReadout's ro-warn part)
    assert "[gMirrorNote.trim(), 'ro-warn']" in SRC
    w = _body(SRC, 'function halfSpanWarning() {')
    assert '—' not in w                                         # house style


def test_teeth_without_the_warning_the_mirror_fails():
    gone = SRC.replace('function halfSpanWarning() {', 'function gone() {')
    try:
        _mirror(gone, a_start=5.5956)
    except (ValueError, AssertionError):
        return
    raise AssertionError('the mirror did not notice the warning was gone')

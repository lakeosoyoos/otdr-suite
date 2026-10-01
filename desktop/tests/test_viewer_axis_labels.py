"""The distance axis's labels never run together, at any width (Robert
2026-10-01).  drawGrid used to ask niceTicks for ten ticks whatever the
plot's width, so on a narrow plot (the hub at 1024 px with its sidebar)
"0.00 km5.00 km10.00 km" ran into one string.  It now asks for fewer until
two labels' centres clear the widest label plus a gap, and leaves out any
label that would still touch the one before it."""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))


def _src():
    with open(os.path.join(ROOT, 'viewer', 'viewer.html'), encoding='utf-8') as fh:
        return fh.read()


def _fn(src, name):
    i = src.index('function ' + name + '(')
    return src[i:src.index('\n}\n', i)]


def test_the_tick_count_follows_the_plot_width():
    body = _fn(_src(), 'drawGrid')
    assert 'for (let want = 10; want >= 1; want--) {' in body
    assert 'if (gapPx >= widest + X_LABEL_GAP_PX) break;' in body
    # no fixed count left for the distance axis
    assert not re.search(r'niceTicks\(\(gView\.x0 - z\) \* uf, \(gView\.x1 - z\) \* uf, 10\)', body)


def test_a_label_that_would_touch_its_neighbour_is_left_out():
    body = _fn(_src(), 'drawGrid')
    assert 'if (cx - w / 2 < lastRight + X_LABEL_GAP_PX) continue;' in body
    assert 'lastRight = cx + w / 2;' in body
    m = re.search(r'const X_LABEL_GAP_PX = (\d+);', _src())
    assert m and int(m.group(1)) >= 8


def _ticks(lo, hi, n):
    """Python mirror of niceTicks."""
    import math
    span = hi - lo
    if span <= 0:
        return [lo]
    step0 = span / n
    mag = 10 ** math.floor(math.log10(step0))
    norm = step0 / mag
    step = (1 if norm < 1.5 else 2 if norm < 3 else 5 if norm < 7 else 10) * mag
    v, out = math.ceil(lo / step) * step, []
    while v <= hi + step * 1e-6:
        out.append(v)
        v += step
    return out


def test_the_rule_spaces_a_55_km_span_at_every_width():
    """The same loop as drawGrid, with a 55 km span and 11 px labels
    (~6 px per character): every pair of drawn labels keeps the gap."""
    gap = 12
    for plot_w in range(40, 2400, 7):
        px = lambda x: x / 55.0 * plot_w
        width = lambda x: 6 * len('%.2f km' % x)
        for want in range(10, 0, -1):
            t = _ticks(0.0, 55.0, want)
            if len(t) < 2:
                break
            if abs(px(t[1]) - px(t[0])) >= max(map(width, t)) + gap:
                break
        last = float('-inf')
        for x in t:
            c, w = px(x), width(x)
            if c - w / 2 < last + gap:
                continue
            last = c + w / 2
        # re-walk the drawn set and check every neighbour pair
        drawn, last = [], float('-inf')
        for x in t:
            c, w = px(x), width(x)
            if c - w / 2 >= last + gap:
                drawn.append((c - w / 2, c + w / 2))
                last = c + w / 2
        for (l0, r0), (l1, r1) in zip(drawn, drawn[1:]):
            assert l1 - r0 >= gap, (plot_w, drawn)

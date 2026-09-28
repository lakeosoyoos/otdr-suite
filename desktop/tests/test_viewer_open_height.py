"""The chart keeps room when the Viewer opens with a remembered table height.

The event-table height is remembered per browser.  One dragged tall in the
900 px pop-out and reopened in the hub's 760 px frame left the chart 1 px
high: traces loaded, the table filled, nothing drawn.  On open the chart now
keeps at least 150 px; dragging can still fold it away on purpose."""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _resizer():
    html = open(os.path.join(ROOT, 'viewer', 'viewer.html'), encoding='utf-8').read()
    i = html.index("const KEY   = 'viewer.eventPanelHeight'")
    return html[i:i + 3000]


def test_remembered_height_is_capped_on_open():
    js = _resizer()
    assert re.search(r'LOAD_MIN_CHART\s*=\s*150', js)
    assert "panel.style.height = openHeight(saved) + 'px'" in js
    assert "panel.style.height = saved + 'px'" not in js


def test_dragging_can_still_fold_the_chart_away():
    assert re.search(r'MIN_CHART\s*=\s*0\b', _resizer())

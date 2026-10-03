"""Show/Hide in Report keeps its switches when the tech leaves the page.

The boss, 2026-09-26: generated a report, changed Show/Hide, made a new
report, and it did not use the new settings.  Streamlit drops a widget's state
on any run that does not draw it, so a trip to the Viewer (or any other tool)
and back put every switch back ON, inside a collapsed box, and the next
report printed everything.  Reproduced in the browser before the fix.
"""
from __future__ import annotations

import pytest

from conftest import run_streamlit, go_tab


def _toggle(at, label):
    return next(t for t in at.toggle if t.label == label)


@pytest.mark.parametrize('page, label', [
    ('Splice Report', 'Splice Loss'),
    ('Splice Report', 'Connectors'),
    ('Unidirectional', 'Bend/Damage'),
])
def test_a_hidden_category_stays_hidden_after_leaving_the_page(page, label):
    at = run_streamlit()
    at.run()
    go_tab(at, page)
    _toggle(at, label).set_value(False).run()
    assert _toggle(at, label).value is False

    go_tab(at, 'Viewer')        # the switch is not drawn
    go_tab(at, page)
    assert _toggle(at, label).value is False, 'switch reset while away'
    # ...and it can still be switched back on
    _toggle(at, label).set_value(True).run()
    assert _toggle(at, label).value is True

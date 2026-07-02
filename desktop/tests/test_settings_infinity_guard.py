"""Regression: an Infinity keystroke in a settings field must not silently revert
a customer REBURN_THRESHOLD override (Fable 2nd-wave MED #2).

Chain of the bug: the panel JS guarded only Number.isNaN, so parseFloat("1e309")
= Infinity passed; JSON.stringify sent it across the Streamlit bridge as null;
app.py's `float(vals.get('fail', 0.0))` got None (key present → the 0.0 default
never applied) and raised TypeError; the outer except popped the whole
otdr_settings dict → overrides = {} → a deliberate REBURN_THRESHOLD=0.120 run
silently executed at the 0.160 baseline, and the panel stayed dead on reruns.

Both halves are UI/bridge code (not unit-testable without a browser + Streamlit
runtime), so lock the fix at the source on both sides.
"""
from conftest import REPO_ROOT


def test_app_settings_commit_is_finite_safe():
    app = (REPO_ROOT / "app.py").read_text(encoding="utf-8")
    assert "_finite_or(vals.get('fail')" in app, \
        "settings commit not guarded — a non-finite fail value can still crash it"
    assert "_finite_or(vals.get('warning')" in app, "warning field not guarded"
    # The old crash-y coercion must be gone.
    assert "float(vals.get('fail', 0.0))" not in app, \
        "old float(vals.get('fail', 0.0)) (raises on None) still present"


def test_settings_panel_js_rejects_infinity():
    html = (REPO_ROOT / "components" / "otdr_settings" / "index.html").read_text(encoding="utf-8")
    assert "Number.isFinite(v)" in html, "JS still admits ±Infinity (NaN-only guard)"
    assert "!Number.isNaN(v)" not in html, \
        "JS NaN-only guard still present — Infinity leaks through to the bridge"

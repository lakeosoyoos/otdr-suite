"""Custom Streamlit component for the EXFO-style OTDR threshold panel.

Renders an HTML / CSS / JS table that visually matches the EXFO threshold
panel exactly (no Streamlit-widget chrome).  Communicates the user's
Apply / Fail / Warning edits back to Python via Streamlit's standard
component-message API on a button click.
"""
from __future__ import annotations
import os
import streamlit.components.v1 as components


_RELEASE = True   # ship the static index.html, no dev server needed
_DIR = os.path.dirname(os.path.abspath(__file__))


_otdr_component = components.declare_component(
    "otdr_settings",
    path=_DIR,
)


def otdr_settings(rows: list, *, default: dict | None = None, key: str | None = None) -> dict | None:
    """Render the EXFO-styled OTDR settings table.

    Parameters
    ----------
    rows : list of dict
        Each row dict must contain:
            key:       internal id (str)
            label:     display text (str)
            unit:      'dB' / 'dB/km' / 'km'
            supported: True if the engine wires this through; False if visual-only
            initial:   {'apply': bool, 'fail': float, 'warning': float}
    default : dict, optional
        Value returned on the first render before the user clicks Apply.
        Keys are row.key, values are {'apply', 'fail', 'warning'}.
    key : str, optional
        Streamlit widget key.

    Returns
    -------
    dict or None
        Mapping {row_key: {'apply', 'fail', 'warning'}} once the user
        clicks Apply settings.  Returns `default` (or None) on every
        rerun until the next Apply click.
    """
    return _otdr_component(rows=rows, default=default, key=key)

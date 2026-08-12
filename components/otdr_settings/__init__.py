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


def otdr_settings(rows: list, *, default: dict | None = None, key: str | None = None,
                  mode: str = 'threshold') -> dict | None:
    """Render the EXFO-styled OTDR settings table.

    Two layouts share the same chrome, so every report page's settings look
    and behave identically:

    ``mode='threshold'`` (default) — the EXFO panel: Description | Apply |
    Fail | Warning.  Used by Splice Report and Splice Report FR.

    ``mode='knobs'`` — Setting | Low | High, for pages whose settings are
    not Apply/Fail/Warning triples.  Used by Unidirectional.  Rows declare
    a ``kind``:

        'range'  — a genuine band; both ends are real (the mid-span
                   reflectance band, the break-detection window)
        'scalar' — a single knob; its input spans both value columns
                   rather than labelling one value "Low"

    Parameters
    ----------
    rows : list of dict
        Common keys:
            key:       internal id (str)
            label:     display text (str)
            unit:      'dB' / 'dB/km' / 'km' / 'fibers' / ''
            supported: True if the engine wires this through
        mode='threshold' rows add:
            initial:   {'apply': bool, 'fail': float, 'warning': float}
        mode='knobs' rows add:
            kind:      'range' | 'scalar'
            initial:   {'low': float, 'high': float} | {'value': float}
            defaults:  same shape as initial — powers the edited-from-
                       default marker and the Reset to defaults button
            min/max/step, help: optional input hints
    default : dict, optional
        Value returned on the first render before the user commits.
    key : str, optional
        Streamlit widget key.
    mode : str
        'threshold' or 'knobs'.

    Returns
    -------
    dict or None
        Mapping {row_key: <that row's slots>} once the panel commits (it
        auto-commits on every edit; the Apply button is a redundant
        affordance).  Returns `default` (or None) until then.
    """
    return _otdr_component(rows=rows, default=default, key=key, mode=mode)

"""The reflectance FAIL gate is judged the way FastReporter judges it.

FR 3.21, Lumen template, Reflectance Fail -50.0 dB, read off FR's own event
table (red cell = fail) on 2026-09-23.  Real shots from Red Rock 4-5 East and
SNARCAAH, plus copies of RDR5RDR4 0119 with the stored float64 edited:

    raw       FR prints   FR
    -49.667   -49.7       FAIL    (RDR5RDR4 0069 -- on the tech's V1 sheet)
    -49.900   -49.9       FAIL    (probe)
    -49.930   -49.9       FAIL    (probe)
    -49.949   -49.9       FAIL    (probe)
    -49.951   -50.0       pass    (probe)
    -49.959   -50.0       pass    (RDR5RDR4 0119 -- NOT on the tech's sheet)
    -49.969   -50.0       pass    (SNARCAAH West 52)
    -49.989   -50.0       pass    (SNARCAAH East 206)
    -50.050   -50.1       pass    (probe)

The engine had two wrong versions of this: its default -49.9 with a raw >=
passed -49.93, and every FR-template profile sent -50.0 into that raw >=, so
the report failed 119, 206 and 52, which the techs and FR pass.
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, 'splicereport'))


def _engine():
    spec = importlib.util.spec_from_file_location(
        'refl_gate_engine', os.path.join(ROOT, 'splicereport', 'splicereportmatchexfo.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


E = _engine()

FR_TABLE = [(-49.667, True), (-49.900, True), (-49.930, True), (-49.949, True),
            (-49.951, False), (-49.959, False), (-49.969, False), (-49.989, False),
            (-50.050, False)]


@pytest.mark.parametrize('raw,fails', FR_TABLE)
def test_matches_fastreporter_at_minus_50(raw, fails):
    assert E.refl_fails(raw, -50.0) is fails


def test_the_default_is_frs_own_number():
    """The engine default is the value an FR template carries, so the Default
    profile and every template profile are judged by the same rule."""
    assert E.LAUNCH_BAD_REFL_DB == -50.0
    assert E.MIDSPAN_REFL_FAIL_DB == -50.0


def test_other_gates_round_the_same_way():
    # AT&T's template fails at -40.0; Intermountain / IIG at -55.0.
    assert E.refl_fails(-39.94, -40.0) and not E.refl_fails(-39.96, -40.0)
    assert E.refl_fails(-54.94, -55.0) and not E.refl_fails(-54.96, -55.0)

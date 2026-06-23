"""Shared fixtures for the helixcal test suite.

Uses the SOR files already committed under desktop/tests/fixtures/ (span_A =
ELMMIL A-direction, span_B = MILELM B-direction) so the tests are
self-contained and run in CI without the /tmp/helixspan working copy.
"""

import glob
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
HELIXCAL_DIR = os.path.dirname(HERE)
REPO_ROOT = os.path.dirname(HELIXCAL_DIR)

# Make the package importable as `helixcal` when pytest is invoked from the
# repo root or from within helixcal/tests.
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

FIXTURE_DIR = os.path.join(REPO_ROOT, "desktop", "tests", "fixtures")
SPAN_A_DIR = os.path.join(FIXTURE_DIR, "span_A")   # ELMMIL (A)
SPAN_B_DIR = os.path.join(FIXTURE_DIR, "span_B")   # MILELM (B)


def _sor_in(d):
    return sorted(glob.glob(os.path.join(d, "*.sor")))


@pytest.fixture(scope="session")
def span_a_files():
    files = _sor_in(SPAN_A_DIR)
    if not files:
        pytest.skip(f"no A-direction SOR fixtures in {SPAN_A_DIR}")
    return files


@pytest.fixture(scope="session")
def span_b_files():
    files = _sor_in(SPAN_B_DIR)
    if not files:
        pytest.skip(f"no B-direction SOR fixtures in {SPAN_B_DIR}")
    return files


@pytest.fixture(scope="session")
def one_sor(span_a_files):
    return span_a_files[0]

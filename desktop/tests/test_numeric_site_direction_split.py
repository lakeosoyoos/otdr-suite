"""Regression: one folder whose two directions differ only by a DIGIT.

Montgomery TX (2026-09-17): Chris pointed the Splice Report's one-folder box at
a folder holding both directions of MTG4↔MTG5 and got "Found only 1 direction
group (MTG)" — direction_prefix keys on the leading ALPHA run, so MTG4… and
MTG5… collapse into one group.  The prefix rule itself must not change
(SEANOR001… would then key per fiber), so the split falls back to the site
token / the SOR location pair, and only when the prefix rule finds one group.
"""
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
import folder_intake as fi  # noqa: E402


def _sor(path, loc_a, loc_b):
    """A stub .sor carrying just the GenParams block the intake reads: the map
    mention of the block name, then the block itself."""
    body = (b'GenParams\x00' + b'EN'
            + b'CABLE\x00' + b'FIBER\x00' + b'\x00\x00\x00\x00'
            + loc_a.encode() + b'\x00' + loc_b.encode() + b'\x00')
    path.write_bytes(b'\x00\x02Map\x00GenParams\x00' + body + b'\xff' * 64)
    return str(path)


def _montgomery(tmp_path, n=6, pair_per_direction=True):
    """One folder, both directions, site codes differing by a digit."""
    d = tmp_path / 'MTG4 TO MTG5'
    d.mkdir()
    out = []
    for i in range(1, n + 1):
        out.append(_sor(d / f'MTG4-{i:04d}_1550.sor', 'MTG4',
                        'MTG5' if pair_per_direction else 'MTG5'))
        out.append(_sor(d / f'MTG5-{i:04d}_1550.sor',
                        'MTG5' if pair_per_direction else 'MTG4',
                        'MTG4' if pair_per_direction else 'MTG5'))
    return sorted(out)


def test_prefix_rule_still_collapses_numeric_site_codes(tmp_path):
    """The primary rule is unchanged — this is the bug it cannot see."""
    files = _montgomery(tmp_path)
    assert set(fi.split_paths_by_direction(files)) == {'MTG'}


def test_location_pair_splits_when_the_prefix_cannot(tmp_path):
    files = _montgomery(tmp_path)                      # A shot MTG4→MTG5, B reversed
    groups, how = fi.resolve_direction_groups(files)
    assert how == 'location'
    assert set(groups) == {'MTG4 → MTG5', 'MTG5 → MTG4'}
    assert all(len(v) == 6 for v in groups.values())


def test_site_token_splits_when_the_file_pair_is_not_reversed(tmp_path):
    """Many OTDRs stamp the SAME location pair both ways; the filename's
    leading alphanumeric run still separates the two directions."""
    files = _montgomery(tmp_path, pair_per_direction=False)
    groups, how = fi.resolve_direction_groups(files)
    assert how == 'sitecode'
    assert set(groups) == {'MTG4', 'MTG5'}


def test_materialize_splits_the_montgomery_folder(tmp_path):
    files = _montgomery(tmp_path)
    da, db, info = fi.materialize_two_directions(files, str(tmp_path / 'work'))
    assert info['a_count'] == 6 and info['b_count'] == 6
    assert info['split_by'] == 'location'
    assert len(os.listdir(da)) == 6 and len(os.listdir(db)) == 6
    # Every file lands in exactly one direction, and the info carries the
    # membership the hub's Secret Sauce hand-off reads.
    assert sorted(info['a_files'] + info['b_files']) == files


def test_one_direction_folder_still_refuses(tmp_path):
    """The fallbacks must not invent a second direction: a folder that really
    is one direction still errors, now saying what it found."""
    d = tmp_path / 'one way'
    d.mkdir()
    files = [_sor(d / f'MTG4-{i:04d}_1550.sor', 'MTG4', 'MTG5') for i in range(1, 7)]
    with pytest.raises(ValueError) as exc:
        fi.materialize_two_directions(files, str(tmp_path / 'work'))
    msg = str(exc.value)
    assert 'only 1 direction group' in msg
    assert 'MTG4 → MTG5' in msg                        # the evidence for the tech


def test_two_fiber_single_direction_is_not_split_by_site_token(tmp_path):
    """SEANOR001 + SEANOR002 is one direction of a two-fiber span, not two
    directions — the size floor on the site-token fallback is what stops it."""
    d = tmp_path / 'two fibers'
    d.mkdir()
    files = [_sor(d / f'SEANOR{i:03d}_1550.sor', 'SEANOR', 'NORSEA')
             for i in (1, 2)]
    assert fi.split_by_site_token(files) == {}
    with pytest.raises(ValueError):
        fi.materialize_two_directions(files, str(tmp_path / 'work'))

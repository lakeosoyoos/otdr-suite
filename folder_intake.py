"""Folder / zip intake helpers for the OTDR Suite hub.

Lets the bidirectional tools accept ONE folder (or a .zip) that contains BOTH
directions, auto-split into an A-direction set and a B-direction set, and pick a
sensible output location (the user's Downloads) for the report.

Engine-free and stdlib-only on purpose: this is imported into the hub process,
which must NOT pull in any engine's divergent sor_reader324802a.py (the process
isolation the build is engineered around).  Direction is decided by the OTDR
filename prefix (SEANOR* vs NORSEA*, HOWLAN* vs LANHOW*, …) — the GenParams
location pair is identical in both directions on a bidirectional shoot, so the
prefix is the signal that separates the two.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import zipfile

OTDR_EXTS = ('.sor', '.json')


def find_otdr_files(folder):
    """All .sor/.json files under `folder` (recursive), sorted."""
    out = []
    for root, _dirs, files in os.walk(folder):
        for fn in files:
            if fn.lower().endswith(OTDR_EXTS):
                out.append(os.path.join(root, fn))
    return sorted(out)


def direction_prefix(path):
    """The OTDR filename's leading alpha run, upper-cased (e.g. 'SEANOR' from
    'SEANOR001_1550.sor').  This is the per-file direction key."""
    base = os.path.basename(path)
    m = re.match(r'([A-Za-z]+)', base)
    return (m.group(1).upper() if m else base.upper())


def split_paths_by_direction(paths):
    """Group OTDR file paths into directions by filename prefix.
    Returns {prefix: [paths]}."""
    groups = {}
    for p in paths:
        groups.setdefault(direction_prefix(p), []).append(p)
    return groups


def extract_zip(zip_source, dest_dir):
    """Extract a .zip (path or file-like, e.g. a Streamlit UploadedFile) into
    `dest_dir`, skipping any zip-slip path-traversal members, and return the
    OTDR files found.  Raises zipfile.BadZipFile on a corrupt archive."""
    os.makedirs(dest_dir, exist_ok=True)
    dest_abs = os.path.abspath(dest_dir)
    with zipfile.ZipFile(zip_source) as zf:
        for member in zf.namelist():
            if member.endswith('/'):
                continue
            target = os.path.abspath(os.path.join(dest_dir, member))
            if target != dest_abs and not target.startswith(dest_abs + os.sep):
                continue                       # zip-slip — skip
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(member) as src, open(target, 'wb') as out:
                shutil.copyfileobj(src, out)
    return find_otdr_files(dest_dir)


def _place(src, dst):
    """Hardlink src→dst when possible (instant, no extra disk), else copy."""
    try:
        os.link(src, dst)
    except (OSError, AttributeError):
        shutil.copy2(src, dst)


def materialize_two_directions(paths, workdir):
    """Split a flat list of OTDR files (mixed A+B) by direction and place each
    group into workdir/A and workdir/B.  Returns (dir_a, dir_b, info).

    Raises ValueError when the files don't form exactly two direction groups
    (the caller surfaces the message)."""
    groups = {k: v for k, v in split_paths_by_direction(paths).items() if v}
    if len(groups) < 2:
        raise ValueError(
            f"Found only {len(groups)} direction group "
            f"({', '.join(groups) or 'none'}). A bidirectional report needs BOTH "
            "directions in the folder/zip (e.g. SEANOR* and NORSEA*).")
    dropped = []
    if len(groups) > 2:
        # Keep the two largest groups; report the rest so nothing is silently lost.
        ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        dropped = [k for k, _v in ordered[2:]]
        groups = dict(ordered[:2])
    keys = sorted(groups)                       # deterministic A/B assignment
    dir_a = os.path.join(workdir, 'A')
    dir_b = os.path.join(workdir, 'B')
    for d, k in ((dir_a, keys[0]), (dir_b, keys[1])):
        os.makedirs(d, exist_ok=True)
        for f in groups[k]:
            dst = os.path.join(d, os.path.basename(f))
            if not os.path.exists(dst):
                _place(f, dst)
    info = {
        'a_prefix': keys[0], 'b_prefix': keys[1],
        'a_count': len(groups[keys[0]]), 'b_count': len(groups[keys[1]]),
        'dropped': dropped,
    }
    return dir_a, dir_b, info


def default_report_dir():
    """Where to save the report — the user's Downloads folder (so it isn't
    buried in the traces folder / a temp auto-split dir).  Falls back to
    Desktop, then home, then cwd."""
    home = os.path.expanduser('~')
    for cand in (os.path.join(home, 'Downloads'),
                 os.path.join(home, 'Desktop'),
                 home):
        if os.path.isdir(cand):
            return cand
    return os.getcwd()

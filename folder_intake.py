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
    """All .sor/.json files under `folder` (recursive), sorted.

    Skips macOS AppleDouble sidecars (``._name``) and ``__MACOSX/`` members,
    which a Mac-made zip embeds next to every real file — left in, they inflate
    the file count and pollute the A/B direction split (a leading-``.`` name has
    no alpha prefix, so it spawns a junk direction group).  The three engines
    all filter these; the hub intake must too."""
    out = []
    for root, _dirs, files in os.walk(folder):
        if '__MACOSX' in root.split(os.sep):
            continue
        for fn in files:
            if fn.startswith('._'):
                continue
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


# Zip-extraction size caps — defense against a malicious/corrupt field zip.
# Zip-slip is already blocked (below); these bound the DECOMPRESSED bytes so a
# small archive can't disk-fill a tech's machine.  Real OTDR spans decompress to
# tens of MB, so these are generous — a legitimate zip is never truncated.
_ZIP_MEMBER_MAX = 512 * 1024 * 1024          # 512 MB per member
_ZIP_TOTAL_MAX = 2 * 1024 * 1024 * 1024      # 2 GB per archive


def _bounded_copy(src, out, limit):
    """Stream src→out writing AT MOST `limit` bytes.  Returns (written, hit_cap)
    so the caller can drop a member that lied about its declared size."""
    written = 0
    while True:
        chunk = src.read(1 << 20)              # 1 MB
        if not chunk:
            return written, False
        if written + len(chunk) > limit:
            out.write(chunk[:max(0, limit - written)])
            return limit, True
        out.write(chunk)
        written += len(chunk)


def extract_zip(zip_source, dest_dir):
    """Extract a .zip (path or file-like, e.g. a Streamlit UploadedFile) into
    `dest_dir`, skipping any zip-slip path-traversal members, bounding total
    decompressed size, and return the OTDR files found.  Raises zipfile.BadZipFile
    on a corrupt archive."""
    os.makedirs(dest_dir, exist_ok=True)
    dest_abs = os.path.abspath(dest_dir)
    with zipfile.ZipFile(zip_source) as zf:
        total = 0
        for member in zf.namelist():
            if member.endswith('/'):
                continue
            target = os.path.abspath(os.path.join(dest_dir, member))
            if target != dest_abs and not target.startswith(dest_abs + os.sep):
                continue                       # zip-slip — skip
            if total >= _ZIP_TOTAL_MAX:
                break                          # archive byte budget exhausted
            try:                               # honest-but-huge member → skip cheaply
                if zf.getinfo(member).file_size > _ZIP_MEMBER_MAX:
                    continue
            except KeyError:
                pass
            os.makedirs(os.path.dirname(target), exist_ok=True)
            cap = min(_ZIP_MEMBER_MAX, _ZIP_TOTAL_MAX - total)
            with zf.open(member) as src, open(target, 'wb') as out:
                n, hit_cap = _bounded_copy(src, out, cap)
            total += n
            if hit_cap:                        # lied about its size — drop the partial
                try:
                    os.remove(target)
                except OSError:
                    pass
    return find_otdr_files(dest_dir)


def find_otdr_files_with_zips(folder, extract_dir):
    """Like find_otdr_files, but also DESCENDS into any .zip archives in
    `folder` (extracting each under `extract_dir`) and includes their OTDR
    files.  This is how a span DELIVERED as separate per-direction zips
    (e.g. 'HOWLAN 15SEC.zip' + 'LANHOW 15SEC.zip', or Miller↔Topeka's four
    zips) loads when the tech points Load span at the parent folder — without
    it, find_otdr_files returns 0 because every trace is still inside a zip,
    and the load dead-ends with "both directions required."  Returns the
    combined sorted list (loose files + everything extracted)."""
    out = list(find_otdr_files(folder))
    zips = []
    for root, _dirs, files in os.walk(folder):
        if '__MACOSX' in root.split(os.sep):
            continue
        for fn in files:
            if not fn.startswith('._') and fn.lower().endswith('.zip'):
                zips.append(os.path.join(root, fn))
    for i, zp in enumerate(sorted(zips)):
        dest = os.path.join(
            extract_dir, '_zip%d_%s' % (i, os.path.splitext(os.path.basename(zp))[0]))
        try:
            out += extract_zip(zp, dest)
        except zipfile.BadZipFile:
            continue
    return sorted(out)


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


def materialize_all(paths, dest):
    """Place ALL the OTDR files flat into `dest` — for tools that take one
    combined folder (Secret Sauce auto-splits a single folder internally).
    Returns dest."""
    os.makedirs(dest, exist_ok=True)
    for f in paths:
        dst = os.path.join(dest, os.path.basename(f))
        if not os.path.exists(dst):
            _place(f, dst)
    return dest


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

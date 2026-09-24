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

import hashlib
import os
import re
import shutil
import struct
import tempfile
import zipfile

OTDR_EXTS = ('.sor', '.json')
# Secret Sauce also reads .trc, which the Viewer and Splice Report do not.
# Kept OUT of OTDR_EXTS deliberately: that constant feeds the unified span
# loader those two tools share, and widening it there would hand them files
# they cannot open.  Callers that want TRC pass this explicitly.
OTDR_EXTS_WITH_TRC = ('.sor', '.json', '.trc')
# Splice Report also reads .bdr — FastReporter's bidirectional report, one
# file per fiber carrying BOTH directions (see splicereport/bdr_reader.py).
# Kept out of OTDR_EXTS for the same reason .trc is: the Viewer plots a
# single direction from a single file and cannot open one of these yet, and
# the unified span loader would hand it files it can't read.
OTDR_EXTS_WITH_BDR = ('.sor', '.json', '.bdr')


def is_bdr_set(paths):
    """True when `paths` is a .bdr set — every readable trace file is a .bdr.

    Mixed sets are NOT .bdr sets: a folder holding both would be two shoots
    of the same cable staged together, and silently preferring one format
    would drop the other without telling anyone.  The caller reports the mix
    rather than guessing.
    """
    paths = [p for p in paths if not os.path.basename(p).startswith('._')]
    if not paths:
        return False
    return all(str(p).lower().endswith('.bdr') for p in paths)


# Directories that hold OUR OWN output and must never be inventoried as input.
# Mirrors run_secretsauce._SKIP_DIRS — the engine has pruned these since the
# LAMBEY click-through audit; this loader never did.
SKIP_DIRS = {'SecretSauce_reports', '__MACOSX'}


def find_otdr_files(folder, exts=OTDR_EXTS):
    """All .sor/.json files under `folder` (recursive), sorted.

    Skips macOS AppleDouble sidecars (``._name``) and ``__MACOSX/`` members,
    which a Mac-made zip embeds next to every real file — left in, they inflate
    the file count and pollute the A/B direction split (a leading-``.`` name has
    no alpha prefix, so it spawns a junk direction group).  The three engines
    all filter these; the hub intake must too.

    ALL dot-prefixed files, not just ``._``  (2026-08-30).  The rule above was
    the stated intent from the start, but the code only ever matched the
    AppleDouble prefix — so the hub's OWN report caches, which it writes INTO
    the folder the user picked (``.sr_grid_cache.json`` and
    ``.srfr_grid_cache.json`` at app.py:2412, ``.uni_result_cache.json`` at
    app.py:3136), were inventoried as acquisitions.  Each one has no alpha
    prefix, so each spawned exactly the junk direction group the docstring
    warns about.  Measured on disk BEFORE this fix:

        SEANOR 6.15.2026   found 434 (of 432)
            groups: SEANOR 432, .SR_GRID_CACHE.JSON 1, .SRFR_GRID_CACHE.JSON 1
        Lumen 432 Boarder Project UNI   found 434 (of 432)
            groups: LAMBEY 432, .UNI_RESULT_CACHE.JSON 1, PAIRS 1

    That is a folder-poisoning bug: SEANOR is a SINGLE-direction folder, so it
    correctly raised "Found only 1 direction group" until a report was run on
    it — after which it presents three groups and materialize_two_directions
    happily pairs 432 real traces against one cache file.  RUNNING A REPORT ON
    A FOLDER BROKE THAT FOLDER'S NEXT INTAKE.  The engine has always been
    immune (run_secretsauce._inventory skips every dotfile, with a comment
    naming these same caches); only this loader was exposed.

    The ``PAIRS`` group above is the same defect one level up: SecretSauce_reports/
    is our own output directory and is now pruned, matching the engine."""
    out = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]    # prune in place
        if SKIP_DIRS & set(root.split(os.sep)):
            continue
        for fn in files:
            # No real acquisition is ever a dotfile.
            if fn.startswith('.'):
                continue
            if fn.lower().endswith(exts):
                out.append(os.path.join(root, fn))
    return sorted(out)


# An explicit direction token in the filename: '-AB' / '-BA' (or '_AB' /
# '_BA') as its own dash- or underscore-separated field, followed by the
# wavelength suffix or the extension.  iOLM exports name BOTH directions with
# the same cable id and tell them apart only by this token:
#   MSO401-MSO402-OSP-0432F-01-0001-AB_1550.sor
#   MSO401-MSO402-OSP-0432F-01-0001-BA_1550.sor
# Anchored to the tail so a location code that happens to contain 'AB' (e.g.
# 'ABILENE...') is never read as a direction.
_DIRECTION_TOKEN = re.compile(r'[-_](AB|BA)(?=(?:[-_][0-9]{3,4}(?:nm)?)?\.[A-Za-z0-9]+$)',
                              re.IGNORECASE)


def direction_prefix(path):
    """The per-file direction key.

    Normally the OTDR filename's leading alpha run, upper-cased ('SEANOR' from
    'SEANOR001_1550.sor'), because crews name the two directions by their
    launch site.  When the filename instead carries an explicit direction
    token (see _DIRECTION_TOKEN) the token is appended to that run, so an
    iOLM export whose two directions share one cable id still splits in two:
    'MSO401-…-0001-AB_1550.sor' -> 'MSO401-AB', '…-0001-BA_1550.sor' ->
    'MSO401-BA'.  Files without the token key exactly as they always have."""
    base = os.path.basename(path)
    m = re.match(r'([A-Za-z]+)', base)
    key = (m.group(1).upper() if m else base.upper())
    t = _DIRECTION_TOKEN.search(base)
    return f"{key}-{t.group(1).upper()}" if t else key


def split_paths_by_direction(paths):
    """Group OTDR file paths into directions by filename prefix.
    Returns {prefix: [paths]}."""
    groups = {}
    for p in paths:
        groups.setdefault(direction_prefix(p), []).append(p)
    return groups


# ─── Fallback direction splits (numeric site codes) ─────────────────────────
# direction_prefix keys on the LEADING ALPHA RUN, so it cannot separate two
# directions whose site codes differ only in a digit.  Montgomery TX
# (2026-09-17, Chris): MTG4↔MTG5, both directions named MTG4… and MTG5…, one
# folder, every file keyed 'MTG' — "Found only 1 direction group (MTG)" on a
# folder that held both.  Widening direction_prefix to alphanumerics is NOT
# the fix: 'SEANOR001_1550.sor' would then key per FIBER (432 groups).
#
# So the prefix rule stays first and unchanged, and these two run only when it
# comes back with a single group.  Each must land on EXACTLY two groups or it
# declines, so a genuine one-direction folder still gets the old error.


def _site_token(path):
    """The filename's leading ALPHANUMERIC run, upper-cased: 'MTG4' from
    'MTG4_0001_1550.sor', 'MTG5' from 'MTG5-MTG4-0001_1550.sor'."""
    base = os.path.basename(path)
    m = re.match(r'([A-Za-z0-9]+)', base)
    return (m.group(1).upper() if m else base.upper())


def split_by_site_token(paths):
    """{token: [paths]} keyed on the leading alphanumeric run, or {} unless it
    lands on exactly two groups of two or more files.

    The size floor is what keeps a two-fiber SINGLE-direction folder
    (SEANOR001 + SEANOR002) from being read as two directions — a real span
    shot both ways has many fibers per side, and anything with three or more
    tokens is already rejected by the exactly-two rule.  A token with no
    letter in it ('0001') is a FIBER number, never a site, and is refused for
    the same reason the prefix rule is not trusted on those names."""
    groups = {}
    for p in paths:
        groups.setdefault(_site_token(p), []).append(p)
    if len(groups) != 2 or any(len(v) < 2 for v in groups.values()):
        return {}
    if any(not re.match(r'[A-Za-z]', k) for k in groups):
        return {}
    return groups


def split_by_location_pair(paths):
    """{'MTG4 → MTG5': [paths], …} keyed on each .sor's ORDERED GenParams
    location pair, or {} unless it lands on exactly two groups that are each
    other's reverse.

    On many bidirectional shoots the OTDR stamps the same pair in both
    directions (which is why the filename is the primary signal), but when the
    crew sets origin and far end per direction this reads the direction
    straight out of the file, whatever the traces are called.  The reverse
    check is what makes it safe: two groups that are not A→B and B→A are two
    different cables staged together, not two directions."""
    groups = {}
    for p in paths:
        pair = sor_location_pair(p) if p.lower().endswith('.sor') else None
        if not pair:
            return {}                      # one unreadable file → no verdict
        groups.setdefault(f'{pair[0]} → {pair[1]}', []).append(p)
    if len(groups) != 2:
        return {}
    (k1, _v1), (k2, _v2) = sorted(groups.items())
    if k1.split(' → ')[::-1] != k2.split(' → '):
        return {}                          # not a reversed pair
    return groups


def resolve_direction_groups(paths):
    """The two direction groups for a flat list of OTDR files, and HOW they
    were found: 'prefix' | 'location' | 'sitecode' | 'unnamed'.  The fallbacks
    run only when the filename prefix cannot do the job; 'unnamed' means no
    rule could, and the caller turns each failure into its own message.

    A name with no letters at all (0001_1550.sor — a crew that puts each
    direction in its OWN folder has no reason to put the site in the name)
    keys on the WHOLE filename, so the prefix rule quietly returns one group
    per FIBER: drop both folders in and 0001 vs 0002 comes back as the two
    directions, and the report pairs two different fibers.  Those names are
    not a direction signal, so the prefix result is not trusted for them."""
    groups = {k: v for k, v in split_paths_by_direction(paths).items() if v}
    named = all(re.match(r'[A-Za-z]', os.path.basename(p)) for p in paths)
    if named and len(groups) >= 2:
        return groups, 'prefix'
    for how, fn in (('location', split_by_location_pair),
                    ('sitecode', split_by_site_token)):
        alt = fn(paths)
        if alt:
            return alt, how
    return (groups, 'prefix') if named else ({}, 'unnamed')


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


def extract_zip(zip_source, dest_dir, exts=OTDR_EXTS):
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
    return find_otdr_files(dest_dir, exts)


def find_otdr_files_with_zips(folder, extract_dir, exts=OTDR_EXTS):
    """Like find_otdr_files, but also DESCENDS into any .zip archives in
    `folder` (extracting each under `extract_dir`) and includes their OTDR
    files.  This is how a span DELIVERED as separate per-direction zips
    (e.g. 'HOWLAN 15SEC.zip' + 'LANHOW 15SEC.zip', or Miller↔Topeka's four
    zips) loads when the tech points Load span at the parent folder — without
    it, find_otdr_files returns 0 because every trace is still inside a zip,
    and the load dead-ends with "both directions required."  Returns the
    combined sorted list (loose files + everything extracted)."""
    out = list(find_otdr_files(folder, exts))
    zips = zip_paths(folder)
    for i, zp in enumerate(sorted(zips)):
        dest = os.path.join(
            extract_dir, '_zip%d_%s' % (i, os.path.splitext(os.path.basename(zp))[0]))
        try:
            out += extract_zip(zp, dest, exts)
        except zipfile.BadZipFile:
            continue
    return sorted(out)


def zip_paths(folder):
    """Every .zip under `folder`, sorted.  Same prune rules as find_otdr_files
    — including SKIP_DIRS, so a zip left in our OWN output directory is never
    descended into.  Nothing writes a zip there today, but the two walks have
    to agree or the prune is only half applied."""
    out = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        if SKIP_DIRS & set(root.split(os.sep)):
            continue
        for fn in files:
            if not fn.startswith('.') and fn.lower().endswith('.zip'):
                out.append(os.path.join(root, fn))
    return sorted(out)


def content_key(path):
    """(lowercased basename, sha256) — the identity used to tell a zip that
    merely RE-DELIVERS the loose files from one that carries new ones.

    Deliberately name AND content, not content alone: a folder may legitimately
    contain a byte-identical copy under a DIFFERENT name (that is a duplicate
    the engine is supposed to find, and the raw-identity short-circuit exists
    for it).  Only a file that matches on both is a re-delivery."""
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return (os.path.basename(path).lower(), h.hexdigest())


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
    groups, how = resolve_direction_groups(paths)
    if len(groups) < 2:
        raise ValueError(_undecidable_message(paths) if how == 'unnamed'
                         else _one_direction_message(paths, groups))
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
        'dropped': dropped, 'split_by': how,
        # The exact files each direction got.  Callers that need the two
        # directions' membership (Secret Sauce's combined folder) must read it
        # from here, not re-derive it with direction_prefix: on a fallback
        # split the group keys are site tokens or location pairs, which that
        # function never returns.
        'a_files': list(groups[keys[0]]), 'b_files': list(groups[keys[1]]),
    }
    return dir_a, dir_b, info


def _undecidable_message(paths):
    """The 'nothing here says which direction' error.  Raised instead of
    guessing when the names carry no site (0001_1550.sor) and the .sor headers
    read the same both ways — the shape a crew gets by dropping two direction
    FOLDERS in at once, since a browser hands us the files without their
    folder."""
    example = os.path.basename(paths[0]) if paths else ''
    return (f"Nothing in these {len(paths)} file(s) says which direction each "
            f"one was shot{f' (e.g. {example})' if example else ''}: the names "
            "carry no site, and the headers read the same both ways. Put each "
            "direction in its own folder and use Two folders (A + B).")


def _one_direction_message(paths, groups):
    """The 'this folder holds one direction' error — with the evidence, so the
    tech can tell a folder we failed to split from a folder that really does
    hold one direction.  Names the group, an example file, and (when the .sor
    headers agree) the one way every trace in it was shot."""
    shot = ''
    pairs = {sor_location_pair(p) for p in paths if p.lower().endswith('.sor')}
    pairs.discard(None)
    if len(pairs) == 1:
        a, b = pairs.pop()
        shot = f' Every .sor in it was shot {a or "?"} → {b or "?"}.'
    example = os.path.basename(paths[0]) if paths else ''
    return (f"Found only 1 direction group "
            f"({', '.join(groups) or 'none'}) in {len(paths)} file(s)"
            f"{f' (e.g. {example})' if example else ''}.{shot} "
            "A bidirectional report needs BOTH directions in the folder/zip. "
            "If this folder is one direction, pick the folder that holds both, "
            "or switch to Two folders (A + B) and give it each direction.")


def stage_uploads(uploads, dest, nest_duplicates=True):
    """Write drag-and-dropped uploads into `dest`, keeping every file's OWN
    name.  Returns (paths, collisions).

    A browser hands us bytes and a NAME, never a path, so a dropped PARENT
    folder arrives flat: Montgomery TX's two direction folders both name their
    traces 0001_1550.sor, and writing each upload straight to dest/<name> let
    the second overwrite the first — half the span gone, with nothing said.

    `nest_duplicates` decides what happens to the second and later arrivals of
    one name.  True puts each in its own numbered subfolder (dest/_dup2/<name>)
    — the name stays intact for the direction split and the fiber-number
    parser, and the bidi intake finds it because find_otdr_files walks the
    tree.  False skips it: for a folder handed STRAIGHT to an engine, which
    inventories with os.listdir and would never see a subfolder, an unused
    copy on disk is worse than none.  Either way the repeated names come back
    in `collisions` so the caller can say so.
    """
    os.makedirs(dest, exist_ok=True)
    paths, collisions, seen = [], [], {}
    for u in uploads:
        name = os.path.basename(str(getattr(u, 'name', '') or 'upload'))
        n = seen.get(name.lower(), 0) + 1
        seen[name.lower()] = n
        if n > 1:
            collisions.append(name)
            if not nest_duplicates:
                continue
            out_dir = os.path.join(dest, '_dup%d' % n)
            os.makedirs(out_dir, exist_ok=True)
        else:
            out_dir = dest
        target = os.path.join(out_dir, name)
        with open(target, 'wb') as fh:
            fh.write(u.getbuffer())
        paths.append(target)
    return paths, collisions


def duplicate_names_message(collisions, kept=True, limit=6):
    """One tech-facing sentence about names that arrived more than once —
    `kept` says whether the repeats were staged (nested) or skipped."""
    if not collisions:
        return ''
    uniq = sorted(set(collisions))
    shown = ', '.join(uniq[:limit]) + (f', … (+{len(uniq) - limit} more)'
                                       if len(uniq) > limit else '')
    tail = ('every copy was kept and the direction read from the file headers '
            'instead. Point at the folder rather than dragging it if anything '
            'below looks wrong.'
            if kept else
            'only the first file of each name was used. Drop one folder at a '
            'time, or point at the folder instead of dragging it.')
    return (f"{len(collisions)} dropped file(s) share a name with another file "
            f"in the drop: {shown}. A browser gives us the files without their "
            f"folder, so {tail}")


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


# ─── Foreign-file audit ─────────────────────────────────────────────────────
# A tech's folder sometimes carries acquisitions from ANOTHER job — the
# Tooele↔Knolls span (2026-09-12) arrived with five HH3WES short shots (HH3→West,
# 10 ns, 156 250 range, 5 km) mixed into 864 real traces (KNOLLS↔TOOELE,
# 275 ns, 1 250 000 range, 78 km).  Those five would have formed a junk
# direction group in the bidi loader and gone straight into the Uni / Secret
# Sauce engines as fibers of the span.  This audit reads three header fields
# from every .sor (stdlib only — no engine import) and takes a majority vote:
#
#   * the GenParams location pair (unordered: a bidi shoot usually leaves the
#     pair identical in both directions, but a careful tech swaps them)
#   * the pulse width actually used (FxdParams +18, ns)
#   * the acquisition range (FxdParams, after the pulse-width list)
#
# A file is FOREIGN when its location pair disagrees with the majority AND at
# least one acquisition setting (pulse or range) disagrees too — a different
# place shot with a different setup.  Requiring both guards the two known
# false-positive shapes: a mistyped location on a handful of files (the ELMMIL
# test fixtures spell MILLER as MILER on one side) and a legitimate re-shoot of
# the same span at a different pulse.  Span length is deliberately NOT a vote:
# on Tooele↔Knolls fibers 1-33 end at 14 km because they are broken, and a
# length rule would throw out real broken fibers.
#
# The vote only fires against a small minority (< FOREIGN_MAX_SHARE of the
# folder); when the folder splits into two large camps (A shot at 275 ns, B at
# 500 ns) nothing is flagged.  .json files carry no comparable header and are
# never flagged.
FOREIGN_MAX_SHARE = 0.25


def _genparams_locations(data):
    """The ORDERED GenParams location pair (origin, far end), upper-cased, from
    the bytes of a Bellcore .sor — or None when the block can't be walked.
    Order is kept: it is the only in-file signal of which way a trace was shot
    (see split_by_location_pair)."""
    i = data.find(b'GenParams')
    i = data.find(b'GenParams', i + 1) if i >= 0 else -1
    if i < 0:
        return None
    o = i + len(b'GenParams') + 1 + 2           # name NUL + language code

    def _cstr(off):
        e = data.index(b'\x00', off)
        if e - off > 512:
            raise ValueError('runaway string')
        return data[off:e].decode('latin-1', errors='replace'), e + 1

    _cable, o = _cstr(o)
    _fiber, o = _cstr(o)
    o += 4                                       # fiber type + nominal λ
    loc_a, o = _cstr(o)
    loc_b, o = _cstr(o)
    return (loc_a.strip().upper(), loc_b.strip().upper())


# GenParams sits in the file header, so the direction split never needs to read
# a whole 55 km trace to find it.
_HEAD_BYTES = 128 * 1024


def sor_location_pair(path, _cap=_HEAD_BYTES):
    """The ordered ('MTG4', 'MTG5') location pair of one .sor, or None.  Reads
    only the head of the file."""
    try:
        with open(path, 'rb') as fh:
            data = fh.read(_cap)
    except OSError:
        return None
    try:
        pair = _genparams_locations(data)
    except (ValueError, IndexError):
        return None
    return pair if pair and any(pair) else None


def sor_header(path):
    """Cheap identity/setup fields from a Bellcore .sor: {'loc_pair', 'pulse_ns',
    'acq_range'} — or {} on any structural surprise.  loc_pair is a sorted tuple
    of the two GenParams location strings (upper-cased).  Reads the file once."""
    try:
        with open(path, 'rb') as fh:
            data = fh.read()
    except OSError:
        return {}
    out = {}
    try:
        pair = _genparams_locations(data)
        if pair:
            out['loc_pair'] = tuple(sorted(pair))
    except (ValueError, IndexError):
        pass
    try:
        i = data.find(b'FxdParams')
        i = data.find(b'FxdParams', i + 1) if i >= 0 else -1
        if i >= 0:
            body = i + len(b'FxdParams') + 1
            num_pw = struct.unpack_from('<H', data, body + 16)[0]
            out['pulse_ns'] = struct.unpack_from('<H', data, body + 18)[0]
            out['acq_range'] = struct.unpack_from('<I', data, body + 18 + num_pw * 2)[0]
    except (struct.error, IndexError):
        pass
    return out


def _majority(values):
    """(winning value, its count) over a list that may hold None; (None, 0) when empty."""
    counts = {}
    for v in values:
        if v is not None:
            counts[v] = counts.get(v, 0) + 1
    if not counts:
        return None, 0
    win = max(counts.items(), key=lambda kv: (kv[1], str(kv[0])))
    return win


def _fmt_range(v):
    return f'{v:,}' if isinstance(v, int) else str(v)


def audit_foreign_files(paths):
    """Split `paths` into (kept, foreign).  `foreign` is a list of dicts
    {'path', 'name', 'reason'} describing every file whose header says it was
    shot somewhere else with a different setup (see the module note above).
    Never raises; a folder with no readable .sor headers is returned intact."""
    paths = list(paths)
    heads = {p: (sor_header(p) if p.lower().endswith('.sor') else {}) for p in paths}
    n_sor = sum(1 for h in heads.values() if h)
    if n_sor < 2:
        return paths, []
    maj_loc, _ = _majority([h.get('loc_pair') for h in heads.values()])
    maj_pw, _ = _majority([h.get('pulse_ns') for h in heads.values()])
    maj_rng, _ = _majority([h.get('acq_range') for h in heads.values()])
    kept, foreign = [], []
    for p in paths:
        h = heads[p]
        loc, pw, rng = h.get('loc_pair'), h.get('pulse_ns'), h.get('acq_range')
        loc_off = loc is not None and maj_loc is not None and loc != maj_loc
        pw_off = pw is not None and maj_pw is not None and pw != maj_pw
        rng_off = rng is not None and maj_rng is not None and rng != maj_rng
        if loc_off and (pw_off or rng_off):
            bits = [f"shot {loc[0] or '?'}↔{loc[1] or '?'} (span is "
                    f"{maj_loc[0] or '?'}↔{maj_loc[1] or '?'})"]
            if pw_off:
                bits.append(f'{pw} ns pulse (span {maj_pw} ns)')
            if rng_off:
                bits.append(f'range {_fmt_range(rng)} (span {_fmt_range(maj_rng)})')
            foreign.append({'path': p, 'name': os.path.basename(p),
                            'reason': ', '.join(bits)})
        else:
            kept.append(p)
    # Only a small minority can be foreign — otherwise this is two real camps
    # (or a mislabelled majority) and the tech must sort it out by hand.
    if foreign and len(foreign) >= FOREIGN_MAX_SHARE * len(paths):
        return paths, []
    return kept, foreign


def foreign_files_message(foreign, limit=12):
    """One tech-facing sentence listing what was excluded and why."""
    if not foreign:
        return ''
    names = [f['name'] for f in foreign]
    shown = ', '.join(names[:limit]) + (f', … (+{len(names) - limit} more)'
                                        if len(names) > limit else '')
    reasons = sorted({f['reason'] for f in foreign})
    why = reasons[0] if len(reasons) == 1 else '; '.join(reasons[:3])
    return (f"{len(foreign)} file(s) in this folder do not belong to this span "
            f"and were EXCLUDED from the report: {shown} ({why}). "
            "Move them out of the folder if they are not part of this job.")


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

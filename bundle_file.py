"""OTDR Suite share files: .zfc (a Field Capture from the phone) and .zdb (a
shared project).

Both are one container: a zip with ``manifest.json`` at the top that names the
file's kind and format version.  The manifest decides what a file is, never
the extension, so a file renamed by hand still opens as what it is, and the
extensions can change later: add the new name at the FRONT of ``KINDS[kind]
['exts']`` (new saves use it) and keep the old one in the tuple so old files
keep opening.

Stdlib-only and engine-free on purpose: the hub process imports it (same rule
as folder_intake.py).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

FORMAT = 'otdr-suite-share'
MANIFEST = 'manifest.json'

# kind -> extensions (first = the one new saves get), the newest format
# version this build writes and reads, and the name a tech sees.
KINDS = {
    'field-capture': {'exts': ('.zfc',), 'version': 1, 'label': 'Field Capture'},
    'project':       {'exts': ('.zdb',), 'version': 1, 'label': 'Project'},
}

# A share file is photos + traces, not a disk image; refuse anything that
# would unpack past this (zip-bomb guard).
MAX_UNPACKED_BYTES = 4 * 1024 ** 3


class ShareFileError(Exception):
    """A file that cannot be opened.  The message is written for the tech."""


def extensions(kind: str | None = None) -> tuple[str, ...]:
    """Every extension this build accepts, for one kind or all of them."""
    kinds = [kind] if kind else list(KINDS)
    return tuple(e for k in kinds for e in KINDS[k]['exts'])


def save_extension(kind: str) -> str:
    return KINDS[kind]['exts'][0]


def _app_version() -> str:
    try:
        from error_report import version_labels
        return version_labels()[0]
    except Exception:
        return ''


def _clean_member(name: str) -> str:
    """A member path that stays inside the file when unpacked, or raise."""
    n = str(name).replace('\\', '/')
    parts = n.split('/')
    if not n or n.startswith('/') or ':' in parts[0] or any(s in ('', '.', '..') for s in parts):
        raise ShareFileError(f'Unsafe name inside the file: {name!r}')
    return n


def write(path, kind: str, files: dict, meta: dict | None = None) -> Path:
    """Write a share file.  ``files`` maps inner path -> bytes or a source
    file path.  The extension is set from ``kind`` when ``path`` has none of
    this kind's extensions.  Written to a temp name and moved into place, so
    a crash never leaves a half file under the real name."""
    if kind not in KINDS:
        raise ValueError(f'unknown kind {kind!r}')
    path = Path(path)
    if path.suffix.lower() not in KINDS[kind]['exts']:
        path = path.with_name(path.name + save_extension(kind))
    manifest = {
        'format': FORMAT,
        'kind': kind,
        'version': KINDS[kind]['version'],
        'app_version': _app_version(),
        'created': _dt.datetime.now(_dt.timezone.utc).isoformat(timespec='seconds'),
        'meta': dict(meta or {}),
    }
    tmp = path.with_name(path.name + '.part')
    try:
        with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr(MANIFEST, json.dumps(manifest, indent=2))
            for name, src in files.items():
                arc = _clean_member(name)
                if arc == MANIFEST:
                    raise ValueError('manifest.json is written by bundle_file itself')
                if isinstance(src, (bytes, bytearray)):
                    z.writestr(arc, bytes(src))
                else:
                    z.write(os.fspath(src), arc)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return path


@dataclass
class ShareFile:
    path: Path
    kind: str
    version: int
    manifest: dict
    names: list = field(default_factory=list)

    @property
    def label(self) -> str:
        return KINDS[self.kind]['label']

    def read(self, name: str) -> bytes:
        with zipfile.ZipFile(self.path) as z:
            return z.read(_clean_member(name))

    def extract_to(self, dest) -> Path:
        """Unpack everything except the manifest under ``dest``."""
        dest = Path(dest)
        root = dest.resolve()
        with zipfile.ZipFile(self.path) as z:
            for info in z.infolist():
                if info.is_dir() or info.filename == MANIFEST:
                    continue
                out = (dest / _clean_member(info.filename)).resolve()
                if root not in out.parents:
                    raise ShareFileError(f'Unsafe name inside the file: {info.filename!r}')
                out.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, open(out, 'wb') as dst:
                    while chunk := src.read(1 << 20):
                        dst.write(chunk)
        return dest


def open_file(path, expect: str | None = None) -> ShareFile:
    """Read and check a share file.  Raises ShareFileError with a message a
    tech can act on.  ``expect`` = the kind the caller can handle."""
    path = Path(path)
    name = path.name
    try:
        z = zipfile.ZipFile(path)
    except FileNotFoundError:
        raise ShareFileError(f'{name} was not found.')
    except (zipfile.BadZipFile, OSError):
        raise ShareFileError(f'{name} is not an OTDR Suite file, or it is damaged '
                             '(it may not have finished downloading).')
    with z:
        try:
            m = json.loads(z.read(MANIFEST).decode('utf-8'))
        except KeyError:
            raise ShareFileError(f'{name} is not an OTDR Suite file.')
        except (ValueError, UnicodeDecodeError):
            raise ShareFileError(f'{name} is damaged (its manifest cannot be read).')
        if not isinstance(m, dict) or m.get('format') != FORMAT:
            raise ShareFileError(f'{name} is not an OTDR Suite file.')
        infos = z.infolist()
    kind = m.get('kind')
    if kind not in KINDS:
        raise ShareFileError(f'{name} holds something this version of OTDR Suite does not know '
                             f'({kind!r}). Update OTDR Suite and try again.')
    try:
        version = int(m.get('version'))
    except (TypeError, ValueError):
        raise ShareFileError(f'{name} is damaged (no format version).')
    if version > KINDS[kind]['version']:
        raise ShareFileError(f'{name} was made by a newer OTDR Suite. Update OTDR Suite and try again.')
    if expect and kind != expect:
        raise ShareFileError(f'{name} is a {KINDS[kind]["label"]} file, not a '
                             f'{KINDS[expect]["label"]} file.')
    if sum(i.file_size for i in infos) > MAX_UNPACKED_BYTES:
        raise ShareFileError(f'{name} is too large to open.')
    names = []
    for i in infos:
        if i.is_dir() or i.filename == MANIFEST:
            continue
        names.append(_clean_member(i.filename))
    return ShareFile(path=path, kind=kind, version=version, manifest=m, names=names)


# kind -> function(ShareFile).  Each feature registers its own opener (Field
# Capture import, project open) so this module never imports them.
_HANDLERS: dict = {}


def register(kind: str, handler) -> None:
    if kind not in KINDS:
        raise ValueError(f'unknown kind {kind!r}')
    _HANDLERS[kind] = handler


def dispatch(path):
    """Open any share file and hand it to the feature that owns its kind."""
    sf = open_file(path)
    handler = _HANDLERS.get(sf.kind)
    if handler is None:
        raise ShareFileError(f'{sf.path.name} is a {sf.label} file, and opening those '
                             'is not available in this version of OTDR Suite yet.')
    return handler(sf)

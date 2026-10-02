"""Fail the build when the installed packages are not exactly the lock.

constraints-desktop.txt pins every package the Windows build installs.  A
constraints file only limits what pip installs; it does not stop a package
the lock has never heard of (a new requirement, or a new dependency of a
bumped one) from coming in at its newest version.  CI runs this right after
installing, and it fails on any installed package that is missing from the
lock or at a different version.  pip and wheel are the installer's own tools
and are not part of the app.

Usage, from the desktop folder:  python check_lock.py
"""
from __future__ import annotations

import re
import sys
from importlib import metadata
from pathlib import Path

LOCK = Path(__file__).resolve().parent / "constraints-desktop.txt"
NOT_SHIPPED = {"pip", "wheel"}


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def read_lock(path: Path = LOCK) -> dict:
    pins = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            name, version = line.split("==", 1)
            pins[norm(name)] = version.strip()
    return pins


def installed() -> dict:
    out = {}
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        if name:
            out[norm(name)] = dist.version
    return out


def problems(have: dict, lock: dict) -> list:
    out = []
    for name in sorted(have):
        if name in NOT_SHIPPED:
            continue
        if name not in lock:
            out.append(f"{name} {have[name]} is installed but not in constraints-desktop.txt")
        elif have[name] != lock[name]:
            out.append(f"{name} is {have[name]}, the lock says {lock[name]}")
    return out


def main() -> int:
    found = problems(installed(), read_lock())
    for p in found:
        print("LOCK MISMATCH: " + p)
    if found:
        print(f"{len(found)} package(s) differ from the lock.  Add or bump them in "
              "constraints-desktop.txt on purpose, in their own PR.")
        return 1
    print("Installed packages match constraints-desktop.txt.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

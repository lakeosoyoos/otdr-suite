"""Split the test files into balanced parts, one per CI runner.

The suite takes over 30 minutes one file after another, so CI runs it as
several parts on separate runners at the same time (build-windows.yml, the
tests job).  Each part gets about the same total run time: files are placed
longest first onto the part with the least time so far, using the seconds
each file took in a recorded main build (ci_test_durations.json, keyed by
key(file stem), a short hash, so the record carries no test names).  A file
the record does not know yet counts as the median.  Every test file lands
in exactly one part, and the split depends only on the file list and the
record, so a re-run splits the same way.

Usage, from the desktop folder:  python ci_test_shard.py <part> <parts>
prints the test files of that part (0-based), one per line.

To refresh the record from a CI log of a full run, see the note in
tests/test_ci_test_shard.py.
"""
from __future__ import annotations

import hashlib
import heapq
import json
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RECORD = HERE / "ci_test_durations.json"


def key(stem: str) -> str:
    """The record's key for a test file stem, e.g. key("test_x")."""
    return hashlib.sha1(stem.encode("utf-8")).hexdigest()[:12]


def test_files() -> list:
    return sorted(p.name for p in (HERE / "tests").glob("test_*.py"))


def split(files: list, seconds: dict, parts: int) -> list:
    """`files` into `parts` lists of about equal total `seconds`."""
    default = statistics.median(seconds.values()) if seconds else 1.0
    weight = {f: seconds.get(key(f[:-3]), default) for f in files}
    heap = [(0.0, i) for i in range(parts)]
    out = [[] for _ in range(parts)]
    for f in sorted(files, key=lambda f: (-weight[f], f)):
        total, i = heapq.heappop(heap)
        out[i].append(f)
        heapq.heappush(heap, (total + weight[f], i))
    return [sorted(p) for p in out]


def main(argv) -> int:
    part, parts = int(argv[1]), int(argv[2])
    if not 0 <= part < parts:
        print(f"part must be 0..{parts - 1}", file=sys.stderr)
        return 2
    seconds = json.loads(RECORD.read_text(encoding="utf-8"))
    for f in split(test_files(), seconds, parts)[part]:
        print("tests/" + f)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

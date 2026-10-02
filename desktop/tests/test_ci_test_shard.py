"""CI runs the suite as balanced parts on separate runners.

One file after another the suite took over 30 minutes of every Windows
build.  build-windows.yml now runs it as 4 parts, each on its own runner, at
the same time as the build; desktop/ci_test_shard.py decides which files go
where from the seconds each file took in a recorded main build.  What must
hold: every test file runs in exactly one part, the parts are about even,
the workflow asks for as many parts as it runs, and nothing is published
until every part (and the build) has passed.

To refresh desktop/ci_test_durations.json from a full CI run: take the job
log, and for each "tests/test_x.py::... PASSED" line add the seconds since
the line before it to that file; write the totals keyed by
ci_test_shard.key("test_x").  Only the balance depends on it; a stale record
still runs every file.
"""
import importlib.util
import json
import re

from conftest import REPO_ROOT

DESKTOP = REPO_ROOT / "desktop"
CI = REPO_ROOT / ".github" / "workflows" / "build-windows.yml"


def _shard():
    spec = importlib.util.spec_from_file_location("ci_test_shard", DESKTOP / "ci_test_shard.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_every_test_file_runs_in_exactly_one_part():
    S = _shard()
    files = S.test_files()
    record = json.loads(S.RECORD.read_text(encoding="utf-8"))
    assert "test_ci_test_shard.py" in files
    for parts in range(1, 7):
        split = S.split(files, record, parts)
        assert len(split) == parts
        flat = [f for p in split for f in p]
        assert sorted(flat) == files and len(flat) == len(set(flat)), parts


def test_the_parts_are_about_even():
    S = _shard()
    record = json.loads(S.RECORD.read_text(encoding="utf-8"))
    split = S.split(S.test_files(), record, 4)
    default = sorted(record.values())[len(record) // 2]
    totals = [sum(record.get(S.key(f[:-3]), default) for f in p) for p in split]
    assert max(totals) - min(totals) <= 0.1 * max(totals), totals


def test_a_file_the_record_does_not_know_still_runs():
    S = _shard()
    split = S.split(["test_known.py", "test_new.py"], {S.key("test_known"): 5.0}, 2)
    assert sorted(f for p in split for f in p) == ["test_known.py", "test_new.py"]


def test_the_workflow_runs_as_many_parts_as_it_asks_for():
    text = CI.read_text(encoding="utf-8")
    m = re.search(r"part:\s*\[([0-9,\s]+)\]", text)
    assert m, "the tests job must list its parts"
    parts = [int(x) for x in m.group(1).split(",")]
    assert parts == list(range(len(parts)))
    assert f"python ci_test_shard.py ${{{{ matrix.part }}}} {len(parts)}" in text


def test_nothing_is_published_until_the_tests_and_the_build_pass():
    text = CI.read_text(encoding="utf-8")
    jobs = text[text.index("\njobs:\n"):]
    tests = jobs[jobs.index("\n  tests:\n"):jobs.index("\n  build:\n")]
    build = jobs[jobs.index("\n  build:\n"):jobs.index("\n  publish:\n")]
    publish = jobs[jobs.index("\n  publish:\n"):]
    assert "python -m pytest" in tests and "python -m pytest" not in build
    assert "needs: [tests, build]" in publish
    for step in ("Generate + sign update manifest", "Publish signed manifest to main",
                 "Rehearse manifest publish", "Publish to permanent Release"):
        assert f"- name: {step}" in publish and f"- name: {step}" not in build, step
    assert "- name: Upload artifact" in build
    assert "actions/download-artifact" in publish

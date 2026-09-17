"""Publishing the signed update manifest when main moves on mid-build
(desktop/publish_update_manifest.py).

A Windows build takes ~20 minutes and ends by pushing update_manifest.json and
its signature to main.  On 2026-09-15 several main builds went red at that push
only because another PR was merged while they built.  These tests stand up a
real bare origin, clone it at depth 1 the way actions/checkout does, land merges
in the middle of the "build", and check what the publish step then does:

  * a merge that changes a build path supersedes the older build: nothing is
    pushed, the Release is skipped, the job stays green, and the newer build
    publishes a manifest that covers both;
  * a commit outside the build paths does not: the manifest lands on the tip,
    and only after its hashes are re-checked against the tip's blobs;
  * main's manifest never goes backwards, and a manifest that does not describe
    the commit it lands on is never pushed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys

import pytest

from conftest import REPO_ROOT

DESKTOP = REPO_ROOT / "desktop"
if str(DESKTOP) not in sys.path:
    sys.path.insert(0, str(DESKTOP))

import publish_update_manifest as P  # noqa: E402

SCRIPT = DESKTOP / "publish_update_manifest.py"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build-windows.yml"

WORKFLOW_TEXT = """name: test build
on:
  push:
    paths:
      - "app.py"
      - "engine/**"
      - ".github/workflows/build-windows.yml"
jobs: {}
"""
ENGINE = ("app.py", "engine/core.py")


def _git_bytes(cwd, *args) -> bytes:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout


def _git(cwd, *args) -> str:
    return _git_bytes(cwd, *args).decode("utf-8").strip()


def _blob_sha(cwd, rev, rel) -> str:
    return hashlib.sha256(_git_bytes(cwd, "cat-file", "blob", f"{rev}:{rel}")).hexdigest()


class Origin:
    """A bare origin plus a developer clone that lands merges on main."""

    def __init__(self, root):
        self.root = root
        self.bare = root / "origin.git"
        self.dev = root / "dev"
        _git(root, "init", "--quiet", "--bare", str(self.bare))
        _git(self.bare, "symbolic-ref", "HEAD", "refs/heads/main")
        _git(root, "init", "--quiet", str(self.dev))
        _git(self.dev, "symbolic-ref", "HEAD", "refs/heads/main")
        _git(self.dev, "remote", "add", "origin", str(self.bare))
        self._write(".gitattributes", "* -text\n")
        self._write(".github/workflows/build-windows.yml", WORKFLOW_TEXT)
        self._write("app.py", "print('hub')\n")
        self._write("engine/core.py", "LOSS = 1\n")
        self._write("docs/notes.txt", "notes\n")
        self._commit("initial")

    def _write(self, rel, text):
        p = self.dev / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))

    def _commit(self, message) -> str:
        _git(self.dev, "add", "-A")
        _git(self.dev, "commit", "--quiet", "-m", message)
        _git(self.dev, "push", "--quiet", "origin", "HEAD:main")
        return _git(self.dev, "rev-parse", "HEAD")

    def merge(self, rel, text, message) -> str:
        """Land one change on main, as a squash merge does."""
        _git(self.dev, "pull", "--quiet", "--ff-only", "origin", "main")
        self._write(rel, text)
        return self._commit(message)

    def rewrite_history(self):
        _git(self.dev, "commit", "--quiet", "--amend", "-m", "rewritten")
        _git(self.dev, "push", "--quiet", "--force", "origin", "HEAD:main")

    @property
    def main(self) -> str:
        return _git(self.bare, "rev-parse", "refs/heads/main")

    def manifest_on_main(self) -> dict:
        return json.loads(_git_bytes(self.bare, "show", "main:update_manifest.json"))

    def start_build(self, name) -> "Build":
        return Build(self, name)


class Build:
    """One CI run: a depth-1 checkout of main at the moment the run started."""

    def __init__(self, origin, name):
        self.dir = origin.root / name
        _git(origin.root, "clone", "--quiet", "--depth", "1", "--branch", "main",
             origin.bare.as_uri(), str(self.dir))
        self.sha = _git(self.dir, "rev-parse", "HEAD")

    def sign_manifest(self, version, engine=ENGINE) -> bytes:
        """What make_update_manifest.py writes: hashes of the checked-out files."""
        files = {rel: hashlib.sha256((self.dir / rel).read_bytes()).hexdigest() for rel in engine}
        body = json.dumps({"version": version, "commit": self.sha, "files": files},
                          sort_keys=True, separators=(",", ":")).encode("utf-8")
        (self.dir / P.MANIFEST_NAME).write_bytes(body)
        (self.dir / P.SIG_NAME).write_bytes(hashlib.sha512(body).digest())
        return body

    def publish(self, version, **kw) -> P.Result:
        return P.publish(self.dir, "main", self.sha, version, log=lambda *_: None, **kw)


@pytest.fixture
def origin(tmp_path, monkeypatch):
    # Keep the developer's own git config (signing, hooks, autocrlf) out of it.
    empty = tmp_path / "gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(key, "dev")
    for key in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(key, "dev@example.com")
    return Origin(tmp_path)


# ── the normal case ────────────────────────────────────────────────────────
def test_main_unchanged_publishes_on_the_built_commit(origin):
    run = origin.start_build("run100")
    body = run.sign_manifest(100)
    res = run.publish(100)
    assert (res.action, res.ship, res.pushed) == ("publish", True, True)
    assert origin.main == res.commit
    assert _git(origin.bare, "rev-parse", "main^") == run.sha
    assert _git_bytes(origin.bare, "show", "main:update_manifest.json") == body
    assert _git(origin.bare, "log", "-1", "--format=%s", "main") == (
        "ci: signed update manifest for run 100 [skip ci]")
    # The checkout that was built and tested is left alone.
    assert _git(run.dir, "rev-parse", "HEAD") == run.sha


# ── back-to-back merges ────────────────────────────────────────────────────
def test_back_to_back_merges_leave_main_green_and_ship_both(origin):
    """Runs 461 and 462 on 2026-09-15: #194 merged, its build started, #196
    merged three minutes later.  The older build must stand down without
    failing, and the newer one publish a manifest covering both."""
    origin.merge("engine/core.py", "LOSS = 2\n", "PR 194")
    older = origin.start_build("run461")
    older.sign_manifest(461)
    origin.merge("app.py", "print('hub 2')\n", "PR 196")
    newer = origin.start_build("run462")
    newer.sign_manifest(462)

    res = older.publish(461)
    assert (res.action, res.ship, res.pushed) == ("superseded", False, False)
    assert newer.sha[:7] in res.reason
    assert origin.main == newer.sha

    res = newer.publish(462)
    assert (res.action, res.ship, res.pushed) == ("publish", True, True)
    manifest = origin.manifest_on_main()
    assert (manifest["version"], manifest["commit"]) == (462, newer.sha)
    assert _git(origin.bare, "show", "main:engine/core.py") == "LOSS = 2"   # PR 194 ships too
    for rel in ENGINE:
        assert _blob_sha(origin.bare, "main", rel) == manifest["files"][rel]


def test_three_merges_inside_one_build_publish_once_from_the_newest(origin):
    """Runs 465, 467 and 468: #195, #184 and #197 inside half an hour."""
    runs = []
    for n, text in ((465, "LOSS = 3\n"), (467, "LOSS = 4\n"), (468, "LOSS = 5\n")):
        origin.merge("engine/core.py", text, f"merge for run {n}")
        run = origin.start_build(f"run{n}")
        run.sign_manifest(n)
        runs.append((n, run))
    results = [run.publish(n) for n, run in runs]
    assert [r.action for r in results] == ["superseded", "superseded", "publish"]
    assert [r.ship for r in results] == [False, False, True]
    assert origin.manifest_on_main()["commit"] == runs[-1][1].sha


def test_newer_build_finishing_first_is_never_rolled_back(origin):
    origin.merge("engine/core.py", "LOSS = 2\n", "PR A")
    older = origin.start_build("run10")
    older.sign_manifest(10)
    origin.merge("engine/core.py", "LOSS = 3\n", "PR B")
    newer = origin.start_build("run11")
    newer.sign_manifest(11)

    assert newer.publish(11).pushed
    published = origin.main
    res = older.publish(10)
    assert (res.action, res.ship, res.pushed) == ("superseded", False, False)
    assert "v11" in res.reason
    assert origin.main == published


def test_old_rerun_cannot_publish_over_a_newer_manifest_on_an_identical_tree(origin):
    """Same shipped files, but main already carries a newer version: landing on
    top would move main's manifest backwards."""
    old = origin.start_build("run20")
    old.sign_manifest(20)
    origin.merge("docs/notes.txt", "more notes\n", "docs only")
    later = origin.start_build("run21")          # e.g. a manual workflow run
    later.sign_manifest(21)
    assert later.publish(21).pushed
    published = origin.main
    res = old.publish(20)
    assert (res.action, res.ship, res.pushed) == ("superseded", False, False)
    assert origin.main == published


def test_commit_outside_the_build_paths_does_not_strand_the_manifest(origin):
    """docs/ is not a build path, so no newer build is coming and standing down
    would leave the fleet without this one.  The shipped files are identical at
    the tip, so the manifest lands there."""
    run = origin.start_build("run30")
    body = run.sign_manifest(30)
    docs = origin.merge("docs/notes.txt", "more notes\n", "docs only")
    res = run.publish(30)
    assert (res.action, res.ship, res.pushed) == ("publish", True, True)
    assert _git(origin.bare, "rev-parse", "main^") == docs
    assert _git_bytes(origin.bare, "show", "main:update_manifest.json") == body
    manifest = origin.manifest_on_main()
    assert manifest["commit"] == run.sha        # still names the commit it was built from
    for rel in ENGINE:
        assert _blob_sha(origin.bare, "main", rel) == manifest["files"][rel]
    assert run.sha in _git(origin.bare, "log", "-1", "--format=%B", "main")


@pytest.mark.parametrize("rel, action, pushed", [
    ("engine/core.py", "superseded", False),
    ("docs/notes.txt", "publish", True),
])
def test_merge_landing_during_the_push_is_read_again_not_forced(origin, monkeypatch, rel,
                                                                action, pushed):
    run = origin.start_build("run40")
    run.sign_manifest(40)
    real_push, landed = P._push, []

    def push_after_a_merge(repo, commit, branch):
        if not landed:
            landed.append(origin.merge(rel, "changed mid-push\n", "merged mid-push"))
        return real_push(repo, commit, branch)

    monkeypatch.setattr(P, "_push", push_after_a_merge)
    res = run.publish(40)
    assert (res.action, res.pushed, res.ship) == (action, pushed, pushed)
    if pushed:
        assert _git(origin.bare, "rev-parse", "main^") == landed[0]
    else:
        assert origin.main == landed[0]


def test_rerun_after_a_successful_publish_ships_without_pushing_again(origin):
    run = origin.start_build("run95")
    run.sign_manifest(95)
    first = run.publish(95)
    assert first.pushed
    res = run.publish(95)
    assert (res.action, res.ship, res.pushed) == ("already", True, False)
    assert origin.main == first.commit


# ── fail loud, push nothing ────────────────────────────────────────────────
def test_main_that_no_longer_contains_the_build_fails_loud(origin):
    run = origin.start_build("run50")
    run.sign_manifest(50)
    origin.rewrite_history()
    before = origin.main
    with pytest.raises(P.PublishError, match="no longer contains"):
        run.publish(50)
    assert origin.main == before


def test_build_change_marked_skip_ci_fails_loud(origin):
    """No build will run for it, so standing down would skip a manifest."""
    run = origin.start_build("run60")
    run.sign_manifest(60)
    origin.merge("engine/core.py", "LOSS = 4\n", "hotfix [skip ci]")
    before = origin.main
    with pytest.raises(P.PublishError, match="skip CI"):
        run.publish(60)
    assert origin.main == before


def test_manifest_that_does_not_describe_the_commit_is_never_pushed(origin):
    """The Windows checkout hazard: CRLF working-tree bytes hash differently
    from the LF blobs the fleet downloads."""
    run = origin.start_build("run70")
    core = run.dir / "engine" / "core.py"
    core.write_bytes(core.read_bytes().replace(b"\n", b"\r\n"))
    run.sign_manifest(70)
    before = origin.main
    with pytest.raises(P.PublishError, match="engine/core.py"):
        run.publish(70)
    with pytest.raises(P.PublishError, match="engine/core.py"):
        run.publish(70, dry_run=True)
    assert origin.main == before


def test_manifest_is_rechecked_on_the_commit_it_lands_on(origin):
    """Were an engine file ever outside the build paths, a change to it would
    neither start a build nor count as superseding, yet the manifest would no
    longer describe the tip.  That must stop, not publish."""
    run = origin.start_build("run75")
    run.sign_manifest(75, engine=ENGINE + ("docs/notes.txt",))
    origin.merge("docs/notes.txt", "edited while building\n", "docs only")
    before = origin.main
    with pytest.raises(P.PublishError, match="docs/notes.txt"):
        run.publish(75)
    assert origin.main == before


def test_manifest_from_another_run_is_refused(origin):
    run = origin.start_build("run80")
    run.sign_manifest(79)
    with pytest.raises(P.PublishError, match="not this run"):
        run.publish(80)


def test_unsigned_build_pushes_nothing_but_still_gates_the_release(origin):
    run = origin.start_build("run90")
    before = origin.main
    res = run.publish(90)
    assert (res.ship, res.pushed) == (True, False)
    origin.merge("engine/core.py", "LOSS = 5\n", "PR")
    res = run.publish(90)
    assert (res.action, res.ship, res.pushed) == ("superseded", False, False)
    assert _git(origin.bare, "rev-parse", "main^") == before


# ── branch-build rehearsal ─────────────────────────────────────────────────
def test_rehearsal_builds_the_commit_but_never_pushes(origin):
    run = origin.start_build("run1")
    run.sign_manifest(1)
    before = origin.main
    res = run.publish(1, dry_run=True)
    assert res.pushed is False and res.commit
    assert origin.main == before
    assert _git(run.dir, "rev-parse", f"{res.commit}^") == run.sha


def test_rehearsal_on_a_force_pushed_branch_does_not_fail_the_build(origin):
    run = origin.start_build("run2")
    run.sign_manifest(2)
    origin.rewrite_history()
    before = origin.main
    res = run.publish(2, dry_run=True)
    assert res.pushed is False
    assert origin.main == before


# ── command line + workflow wiring ─────────────────────────────────────────
def test_cli_exit_codes_and_step_outputs(origin, tmp_path):
    run = origin.start_build("run3")
    run.sign_manifest(3)
    origin.merge("engine/core.py", "LOSS = 6\n", "PR")
    out, summary = tmp_path / "out.txt", tmp_path / "summary.md"
    env = dict(os.environ, GITHUB_OUTPUT=str(out), GITHUB_STEP_SUMMARY=str(summary))
    cmd = [sys.executable, str(SCRIPT), "--repo", str(run.dir), "--branch", "main",
           "--built", run.sha, "--version", "3"]

    proc = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert proc.returncode == 0, proc.stdout.decode("utf-8", "replace")
    assert out.read_text(encoding="utf-8").splitlines() == ["ship=false", "pushed=false"]
    assert "superseded" in summary.read_text(encoding="utf-8")

    (run.dir / P.SIG_NAME).unlink()
    proc = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert proc.returncode == 1
    assert out.read_text(encoding="utf-8").splitlines()[-2:] == ["ship=false", "pushed=false"]


def test_reads_the_real_workflow_path_filter():
    specs = P.build_pathspecs(CI_WORKFLOW.read_text(encoding="utf-8"))
    assert {"app.py", "desktop/", "viewer/", ".github/workflows/build-windows.yml"} <= set(specs)


def test_a_path_filter_it_cannot_read_stops_the_publish():
    with pytest.raises(P.PublishError, match="unsupported"):
        P.build_pathspecs(WORKFLOW_TEXT.replace('"engine/**"', '"engine/*.py"'))


def _step(ci, name):
    m = re.search(rf"- name: {re.escape(name)}.*?(?=\n      - name:|\Z)", ci, re.DOTALL)
    assert m, f"CI must have a '{name}' step"
    return m.group(0)


def test_workflow_publishes_through_the_script_and_gates_the_release_on_it():
    ci = CI_WORKFLOW.read_text(encoding="utf-8")
    pub = _step(ci, "Publish signed manifest to main")
    assert "publish_update_manifest.py" in pub and "--dry-run" not in pub
    assert re.search(r"id:\s*publish\b", pub)
    assert not re.search(r"^\s*git push", ci, re.MULTILINE), (
        "the manifest must reach main only through publish_update_manifest.py")
    release = _step(ci, "Publish to permanent Release (windows-build)")
    assert "github.ref == 'refs/heads/main'" in release
    assert "steps.publish.outputs.ship == 'true'" in release, (
        "a build that stood down must not overwrite the installer")
    rehearsal = _step(ci, "Rehearse manifest publish")
    assert "--dry-run" in rehearsal and "github.ref != 'refs/heads/main'" in rehearsal


def test_release_upload_retries_a_flaky_uploads_endpoint_then_fails_loud():
    """uploads.github.com 5xx must not redden a finished build (run 533,
    2026-09-17), and must not quietly leave a stale installer either: main
    would carry that run's manifest while the Release served the previous
    .exe.  So the upload retries, and still fails the job when it runs out."""
    release = _step(CI_WORKFLOW.read_text(encoding="utf-8"),
                    "Publish to permanent Release (windows-build)")
    assert "--clobber" in release, "retrying is only safe because the upload replaces"
    assert re.search(r"\$delays\s*=\s*@\(", release), "the upload must retry"
    assert "Start-Sleep" in release, "retries must back off, not hammer"
    # Loud on exhaustion: a stale installer under a published manifest is the
    # one outcome that must never pass as success.
    assert re.search(r"Write-Error[^\n]*release upload failed", release)
    assert re.search(r"^\s*exit 1\s*$", release, re.MULTILINE)

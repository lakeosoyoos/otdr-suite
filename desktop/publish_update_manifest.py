"""
Publish the signed auto-update manifest to main, or stand down (CI-only).
=========================================================================
make_update_manifest.py signs a manifest for the commit this run BUILT: every
engine file's SHA-256 at that commit, the run number as a monotonic version,
and the commit itself (the launcher downloads the files at that commit and
refuses any version that is not newer than the one it has).  Publishing it is
the last thing a ~20 minute Windows build does, and on a busy day another PR
is often merged inside that window.  main is then no longer the commit that was
built, the old bare push was rejected, and the build went red even though the
tests, the exe and the boot self-test had all passed (several main builds on
2026-09-15 failed on that alone).

Rebasing the manifest onto the newer main is NOT a fix: its hashes describe the
older tree, its version would claim a tree it never saw, and the Release upload
after it would replace the installer with an older build.

So this script fetches main and decides:

  publish     main is still the built commit: the manifest goes on it.
  publish     main moved on only through commits that leave every build path
              untouched (docs, another run's manifest commit), so the files
              this build ships are byte-identical at the tip, and no newer
              build is coming: the manifest goes on the tip.
  superseded  a later commit changed a build path (that push started its own
              build, which publishes a manifest and installer that include
              this commit), or main already carries a newer manifest.  Nothing
              is pushed, the Release upload is skipped, exit 0.
  already     an earlier attempt of this same run published it already.

Before anything is pushed, every hash in the manifest is re-checked against the
blobs of the built commit and of the commit it lands on: the same check the
fleet makes.  Anything unexpected (main no longer contains the built commit, a
hash that does not match, a build-path change marked [skip ci], a manifest for
another run, a push rejected five times) raises PublishError: exit 1, before
the Release upload, as the old step did.

Usage (CI, from the repo root):
    python desktop/publish_update_manifest.py --branch main --built <SHA> --version <N>
    python desktop/publish_update_manifest.py --branch <ref> --built <SHA> --version <N> --dry-run

--dry-run (branch builds) makes the same fetch, decision, hash check and
manifest commit, and stops short of the push, so a broken publish path fails on
the PR instead of on main.  Writes ship=true|false to $GITHUB_OUTPUT; the
Release upload runs only on true.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_NAME = "update_manifest.json"
SIG_NAME = "update_manifest.json.sig"
WORKFLOW_REL = Path(".github") / "workflows" / "build-windows.yml"
CI_NAME = "otdr-suite-ci"
CI_EMAIL = "ci@users.noreply.github.com"
PUSH_ATTEMPTS = 5
# Commit-message markers GitHub honours to skip the workflow runs of a push.
SKIP_MARKERS = ("[skip ci]", "[ci skip]", "[no ci]", "[skip actions]", "[actions skip]")


class PublishError(RuntimeError):
    """Stop before the Release upload: a person needs to look."""


@dataclass
class Decision:
    action: str                    # "publish" | "superseded" | "already"
    reason: str
    parent: Optional[str] = None   # the commit the manifest lands on (publish)


@dataclass
class Result:
    action: str
    reason: str
    ship: bool                     # True: the Release upload may run
    pushed: bool = False
    commit: Optional[str] = None


def _git(repo, *args, stdin=None, env=None, check=True):
    proc = subprocess.run(["git", "-C", str(repo), *args], input=stdin,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    if check and proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise PublishError(f"git {' '.join(args)} failed ({proc.returncode}): {err}")
    return proc


def _out(repo, *args, **kw) -> str:
    return _git(repo, *args, **kw).stdout.decode("utf-8", "replace").strip()


def build_pathspecs(workflow_text: str) -> List[str]:
    """on.push.paths from the workflow, as git pathspecs.  A push touching one
    of these starts a build, which is what lets a later commit supersede this
    one.  Only the two forms the workflow uses are understood (a plain path and
    `dir/**`); anything else stops the publish rather than guess."""
    globs, in_paths = [], False
    for ln in workflow_text.splitlines():
        s = ln.strip()
        if s == "paths:":
            in_paths = True
            continue
        if in_paths:
            if s.startswith("- "):
                globs.append(s[2:].strip().strip('"').strip("'"))
            elif s and not s.startswith("#"):
                break   # first non-list, non-comment line ends the paths block
    if not globs:
        raise PublishError("could not read on.push.paths from the workflow")
    specs = []
    for g in globs:
        stem = g[:-3] if g.endswith("/**") else g
        if any(c in stem for c in "*?[!"):
            raise PublishError(f"unsupported path filter {g!r} in the workflow; "
                               "build_pathspecs() needs to learn it")
        specs.append(stem + "/" if g.endswith("/**") else g)
    return specs


def _manifest_at(repo, rev) -> Optional[bytes]:
    proc = _git(repo, "cat-file", "blob", f"{rev}:{MANIFEST_NAME}", check=False)
    return proc.stdout if proc.returncode == 0 else None


def _version_of(manifest_bytes: Optional[bytes]) -> int:
    try:
        return int(json.loads(manifest_bytes.decode("utf-8"))["version"])
    except Exception:
        return 0


def fetch_tip(repo, branch) -> str:
    ref = f"refs/remotes/origin/{branch}"
    _git(repo, "fetch", "--no-tags", "--quiet", "origin", f"+refs/heads/{branch}:{ref}")
    return _out(repo, "rev-parse", ref)


def decide(repo, branch, built, version, tip, pathspecs) -> Decision:
    b, t = built[:7], tip[:7]
    if tip == built:
        return Decision("publish", f"{branch} is still at {b}, the commit this run built", built)

    anc = _git(repo, "merge-base", "--is-ancestor", built, tip, check=False).returncode
    if anc == 1:
        raise PublishError(f"{branch} ({t}) no longer contains {b}, the commit this run "
                           "built (history rewritten?)")
    if anc != 0:
        raise PublishError(f"could not tell whether {t} contains {b} (git exit {anc})")

    # Never move main's manifest backwards, whatever the tree looks like.
    tip_version = _version_of(_manifest_at(repo, tip))
    if tip_version > version:
        return Decision("superseded", f"{branch} already carries manifest v{tip_version}, "
                        f"newer than this run's v{version}")

    diff = _git(repo, "diff", "--quiet", built, tip, "--", *pathspecs, check=False).returncode
    if diff not in (0, 1):
        raise PublishError(f"git diff {b} {t} failed (exit {diff})")
    if diff == 1:
        newest = _out(repo, "rev-list", "-1", f"{built}..{tip}", "--", *pathspecs)
        if not newest:
            raise PublishError(f"the build paths differ between {b} and {t} but no commit "
                               "between them changes them")
        message = _out(repo, "log", "-1", "--format=%B", newest)
        subject = message.splitlines()[0] if message else ""
        if any(m in message.lower() for m in SKIP_MARKERS):
            raise PublishError(f"{newest[:7]} ({subject}) changed the build paths but asked "
                               "GitHub to skip CI, so no build will publish it; run the "
                               f"workflow by hand on {branch}")
        return Decision("superseded", f"{newest[:7]} ({subject}) changed the build paths "
                        f"after {b}; its own build publishes a manifest and installer "
                        "that include this commit")

    if tip_version == version:
        return Decision("already", f"manifest v{version} is already on {branch} ({t})")
    return Decision("publish", f"{branch} moved on to {t} only through commits outside the "
                    f"build paths, so the files built at {b} are identical there", tip)


def verify_manifest(repo, rev, manifest: dict) -> None:
    """Each file's SHA-256 against its blob at `rev`: the fleet's own check."""
    bad = []
    for rel, want in sorted(manifest["files"].items()):
        proc = _git(repo, "cat-file", "blob", f"{rev}:{rel}", check=False)
        if proc.returncode != 0:
            bad.append(f"{rel} (missing)")
        elif hashlib.sha256(proc.stdout).hexdigest() != want:
            bad.append(rel)
    if bad:
        raise PublishError(f"manifest hashes do not match {rev[:7]} for {len(bad)} "
                           f"file(s): {', '.join(bad[:5])}")


def make_commit(repo, parent, manifest_bytes, sig_bytes, version, built) -> str:
    """The manifest commit, made with plumbing on `parent` so the checkout (the
    tree that was just built and tested) is never touched."""
    blobs = [(name, _out(repo, "hash-object", "-w", "--no-filters", "--stdin", stdin=data))
             for name, data in ((MANIFEST_NAME, manifest_bytes), (SIG_NAME, sig_bytes))]
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(tmp) / "index"),
                   GIT_AUTHOR_NAME=CI_NAME, GIT_AUTHOR_EMAIL=CI_EMAIL,
                   GIT_COMMITTER_NAME=CI_NAME, GIT_COMMITTER_EMAIL=CI_EMAIL)
        _git(repo, "read-tree", parent, env=env)
        for name, blob in blobs:
            _git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{name}", env=env)
        tree = _out(repo, "write-tree", env=env)
        message = f"ci: signed update manifest for run {version} [skip ci]"
        if parent != built:
            message += (f"\n\nBuilt from {built}. main had moved on only through commits "
                        "outside the build paths, so the shipped files are identical here.")
        return _out(repo, "commit-tree", tree, "-p", parent, "-m", message, env=env)


def _push(repo, commit, branch):
    return _git(repo, "push", "--quiet", "origin", f"{commit}:refs/heads/{branch}", check=False)


def publish(repo, branch, built, version, *, dry_run=False, workflow=None, log=print) -> Result:
    repo = Path(repo)
    workflow = Path(workflow) if workflow else repo / WORKFLOW_REL
    pathspecs = build_pathspecs(workflow.read_text(encoding="utf-8"))

    manifest = manifest_bytes = sig_bytes = None
    if (repo / MANIFEST_NAME).exists():
        if not (repo / SIG_NAME).exists():
            raise PublishError(f"{MANIFEST_NAME} has no {SIG_NAME} next to it")
        manifest_bytes = (repo / MANIFEST_NAME).read_bytes()
        sig_bytes = (repo / SIG_NAME).read_bytes()
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        if manifest.get("version") != version or manifest.get("commit") != built:
            raise PublishError(f"{MANIFEST_NAME} is v{manifest.get('version')} at "
                               f"{str(manifest.get('commit'))[:7]}, not this run "
                               f"(v{version} at {built[:7]})")
        verify_manifest(repo, built, manifest)
    else:
        log(f"No {MANIFEST_NAME} (signing secret unset): nothing to publish.")

    for attempt in range(1, PUSH_ATTEMPTS + 1):
        try:
            tip = fetch_tip(repo, branch)
            d = decide(repo, branch, built, version, tip, pathspecs)
        except PublishError as exc:
            if not dry_run:
                raise
            # A branch that was force-pushed or deleted mid-build is not a defect.
            log(f"rehearsal: a real publish would stop here: {exc}")
            tip, d = built, Decision("publish", "rehearsing on the built commit", built)
        log(f"{d.action}: {d.reason}")

        if not dry_run:
            if d.action == "superseded":
                return Result(d.action, d.reason, ship=False)
            if manifest_bytes is None:
                return Result(d.action, d.reason, ship=True)
            if d.action == "already":
                if _manifest_at(repo, tip) != manifest_bytes:
                    raise PublishError(f"{branch} carries a different manifest v{version} "
                                       "than this run signed")
                return Result(d.action, d.reason, ship=True)
        elif manifest_bytes is None:
            return Result(d.action, d.reason, ship=d.action != "superseded")

        parent = d.parent if d.action == "publish" else built
        if parent != built:
            verify_manifest(repo, parent, manifest)
        commit = make_commit(repo, parent, manifest_bytes, sig_bytes, version, built)
        if dry_run:
            log(f"rehearsal: made manifest commit {commit[:7]} on {parent[:7]}; not pushing")
            return Result(d.action, d.reason, ship=d.action != "superseded", commit=commit)

        proc = _push(repo, commit, branch)
        if proc.returncode == 0:
            log(f"pushed manifest v{version} to {branch} as {commit[:7]} (on {parent[:7]})")
            return Result(d.action, d.reason, ship=True, pushed=True, commit=commit)
        err = proc.stderr.decode("utf-8", "replace").strip()
        log(f"push attempt {attempt} rejected ({err}); reading {branch} again")
    raise PublishError(f"manifest push to {branch} rejected {PUSH_ATTEMPTS} times")


def _write_github_files(res: Optional[Result], error: Optional[str]) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"ship={'true' if res and res.ship else 'false'}\n")
            fh.write(f"pushed={'true' if res and res.pushed else 'false'}\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        line = f"failed: {error}" if error else f"{res.action}: {res.reason}"
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(f"**Update manifest** {line}\n")


def main(argv=None) -> int:
    # Commit subjects can carry non-ASCII; the Windows runner's console is cp1252.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Publish the signed update manifest, or stand down.")
    ap.add_argument("--branch", required=True)
    ap.add_argument("--built", required=True, help="the commit this run checked out and built")
    ap.add_argument("--version", required=True, type=int, help="the run number")
    ap.add_argument("--dry-run", action="store_true", help="decide and verify, never push")
    ap.add_argument("--repo", default=str(REPO_ROOT))
    args = ap.parse_args(argv)
    try:
        res = publish(args.repo, args.branch, args.built, args.version, dry_run=args.dry_run)
    except PublishError as exc:
        print(f"::error::Update manifest not published: {exc}")
        _write_github_files(None, str(exc))
        return 1
    if res.action == "superseded" and not args.dry_run:
        print(f"::notice::Update manifest not published, superseded: {res.reason}")
    _write_github_files(res, None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

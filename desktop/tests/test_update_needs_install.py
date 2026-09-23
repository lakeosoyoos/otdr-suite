"""A build that adds engine files cannot reach an installed exe by update.

ENGINE_FILES is frozen into the launcher, and the launcher is the one thing
auto-update never replaces.  So when build 658 (the FQA Builder) took the
list from 21 files to 34, every installed exe refused manifest 658 at boot:
one "file set != ENGINE_FILES" line in a log nobody reads.  The hub, which
only ever looked at the manifest's version number, saw 658 > 657 and put up
"Report generation is paused — needs a restart" with a restart button.  The
boss clicked it; the launcher refused the manifest again; the page came back
identical.  Reports were blocked all day by an instruction that could not
work, and nothing reached Slack.

Two halves, tested from both ends:

  * the launcher tells the difference between "cannot carry this manifest"
    and the other rejections, records why (NEEDS_INSTALL_ENV), posts once per
    exe build to the channel, and publishes its own file list
    (ENGINE_FILES_ENV) so the hub can see it coming;
  * the hub, from either the launcher's verdict or the manifest's file list
    against that published one, says "needs a fresh install" with the
    installer link — in the banner, the report block and the footer — and
    offers no restart.  Both lists unknown is fail-open: the restart stays.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import types
import urllib.request

import pytest

from conftest import (FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR,
                      run_streamlit)
import error_report as R
from test_cache_pin import _FakeSt, _app_constant
from test_engine_self_verify import _load_app_helper, _load_launcher
from test_update_ping import BRIDGE_HDR_RE

NEEDS_ENV = "OTDR_SUITE_NEEDS_INSTALL"
FILES_ENV = "OTDR_SUITE_ENGINE_FILES"
NEW_FILE = "fqa/new_tab.py"


@pytest.fixture(autouse=True)
def _no_env_leaks():
    """The launcher SETS these on the real os.environ (see the same fixture
    in test_cache_pin.py): pop them either side so a verdict from one test
    cannot turn a later AppTest's restart banner into the install notice."""
    for k in (NEEDS_ENV, FILES_ENV, "OTDR_SUITE_CACHE_PINNED"):
        os.environ.pop(k, None)
    yield
    for k in (NEEDS_ENV, FILES_ENV, "OTDR_SUITE_CACHE_PINNED"):
        os.environ.pop(k, None)


# ═════════════════════════════════════════════════════════════════════════
#  the launcher
# ═════════════════════════════════════════════════════════════════════════

def _serve(L, monkeypatch, version, manifest_files, commit="a" * 40):
    """A _fetch stand-in: a signed-looking manifest naming `manifest_files`,
    its signature, and a matching body for every file at the manifest's
    commit.  Returns the list of URLs asked for."""
    hashes, bodies = {}, {}
    for rel in manifest_files:
        body = f"# {rel}".encode()
        hashes[rel] = hashlib.sha256(body).hexdigest()
        bodies[L.RAW_REF_URL_FMT.format(ref=commit, path=rel)] = body
    manifest = json.dumps({"version": version, "commit": commit,
                           "files": hashes}).encode()
    urls = []

    def fetch(url, timeout=15):
        urls.append(url)
        if url == L.MANIFEST_URL:
            return manifest
        if url == L.MANIFEST_SIG_URL:
            return b"sig"
        return bodies.get(url)

    monkeypatch.setattr(L, "_fetch", fetch)
    monkeypatch.setattr(L, "_verify_manifest_signature", lambda m, s: True)
    return urls


def _launcher(monkeypatch, tmp_path, build=476):
    L = _load_launcher()
    posts = []
    monkeypatch.setattr(L, "_post_slack", lambda text: posts.append(text))
    monkeypatch.setattr(L.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(L, "_bundled_build", lambda: build)
    return L, posts


def test_a_manifest_with_files_this_exe_lacks_is_refused_with_the_reason(
        tmp_path, monkeypatch):
    """The boss's case: build 476, manifest 658 with files 476 never heard of.
    Refused, but now with the reason where the hub and the channel can see
    it — and nothing downloaded for an engine this exe cannot run."""
    L, posts = _launcher(monkeypatch, tmp_path)
    urls = _serve(L, monkeypatch, 658, list(L.ENGINE_FILES) + [NEW_FILE])
    assert L._try_auto_update(tmp_path / "staging") is None
    reason = os.environ[NEEDS_ENV]
    assert "update 658" in reason and "fresh install" in reason, reason
    assert "476" in reason and NEW_FILE in reason, reason
    assert urls == [L.MANIFEST_URL, L.MANIFEST_SIG_URL], (
        "no engine file may be fetched for a manifest this exe cannot carry")
    assert not (tmp_path / "staging").exists()
    assert len(posts) == 1 and reason in posts[0], posts


def test_a_manifest_that_drops_a_file_this_exe_runs_is_refused_too(
        tmp_path, monkeypatch):
    L, posts = _launcher(monkeypatch, tmp_path)
    _serve(L, monkeypatch, 658, list(L.ENGINE_FILES)[:-1])
    assert L._try_auto_update(tmp_path / "staging") is None
    assert "drops 1" in os.environ[NEEDS_ENV], os.environ[NEEDS_ENV]
    assert len(posts) == 1


def test_a_manifest_this_exe_can_carry_is_untouched_by_the_check(
        tmp_path, monkeypatch):
    L, posts = _launcher(monkeypatch, tmp_path)
    _serve(L, monkeypatch, 658, list(L.ENGINE_FILES))
    manifest = L._try_auto_update(tmp_path / "staging")
    assert manifest is not None and manifest["__version_int"] == 658
    assert NEEDS_ENV not in os.environ
    assert posts == []


def test_the_install_post_is_once_per_exe_build(tmp_path, monkeypatch):
    """Every main build after 658 carries the same 34 files, so a build-476
    machine refuses every one of them.  One post per machine until it is
    reinstalled, not one per build — and once more if it happens again."""
    L, posts = _launcher(monkeypatch, tmp_path)
    L._report_install_needed("update 658 needs a fresh install")
    L._report_install_needed("update 659 needs a fresh install")
    L._report_install_needed("update 660 needs a fresh install")
    assert len(posts) == 1, posts
    monkeypatch.setattr(L, "_bundled_build", lambda: 660)   # the tech installed
    L._report_install_needed("update 700 needs a fresh install")
    assert len(posts) == 2, posts


def test_the_install_post_is_not_filed_as_an_error(tmp_path, monkeypatch):
    """It is the rollout list, not a bug: the Slack→issues bridge must not
    turn twenty techs on an old exe into twenty phantom issues."""
    L, posts = _launcher(monkeypatch, tmp_path)
    L._report_install_needed("update 658 needs a fresh install (app build 476)")
    (text,) = posts
    assert not BRIDGE_HDR_RE.search(text), text
    assert "needs a fresh install" in text and "app build 476" in text, text


def test_the_launcher_publishes_its_file_list_for_the_hub(monkeypatch):
    L = _load_launcher()
    monkeypatch.setenv("OTDR_SUITE_NO_UPDATE", "1")      # the shortest path through
    L._prepare_engine()
    assert json.loads(os.environ[FILES_ENV]) == list(L.ENGINE_FILES)


# ═════════════════════════════════════════════════════════════════════════
#  the two sides agree
# ═════════════════════════════════════════════════════════════════════════

def test_the_app_and_the_launcher_agree_on_the_variables():
    L = _load_launcher()
    assert _app_constant("_NEEDS_INSTALL_ENV") == L.NEEDS_INSTALL_ENV == NEEDS_ENV
    assert _app_constant("_ENGINE_FILES_ENV") == L.ENGINE_FILES_ENV == FILES_ENV


def test_the_app_and_the_launcher_agree_on_what_needs_an_install():
    """The hub predicts the launcher's verdict off the same two lists.  If
    the rules ever drift, the banner says 'restart' for a manifest the
    launcher refuses (today's bug) or 'install' for one it would take."""
    L = _load_launcher()
    refuse = _load_app_helper("_launcher_would_refuse")
    exe = list(L.ENGINE_FILES)
    for files in (exe, exe + [NEW_FILE], exe[:-1]):
        assert bool(L._install_needed(658, files)) == refuse(files, exe), files
    assert refuse(exe + [NEW_FILE], []) is False, "no published list → fail open"
    assert refuse([], exe) is False, "no manifest list → fail open"


def test_a_restart_works_the_install_state_out_afresh():
    """Both are launcher-derived: the relaunched exe must not inherit a
    verdict that a reinstall in between may have cleared."""
    derived = _app_constant("_LAUNCHER_DERIVED_ENV")
    assert NEEDS_ENV in derived and FILES_ENV in derived


# ═════════════════════════════════════════════════════════════════════════
#  the hub
# ═════════════════════════════════════════════════════════════════════════

def _needs(st):
    return _load_app_helper(
        "_needs_install", st=st, os=os, json=json,
        _NEEDS_INSTALL_ENV=NEEDS_ENV, _ENGINE_FILES_ENV=FILES_ENV,
        _launcher_would_refuse=_load_app_helper("_launcher_would_refuse"))


def test_the_launcher_verdict_alone_is_enough(monkeypatch):
    st = _FakeSt()
    monkeypatch.setenv(NEEDS_ENV, "update 658 needs a fresh install (app build 476)")
    assert "658" in _needs(st)()


def test_the_manifest_file_list_is_enough_before_any_restart(monkeypatch):
    """The banner can say 'install' the moment the manifest is read, without
    sending the tech through a restart that changes nothing."""
    st = _FakeSt()
    monkeypatch.setenv(FILES_ENV, json.dumps(["app.py"]))
    st.session_state["upd_manifest_files"] = ["app.py", NEW_FILE]
    assert _needs(st)()
    st.session_state["upd_manifest_files"] = ["app.py"]
    assert _needs(st)() == ""


def test_it_fails_open_when_either_list_is_unknown(monkeypatch):
    """An exe whose launcher predates this (no published list), a garbled
    list, or no manifest read yet: the restart stays on offer, and the
    launcher gives the real answer at the next boot."""
    st = _FakeSt()
    st.session_state["upd_manifest_files"] = ["app.py", NEW_FILE]
    monkeypatch.delenv(FILES_ENV, raising=False)
    assert _needs(st)() == ""
    monkeypatch.setenv(FILES_ENV, "not json")
    assert _needs(st)() == ""
    monkeypatch.setenv(FILES_ENV, json.dumps(["app.py"]))
    st.session_state.pop("upd_manifest_files")
    assert _needs(st)() == ""


def test_the_nudge_says_install_and_offers_no_restart(tmp_path):
    st = _FakeSt()
    shown = []
    nudge = _load_app_helper(
        "_render_update_nudge", st=st, os=os, sys=sys,
        _restart_marker_path=lambda: str(tmp_path / "no-such-marker"),
        _update_state=lambda: (658, 657), _cache_pinned=lambda: "",
        _needs_install=lambda: "reason",
        _render_install_notice=lambda latest, running, sidebar=False:
            shown.append((latest, running)),
        _relaunch_and_exit=lambda: True, _render_restart_watchdog=lambda: None)
    nudge()
    assert shown == [(658, 657)]
    assert not any(c[0] == "button" for c in st.calls), st.calls
    assert not any("is available" in str(c[1]) for c in st.calls), st.calls


def test_the_notice_names_the_installer_and_no_jargon():
    st = _FakeSt()
    notice = _load_app_helper("_render_install_notice", st=st,
                              INSTALLER_URL=_app_constant("INSTALLER_URL"))
    notice(658, 657)
    (kind, text), = st.calls
    assert kind == "warning"
    assert "658" in text and "657" in text and "OTDRSuite-Setup.exe" in text
    assert "install" in text.lower()
    for jargon in ("cache", "launcher", "manifest", "—"):
        assert jargon not in text, f"{jargon!r} is not for a tech to read"


def test_the_gate_blocks_with_the_install_instruction_and_no_restart_button():
    """Still blocked — a different engine prints different numbers — but the
    way out is the one that works, and the button that does not is gone,
    even on a frozen build."""
    st = _FakeSt()
    gate = _load_app_helper(
        "_report_gate", st=st, sys=types.SimpleNamespace(frozen=True),
        _update_state=lambda: (658, 657), _needs_install=lambda: "reason",
        STALE_BLOCK_MSG=_app_constant("STALE_BLOCK_MSG"),
        INSTALL_BLOCK_MSG=_app_constant("INSTALL_BLOCK_MSG"),
        INSTALLER_URL=_app_constant("INSTALLER_URL"),
        _relaunch_and_exit=lambda: True, _render_restart_watchdog=lambda: None)
    assert gate("sr") == (658, 657)
    (text,) = [c[1] for c in st.calls if c[0] == "error"]
    assert "fresh install" in text and "OTDRSuite-Setup.exe" in text, text
    assert "engine 657" in text and "engine 658" in text, text
    assert "Nothing is lost" in text, text
    assert not any(c[0] == "button" for c in st.calls), st.calls


# ═════════════════════════════════════════════════════════════════════════
#  end to end — what the boss would have seen
# ═════════════════════════════════════════════════════════════════════════

def _fake_manifest(version, files):
    body = json.dumps({"version": version, "commit": "abc1234",
                       "files": {f: "0" * 64 for f in files}}).encode()

    class _Resp:
        def read(self):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return lambda req, timeout=None, **kw: _Resp()


def _arm(monkeypatch, tmp_path, urlopen, exe_files):
    """Build 476 with engine 657 applied, frozen, an isolated home, and the
    launcher's published file list set to `exe_files`."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    drive, tail = os.path.splitdrive(str(tmp_path))
    monkeypatch.setenv("HOMEDRIVE", drive)
    monkeypatch.setenv("HOMEPATH", tail)
    monkeypatch.delenv("SS_ERROR_WEBHOOK", raising=False)
    monkeypatch.setattr(R, "version_labels", lambda *a, **k: (
        "build 476 (2026-09-15)", "update 657 applied 2026-09-15 13:18 PDT"))
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv(FILES_ENV, json.dumps(exe_files))


def _old_exe(L):
    """The 21-file list a build-476 launcher carries."""
    return [f for f in L.ENGINE_FILES if not f.startswith("fqa/")]


def test_apptest_the_boss_sees_install_not_restart(monkeypatch, tmp_path):
    L = _load_launcher()
    _arm(monkeypatch, tmp_path, _fake_manifest(658, L.ENGINE_FILES), _old_exe(L))
    at = run_streamlit().run()
    assert not at.exception, f"page raised: {list(at.exception)}"
    warnings = [w.value for w in at.sidebar.warning]
    assert any("Update 658 needs a fresh install (running 657)" in w
               and "OTDRSuite-Setup.exe" in w for w in warnings), warnings
    assert not any("is available" in w for w in warnings), warnings
    assert not any(b.key == "upd_nudge_restart" for b in at.sidebar.button), (
        [b.key for b in at.sidebar.button])


def test_apptest_a_manifest_this_exe_can_carry_still_offers_the_restart(
        monkeypatch, tmp_path):
    L = _load_launcher()
    _arm(monkeypatch, tmp_path, _fake_manifest(658, L.ENGINE_FILES),
         list(L.ENGINE_FILES))
    at = run_streamlit().run()
    assert not at.exception, f"page raised: {list(at.exception)}"
    warnings = [w.value for w in at.sidebar.warning]
    assert any("Update 658 is available (running 657)" in w for w in warnings), warnings
    assert any(b.key == "upd_nudge_restart" for b in at.sidebar.button)


def test_apptest_the_report_block_says_install(monkeypatch, tmp_path):
    L = _load_launcher()
    _arm(monkeypatch, tmp_path, _fake_manifest(658, L.ENGINE_FILES), _old_exe(L))
    at = run_streamlit().run()
    at.session_state["view_dir_a_input"] = str(FIXTURE_SPLICE_A_DIR)
    at.session_state["view_dir_b_input"] = str(FIXTURE_SPLICE_B_DIR)
    at.sidebar.radio[0].set_value("Splice Report").run()
    assert not at.exception, f"page raised: {list(at.exception)}"
    text = " ".join(e.value for e in at.error)
    assert "needs a fresh install" in text and "OTDRSuite-Setup.exe" in text, text
    assert "engine 657" in text and "engine 658" in text, text
    generate = next(b for b in at.button if b.label == "Generate Splice Report")
    assert generate.disabled is True
    assert not any("Update & restart" in b.label for b in at.button), (
        [b.label for b in at.button])

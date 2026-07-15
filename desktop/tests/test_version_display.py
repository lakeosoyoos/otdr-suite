"""Build-identity (version display) tests.

Every release build must carry a human-readable version so the boss can
confirm a tech runs the latest, and so a field error identifies its build:

  * CI writes version.json at the repo root BEFORE the PyInstaller step
    ({"build": run_number, "date": "<YYYY-MM-DD UTC>", "commit": "<short sha>"}),
    on EVERY ref (not gated to main),
  * both specs bundle it (conditional datas — absent in dev checkouts),
  * error_report.version_labels() turns it + the launcher's update state into
    ('build 54 (2026-07-14)', 'bundled' | 'update 56 applied'), 'dev' fallback,
  * app.py shows it as a sidebar footer caption,
  * every Slack error payload gains an ADDITIVE `build:` line (the existing
    lines stay byte-identical — the Slack→issues bridge parses them),
  * the launcher records the applied manifest version (engine.meta.json) on a
    verified update swap — that file is what the engine label reads.

No network anywhere; the launcher update test uses an ephemeral Ed25519 key
with monkeypatched fetch, mirroring test_autoupdate.py.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re

import pytest

from conftest import REPO_ROOT, run_streamlit
import error_report as R
from test_error_reporting import _capture_slack_text

LAUNCHER = REPO_ROOT / "desktop" / "launcher.py"
SPEC_WIN = REPO_ROOT / "desktop" / "OTDRSuite.spec"
SPEC_MAC = REPO_ROOT / "desktop" / "OTDRSuite-mac.spec"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build-windows.yml"

try:
    import cryptography  # noqa: F401
    HAVE_CRYPTO = True
except Exception:
    HAVE_CRYPTO = False


def _load_launcher():
    spec = importlib.util.spec_from_file_location("otdr_launcher_vd", LAUNCHER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ═════════════════════════════════════════════════════════════════════════
#  1. version_labels — the single source of truth (app + payload share it)
# ═════════════════════════════════════════════════════════════════════════
def test_version_labels_dev_fallback(tmp_path, monkeypatch):
    """No version.json + not launched by the launcher → ('dev', 'dev')."""
    monkeypatch.delenv("OTDR_SUITE_SOURCE", raising=False)
    app, eng = R.version_labels(bundle_dir=str(tmp_path),
                                meta_path=str(tmp_path / "absent.json"))
    assert (app, eng) == ("dev", "dev")


def test_version_labels_reads_version_json(tmp_path, monkeypatch):
    """A CI-shaped version.json produces the human 'build N (date)' label."""
    monkeypatch.delenv("OTDR_SUITE_SOURCE", raising=False)
    (tmp_path / "version.json").write_bytes(
        json.dumps({"build": 54, "date": "2026-07-14",
                    "commit": "9bf652d"}).encode("utf-8"))
    app, _ = R.version_labels(bundle_dir=str(tmp_path))
    assert app == "build 54 (2026-07-14)"


def test_version_labels_malformed_version_json_is_dev(tmp_path, monkeypatch):
    """A corrupt/partial version.json must degrade to 'dev', never raise —
    a bad build stamp must not take error reporting (or the sidebar) down."""
    monkeypatch.delenv("OTDR_SUITE_SOURCE", raising=False)
    (tmp_path / "version.json").write_bytes(b"\x00not json {{{")
    app, _ = R.version_labels(bundle_dir=str(tmp_path),
                              meta_path=str(tmp_path / "absent.json"))
    assert app == "dev"


def test_engine_label_bundled(tmp_path, monkeypatch):
    """Every 'bundled*' launcher source label → engine 'bundled'."""
    for src in ("bundled (auto-update disabled — no signing key)",
                "bundled (offline)", "bundled .exe"):
        monkeypatch.setenv("OTDR_SUITE_SOURCE", src)
        _, eng = R.version_labels(bundle_dir=str(tmp_path))
        assert eng == "bundled", src


def test_engine_label_update_applied(tmp_path, monkeypatch):
    """A cached/latest verified update reads the manifest version the launcher
    recorded in engine.meta.json → 'update N applied'."""
    meta = tmp_path / "engine.meta.json"
    meta.write_bytes(json.dumps({"version": 56, "commit": "abc"}).encode("utf-8"))
    for src in ("latest (verified update v56)", "cached (last verified update)"):
        monkeypatch.setenv("OTDR_SUITE_SOURCE", src)
        _, eng = R.version_labels(bundle_dir=str(tmp_path), meta_path=str(meta))
        assert re.match(r"update 56 applied \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC$", eng), (src, eng)


def test_engine_label_update_without_meta_is_still_flagged(tmp_path, monkeypatch):
    """Updated source but no readable meta → still says an update is applied
    (never silently claims 'bundled' or 'dev' for updated code)."""
    monkeypatch.setenv("OTDR_SUITE_SOURCE", "cached (last verified update)")
    _, eng = R.version_labels(bundle_dir=str(tmp_path),
                              meta_path=str(tmp_path / "absent.json"))
    assert eng == "update applied (version unknown)"


# ═════════════════════════════════════════════════════════════════════════
#  2. Slack error payload — additive build line
# ═════════════════════════════════════════════════════════════════════════
def test_error_payload_contains_build_line(monkeypatch):
    """Every report carries a `build:` line naming app + engine build.  It must
    be a NEW line (the bridge parses the existing ones): the rotating-light
    header, the `*Type*:` line and the `tech: … source: …` line all survive."""
    monkeypatch.delenv("OTDR_SUITE_SOURCE", raising=False)
    text = _capture_slack_text(monkeypatch, "unit — version", ValueError("boom"))
    assert "\nbuild: app dev  |  engine dev\n" in text
    # Backward-compat: the pre-existing lines are still intact.
    assert ":rotating_light: *OTDR Suite error* — unit — version" in text
    assert "*ValueError*: boom" in text
    assert re.search(r"\ntech: `.*`  \|  os: .*  \|  source: dev", text)


def test_error_payload_build_line_uses_version_labels(monkeypatch):
    """The payload's build line reflects version_labels() (a real build would
    show its CI stamp + engine state here)."""
    monkeypatch.setattr(
        R, "version_labels",
        lambda *a, **k: ("build 54 (2026-07-14)", "update 56 applied"))
    text = _capture_slack_text(monkeypatch, "unit — version 2", ValueError("boom"))
    assert "\nbuild: app build 54 (2026-07-14)  |  engine update 56 applied\n" in text


def test_report_survives_broken_version_labels(monkeypatch):
    """A blown-up version helper must never kill a report (never-raise rule)."""
    def _boom(*a, **k):
        raise RuntimeError("version lookup exploded")
    monkeypatch.setattr(R, "version_labels", _boom)
    text = _capture_slack_text(monkeypatch, "unit — version 3", ValueError("boom"))
    assert "\nbuild: unknown\n" in text


# ═════════════════════════════════════════════════════════════════════════
#  3. Launcher records the applied manifest version (engine.meta.json)
# ═════════════════════════════════════════════════════════════════════════
@pytest.mark.skipif(not HAVE_CRYPTO, reason="cryptography not installed locally")
def test_launcher_records_applied_update_version(monkeypatch, tmp_path):
    """A verified signed update swap must persist the applied manifest version
    to <home>/.otdrSuite/engine.meta.json — that file is what the app's
    'engine: update N applied' label (and anti-rollback) read.  Mirrors the
    test_autoupdate.py fixture pattern: ephemeral Ed25519 key, monkeypatched
    fetch, NO network."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    L = _load_launcher()
    priv = Ed25519PrivateKey.generate()
    raw_pub = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    monkeypatch.setattr(L, "UPDATE_PUBLIC_KEY_HEX", raw_pub.hex())
    monkeypatch.delenv("OTDR_SUITE_NO_UPDATE", raising=False)
    monkeypatch.setattr(L.Path, "home", staticmethod(lambda: tmp_path))

    files = {rel: hashlib.sha256((REPO_ROOT / rel).read_bytes()).hexdigest()
             for rel in L.ENGINE_FILES}
    manifest = {"version": 9, "commit": "abc1234", "files": files}
    mbytes = json.dumps(manifest).encode()
    sig = priv.sign(mbytes)

    def good_fetch(url, timeout=15):
        if url == L.MANIFEST_URL:
            return mbytes
        if url == L.MANIFEST_SIG_URL:
            return sig
        for rel in L.ENGINE_FILES:
            if url.endswith(rel):
                return (REPO_ROOT / rel).read_bytes()
        return None

    monkeypatch.setattr(L, "_fetch", good_fetch)
    engine_dir, label = L._prepare_engine()

    assert engine_dir == tmp_path / ".otdrSuite" / "engine"
    assert "v9" in label, label
    meta = tmp_path / ".otdrSuite" / "engine.meta.json"
    assert meta.is_file(), "verified swap must record engine.meta.json"
    recorded = json.loads(meta.read_bytes().decode("utf-8"))
    assert recorded["version"] == 9
    assert recorded["commit"] == "abc1234"

    # And the shared label helper turns exactly that file into the UI string.
    monkeypatch.setenv("OTDR_SUITE_SOURCE", label)
    _, eng = R.version_labels(bundle_dir=str(tmp_path), meta_path=str(meta))
    assert re.match(r"update 9 applied \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC$", eng), eng


# ═════════════════════════════════════════════════════════════════════════
#  4. Source-locks — specs bundle the stamp; CI writes it before the build
# ═════════════════════════════════════════════════════════════════════════
def test_specs_bundle_version_json():
    """Both specs must carry the conditional version.json datas entry (same
    tolerate-absence pattern as _webhook.cfg — dev checkouts have no stamp)."""
    for spec in (SPEC_WIN, SPEC_MAC):
        text = spec.read_text(encoding="utf-8")
        assert 'os.path.join(REPO_ROOT, "version.json")' in text, (
            f"{spec.name} must reference the repo-root version.json")
        m = re.search(
            r'_version = os\.path\.join\(REPO_ROOT, "version\.json"\)\s*\n'
            r'if os\.path\.exists\(_version\):\s*\n'
            r'\s+datas \+= \[\(_version, "\."\)\]', text)
        assert m, (f"{spec.name} must bundle version.json CONDITIONALLY "
                   f"(datas += [(_version, '.')] guarded by os.path.exists)")


def test_ci_writes_version_stamp_before_pyinstaller_ungated():
    """CI must write version.json BEFORE the PyInstaller step, using the run
    number + sha, and the step must NOT be gated to main (branch builds get a
    stamp too)."""
    ci = CI_WORKFLOW.read_text(encoding="utf-8")
    low = ci.lower()
    assert "version.json" in low, "CI must write version.json"
    assert low.find("version.json") < low.find("pyinstaller"), (
        "the version stamp must be written before the pyinstaller build")
    # The step block itself: from its name to the next step — no `if:` gate.
    m = re.search(r"- name: Write version\.json.*?(?=\n      - name:|\Z)",
                  ci, re.DOTALL)
    assert m, "CI must have a 'Write version.json' step"
    step = m.group(0)
    assert "if:" not in step, "the version stamp must run on every ref, not just main"
    assert "GITHUB_RUN_NUMBER" in step, "stamp must carry the run number (the build no.)"
    assert "GITHUB_SHA" in step, "stamp must carry the commit sha"


def test_version_json_is_gitignored():
    """The stamp is CI-written build output; a committed copy would masquerade
    as a real build's identity in every dev run."""
    gi = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert re.search(r"^version\.json$", gi, re.MULTILINE), (
        ".gitignore must ignore the CI-written version.json")


# ═════════════════════════════════════════════════════════════════════════
#  5. Sidebar footer (AppTest) — dev run shows the dev identity
# ═════════════════════════════════════════════════════════════════════════
def test_sidebar_footer_shows_build_identity_in_dev(monkeypatch):
    """The hub renders the build-identity footer in the sidebar.  A dev checkout
    (no version.json, no launcher) collapses to 'OTDR Suite · dev'."""
    monkeypatch.delenv("OTDR_SUITE_SOURCE", raising=False)
    at = run_streamlit().run()
    assert not at.exception, f"page raised: {list(at.exception)}"
    caps = [c.value for c in at.sidebar.caption]
    assert any(v.strip() == "OTDR Suite · dev" for v in caps), (
        f"sidebar footer missing; captions were: {caps}")

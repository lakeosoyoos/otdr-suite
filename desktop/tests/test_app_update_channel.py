"""OTDR App updates itself from its own release, never from main's.

Robert, 2026-09-29: push updates to the App without sending the boss a new
installer each time, from a separate installer link.  The App reads release
tag app-build (installer + signed manifest), which CI fills only from the
app-release branch, and takes only a manifest marked channel "app".  The
Sample Span's real photos never ride in that public installer: a .zip sent
privately is added once from the Home screen.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import time
import zipfile
from pathlib import Path

import pytest

from conftest import REPO_ROOT, run_streamlit

DESKTOP = REPO_ROOT / "desktop"
LAUNCHER = DESKTOP / "launcher.py"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build-windows.yml"

try:
    import cryptography  # noqa: F401
    HAVE_CRYPTO = True
except Exception:
    HAVE_CRYPTO = False


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _launcher():
    return _load(LAUNCHER, "otdr_launcher_channel")


# ═════════════════════════════════════════════════════════════════════════
#  the launcher takes the App's manifest and nothing else
# ═════════════════════════════════════════════════════════════════════════

def _signed(L, monkeypatch, manifest):
    """A real Ed25519 key baked in, and `manifest` signed with it."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    priv = Ed25519PrivateKey.generate()
    raw_pub = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    monkeypatch.setattr(L, "UPDATE_PUBLIC_KEY_HEX", raw_pub.hex())
    body = json.dumps(manifest).encode()
    return body, priv.sign(body)


def _serve(L, monkeypatch, body, sig):
    """Serve the feed and the real engine files; record every URL asked for."""
    asked = []

    def fetch(url, timeout=15):
        asked.append(url)
        if url == L.MANIFEST_URL:
            return body
        if url == L.MANIFEST_SIG_URL:
            return sig
        for rel in sorted(L.ENGINE_FILES, key=len, reverse=True):
            if url.endswith("/" + rel):
                return (REPO_ROOT / rel).read_bytes()
        return None

    monkeypatch.setattr(L, "_fetch", fetch)
    return asked


def _manifest(L, **extra):
    files = {rel: hashlib.sha256((REPO_ROOT / rel).read_bytes()).hexdigest()
             for rel in L.ENGINE_FILES}
    return {"version": 9, "commit": "c" * 40, "files": files, **extra}


@pytest.mark.skipif(not HAVE_CRYPTO, reason="cryptography not installed locally")
def test_a_signed_manifest_from_main_is_refused(monkeypatch, tmp_path):
    """main's manifest is signed with the same key and names the same files:
    only the missing channel tells it apart.  Taking it would replace the
    App's screens with main's."""
    L = _launcher()
    body, sig = _signed(L, monkeypatch, _manifest(L))
    asked = _serve(L, monkeypatch, body, sig)
    assert L._try_auto_update(tmp_path / "staging") is None
    assert asked == [L.MANIFEST_URL, L.MANIFEST_SIG_URL], "no engine file may be fetched"


@pytest.mark.skipif(not HAVE_CRYPTO, reason="cryptography not installed locally")
def test_a_manifest_for_another_channel_is_refused(monkeypatch, tmp_path):
    L = _launcher()
    body, sig = _signed(L, monkeypatch, _manifest(L, channel="beta"))
    asked = _serve(L, monkeypatch, body, sig)
    assert L._try_auto_update(tmp_path / "staging") is None
    assert asked == [L.MANIFEST_URL, L.MANIFEST_SIG_URL]


@pytest.mark.skipif(not HAVE_CRYPTO, reason="cryptography not installed locally")
def test_the_apps_own_manifest_is_taken_at_its_commit(monkeypatch, tmp_path):
    L = _launcher()
    body, sig = _signed(L, monkeypatch, _manifest(L, channel="app"))
    asked = _serve(L, monkeypatch, body, sig)
    got = L._try_auto_update(tmp_path / "staging")
    assert got is not None and got["__version_int"] == 9
    assert (tmp_path / "staging" / "app.py").read_bytes() == (REPO_ROOT / "app.py").read_bytes()
    engine = [u for u in asked if u not in (L.MANIFEST_URL, L.MANIFEST_SIG_URL)]
    assert engine and all(f"/{'c' * 40}/" in u for u in engine)


def test_the_feed_is_the_app_release_not_a_branch():
    L = _launcher()
    base = "https://github.com/lakeosoyoos/otdr-suite/releases/download/app-build/"
    assert L.MANIFEST_URL == base + "update_manifest.json"
    assert L.MANIFEST_SIG_URL == base + "update_manifest.json.sig"
    assert L.INSTALLER_URL == base + "OTDRApp-Setup.exe"
    assert L.AUTO_UPDATE is True and L.UPDATE_CHANNEL == "app"


def test_the_launcher_hands_the_hub_its_feed(monkeypatch, tmp_path):
    """The hub's banner and Check for Updates must read the feed this
    launcher applies, and name the App's installer."""
    L = _launcher()
    monkeypatch.setattr(L.Path, "home", staticmethod(lambda: tmp_path))
    for k in ("OTDR_SUITE_MANIFEST_URL", "OTDR_SUITE_UPDATE_CHANNEL",
              "OTDR_SUITE_INSTALLER_URL", "OTDR_SUITE_NO_UPDATE"):
        monkeypatch.delenv(k, raising=False)
    L._export_edition()
    assert os.environ["OTDR_SUITE_MANIFEST_URL"] == L.MANIFEST_URL
    assert os.environ["OTDR_SUITE_UPDATE_CHANNEL"] == "app"
    assert os.environ["OTDR_SUITE_INSTALLER_URL"] == L.INSTALLER_URL
    assert "OTDR_SUITE_NO_UPDATE" not in os.environ, "the App updates now"


# ═════════════════════════════════════════════════════════════════════════
#  the manifest generator
# ═════════════════════════════════════════════════════════════════════════

def test_the_generator_marks_the_channel_only_when_asked():
    M = _load(DESKTOP / "make_update_manifest.py", "otdr_make_manifest_channel")
    plain = json.loads(M.build_manifest(5, "abc"))
    assert "channel" not in plain, "main's manifest bytes must not change"
    app = json.loads(M.build_manifest(5, "abc", "app"))
    assert app["channel"] == "app" and app["files"] == plain["files"]


def test_ci_marks_every_manifest_for_the_app():
    ci = CI_WORKFLOW.read_text(encoding="utf-8")
    step = ci.split("- name: Generate + sign update manifest", 1)[1].split("- name:", 1)[0]
    assert "--channel app" in step


# ═════════════════════════════════════════════════════════════════════════
#  the hub reads the launcher's feed
# ═════════════════════════════════════════════════════════════════════════

class _Resp:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _hub_fetch(monkeypatch, manifest):
    import urllib.request
    asked = []

    def urlopen(req, timeout=None, **k):
        asked.append(req.full_url)
        return _Resp(json.dumps(manifest).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return asked


def test_the_hub_reads_the_feed_the_launcher_names(monkeypatch):
    import app
    url = "https://github.com/lakeosoyoos/otdr-suite/releases/download/app-build/update_manifest.json"
    monkeypatch.setenv("OTDR_SUITE_MANIFEST_URL", url)
    monkeypatch.setenv("OTDR_SUITE_UPDATE_CHANNEL", "app")
    asked = _hub_fetch(monkeypatch, {"version": 12, "channel": "app", "files": {}})
    assert app._latest_manifest()["version"] == 12
    assert asked == [url]


def test_the_hub_ignores_a_manifest_for_another_edition(monkeypatch):
    """The banner must not offer an update the launcher will refuse."""
    import app
    monkeypatch.setenv("OTDR_SUITE_MANIFEST_URL", "https://example.invalid/m.json")
    monkeypatch.setenv("OTDR_SUITE_UPDATE_CHANNEL", "app")
    _hub_fetch(monkeypatch, {"version": 12, "files": {}})
    assert app._latest_manifest() is None


def test_the_regular_hub_still_reads_mains_feed(monkeypatch):
    import app
    monkeypatch.delenv("OTDR_SUITE_MANIFEST_URL", raising=False)
    monkeypatch.delenv("OTDR_SUITE_UPDATE_CHANNEL", raising=False)
    asked = _hub_fetch(monkeypatch, {"version": 7, "files": {}})
    assert app._latest_manifest()["version"] == 7
    assert asked == ["https://raw.githubusercontent.com/lakeosoyoos/otdr-suite/main/"
                     "update_manifest.json"]


def _installer_url():
    """app.py's INSTALLER_URL assignments, run in order on their own (no
    imports in the namespace, as the watchdog tests run that block)."""
    src = (REPO_ROOT / "app.py").read_text(encoding="utf-8")
    ns = {}
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == "INSTALLER_URL" for t in node.targets):
            exec(compile(ast.Module([node], []), "app.py", "exec"), ns)
    return ns["INSTALLER_URL"]


def test_the_install_notices_point_at_the_apps_installer(monkeypatch):
    app_link = "https://github.com/lakeosoyoos/otdr-suite/releases/download/app-build/OTDRApp-Setup.exe"
    monkeypatch.setenv("OTDR_SUITE_INSTALLER_URL", app_link)
    assert _installer_url() == app_link
    monkeypatch.delenv("OTDR_SUITE_INSTALLER_URL")
    assert _installer_url().endswith("/windows-build/OTDRSuite-Setup.exe")


# ═════════════════════════════════════════════════════════════════════════
#  the release publisher (CI)
# ═════════════════════════════════════════════════════════════════════════

class FakeGH:
    """gh release view/create/download/upload against an in-memory release."""

    def __init__(self, assets=None, exists=True, fail_uploads=0):
        self.assets = dict(assets or {})
        self.exists = exists
        self.fail_uploads = fail_uploads
        self.calls = []

    def __call__(self, args, check=True):
        self.calls.append(list(args))
        verb = args[1]
        if verb == "view":
            return (0 if self.exists else 1), ""
        if verb == "create":
            assert "--prerelease" in args and "--latest=false" in args
            self.exists = True
            return 0, ""
        if verb == "download":
            name, d = args[args.index("-p") + 1], args[args.index("-D") + 1]
            if name not in self.assets:
                if check:
                    raise AssertionError(f"download of missing {name}")
                return 1, ""
            Path(d, name).write_bytes(self.assets[name])
            return 0, ""
        if verb == "upload":
            if self.fail_uploads:
                self.fail_uploads -= 1
                return 1, ""
            p = Path(args[3])
            self.assets[p.name] = p.read_bytes()
            return 0, ""
        raise AssertionError(args)

    def uploaded(self):
        return [Path(c[3]).name for c in self.calls if c[1] == "upload"]


def _pub():
    return _load(DESKTOP / "publish_app_release.py", "otdr_publish_app_release")


def _stage(tmp_path, version=40, commit="d" * 40, channel="app"):
    m = {"version": version, "commit": commit, "files": {}}
    if channel is not None:
        m["channel"] = channel
    (tmp_path / "update_manifest.json").write_bytes(json.dumps(m).encode())
    (tmp_path / "update_manifest.json.sig").write_bytes(b"s" * 64)
    inst = tmp_path / "dist" / "OTDRApp-Setup.exe"
    inst.parent.mkdir()
    inst.write_bytes(b"MZ installer")
    return inst


def test_a_new_build_goes_up_installer_first_then_the_feed(tmp_path):
    P = _pub()
    inst = _stage(tmp_path)
    gh = FakeGH(exists=False)
    assert P.publish(tmp_path, 40, "d" * 40, inst, gh=gh, sleep=lambda s: None) == "published"
    assert gh.uploaded() == ["OTDRApp-Setup.exe", "update_manifest.json.sig",
                             "update_manifest.json"]
    assert gh.assets["update_manifest.json"] == (tmp_path / "update_manifest.json").read_bytes()
    assert gh.assets["OTDRApp-Setup.exe"] == b"MZ installer"


def test_a_build_older_than_the_release_stands_down(tmp_path):
    """A later app-release build finished first: nothing is replaced, so the
    installer and the feed stay on the same, newest build."""
    P = _pub()
    inst = _stage(tmp_path, version=40)
    live = json.dumps({"version": 41, "channel": "app"}).encode()
    gh = FakeGH(assets={"update_manifest.json": live})
    assert P.publish(tmp_path, 40, "d" * 40, inst, gh=gh, sleep=lambda s: None) == "superseded"
    assert gh.uploaded() == []


@pytest.mark.parametrize("problem", ["no manifest", "wrong channel", "no channel",
                                     "wrong commit", "wrong version", "no installer"])
def test_the_publisher_fails_loud(tmp_path, problem):
    P = _pub()
    inst = _stage(tmp_path, channel={"wrong channel": "main", "no channel": None}.get(problem, "app"),
                  commit="e" * 40 if problem == "wrong commit" else "d" * 40,
                  version=39 if problem == "wrong version" else 40)
    if problem == "no manifest":
        (tmp_path / "update_manifest.json").unlink()
    if problem == "no installer":
        inst.unlink()
    gh = FakeGH()
    with pytest.raises(P.PublishError):
        P.publish(tmp_path, 40, "d" * 40, inst, gh=gh, sleep=lambda s: None)
    assert gh.uploaded() == []


def test_the_publisher_retries_then_gives_up(tmp_path):
    P = _pub()
    inst = _stage(tmp_path)
    gh = FakeGH(fail_uploads=2)
    assert P.publish(tmp_path, 40, "d" * 40, inst, gh=gh, sleep=lambda s: None) == "published"
    gh = FakeGH(fail_uploads=99)
    with pytest.raises(P.PublishError):
        P.publish(tmp_path, 40, "d" * 40, inst, gh=gh, sleep=lambda s: None)
    assert "update_manifest.json" not in gh.assets, "no feed without its installer"


def test_ci_publishes_the_app_release_from_app_release_only():
    ci = CI_WORKFLOW.read_text(encoding="utf-8")
    names = re.findall(r"^\s+- name: (.+)$", ci, re.MULTILINE)
    step = ci.split("- name: Publish OTDR App release (app-release only)", 1)[1]
    step = step.split("\n      - name:", 1)[0]
    cond = re.search(r"\n\s+if: (.+)", step).group(1).strip()
    assert cond == "github.ref == 'refs/heads/app-release'"
    assert "publish_app_release.py" in step and "OTDRApp-Setup.exe" in step
    i = names.index("Publish OTDR App release (app-release only)")
    assert i > names.index("Generate + sign update manifest")
    assert i > names.index("Verify OTDRApp-Setup.exe signature")
    assert i > names.index("Boot self-test (launch exe, poll /_stcore/health)")
    # Nothing else writes to the App's release tag.
    assert not re.search(r"gh release [^\n]*app-build", ci.replace(step, ""))


# ═════════════════════════════════════════════════════════════════════════
#  the Sample Span's real photos, added once from a .zip
# ═════════════════════════════════════════════════════════════════════════

def _jpeg(shade):
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (32, 24), (shade, 2, 3)).save(b, "JPEG")
    return b.getvalue()


def _photo_zip(names=("A-1.jpg", "A-2.jpg", "Z-1.jpg", "Z-2.jpg"), folder="Sample Photos/",
               body=None):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as zf:
        for i, n in enumerate(names):
            zf.writestr(folder + n, body if body is not None else _jpeg(10 + i))
    return b.getvalue()


@pytest.fixture
def photo_env(tmp_path, monkeypatch):
    """An installed App: its own app folder, a demo/ with only the drawn
    package, and a projects folder of its own."""
    import app
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    monkeypatch.setenv("OTDR_SUITE_APP_DIR", str(app_dir))
    demo = tmp_path / "demo"
    demo.mkdir()
    shutil.copy2(os.path.join(app.DEMO_DIR, "Demo Field Capture.zfc"), demo)
    monkeypatch.setattr(app, "DEMO_DIR", str(demo))
    projects = tmp_path / "projects"
    projects.mkdir()
    return app, app_dir, projects


def test_the_photos_zip_lands_in_the_app_folder(photo_env):
    app, app_dir, projects = photo_env
    assert app.sample_photos_real(str(projects)) is False
    blob = _photo_zip()
    assert app.install_sample_photos(blob) == ""
    got = sorted(p.name for p in (app_dir / "sample_photos").iterdir())
    assert got == ["A-1.jpg", "A-2.jpg", "Z-1.jpg", "Z-2.jpg"]
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        assert (app_dir / "sample_photos" / "Z-2.jpg").read_bytes() == zf.read("Sample Photos/Z-2.jpg")
    assert app.sample_photos_real(str(projects)) is True


@pytest.mark.parametrize("blob,says", [
    (b"not a zip", "not a .zip"),
    (_photo_zip(names=("A-1.jpg", "A-2.jpg", "Z-1.jpg")), "missing Z-2.jpg"),
    (_photo_zip(body=b"GIF89a..."), "not a JPEG"),
])
def test_a_wrong_file_is_refused_and_nothing_changes(photo_env, blob, says):
    app, app_dir, projects = photo_env
    assert says in app.install_sample_photos(blob)
    assert not (app_dir / "sample_photos").exists()


def test_a_path_inside_the_zip_cannot_place_a_file_elsewhere(photo_env, tmp_path):
    app, app_dir, projects = photo_env
    blob = _photo_zip(folder="../../escape/")
    assert app.install_sample_photos(blob) == ""
    assert not (tmp_path / "escape").exists()
    assert sorted(p.name for p in (app_dir / "sample_photos").iterdir()) == \
        ["A-1.jpg", "A-2.jpg", "Z-1.jpg", "Z-2.jpg"]


def test_an_open_sample_span_gets_the_photos_and_keeps_its_history(photo_env):
    """The sample was made with the drawn photos.  Adding the real ones
    rebuilds its capture package in place, with the file's old time, so the
    project's folder scan does not log it as newly found."""
    app, app_dir, projects = photo_env
    work = projects / app.DEMO_NAME
    field = Path(app.work_sub("field", str(work)))
    field.mkdir(parents=True)
    zfc = field / "Demo Field Capture.zfc"
    shutil.copy2(os.path.join(app.DEMO_DIR, "Demo Field Capture.zfc"), zfc)
    old = time.time() - 86400 * 30
    os.utime(zfc, (old, old))
    assert app.sample_photos_real(str(projects)) is False

    assert app.install_sample_photos(_photo_zip()) == ""
    assert app.refresh_sample_capture(str(projects)) is True
    import folder_intake as fi
    sf = fi.share_open(str(zfc), expect="field-capture")
    assert sf.read("photos/A-1-1.jpg") == (app_dir / "sample_photos" / "A-1.jpg").read_bytes()
    assert abs(os.stat(zfc).st_mtime - old) < 2

    # The package itself now tells: an upgrade that lost the app folder's
    # copy (or a private demo build that put them only in the package) still
    # counts as real photos, so the Home box stays away.
    shutil.rmtree(app_dir / "sample_photos")
    assert app.sample_photos_real(str(projects)) is True


def test_from_home_the_sample_span_is_found_through_the_recent_projects(
        photo_env, tmp_path, monkeypatch):
    """The boss's case: his Sample Span was made by the private demo build,
    so its package already holds the real photos, and the Home screen (which
    draws before the projects folder helpers exist) must see that and stay
    quiet."""
    app, app_dir, projects = photo_env
    monkeypatch.setenv("OTDR_SETTINGS_DIR", str(tmp_path / "settings"))
    (tmp_path / "settings").mkdir()
    work = projects / app.DEMO_NAME
    field = Path(app.work_sub("field", str(work)))
    field.mkdir(parents=True)
    zfc = field / "Demo Field Capture.zfc"
    shutil.copy2(os.path.join(app.DEMO_DIR, "Demo Field Capture.zfc"), zfc)
    proj_file = work / (app.DEMO_NAME + ".otdrproj")
    proj_file.write_text("{}", encoding="utf-8")
    assert app.sample_photos_real() is False, "not in the recent list yet"
    app._remember_project(str(proj_file))
    assert app._demo_capture_path() == str(zfc)
    assert app.sample_photos_real() is False, "drawn photos"
    assert app.install_sample_photos(_photo_zip()) == ""
    assert app.refresh_sample_capture() is True
    shutil.rmtree(app_dir / "sample_photos")
    assert app.sample_photos_real() is True


def test_no_sample_span_yet_means_nothing_to_rebuild(photo_env):
    app, app_dir, projects = photo_env
    assert app.refresh_sample_capture(str(projects)) is False


# ═════════════════════════════════════════════════════════════════════════
#  the Home box (AppTest)
# ═════════════════════════════════════════════════════════════════════════

def _home(monkeypatch, tmp_path, edition):
    monkeypatch.setenv("OTDR_TEST_HOME", "1")
    monkeypatch.setenv("OTDR_SETTINGS_DIR", str(tmp_path / "settings"))
    (tmp_path / "settings").mkdir(exist_ok=True)
    monkeypatch.setenv("OTDR_SUITE_APP_DIR", str(tmp_path / "appdir"))
    if edition:
        monkeypatch.setenv("OTDR_SUITE_EDITION", edition)
    at = run_streamlit()
    at.run()
    assert not at.exception, at.exception
    return [e.label for e in at.expander]


def test_the_app_home_offers_the_photos_box_until_they_are_in(monkeypatch, tmp_path):
    assert "Sample Span Photos" in _home(monkeypatch, tmp_path, "OTDR App")
    photos = tmp_path / "appdir" / "sample_photos"
    photos.mkdir(parents=True)
    for n in ("A-1.jpg", "A-2.jpg", "Z-1.jpg", "Z-2.jpg"):
        (photos / n).write_bytes(_jpeg(9))
    assert "Sample Span Photos" not in _home(monkeypatch, tmp_path, "OTDR App")


def test_the_regular_home_has_no_photos_box(monkeypatch, tmp_path):
    assert "Sample Span Photos" not in _home(monkeypatch, tmp_path, "")

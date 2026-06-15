"""
BUILD / PACKAGING contract guards for the OTDR Suite desktop app.
====================================================================
These are STATIC text + filesystem assertions only.  They deliberately do
NOT import the viewer or secretsauce engine modules: viewer/ and
secretsauce/ each ship a DIFFERENT sor_reader324802a.py, and importing both
in one process is exactly the collision the build is engineered around (see
conftest.py and OTDRSuite.spec).  Stdlib + file reads only.

The class of bug guarded here:
  * a green-but-DOA Windows build (toolchain pin drift, sor_reader collision
    leaking into hiddenimports, missing boot self-test), and
  * a runtime port collision with the other desktop apps on this machine.
"""
from __future__ import annotations

import re
from pathlib import Path

from conftest import REPO_ROOT

# ── Paths (all relative to REPO_ROOT from conftest) ──────────────────────
DESKTOP_DIR        = REPO_ROOT / "desktop"
LAUNCHER_PY        = DESKTOP_DIR / "launcher.py"
APP_PY             = REPO_ROOT / "app.py"
SPEC_WIN           = DESKTOP_DIR / "OTDRSuite.spec"
SPEC_MAC           = DESKTOP_DIR / "OTDRSuite-mac.spec"
REQS_DESKTOP       = DESKTOP_DIR / "requirements-desktop.txt"
VIEWER_SOR         = REPO_ROOT / "viewer" / "sor_reader324802a.py"
SECRETSAUCE_SOR    = REPO_ROOT / "secretsauce" / "sor_reader324802a.py"
CI_WORKFLOW        = REPO_ROOT / ".github" / "workflows" / "build-windows.yml"

# Known ports already claimed by the other desktop apps on this machine
# (project-desktop-ports-registry).  The hub MUST avoid every one of them.
SECRET_SAUCE_PORT  = 8501   # Secret Sauce standalone Streamlit app
SPLICE_REPORT_PORT = 8503   # Splice Report app
UNIDIRECTIONAL_PORT = 8505  # Unidirectional app
KNOWN_TAKEN_PORTS  = {SECRET_SAUCE_PORT, SPLICE_REPORT_PORT, UNIDIRECTIONAL_PORT}

HUB_PORT           = 8510   # the hub's reserved port


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


# ═════════════════════════════════════════════════════════════════════════
#  1. PORT UNIQUENESS — the user's explicit requirement
# ═════════════════════════════════════════════════════════════════════════
def test_launcher_port_is_8510():
    """launcher.py must define PORT = 8510 (the reserved hub port)."""
    text = _read(LAUNCHER_PY)
    m = re.search(r"^\s*PORT\s*=\s*(\d+)", text, re.MULTILINE)
    assert m is not None, "launcher.py must define a top-level PORT = <int>"
    assert int(m.group(1)) == HUB_PORT, (
        f"launcher PORT must be {HUB_PORT}, found {m.group(1)}"
    )


def test_launcher_port_does_not_collide_with_other_apps():
    """The hub port must not be any of Secret Sauce / Splice Report /
    Unidirectional's ports — that would be a live runtime collision."""
    text = _read(LAUNCHER_PY)
    m = re.search(r"^\s*PORT\s*=\s*(\d+)", text, re.MULTILINE)
    assert m is not None, "launcher.py must define a top-level PORT = <int>"
    port = int(m.group(1))
    assert port not in KNOWN_TAKEN_PORTS, (
        f"launcher PORT {port} collides with a known taken port "
        f"{sorted(KNOWN_TAKEN_PORTS)}"
    )


# ═════════════════════════════════════════════════════════════════════════
#  2. HUB INTERNAL PORT CONSISTENCY — TRACE_PORT_BASE must not clash
# ═════════════════════════════════════════════════════════════════════════
def test_app_trace_port_base_distinct():
    """app.py's in-process trace server base port (8771) must not equal the
    hub port nor any of the three known app ports."""
    text = _read(APP_PY)
    m = re.search(r"^\s*TRACE_PORT_BASE\s*=\s*(\d+)", text, re.MULTILINE)
    assert m is not None, "app.py must define TRACE_PORT_BASE = <int>"
    base = int(m.group(1))
    assert base == 8771, f"expected TRACE_PORT_BASE == 8771, found {base}"
    assert base != HUB_PORT, "TRACE_PORT_BASE must differ from the hub port"
    assert base not in KNOWN_TAKEN_PORTS, (
        f"TRACE_PORT_BASE {base} collides with a known taken port "
        f"{sorted(KNOWN_TAKEN_PORTS)}"
    )


# ═════════════════════════════════════════════════════════════════════════
#  3. SECRET-SAUCE SUBPROCESS CONTRACT — the two halves must agree
# ═════════════════════════════════════════════════════════════════════════
def test_secretsauce_subprocess_contract():
    """The hub (app.py) shells out to the Secret Sauce engine via the
    --run-secretsauce sentinel; the launcher must dispatch that sentinel.
    Both sides of the contract must be present or the frozen subprocess
    silently does nothing / crashes."""
    app_text = _read(APP_PY)
    launcher_text = _read(LAUNCHER_PY)

    # Hub side: a secretsauce_cmd builder that branches on FROZEN and uses
    # the sentinel when frozen.
    assert "def secretsauce_cmd(" in app_text, (
        "app.py must define a secretsauce_cmd() builder"
    )
    assert "FROZEN" in app_text, "app.py must branch on FROZEN"
    assert "--run-secretsauce" in app_text, (
        "app.py must emit the --run-secretsauce sentinel when frozen"
    )

    # Launcher side: a dispatcher that checks argv for the sentinel.
    assert "--run-secretsauce" in launcher_text, (
        "launcher.py must recognise the --run-secretsauce sentinel"
    )
    assert "_maybe_run_engine" in launcher_text, (
        "launcher.py must have the _maybe_run_engine subprocess dispatcher"
    )
    assert "sys.argv" in launcher_text, (
        "launcher dispatch must inspect sys.argv for the sentinel"
    )


# ═════════════════════════════════════════════════════════════════════════
#  4. SOR_READER ISOLATION CONTRACT — the divergence that forces bundling
# ═════════════════════════════════════════════════════════════════════════
def test_both_sor_readers_exist():
    assert VIEWER_SOR.is_file(), f"missing {VIEWER_SOR}"
    assert SECRETSAUCE_SOR.is_file(), f"missing {SECRETSAUCE_SOR}"


def test_sor_readers_are_divergent():
    """viewer/ and secretsauce/ ship DIFFERENT sor_reader324802a.py copies.
    If these ever converge, the on-disk-data bundling is unnecessary; if a
    refactor accidentally made one import the other this test still catches
    the collision risk by byte-comparing them."""
    vb = VIEWER_SOR.read_bytes()
    sb = SECRETSAUCE_SOR.read_bytes()
    assert vb != sb, (
        "viewer and secretsauce sor_reader324802a.py are byte-identical — "
        "the two-engine divergence the build relies on has vanished"
    )
    # Sizes differ too (cheap sanity signal mirroring the divergence).
    assert len(vb) != len(sb), (
        "sor_reader copies are the same size — expected divergent files"
    )


# ═════════════════════════════════════════════════════════════════════════
#  5. SPEC SAFETY — engine module must NOT appear by name in the spec
# ═════════════════════════════════════════════════════════════════════════
def _strip_comments(text: str) -> str:
    """Drop full-line and trailing `#` comments so we test only live spec
    code.  (The specs legitimately *document* the sor_reader collision in
    their comments; what must never happen is naming the module in code.)"""
    out_lines = []
    for line in text.splitlines():
        # naive but sufficient for these specs: no `#` appears inside any
        # string literal in OTDRSuite*.spec.
        code = line.split("#", 1)[0]
        out_lines.append(code)
    return "\n".join(out_lines)


def test_specs_do_not_name_sor_reader_in_code():
    """Neither spec may reference sor_reader324802a in LIVE CODE: it must be
    bundled as ON-DISK DATA via _add_dir(), never named as a hiddenimport
    (two same-named modules collide in one frozen archive).  Comments that
    document the strategy are fine; code that names the module is not."""
    for spec in (SPEC_WIN, SPEC_MAC):
        code = _strip_comments(_read(spec))
        assert "sor_reader324802a" not in code, (
            f"{spec.name} names sor_reader324802a in live code — it must be "
            f"bundled as data via _add_dir(), not named in the spec"
        )


def test_specs_do_not_hiddenimport_sor_reader():
    """Belt-and-suspenders: sor_reader324802a must never appear in a
    hiddenimports assignment context in either spec."""
    for spec in (SPEC_WIN, SPEC_MAC):
        text = _read(spec)
        for m in re.finditer(r"hiddenimports\s*\+?=.*", text):
            assert "sor_reader324802a" not in m.group(0), (
                f"{spec.name} lists sor_reader324802a in a hiddenimports "
                f"context: {m.group(0)!r}"
            )


def test_specs_bundle_engine_dirs_as_data():
    """Both specs must bundle viewer/ and secretsauce/ as on-disk data."""
    for spec in (SPEC_WIN, SPEC_MAC):
        text = _read(spec)
        assert '_add_dir("viewer")' in text, (
            f"{spec.name} must call _add_dir(\"viewer\")"
        )
        assert '_add_dir("secretsauce")' in text, (
            f"{spec.name} must call _add_dir(\"secretsauce\")"
        )


# ═════════════════════════════════════════════════════════════════════════
#  6. TOOLCHAIN PIN — setuptools must be pinned EXACTLY
# ═════════════════════════════════════════════════════════════════════════
def test_setuptools_pinned_exactly():
    """requirements-desktop.txt must pin setuptools==65.5.1 — the version
    whose pkg_resources keeps the frozen exe from crashing at launch."""
    text = _read(REQS_DESKTOP)
    assert "setuptools==65.5.1" in text, (
        "requirements-desktop.txt must pin setuptools==65.5.1 exactly"
    )


# ═════════════════════════════════════════════════════════════════════════
#  7. CI BOOT SELF-TEST — DOA build can't reach the Release page
# ═════════════════════════════════════════════════════════════════════════
def test_ci_workflow_exists():
    assert CI_WORKFLOW.is_file(), f"missing CI workflow {CI_WORKFLOW}"


def test_ci_runs_on_windows_with_python_311():
    text = _read(CI_WORKFLOW)
    assert "windows-latest" in text, "CI must run on windows-latest"
    assert re.search(r'python-version:\s*["\']?3\.11', text), (
        "CI must use python-version 3.11"
    )


def test_ci_boot_self_test_polls_health():
    """CI must poll the hub health endpoint on the reserved port — this is
    the boot self-test that fails a DOA build before it ships."""
    text = _read(CI_WORKFLOW)
    assert "http://127.0.0.1:8510/_stcore/health" in text, (
        "CI boot self-test must poll http://127.0.0.1:8510/_stcore/health"
    )


def test_ci_invokes_pyinstaller():
    text = _read(CI_WORKFLOW)
    assert re.search(r"pyinstaller", text, re.IGNORECASE), (
        "CI must invoke pyinstaller"
    )


# TODO(parent): CI currently runs `python test_ui.py` (a smoke script) before
# the PyInstaller build, NOT a literal `pytest` invocation.  The desired state
# is a `pytest` step gating the build so this whole contract suite runs in CI
# BEFORE the (expensive) frozen build.  Wire `pytest` into build-windows.yml
# ahead of the "PyInstaller build" step, then flip this to a normal passing
# test.  Until then it is a strict xfail documenting the gap.
import pytest  # noqa: E402  (stdlib-adjacent test dep; no engine import)


def test_ci_runs_pytest_before_pyinstaller():
    """DESIRED: a literal `pytest` invocation must precede the pyinstaller
    build step so this contract suite gates the build."""
    text = _read(CI_WORKFLOW)
    lower = text.lower()
    idx_pytest = lower.find("pytest")
    idx_pyinstaller = lower.find("pyinstaller")
    assert idx_pytest != -1, "CI must invoke pytest"
    assert idx_pyinstaller != -1, "CI must invoke pyinstaller"
    assert idx_pytest < idx_pyinstaller, (
        "pytest must run BEFORE pyinstaller so a failing contract blocks the build"
    )


def test_webhook_cfg_is_gitignored():
    """NON-NEGOTIABLE: the webhook file must be ignored (repo is public; Slack
    auto-revokes leaked webhooks)."""
    gi = _read(REPO_ROOT / ".gitignore")
    assert "_webhook.cfg" in gi, ".gitignore must ignore _webhook.cfg"


def test_no_slack_webhook_url_committed():
    """No real Slack webhook URL may appear in any TRACKED source file."""
    import subprocess
    # Build the needle at runtime so THIS test file doesn't itself contain the
    # full literal (it is a tracked file and would otherwise flag itself).
    needle = "hooks.slack" + ".com/" + "services/"
    out = subprocess.run(["git", "ls-files"], cwd=str(REPO_ROOT),
                         capture_output=True, text=True).stdout.split()
    offenders = []
    for rel in out:
        p = REPO_ROOT / rel
        try:
            if needle in p.read_text(encoding="utf-8", errors="ignore"):
                offenders.append(rel)
        except Exception:
            pass
    assert not offenders, f"Slack webhook URL committed in: {offenders}"


def test_ci_bakes_webhook_before_build():
    """CI must write _webhook.cfg from the SLACK_ERROR_WEBHOOK secret BEFORE the
    pyinstaller bundle step (so the spec can bundle it)."""
    text = _read(CI_WORKFLOW)
    lower = text.lower()
    assert "slack_error_webhook" in lower, "CI must read the SLACK_ERROR_WEBHOOK secret"
    assert "_webhook.cfg" in lower, "CI must write _webhook.cfg"
    assert lower.find("_webhook.cfg") < lower.find("pyinstaller"), (
        "the webhook bake must run before the pyinstaller build"
    )


def test_specs_bundle_error_report_module():
    for spec in (SPEC_WIN, SPEC_MAC):
        assert "error_report.py" in _read(spec), f"{spec.name} must bundle error_report.py"


def test_engine_third_party_imports_are_pinned():
    """Every TOP-LEVEL third-party import in viewer/ and secretsauce/ must be
    pinned in requirements-desktop.txt.  Otherwise the frozen build (or a
    clean Windows machine) hits ModuleNotFoundError at runtime — exactly the
    `scipy` gap that shipped green on macOS (scipy was incidentally present)
    but broke Secret Sauce on the first clean CI run.  Static AST walk, no
    engine import (so no sor_reader collision)."""
    import ast

    engine_dirs = [REPO_ROOT / "viewer", REPO_ROOT / "secretsauce"]
    local = {f.stem for d in engine_dirs for f in d.glob("*.py")}
    stdlib = {
        "os", "sys", "re", "json", "math", "io", "csv", "struct", "base64",
        "zlib", "datetime", "collections", "itertools", "functools", "argparse",
        "shutil", "tempfile", "subprocess", "glob", "warnings", "traceback",
        "typing", "pathlib", "hashlib", "time", "threading", "socket", "http",
        "urllib", "__future__", "abc", "dataclasses", "enum", "random", "copy",
        "decimal", "statistics", "textwrap", "contextlib", "operator", "string",
    }
    reqs = _read(REQS_DESKTOP).lower()

    def pinned(pkg: str) -> bool:
        return re.search(rf"(^|\n)\s*{re.escape(pkg)}\b", reqs) is not None

    missing: dict[str, list[str]] = {}
    for d in engine_dirs:
        for f in d.glob("*.py"):
            tree = ast.parse(_read(f), str(f))
            for node in tree.body:                      # TOP-LEVEL imports only
                roots = []
                if isinstance(node, ast.Import):
                    roots = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    roots = [node.module.split(".")[0]]
                for r in roots:
                    if r in local or r in stdlib:
                        continue
                    if not pinned(r.lower()):
                        missing.setdefault(r.lower(), []).append(f.name)
    assert not missing, (
        f"engine third-party imports not pinned in requirements-desktop.txt: {missing}"
    )


# ═════════════════════════════════════════════════════════════════════════
#  OPTIONAL strict-xfail — desired-but-missing self-update guard
# ═════════════════════════════════════════════════════════════════════════
# TODO(parent): launcher.py has no GitHub auto-update mechanism.  Techs get
# the exe from the permanent "windows-build" Release tag, but the running exe
# can't detect/fetch a newer build.  Desired: a self-update check (poll the
# GitHub Releases API, offer to download).  Flip to a normal test once added.
@pytest.mark.xfail(strict=True, reason="launcher.py has no GitHub auto-update "
                   "mechanism yet (documented TODO)")
def test_launcher_has_auto_update():
    """DESIRED: the launcher should check GitHub Releases for a newer build."""
    text = _read(LAUNCHER_PY).lower()
    assert ("api.github.com" in text or "releases" in text or "auto-update" in text
            or "self_update" in text or "check_for_update" in text), (
        "launcher.py should implement a GitHub auto-update check"
    )


# ═════════════════════════════════════════════════════════════════════════
#  Splice Report integration (page → click cell → jump in Viewer)
# ═════════════════════════════════════════════════════════════════════════
VIEWER_HTML     = REPO_ROOT / "viewer" / "viewer.html"
SPLICE_SOR      = REPO_ROOT / "splicereport" / "sor_reader324802a.py"
SPLICE_RUNNER   = REPO_ROOT / "splicereport" / "run_splicereport.py"
SPLICE_WIN_SPEC = SPEC_WIN
SPLICE_MAC_SPEC = SPEC_MAC
VIEWER_SOR      = REPO_ROOT / "viewer" / "sor_reader324802a.py"


def test_splicereport_engine_isolated():
    """The splice engine ships its own sor_reader copy and a subprocess runner
    (it must never share the viewer's or Secret Sauce's namespace)."""
    assert SPLICE_RUNNER.is_file(), "missing splicereport/run_splicereport.py"
    assert SPLICE_SOR.is_file(), "missing splicereport/sor_reader324802a.py"
    # All three engine sor_reader copies are distinct lineages → must stay isolated.
    v = SPLICE_SOR.read_bytes()
    assert v != VIEWER_SOR.read_bytes(), "splice sor_reader must differ from viewer's"


def test_app_has_splicereport_subprocess_and_nav():
    src = _read(APP_PY)
    assert "def splicereport_cmd" in src, "app.py needs splicereport_cmd"
    assert "--run-splicereport" in src, "app.py must use the --run-splicereport sentinel"
    assert "FROZEN" in src
    # query-param nav handler that turns a grid cell click into a viewer jump.
    assert "def _handle_nav" in src, "app.py needs a _handle_nav query-param handler"
    assert "viewer_target" in src, "app.py must pass a viewer_target into the iframe"
    assert "def page_splice_report" in src


def test_launcher_dispatches_splicereport():
    src = _read(LAUNCHER_PY)
    assert "--run-splicereport" in src, "launcher must dispatch --run-splicereport"


def test_specs_bundle_splicereport():
    for spec in (SPLICE_WIN_SPEC, SPLICE_MAC_SPEC):
        assert '_add_dir("splicereport")' in _read(spec), f"{spec.name} must bundle splicereport/"


def test_viewer_html_reads_deeplink_params():
    src = _read(VIEWER_HTML)
    assert "URLSearchParams" in src, "viewer.html must parse deep-link query params"
    assert "zoomToKm" in src, "viewer.html must zoom to the linked km"

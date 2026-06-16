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
#  Auto-update path present (security details guarded further below)
# ═════════════════════════════════════════════════════════════════════════
def test_launcher_has_auto_update():
    """The launcher has the signed engine-update path (verified-latest → cached
    → bundled), so engine changes land on relaunch without a re-download."""
    text = _read(LAUNCHER_PY)
    assert "raw.githubusercontent.com" in text, "launcher must fetch from raw GitHub"
    assert ("ENGINE_FILES" in text and "_try_auto_update" in text
            and "_prepare_engine" in text), (
        "launcher must have the signed engine-update path"
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


# ═════════════════════════════════════════════════════════════════════════
#  Windows TEXT-ENCODING contract — open()/read_text() must pin encoding
# ═════════════════════════════════════════════════════════════════════════
# The class of bug: a text-mode read with no encoding= defaults to the
# platform encoding.  On macOS that's UTF-8 (everything passes); on a tech's
# Windows box it's cp1252, which crashes on the first non-ASCII byte — vendor
# / location strings or a UTF-8 BOM in an OTDR JSON, the °/µ glyph in any
# file.  Mac CI can NEVER catch it, so guard it statically here.  This is the
# exact failure that took down the first live Windows CI run (viewer.html read
# + three runtime json_reader/report.py reads).
SHIPPING_PY = (
    list((REPO_ROOT / "viewer").glob("*.py"))
    + list((REPO_ROOT / "secretsauce").glob("*.py"))
    + list((REPO_ROOT / "splicereport").glob("*.py"))
    + list((REPO_ROOT / "components").rglob("*.py"))
    + [APP_PY, REPO_ROOT / "error_report.py", LAUNCHER_PY]
)


def _unencoded_text_io(path: Path):
    """Yield 'file:line — snippet' for every builtin open() text read and every
    read_text()/write_text() call in `path` that omits encoding=.  Binary
    open() (mode contains 'b') is exempt; encoding is irrelevant there."""
    import ast

    src = _read(path)
    tree = ast.parse(src, str(path))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        has_encoding = any(k.arg == "encoding" for k in node.keywords)
        func = node.func
        # builtin open(path[, mode, ...])
        if isinstance(func, ast.Name) and func.id == "open":
            mode = None
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                mode = node.args[1].value
            for k in node.keywords:
                if k.arg == "mode" and isinstance(k.value, ast.Constant):
                    mode = k.value.value
            if isinstance(mode, str) and "b" in mode:
                continue                      # binary — encoding N/A
            if not has_encoding:
                offenders.append(f"{path.name}:{node.lineno} — open()")
        # pathlib .read_text()/.write_text() (unambiguously text I/O)
        elif isinstance(func, ast.Attribute) and func.attr in ("read_text", "write_text"):
            if not has_encoding:
                offenders.append(f"{path.name}:{node.lineno} — .{func.attr}()")
    return offenders


def test_no_unencoded_text_io_in_shipping_code():
    """Every text-mode open()/read_text()/write_text() in shipping code must
    pass encoding= explicitly (utf-8 / utf-8-sig) so the app behaves the same
    on a tech's Windows machine as it does on the Mac it was built on."""
    offenders = []
    for p in SHIPPING_PY:
        if p.is_file():
            offenders += _unencoded_text_io(p)
    assert not offenders, (
        "text-mode file I/O without an explicit encoding= (crashes on Windows "
        "cp1252, silent on macOS UTF-8):\n  " + "\n  ".join(offenders)
    )


# ═════════════════════════════════════════════════════════════════════════
#  .trc DECODER — must import the BUNDLED decoder, not a dev-only path
# ═════════════════════════════════════════════════════════════════════════
# The class of bug: trc_parser.py did
#   sys.path.insert(0, os.path.expanduser('~/Desktop/ExfoCrack'))
#   from exfo_proprietary_decoder import decode_all_fields
# '~/Desktop/ExfoCrack' exists only on the dev box, so on EVERY tech machine
# the import raised ModuleNotFoundError and every .trc report died.  The
# bundled secretsauce/exfo_proprietary_decoder.py is the real source and is
# already importable (the runner puts secretsauce/ on sys.path).  Static check
# only — importing the secretsauce engine here would collide with the viewer's
# sor_reader copy (see module docstring).
SECRETSAUCE_DIR_PC = REPO_ROOT / "secretsauce"
TRC_PARSER_PY      = SECRETSAUCE_DIR_PC / "trc_parser.py"
BUNDLED_DECODER_PY = SECRETSAUCE_DIR_PC / "exfo_proprietary_decoder.py"


def test_trc_parser_does_not_reach_for_dev_exfocrack_path():
    """trc_parser.py must NOT insert a '~/Desktop/ExfoCrack' dev path — that
    folder is absent on tech machines, so it broke every .trc report."""
    src = _read(TRC_PARSER_PY)
    assert "ExfoCrack" not in src, (
        "trc_parser.py still references a dev-only '~/Desktop/ExfoCrack' path; "
        "the bundled exfo_proprietary_decoder.py must be imported instead"
    )
    # It must still import the decoder it needs.
    assert "from exfo_proprietary_decoder import decode_all_fields" in src, (
        "trc_parser.py must import decode_all_fields from the bundled decoder"
    )


def test_bundled_decoder_exports_decode_all_fields():
    """The bundled decoder that ships beside trc_parser.py must define the
    symbol trc_parser imports, so the plain import resolves on a tech box."""
    assert BUNDLED_DECODER_PY.is_file(), f"missing {BUNDLED_DECODER_PY}"
    src = _read(BUNDLED_DECODER_PY)
    assert re.search(r"^def decode_all_fields\(", src, re.MULTILINE), (
        "exfo_proprietary_decoder.py must define decode_all_fields()"
    )


# ═════════════════════════════════════════════════════════════════════════
#  INSTALLER — per-user installer that removes the prior version on upgrade
# ═════════════════════════════════════════════════════════════════════════
INNO_ISS = DESKTOP_DIR / "OTDRSuite.iss"


def test_inno_setup_script_present_and_sane():
    """The Inno Setup script must exist and carry the properties that make it a
    clean per-user upgrade: a FIXED AppId (so Inno recognises upgrades), lowest
    privileges (no admin prompt), an [InstallDelete] that clears the install dir
    (so old files don't linger), the right launcher exe + output name, and it
    must source the PyInstaller one-folder output."""
    assert INNO_ISS.is_file(), f"missing {INNO_ISS}"
    iss = _read(INNO_ISS)
    # Fixed AppId GUID — the upgrade key. Must be a concrete GUID, not a macro.
    assert re.search(r"^\s*AppId=\{\{[0-9A-Fa-f-]{36}\}", iss, re.MULTILINE), (
        "OTDRSuite.iss needs a fixed AppId={{<GUID>} so upgrades replace the prior install"
    )
    assert "PrivilegesRequired=lowest" in iss, "installer must be per-user (no admin prompt)"
    assert "[InstallDelete]" in iss and re.search(r'Name:\s*"\{app\}\\\*"', iss), (
        "installer must clear {app}\\* on upgrade so removed files don't linger"
    )
    assert "OTDRSuite.exe" in iss, "installer must reference the launcher exe"
    assert "OutputBaseFilename=OTDRSuite-Setup" in iss, "installer output must be OTDRSuite-Setup.exe"
    assert re.search(r'Source:\s*"dist\\OTDRSuite\\\*"', iss), (
        "installer must bundle the PyInstaller one-folder output dist\\OTDRSuite\\*"
    )


def test_ci_builds_and_publishes_installer():
    """CI must compile the installer (after the boot self-test) and publish the
    Setup.exe to the permanent release."""
    ci = _read(CI_WORKFLOW)
    low = ci.lower()
    assert "innosetup" in low, "CI must install Inno Setup"
    assert "iscc" in low or "inno setup 6" in low, "CI must invoke the Inno compiler (ISCC)"
    assert "OTDRSuite.iss" in ci, "CI must compile OTDRSuite.iss"
    # Installer is wrapped only after the boot self-test passed (no DOA installer).
    assert low.find("boot self-test") < low.find("inno setup"), (
        "the installer must be built AFTER the boot self-test"
    )
    # Setup.exe is published to the permanent release.
    assert "OTDRSuite-Setup.exe" in ci, "CI must publish OTDRSuite-Setup.exe"


def test_ci_publish_is_guarded_to_main():
    """The publish step must be gated to refs/heads/main so a sandbox branch can
    build + boot-test + compile the installer WITHOUT clobbering the live release."""
    ci = _read(CI_WORKFLOW)
    # The publish step carries an `if:` pinning it to main.
    assert re.search(r"if:\s*github\.ref\s*==\s*'refs/heads/main'", ci), (
        "the Publish step must be guarded with if: github.ref == 'refs/heads/main'"
    )


# ═════════════════════════════════════════════════════════════════════════
#  SIGNED AUTO-UPDATE — the anti-RCE contract (static text/file assertions)
# ═════════════════════════════════════════════════════════════════════════
# The class of bug: the launcher used to fetch raw .py from main and run it
# behind a "non-empty + compiles" gate — fleet-wide RCE for anyone who could
# write main, poison a branch, leak a token, or MITM the fetch.  The fix is a
# SIGNED MANIFEST (Ed25519) verified against a baked PUBLIC key, per-file
# SHA-256, anti-rollback, and FAIL-CLOSED-by-default.  These guard that the
# pieces stay in place; the crypto behaviour itself is exercised in
# test_autoupdate.py (skipped when cryptography isn't installed locally).
MAKE_MANIFEST   = DESKTOP_DIR / "make_update_manifest.py"


def test_launcher_verifies_signed_manifest():
    """The launcher must verify an Ed25519-signed manifest before trusting any
    downloaded file — the only acceptable trust gate for remote code."""
    text = _read(LAUNCHER_PY)
    assert "UPDATE_PUBLIC_KEY_HEX" in text, "launcher needs a baked Ed25519 pubkey"
    assert "_verify_manifest_signature" in text, "launcher needs a signature-verify fn"
    assert "Ed25519PublicKey" in text, "launcher must verify with Ed25519"
    assert "sha256" in text, "launcher must check each file's SHA-256 vs the manifest"
    # The old unverified trust gate must be GONE — no 'compiles == trusted'.
    assert "_validate" not in text, (
        "the old compile-only trust gate must be removed (it was the RCE vector)"
    )


def test_launcher_fails_closed_without_key():
    """With the placeholder pubkey the launcher must DISABLE auto-update (no
    fetch), not fall back to the old unverified fetch."""
    text = _read(LAUNCHER_PY)
    assert "REPLACE_WITH_ED25519_PUBLIC_KEY_HEX" in text, (
        "launcher must ship a clear pubkey placeholder (human provisioning step)"
    )
    assert "update_signing_configured" in text, "launcher needs the fail-closed gate"
    assert "fail closed" in text.lower() or "fail-closed" in text.lower(), (
        "the fail-closed behaviour must be documented in the launcher"
    )


def test_launcher_has_anti_rollback_and_atomic_swap():
    text = _read(LAUNCHER_PY)
    assert "anti-rollback" in text.lower(), "launcher must document anti-rollback"
    assert ".prev" in text, "launcher must keep the prior cache as engine.prev"
    assert "version" in text and "<=" in text, (
        "launcher must refuse a non-newer manifest version"
    )


def test_launcher_uses_explicit_tls_context():
    """The HIGH TLS finding: every HTTPS fetch (update + Slack) must pass an
    explicit verifying SSL context (certifi CA bundle).  The localhost health
    poll (plain http://127.0.0.1) is exempt — TLS is meaningless there."""
    text = _read(LAUNCHER_PY)
    assert "ssl.create_default_context" in text, "launcher must build a verifying TLS ctx"
    assert "certifi" in text, "launcher must use certifi's CA bundle for the frozen exe"
    import ast
    tree = ast.parse(text)
    # The only urlopen allowed WITHOUT context= is the localhost health poll,
    # whose URL arg is the HEALTH_URL constant.  Everything else (HTTPS) must
    # pass an explicit verifying context.
    bare = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "urlopen":
            if any(k.arg == "context" for k in node.keywords):
                continue
            first = node.args[0] if node.args else None
            is_localhost = isinstance(first, ast.Name) and first.id == "HEALTH_URL"
            if not is_localhost:
                bare.append(node.lineno)
    assert not bare, (
        f"HTTPS urlopen without an explicit TLS context at lines {bare}"
    )


def test_crypto_and_certifi_pinned_and_bundled():
    reqs = _read(REQS_DESKTOP).lower()
    assert "cryptography" in reqs, "requirements-desktop.txt must pin cryptography"
    assert "certifi" in reqs, "requirements-desktop.txt must pin certifi"
    for spec in (SPEC_WIN, SPEC_MAC):
        s = _read(spec)
        assert "cryptography" in s and "certifi" in s, (
            f"{spec.name} must bundle cryptography + certifi (collect_all)"
        )


def test_ci_generates_and_signs_manifest_before_publish():
    """CI must generate + sign the update manifest (reading the
    OTDR_UPDATE_SIGNING_KEY secret) BEFORE the publish steps, and only after the
    boot self-test (so a manifest never points at a DOA build)."""
    assert MAKE_MANIFEST.is_file(), "missing desktop/make_update_manifest.py"
    ci = _read(CI_WORKFLOW)
    low = ci.lower()
    assert "otdr_update_signing_key" in low, "CI must read the OTDR_UPDATE_SIGNING_KEY secret"
    assert "make_update_manifest.py" in low, "CI must run the manifest generator"
    # Sign after the build/boot test, and before publishing the manifest.
    assert low.find("boot self-test") < low.find("make_update_manifest.py"), (
        "manifest must be signed AFTER the boot self-test"
    )
    assert low.find("make_update_manifest.py") < low.find("publish signed manifest"), (
        "manifest must be generated+signed BEFORE it is published"
    )


def test_ci_manifest_publish_guarded_to_main():
    """The manifest-publish (push to main) must be gated to refs/heads/main so a
    sandbox branch never pushes a manifest."""
    ci = _read(CI_WORKFLOW)
    # Find the 'Publish signed manifest' step and assert it carries the main guard.
    m = re.search(r"Publish signed manifest.*?(?=\n      - name:|\Z)", ci, re.DOTALL)
    assert m, "CI must have a 'Publish signed manifest' step"
    assert re.search(r"if:\s*github\.ref\s*==\s*'refs/heads/main'", m.group(0)), (
        "the manifest-publish step must be guarded with if: github.ref == 'refs/heads/main'"
    )


def test_make_manifest_skips_gracefully_without_key():
    """The generator must SKIP signing (exit 0, no files) when the signing secret
    is absent — a build without the secret still succeeds (fail-closed launcher)."""
    src = _read(MAKE_MANIFEST)
    assert "OTDR_UPDATE_SIGNING_KEY" in src
    assert "skipping" in src.lower() or "skip" in src.lower(), (
        "generator must skip gracefully when the key is unset"
    )


# ═════════════════════════════════════════════════════════════════════════
#  AUDIT MEDIUMS — engine subprocess hardening + per-file/grid sanity guards
# ═════════════════════════════════════════════════════════════════════════
SECRETSAUCE_REPORT_PY = REPO_ROOT / "secretsauce" / "report.py"


def test_hub_engine_runs_are_hardened():
    """FIX 1 — the hub shells out to BOTH engines (Secret Sauce + Splice Report).
    Each run must be hardened so a wedged or non-cp1252 engine can't hang or
    crash the Streamlit page, and a windowed Windows build doesn't flash a
    console:  a timeout, utf-8 + errors=replace decode, and CREATE_NO_WINDOW on
    win32.  The fix factors a shared run_engine() helper, so assert the helper
    carries all three and that both call sites go through it (no bare
    subprocess.run with capture_output that bypasses the hardening)."""
    src = _read(APP_PY)
    # The shared helper exists and carries every guard.
    assert "def run_engine(" in src, "app.py must factor a shared run_engine() helper"
    helper = src[src.index("def run_engine("):]
    helper = helper[:helper.find("\ndef ", 1) if "\ndef " in helper[1:] else len(helper)]
    assert "timeout=" in helper, "run_engine must pass a timeout (wedged engine can't hang the page)"
    assert "encoding='utf-8'" in helper or 'encoding="utf-8"' in helper, (
        "run_engine must decode output as utf-8 (cp1252 default → UnicodeDecodeError)")
    assert "errors='replace'" in helper or 'errors="replace"' in helper, (
        "run_engine must use errors='replace' so odd bytes can't crash the page")
    assert "CREATE_NO_WINDOW" in helper and "win32" in helper, (
        "run_engine must pass CREATE_NO_WINDOW on win32 (no console flash)")
    # Both engine dispatches go through the helper, and BOTH handle TimeoutExpired.
    assert src.count("run_engine(cmd)") >= 2, "both engine call sites must use run_engine()"
    assert src.count("subprocess.TimeoutExpired") >= 2, (
        "both engine call sites must handle subprocess.TimeoutExpired (UI error + report_error)")
    # No bare hardening-bypassing subprocess.run on the captured engine output.
    assert "subprocess.run(cmd, capture_output=True, text=True)" not in src, (
        "engine call sites must not bypass run_engine() with a bare subprocess.run")


def test_secretsauce_trc_batch_is_per_file_guarded():
    """FIX 2 — like the JSON path, the .trc batch loader must skip+continue on a
    malformed file instead of one bad .trc aborting the whole run.  Guard that
    a _load_trc_files() helper exists (mirroring _load_json_files) and that the
    bare `[load_trc_file(p) for p in paths]` comprehensions are gone."""
    src = _read(SECRETSAUCE_REPORT_PY)
    assert "def _load_trc_files(" in src, (
        "report.py must factor a per-file-guarded _load_trc_files() helper")
    helper = src[src.index("def _load_trc_files("):]
    helper = helper[:helper.find("\ndef ", 1)]
    assert "try:" in helper and "except" in helper, "_load_trc_files must per-file try/except"
    assert "continue" not in helper or "skipped" in helper  # skip+warn shape
    assert "file=sys.stderr" in helper, "_load_trc_files must warn skipped files to stderr"
    assert "RuntimeError" in helper, "_load_trc_files must raise only if NOTHING loads"
    # The fragile bare comprehension must no longer be the batch loader.
    assert "[load_trc_file(p) for p in paths]" not in src, (
        "TRC batch must go through _load_trc_files(), not a bare comprehension that "
        "aborts the whole batch on one malformed .trc")


def test_splicereport_warns_on_skewed_fiber_numbers():
    """FIX 3 — n_fibers = max(fa.keys()) lets one stray high fiber-number file
    balloon the ribbon×splice grid silently.  Keep the behavior but warn to
    stderr when the max fiber number greatly exceeds the file count."""
    src = SPLICE_RUNNER.read_text(encoding="utf-8")
    assert "n_fibers = max(fa.keys())" in src, "behavior preserved: still max(fa.keys())"
    # A warning guard keyed on the max-vs-count skew, emitted to stderr.
    assert re.search(r"n_fibers\s*>\s*2\s*\*\s*len\(fa\)", src), (
        "splicereport must warn when max fiber number > 2× the A-side file count")
    warn_region = src[src.index("n_fibers = max(fa.keys())"):]
    assert "file=sys.stderr" in warn_region and "warning" in warn_region.lower(), (
        "the skewed-grid case must print a stderr warning so a mislabeled file surfaces")


def test_launcher_streamlit_import_is_inside_fatal_start_guard():
    """FIX 4 — a missing/broken streamlit is a top frozen-build failure mode.
    `from streamlit.web import cli` must sit INSIDE the try/except that posts the
    fatal-start error to Slack, otherwise its ImportError escapes the handler and
    only lands in the local log."""
    src = _read(LAUNCHER_PY)
    main_src = src[src.index("def main("):]
    imp = main_src.index("from streamlit.web import cli")
    handler = main_src.index("launcher failed to start")   # the fatal-start Slack msg
    assert imp < handler, "the import must precede the fatal-start handler in main()"

    # The 8-space `try:` that opens the block the import lives in: the nearest
    # `\n        try:` at or before the import line.
    try_kw = main_src.rfind("\n        try:", 0, imp)
    assert try_kw != -1, (
        "the streamlit import must be INSIDE an 8-space try: block in main() — "
        "an ImportError above the try escapes the fatal-start Slack handler")
    # That same try's body must reach stcli.main() (so the run is inside it too),
    # and the fatal-start except handler must follow — i.e. the import-to-handler
    # span contains no dedent that would close the guard before the import.
    assert "return stcli.main()" in main_src[imp:handler], (
        "the streamlit run (stcli.main) must be inside the same guarded block")
    # No bare top-level (4-space) `try:` reopens between the import and handler
    # that would mean the import sits above the real guard.
    assert main_src.find("\n    try:", imp, handler) == -1, (
        "the fatal-start guard must already be open at the import line, not "
        "opened after it")

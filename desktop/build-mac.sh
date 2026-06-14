#!/usr/bin/env bash
# =============================================================================
#  OTDR Suite — local macOS build (for de-risking, NOT for shipping)
# =============================================================================
#  Produces dist/OTDRSuite.app and copies it to ~/Desktop/OTDRSuite.app so you
#  can double-click it.  This Mac build flushes OS-independent packaging bugs
#  (the sor_reader isolation, the --run-secretsauce subprocess dispatch, the
#  Streamlit first-run hang) before we burn a Windows CI cycle.  A green Mac
#  build does NOT prove the Windows app launches — the Windows CI boot
#  self-test is the authoritative check for what techs download.
#
#  Uses /usr/bin/python3 (must be < 3.12; we pin setuptools 65.5.1 which needs
#  pkgutil.ImpImporter, removed in 3.12).  Deps install to the user site.
# =============================================================================
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PY="/usr/bin/python3"
[[ -x "$PY" ]] || { echo "[build-mac] ERROR — $PY missing (xcode-select --install)"; exit 1; }
echo "[build-mac] Using: $PY ($($PY --version 2>&1))"
"$PY" -c "import sys; sys.exit(0 if sys.version_info < (3,12) else 1)" || {
    echo "[build-mac] ERROR — Python 3.12+ removed pkgutil.ImpImporter; use <3.12."; exit 1; }

# ── 1. Build deps (user site, idempotent) ────────────────────────────────────
"$PY" -m pip install --user --upgrade pip wheel >/dev/null
"$PY" -m pip install --user -r requirements-desktop.txt
"$PY" -m pip install --user --force-reinstall "setuptools==65.5.1"
export PATH="$("$PY" -m site --user-base)/bin:$PATH"

# ── 2. PyInstaller build ─────────────────────────────────────────────────────
rm -rf build dist
"$PY" -m PyInstaller OTDRSuite-mac.spec --noconfirm --clean
[[ -d "dist/OTDRSuite.app" ]] || { echo "[build-mac] ERROR — dist/OTDRSuite.app missing."; exit 1; }

# ── 3. Boot self-test — launch the .app exe, poll health, kill ───────────────
EXE="dist/OTDRSuite.app/Contents/MacOS/OTDRSuite"
echo "[build-mac] Boot self-test: launching $EXE ..."
"$EXE" >/tmp/otdrsuite_boot.out 2>&1 &
BOOT_PID=$!
OK=0
for i in $(seq 1 60); do
    if ! kill -0 "$BOOT_PID" 2>/dev/null; then echo "[build-mac] process exited early"; break; fi
    if curl -fs "http://127.0.0.1:8510/_stcore/health" 2>/dev/null | grep -q "ok"; then
        OK=1; echo "[build-mac] health=ok after ~$((i*2))s"; break
    fi
    sleep 2
done
kill "$BOOT_PID" 2>/dev/null || true
pkill -f "OTDRSuite.app/Contents/MacOS/OTDRSuite" 2>/dev/null || true
if [[ "$OK" -ne 1 ]]; then
    echo "[build-mac] BOOT TEST FAILED — no health=ok in 120s. Log:"; echo "----"
    cat "$HOME/.otdrSuite/otdrsuite.log" 2>/dev/null | tail -40
    echo "---- boot stdout ----"; tail -40 /tmp/otdrsuite_boot.out 2>/dev/null
    exit 1
fi
echo "[build-mac] BOOT TEST PASSED."

# ── 4. Refresh the .app on the Desktop ───────────────────────────────────────
DEST="$HOME/Desktop/OTDRSuite.app"
rm -rf "$DEST"; cp -R "dist/OTDRSuite.app" "$DEST"
xattr -dr com.apple.quarantine "$DEST" 2>/dev/null || true

echo "[build-mac] ============================================================"
echo "[build-mac]  Build OK + boot-tested.   $DEST"
echo "[build-mac]  (Local de-risk only — techs get the Windows CI build.)"
echo "[build-mac] ============================================================"

@echo off
REM ===========================================================================
REM  OTDR Suite — Windows one-click build  (run on a clean Windows machine)
REM ===========================================================================
REM  Requires Python 3.11 (NOT 3.12+).  Produces:
REM     dist\OTDRSuite\OTDRSuite.exe   + dist\OTDRSuite-Windows.zip
REM  Includes a BOOT SELF-TEST (step 6): a green PyInstaller build that does
REM  not actually launch is treated as a FAILED build.
REM ===========================================================================
setlocal ENABLEDELAYEDEXPANSION
cd /d "%~dp0"

REM ── 1. Confirm Python 3.11 ────────────────────────────────────────────────
set "PY=py -3.11"
%PY% --version >nul 2>&1
if errorlevel 1 (
    set "PY=python"
    python --version 2>nul | findstr /R "3\.11\." >nul
    if errorlevel 1 (
        echo [build] ERROR — Python 3.11 not found. Install it from python.org.
        echo         3.12+ compiles green but the exe crashes at launch.
        exit /b 1
    )
)
echo [build] Using: %PY%
%PY% --version

REM ── 2. Fresh venv ─────────────────────────────────────────────────────────
if exist .venv rmdir /s /q .venv
%PY% -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip wheel

REM ── 3. Deps + re-pin setuptools LAST ──────────────────────────────────────
pip install -r requirements-desktop.txt
pip install --force-reinstall setuptools==65.5.1

REM ── 4. PyInstaller build ──────────────────────────────────────────────────
if exist build rmdir /s /q build
if exist dist  rmdir /s /q dist
pyinstaller OTDRSuite.spec --noconfirm --clean
if errorlevel 1 (
    echo [build] ERROR — PyInstaller failed.
    exit /b 1
)

REM ── 5. Boot self-test — launch the exe, poll health, kill it ──────────────
echo [build] Boot self-test: launching exe and polling /_stcore/health ...
start "" "dist\OTDRSuite\OTDRSuite.exe"
set "BOOT_OK="
powershell -NoProfile -Command ^
  "$d=(Get-Date).AddSeconds(120); while((Get-Date) -lt $d){ try{ $r=Invoke-WebRequest -Uri 'http://127.0.0.1:8510/_stcore/health' -TimeoutSec 2 -UseBasicParsing; if($r.Content.Trim() -eq 'ok'){ exit 0 } }catch{}; Start-Sleep -Milliseconds 750 }; exit 1"
if errorlevel 1 (
    echo [build] BOOT TEST FAILED — exe did not serve health=ok within 120s.
    echo         Check %%USERPROFILE%%\.otdrSuite\otdrsuite.log for the traceback.
    taskkill /IM OTDRSuite.exe /F >nul 2>&1
    exit /b 1
)
echo [build] Boot test PASSED — health=ok.
taskkill /IM OTDRSuite.exe /F >nul 2>&1

REM ── 6. Zip the dist folder ────────────────────────────────────────────────
powershell -NoProfile -Command ^
    "Compress-Archive -Path 'dist\OTDRSuite\*' -DestinationPath 'dist\OTDRSuite-Windows.zip' -Force"

echo.
echo [build] ===========================================================
echo [build]  Build OK and boot-tested.
echo [build]    EXE : %CD%\dist\OTDRSuite\OTDRSuite.exe
echo [build]    ZIP : %CD%\dist\OTDRSuite-Windows.zip
echo [build] ===========================================================
endlocal

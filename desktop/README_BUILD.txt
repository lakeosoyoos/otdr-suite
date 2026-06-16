OTDR Suite — Windows desktop build
==================================

WHAT THIS PRODUCES
  A self-contained Windows app a tech can run on a clean machine with NO
  Python installed:
      dist\OTDRSuite\OTDRSuite.exe        (double-click to launch)
      dist\OTDRSuite-Windows.zip          (what you hand to the tech)
  Double-clicking the exe starts a local server and opens the default
  browser to http://127.0.0.1:8510 — the OTDR Suite hub (Viewer +
  Duplicate Check).  Nothing leaves the machine; no internet needed.

HOW TO BUILD  (must be done ON Windows — PyInstaller can't cross-build
              from macOS/Linux)
  1. Install Python 3.11 from python.org.  NOT 3.12+ (see the toolchain
     note in OTDRSuite.spec — 3.12 removed pkgutil.ImpImporter and the exe
     crashes at launch).
  2. Open a Command Prompt in this desktop\ folder.
  3. Run:  build.bat
     It makes a fresh venv, installs requirements-desktop.txt, re-pins
     setuptools==65.5.1 LAST, runs PyInstaller, then BOOT-TESTS the exe
     (launches it and waits for /_stcore/health = ok).  A build that
     compiles but won't launch FAILS here — it will not produce a zip.

ARCHITECTURE NOTES (why the build is shaped this way)
  * ONE exe, two roles.  Normal double-click boots the Streamlit hub.
    The hub runs Secret Sauce in a clean subprocess by re-invoking the
    same exe with `--run-secretsauce` (in a frozen build sys.executable
    IS the exe, so we can't shell out to "python").  launcher.py
    dispatches that sentinel before any Streamlit code runs.
  * The Viewer's trace server runs as a background thread INSIDE the hub
    process (port 8771+), embedded in the Viewer page via an iframe.
  * Two divergent sor_reader324802a.py copies (viewer/ vs secretsauce/)
    can't share one frozen module namespace, so all our engine .py ship
    as ON-DISK DATA under viewer\ and secretsauce\ and are imported via
    sys.path at runtime — never as PyInstaller hidden-imports.  Their
    third-party deps (numpy/openpyxl/reportlab/matplotlib) are bundled by
    collect_all() in the spec.

UPDATE SIGNING KEY  *** ONE-TIME HUMAN STEP — DO THIS BEFORE AUTO-UPDATE
                        CAN TURN ON ***
  The launcher auto-updates the engine from GitHub, but ONLY when it can
  verify an Ed25519-signed manifest against a PUBLIC key baked into
  launcher.py.  Until that key is provisioned, auto-update is DISABLED
  (fail closed) and the app runs the bundled engine — so the app is safe to
  ship today; updates simply stay off until you do the steps below.

  Generate the keypair (any machine with the `cryptography` lib):

      python -c "from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey; \
from cryptography.hazmat.primitives import serialization as s; \
k=Ed25519PrivateKey.generate(); \
priv=k.private_bytes(s.Encoding.Raw, s.PrivateFormat.Raw, s.NoEncryption()).hex(); \
pub=k.public_key().public_bytes(s.Encoding.Raw, s.PublicFormat.Raw).hex(); \
print('PRIVATE (secret):', priv); print('PUBLIC  (commit) :', pub)"

  Then:
    1. Paste the PUBLIC hex into launcher.py:
           UPDATE_PUBLIC_KEY_HEX = "<64-hex-char public key>"
       (replacing REPLACE_WITH_ED25519_PUBLIC_KEY_HEX).  The public key is
       safe to commit.  Commit + let CI rebuild.
    2. Set the PRIVATE hex as a repo Actions secret named
           OTDR_UPDATE_SIGNING_KEY
       (Settings -> Secrets and variables -> Actions -> New repository
       secret).  NEVER commit the private key.
    3. The next build on main signs update_manifest.json and pushes it +
       its .sig to main; from then on each launch verifies the signature,
       checks every file's SHA-256, enforces anti-rollback, and only then
       swaps the new engine in.
  Key rotation: regenerate, repeat steps 1-2.  Old exes (with the old baked
  pubkey) will simply stop auto-updating until they're reinstalled — they
  never run an unverified update.

LOGS (give these to whoever debugs a tech's machine)
  %USERPROFILE%\.otdrSuite\otdrsuite.log

PORTS
  Hub server      : 127.0.0.1:8510   (claimed in the ports registry)
  Viewer traces   : 127.0.0.1:8771+  (internal, in-process thread)

TODO / not yet wired
  * GitHub Actions Windows boot-self-test workflow (like the Splice Report
    repo's build-windows.yml) — add once this lives in a repo.
  * Provision the update-signing key (see "UPDATE SIGNING KEY" above) to
    turn auto-update ON.  Until then it ships safely DISABLED (fail closed).

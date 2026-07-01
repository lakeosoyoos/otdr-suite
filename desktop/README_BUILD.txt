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

UPDATE SIGNING KEY  *** ONE-TIME HUMAN STEP — SET ONE SECRET TO TURN
                        AUTO-UPDATE ON ***
  The launcher auto-updates the engine from GitHub, but ONLY when it can
  verify an Ed25519-signed manifest against the PUBLIC key baked into the
  SHIPPED exe.  The committed launcher.py always keeps the fail-closed
  placeholder, so the app is safe to ship today — auto-update stays OFF until
  you set the one secret below.  (CI tests enforce that the placeholder stays
  in source; do NOT paste a key into launcher.py.)

  You manage exactly ONE value: the PRIVATE key, as a repo secret.  The build
  DERIVES the matching public key from it and stamps it into launcher.py
  automatically (desktop/inject_update_pubkey.py, run in the "Inject
  update-signing public key" CI step) — so the exe trusts exactly the key that
  signs the manifest and the two halves can never drift.

  Generate the key (any machine with the `cryptography` lib) — you only need the
  PRIVATE half; the public half is derived for you at build time:

      python -c "from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey; \
from cryptography.hazmat.primitives import serialization as s; \
k=Ed25519PrivateKey.generate(); \
print('PRIVATE (secret):', k.private_bytes(s.Encoding.Raw, s.PrivateFormat.Raw, s.NoEncryption()).hex())"

  Then:
    1. Set the PRIVATE hex as a repo Actions secret named
           OTDR_UPDATE_SIGNING_KEY
       (Settings -> Secrets and variables -> Actions -> New repository
       secret), or:  gh secret set OTDR_UPDATE_SIGNING_KEY < privkey.txt
       NEVER commit the private key.  That is the whole setup — no source edit.
    2. The next build on main injects the derived pubkey into the exe, then
       signs update_manifest.json and pushes it + its .sig to main.  From then
       on each launch verifies the signature, checks every file's SHA-256,
       enforces anti-rollback, and only then swaps the new engine in.
    3. Ship that build once — it carries the pubkey.  Every launch after checks
       for updates automatically.  Because the pubkey lives in launcher.py
       (which auto-update cannot replace), only a key ROTATION needs a fresh
       install.
  Key rotation: regenerate the private key, update the OTDR_UPDATE_SIGNING_KEY
  secret.  Old exes (with the old baked pubkey) simply stop auto-updating until
  reinstalled — they never run an unverified update.

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

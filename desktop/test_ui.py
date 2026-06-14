#!/usr/bin/env python3
"""OTDR Suite — UI smoke test (Streamlit AppTest).

Runs the hub (app.py) headlessly through AppTest and asserts both pages
render without an exception.  No browser, no .exe, ~10 s.  Runs in CI
BEFORE the PyInstaller build so a UI regression fails fast instead of
after the multi-minute bundle step.

Exit code 0 = pass, 1 = fail (so `python test_ui.py` gates the workflow).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
APP_PATH = os.path.join(REPO_ROOT, "app.py")


def main() -> int:
    from streamlit.testing.v1 import AppTest

    # Page 1 — Viewer (default).  Starts the trace-server thread + renders iframe.
    at = AppTest.from_file(APP_PATH, default_timeout=60).run()
    if at.exception:
        print("FAIL: Viewer page raised:", at.exception)
        return 1
    print("OK: Viewer page rendered.")

    # Page 2 — Duplicate Check.
    at2 = AppTest.from_file(APP_PATH, default_timeout=60).run()
    at2.sidebar.radio[0].set_value("Duplicate Check").run()
    if at2.exception:
        print("FAIL: Duplicate Check page raised:", at2.exception)
        return 1
    print("OK: Duplicate Check page rendered.")

    print("UI smoke test PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

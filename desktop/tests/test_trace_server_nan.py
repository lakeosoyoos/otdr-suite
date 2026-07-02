"""Regression: /api/trace must emit VALID JSON when event fields are non-finite.

Real EXFO JSON exports carry literal NaN Loss; json.dumps' default allow_nan
emitted a bare `NaN` token, so the browser's JSON.parse threw and the whole
trace pane failed to load for exactly the high-loss fibers.  trace_server now
routes every response through _finite (non-finite float → null).  The viewer's
per-fiber event table was also hardened to render null as '—' instead of calling
.toFixed on it (viewer.html) — that JS mirror can't be unit-tested here, but this
locks the server-side fix that is the root of the pane failure.
"""
import subprocess
import sys
import textwrap

from conftest import VIEWER_DIR


def _run(body):
    header = ("import sys\n"
              f"sys.path.insert(0, {str(VIEWER_DIR)!r})\n"
              "import trace_server as T\n"
              "import json, math\n")
    p = subprocess.run([sys.executable, "-c", header + textwrap.dedent(body)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


def test_trace_json_is_valid_when_event_fields_are_nan():
    _run("""
        payload = {"direction": "A", "fiber": 7,
                   "trace_db": [0.0, float("nan"), 1.5],
                   "events": [{"number": 1, "splice_loss": float("nan"),
                               "reflection": float("-inf"), "slope": 0.1,
                               "is_end": False}]}
        s = json.dumps(T._finite(payload))
        assert "NaN" not in s and "Infinity" not in s, s      # no bare non-JSON tokens
        back = json.loads(s)                                  # valid JSON parses cleanly
        assert back["events"][0]["splice_loss"] is None       # NaN  -> null
        assert back["events"][0]["reflection"] is None        # -inf -> null
        assert back["events"][0]["slope"] == 0.1              # finite value untouched
        assert back["trace_db"][1] is None and back["trace_db"][2] == 1.5
        assert back["fiber"] == 7 and back["direction"] == "A"
        print("OK")
    """)

"""Regression: JSON wavelength fallback must not be dead code (Fable 2nd-wave MED).

`data.get('LinkResults', {}).get('Results', [{}])` returned a TRUTHY [{}] when
LinkResults was absent, so the per-measurement `Wavelength` fallback (else branch)
never ran and every LinkResults-less .json silently parsed as 1550 nm — the
boss-facing Acquisition sheet then mislabels 1625/1310 runs and merges buckets.

The fix (`(data.get('LinkResults') or {}).get('Results') or []`) lives identically
in BOTH json_reader copies; lock it so a revert or a missed copy is caught.  The
extraction is inline in a large file-path parser, so this guards the exact edit
rather than reverse-engineering the whole OTDR-JSON schema.
"""
from conftest import REPO_ROOT

_COPIES = [
    REPO_ROOT / "splicereport" / "json_reader.py",
    REPO_ROOT / "viewer" / "json_reader.py",
]


def test_wavelength_fallback_is_not_dead_code():
    for p in _COPIES:
        src = p.read_text(encoding="utf-8")
        assert "(data.get('LinkResults') or {}).get('Results') or []" in src, (
            f"{p} missing the wavelength-fallback fix"
        )
        # The buggy assignment (truthy-[{}] default that made the else-branch
        # unreachable) must be gone from the CODE.  Match the full assignment so a
        # comment that merely quotes the old pattern doesn't trip this.
        assert "link_results = data.get('LinkResults', {}).get('Results', [{}])" not in src, (
            f"{p} still has the dead-code assignment"
        )

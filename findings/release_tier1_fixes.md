# Tier-1 release fixes — branch `sandbox/release-fixes` off main (f7f843c)

Fixes for the top boss-facing issues the 6-agent audit found in the f7f843c
release. **NOT pushed / merged — awaiting Robert's approval.** All changes are
additive / fail-safe; default behavior is unchanged except where it was broken.

## Fixes, by boss-facing symptom

1. **Splice Report "report failed" on some spans** — zero-closure spans (short /
   all-bend / mostly-broke) crashed `scan_a_standalone_events` via `splices[None]`.
   - `splicereport/splicereportmatchexfo.py`: guard `if best_si is None: continue`
     after the nearest-closure search (no closure → skip, don't index `splices[None]`
     or store a `(fnum, None)` key).
   - `splicereport/run_splicereport.py`: grid loop now also guards `si is None`.
   - Test: `test_scan_a_standalone_no_closures_does_not_crash`.

2. **Splice Report "looks hung" on big spans** — no live progress for minutes.
   - `run_splicereport.py`: live phase markers to stderr ("Loading A/B trace
     files…", "Analyzing N fibers across M closures…", "Writing the Excel
     report…") — the hub's `_engine_tail` now shows a moving step.

3. **Secret Sauce freezes the browser** — 372k–662k pairs → ~80–140 MB HTML.
   - `secretsauce/run_secretsauce.py` `_emit_pairs`: cap emitted pairs to the
     worst-first top 500 (`MAX_EMIT_PAIRS`); keep the TRUE `n_pairs`/`n_flagged`;
     add `pairs_truncated` / `pairs_shown`.
   - `app.py` `_render_pairs_report`: defensive 500-row cap + "showing top N of M"
     caption. (Source-locked in tests.)

4. **Can't load his spans** — they ship as separate per-direction zips.
   - `folder_intake.py`: new `find_otdr_files_with_zips()` descends into zips;
     `find_otdr_files()` now skips `._*` / `__MACOSX` (Mac-zip junk that polluted
     the A/B split).
   - `app.py` `_load_span`: uses `find_otdr_files_with_zips` for folders, and
     accepts MULTIPLE uploaded zips; clearer "no files" guidance.
   - `app.py` uploader: `accept_multiple_files=True`.
   - Tests: `test_find_otdr_files_skips_appledouble`,
     `test_find_otdr_files_with_zips_descends_into_per_direction_zips`.

5. **Dead "Browse for folder" button on Windows** — Tcl/Tk not bundled.
   - `app.py` `pick_folder`: returns `None` when the picker is unavailable (vs a
     silent `''`); the UI shows "picker unavailable — paste the path / upload the
     zip(s)" and the path field is now the documented primary input.
   - `desktop/OTDRSuite.spec`: `+ collect_all("tkinter")` to ship Tcl/Tk.
     **VERIFY on a Windows CI build** — Tcl/Tk bundling is environment-sensitive.

6. **Viewer "froze" (issue #5) + hang on big fiber ranges.**
   - `viewer/viewer.html` `markerHit`: `if (!gView) return null;` (the null-gView
     crash); `clearAll` resets markers so a stale marker can't re-trigger it.
   - `viewer/viewer.html` `addFibers`: cap at 48 traces + batched fetch so a big
     range can't flood the single-threaded server.

7. **Error reporter silently blind on Windows** — no TLS trust store, so his
   crash reports never sent (why no new issue auto-logged).
   - `error_report.py`: `urlopen` now uses a certifi CA-bundle SSL context (same
     as `launcher._tls_context`); test mock updated to accept `context=`.

## Verification
- Full desktop suite: **159 passed / 2 xfailed** (+4 new release-fix tests; the
  error-report mock updated for the new `context=` kwarg).
- HOWLAN 864-fiber end-to-end via `run_splicereport.py`: `ok=True`, 234 flags,
  16 closures, all three progress markers printed — identical to the pre-fix
  baseline (no regression).
- NOT auto-verifiable on macOS (reviewed, plus a UI fallback): the tkinter spec
  bundling needs a Windows CI build; the viewer JS has no `node` to lint — but
  the path-paste fallback (#5) covers the Browse case regardless.

## Deliberately out of scope (Tier-2 / flagged for later)
Miller↔Topeka SH-trace drop; A/B alphabetical inversion vs the tech's columns;
Secret Sauce report saved to a temp dir; viewer km axis mis-scale for non-EXFO
SOR; adding CI coverage for the frozen `--run-*` subprocess path.

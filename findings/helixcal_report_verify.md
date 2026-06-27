# helixcal report — output verification (sandbox)

Verifies that `helixcal/report.py` (the openpyxl xlsx writer) renders every
field the report task requires, in the suite's house style, and is gated green.

## Output path / shape
- API: `helixcal.report.write_report(result, output_path) -> output_path`
  (also `build_workbook(result) -> openpyxl.Workbook` if you want to embed it
  in an existing book).
- Driven end-to-end by `helixcal/cli.py::run(src, anchor_table, output_path, ...)`
  where `src` is a folder or zip of `.sor`.
- Workbook = **3 sheets**, in order:
  1. `Helix Calibration` — Parameter | Result QA table (acquisition_audit idiom:
     Calibri, D9D9D9 header, green=pass / amber=warn / red=outlier, BFBFBF box
     borders, freeze A4).
  2. `Per-Anchor Residuals` — grid (write_xlsx idiom: 1F4E79 white-bold header,
     freeze A2, autofit). Columns: Fiber, Closure, Type, Direction,
     OTDR fiber dist (m/ft), Known sheath (m/ft), Residual (m/ft). Residual
     > 1 m row is amber.
  3. `Per-Fiber Spread` — grid. Columns: Fiber, # anchors, m, EFL %, b (m), R²,
     Stored IOR, Outlier?, Note. Outlier fibers get a full RED (FFC7CE) row.

## Required fields — all present (confirmed by rendering real fixtures + edge case)
- fitted m, EFL% = (1/m−1)·100, offset b (m + ft, reported separately), R².
- per-anchor residuals (m + ft) on sheet 2.
- per-fiber factor spread: mean, std-dev, range on summary; per-fiber m/EFL/b/R²
  on sheet 3; **outlier flags** = RED summary row "Outlier fibers" + RED per-fiber
  row + "YES" + σ-note ("m=… is N.Nσ from cohort mean …").
- IOR flags: "Stored IOR cohort median", optional "Expected fiber-spec IOR",
  per-trace divergence flags as indented amber rows, and the guardrail label —
  shows **"combined empirical factor (IOR not independently verified)"** (amber)
  unless every trace's stored IOR is confirmed against an expected spec value,
  in which case "IOR independently verified against fiber spec" (green).
- cable-type band sanity verdict: PASS (green) when m is inside the resolved
  cable-type's AEN-142 band, else WARNING (amber) with the "likely IOR error /
  mismatched anchor, not a real cable factor" caveat; cable type + source
  (manual / genparams / default) + expected band echoed.
- Warnings banner (amber bullets) for band-out / IOR-divergence / skipped-band.

House-style primitives are COPIED into report.py (not imported) so we match
write_xlsx / acquisition_audit without re-running those purpose-built functions.
Distances shown in BOTH meters and feet per house convention.

## Verification run
- Real fixtures (desktop/tests/fixtures/span_A, 4 traces) + synthetic anchors
  (m_true=0.976, b=6 ft): report wrote a 3-sheet .xlsx, m=0.97600, EFL=2.459%,
  b=6.000 m (19.69 ft), R²=1.000000, band PASS (stranded loose-tube),
  IOR verified (expected 1.47).
- Hand-built edge result exercised the other branches: IOR-not-verified label +
  per-trace flag rows, central-tube band WARNING + warnings bullets, and an
  outlier fiber (0042) rendered RED on both the summary and the per-fiber sheet.

## Gating
- `python3 -m pytest helixcal/tests/` → 43 passed (incl. test_report_cli.py:
  three-sheet workbook, headline rows, CLI folder + zip, band WARNING on
  central_tube).
- `python3 -m pytest desktop/tests/` → 130 passed, 2 skipped, 2 xfailed
  (shared suite untouched; report module is isolated, imports the parser only).

Sandbox only. No push, no merge.

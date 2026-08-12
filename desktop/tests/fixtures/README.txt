OTDR Suite test fixtures
========================

Real OTDR SOR acquisitions (not synthetic — content sniffs and parser quirks
only surface on real files).  ~1.1 MB total, committed on purpose.

PROVENANCE
  Span: Elmdale <-> Miller "Long Shots" (Downloads/Long Shots, ELMMIL/MILELM
  zips, 1550 nm, 1152-fiber cable).  4 fibers x 2 directions = 8 files.

  span_A/  ELMMIL0001..0004_1550.sor   (A-direction, Elmdale -> Miller)
  span_B/  MILELM0001..0004_1550.sor   (B-direction, Miller -> Elmdale)

WHY 4 PER DIRECTION
  Secret Sauce groups SOR files by their file-internal GenParams direction
  key and needs >=2 files per group to form pairs.  4 per direction gives 6
  pairs per group — enough to exercise the regime classifier and xlsx build.

USED BY
  conftest.py exports FIXTURE_A_DIR / FIXTURE_B_DIR and mixed_fixture_dir().
  The viewer-engine, secret-sauce-runner, and hub-contract suites all build
  against these.


shortspan_A/  — SYNTHETIC, and the only synthetic fixture here
================================================================
  6 files, ~1.1 MB.  Used by test_short_span.py.

  WHY IT IS SYNTHETIC
    The short-span defect needs a SHORT span that contains a break.  The real
    short-span folder we have (Defuniak Springs Tie Panels, DNN1<->DNN2, 144
    fibers per direction) has NO broken fiber — all 288 traces run the full
    2.0415 km, confirmed against the raw samples.  So the positive control had
    to be constructed rather than found, and it is constructed in the open.

  BUILT FROM
    Real bytes: Defuniak "Aside" DNN1DNN2000{1..6}.sor (EXFO, 5 ns, 1550 nm).
    Header, GenParams, FxdParams and the full trace are untouched real data.

  THE EDITS (both length-preserving — no block size or map offset moves)
    all 6 files : event #2 retyped 1F -> 0F, so the file reads as already
                  trimmed and distances reach the detection gates unshifted
                  (a plain 2.0415 km span instead of the real file's
                  launch-reel / 30.9 m panel / receive-reel shape)
    fibers 5, 6 : event #3 retyped 1F -> 1E, KeyEvents count 4 -> 3, and the
                  samples past 1.0361 km overwritten with the file's own
                  post-EOF noise — a fiber genuinely dead at the connector

  REGENERATE
    python3 desktop/tests/fixtures/make_shortspan.py <path to Defuniak Aside>
    (make_shortspan.py carries the full rationale and reuses the shipped
    sor_reader block-directory parser rather than a copy of it)

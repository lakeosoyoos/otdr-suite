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

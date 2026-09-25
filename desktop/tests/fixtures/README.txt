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

panelfarrefl/   LSC1<->LSC6 ribbon 11 (fibers 121-132), A and B.  Span start on the
                near panel, end marker ON the far panel, nothing between.  Fiber
                128's B shot reads the far (LSC1) panel at -49.78 dB on its end
                marker, which FR fails (test_panel_far_refl_on_end_marker.py).

strayreshoot/   SNARCAAH 1 East ribbon 9 (fibers 97-108), A and B, plus the B-side
                re-shoot of 103 saved as SNA2ESNA1103.sor (reads as 1103; internal
                id E103).  The dead first shot SNA2ESNA1E103.sor is kept beside it
                (test_filename_site_digit.py).

paneljumper/    FTH01<->FTH06 West Panel B, ribbon 1 (fibers 1-12), A and B.  Tie
                panel shot through 15 m sacrificial jumpers: launch reel, jumper,
                panel A, 62 m tie, panel B, jumper, receive reel.  The tech's
                span start is on panel A and the end marker on panel B, so the
                table carries a jumper joint at -0.015 km and two events past the
                end.  FR grades neither (test_panel_jumper_second_spike.py).

panelspan/      Two Defuniak Springs tie-panel fibers: 31 m of cable between two
                panels, shot through a 1.0047 km launch reel into a 1.0053 km
                receive reel.  A span with NO closures at all — the shape that
                crashed the runner on 2026-08-25 and then rendered only its two
                ILA end columns.  FastReporter's own table for it reads
                0.000 dB / 0.000 dB/km across the 31 m between the panels.

frspan_long/    Lumen Denver->KC Span 7, Monument -> Grainfield, fiber 183 (9-24-26 shoot,
                500 ns, 56.72 km, 1.0095 km launch reel).  MONGRA0183_1550.sor as shot;
                *_fr_span_start.sor = the same file after FastReporter 3 "Spans by
                Distance", Launch fiber length 1.0095 km, then Save
                (test_sor_span_write_long.py).  Long enough that the old 0.02998 m/tot
                constant (25 ppm long) put the events past 40 km more than 1 m from
                FR's records and set_span refused every file on the span.

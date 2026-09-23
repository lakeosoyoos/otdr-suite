# FQA Builder

Builds the Lumen **Site Survey / Fiber Quality Assurance** submittal
package for a span from that span's ZeroDB **production sheet**.

Standalone — it does not need traces, the hub, or any other tool in the
suite to be useful. Point it at a production sheet and it fills the
cover page, the Fiber Assignment Table, the Event Log and the Exception
Reporting tab of the Lumen form.

```bash
streamlit run fqa/app.py --server.port 8514 --server.maxUploadSize 512
python -m fqa.run_fqa --production SHEET.xlsx --out FQA.xlsm   # the engine
```

Drag the production sheet onto the app, or Browse for it. Streamlit's
default upload ceiling is 200 MB and these sheets reach 250, hence
`--server.maxUploadSize`; for the very largest there is a path box that
skips the upload entirely.

## What comes from where

| FQA tab | Filled from |
|---|---|
| Site Survey Data | the production sheet's terminations (rack, RMU, panel, connector, fibre count, cable) plus the job facts you supply (addresses, CLLIs, testers, calibration date) |
| FAT | fibre count + the lateral cables listed at each termination — entirely derived |
| Event Log | one row per production-sheet tab, in tab order; distances from the traces, checked against the sheet's footage marks |
| Exception Reporting | the OOS / reburned fibres you list |
| Submittal Checklist | nothing — it is formulas over the tabs above, and it recalculates on open |

## Distances

The production sheet records each cable's sequential footage mark at
every location, and those marks chain down the span. On Span 4 Flagler
to Bethune the chain reproduces the real Event Log to within 4–35 m over
seven of its eleven segments — and then falls apart, because Splice 3
and Splice 2 both record a mark near 200 ft on the same reel.

So the **measured trace distances fill the column** and the footage marks
**check** it. Every segment where the two disagree by more than the
tolerance is reported by name. Without measured distances the tool falls
back to the marks and says so.

## Not complete

Every run ends with a list of what the package is still missing, pulled
from all three places gaps hide: the job facts nobody typed, the
production sheet the crew left half-filled, and measurements never taken.
Each line says where the gap is, what is missing and what the field is.

Items are split into the ones Lumen will send the package back for and
the ones worth a look. In the app the list is live — it shrinks as you
fill the boxes above it, so it is something to work through rather than a
verdict at the end.

## The template is Lumen's form, not a copy of it

`templates/FQA_Site_Survey_v1_1.xlsm` is Lumen's form **revision 1.1**
(published 2025-11-14) with one span's values and site photos taken out.

51 of its 65 zip parts are byte-identical to the package Lumen issued —
the VBA, the Microsoft sensitivity label, all four customXml parts, the
styles, the data validation, the printer settings and the comments. The
package this tool writes is that file with values patched into cells.
Nothing about the form is reconstructed, no cell style is altered, and
no fill is added.

Only four things differ from the issued file:

| Difference | Why |
|---|---|
| five sheets' bytes | the span's values cleared out |
| `calcChain.xml` dropped | forced: a stale chain makes Excel offer to repair the file, and it rebuilds the chain silently when absent |
| `workbook.xml` | `fullCalcOnLoad` set, so the Submittal Checklist recalculates instead of showing cached answers |
| 4 images, 2 drawings | one customer's ILA site photos; the Pictures tab is where the tech puts their own |

`test_the_package_is_lumens_form_part_for_part` and
`test_the_writer_never_changes_a_cell_style` hold that down.

**Any revision of the Lumen form is accepted** as a template. Two are in
circulation — this one, which carries a Version History tab, and an older
7-tab one without it — and their layouts are identical for every cell
this tool writes. A template that is not 1.1 is not refused; it shows up
as a line in the completeness list saying the revision has not been
checked cell by cell.

To adopt a newer revision as the shipped default: Cells are
patched inside the .xlsm zip rather than through openpyxl, which cannot
round-trip the form's VBA, customXml, sensitivity label, comments or
printer settings.

When Lumen publishes a new revision, build a fresh template from a
package made on it:

```bash
python -m fqa.make_template "Some Span - FQA SITE SURVEY.xlsm" \
    fqa/templates/FQA_Site_Survey_v1_2.xlsm
```

## Validated against

| Span | Cable | Result |
|---|---|---|
| Span 4 Flagler to Bethune (Denver to Kansas City) | 1152ct | 126/126 checked cells identical to the submitted package |
| Span 3 Tucumcari to Santa Rosa (Stradford to El Paso) | 864ct | 134/141; the differences are two house-style conventions and one disagreement worth a look (below) |

Conventions differ between projects and the tool follows whatever the
production sheet says, so some cells are a matter of house style rather
than correctness:

* headings are `East`/`West` on one project and `SOUTH/WEST`/`NORTH/EAST`
  on the other — learnt from the span, never assumed
* the RMU separator is `8 & 19` on one and `13,19` on the other — edit the
  RMU box and the test-from-device string and every FAT row follow
* the entry vault reads `ENTRY` on one and `Entry` on the other

One genuine disagreement on Tucumcari: its FAT repeats the backbone
numbers down the Site Z lateral column, where the Santa Rosa entry splice
sheet says both laterals carry 1-432 and should restart. Span 4's package
restarts. This tool follows the splice sheet.

Tests: `desktop/tests/test_fqa_builder.py` (57).

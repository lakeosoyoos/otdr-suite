# Helix Calibration — M0 Read-only Survey (HOWESPAN→LANCASTER)

Branch: sandbox/helix-calibration. READ-ONLY; no engine/app touched.

## Parser entry points (REUSE — do not fork)
splicereport/sor_reader324802a.py:
- parse_sor_full(path) -> dict with 'events', 'wavelength', 'acq_range',
  'trace', exfo_* fields. Does NOT surface stored IOR (verified: no 'ior' key).
- _parse_block_directory(data) -> {blockname: {offset,size,ver,body}}
- _parse_fxd_params(data, blocks) -> wavelength(/10), acq_range, duration, date.
- _parse_key_events -> per-event dist_km, time_of_travel, splice_loss, is_end, type.
- _sor_ior_from_events(sor_data, default) -> IOR DERIVED from tot/dist (cross-check only).
- _read_ior(data) EXISTS but is an unanchored byte-scan of first 1000 bytes — NOT FxdParams-anchored.

## STORED IOR (the guardrail value)
- Lives at FxdParams body + 28 as uint32, value/100000. Verified file off 309 = 147000 -> 1.47000.
- Stable 1.47000 across A & B, multiple fibers. Derived IOR matches to ~4-5 dp.
- New helper needed (small, reuses _parse_block_directory + _parse_fxd_params):
  read uint32 at blocks['FxdParams']['body']+28, /1e5. Surface as 'stored_ior'.

## GenParams location codes
- _parse_block_directory returns WRONG body for GenParams (first block): its name appears
  in the directory header first, so find() lands on the directory entry, not the data block.
  Fix in helper: use the LAST occurrence of b'GenParams\x00', bound by SupParams offset.
- Real block (after 2-byte 'EN'): [1]=CableID/fiberID string ('HOWLAN001'),
  [2 tail]=OriginatingLocation 'HOWESPAN', [3]=TerminatingLocation 'LANCASTER'.
- cable_code field ([5]='BC...') is junk/non-informative; cable-type auto-detect CANNOT work.
  => helix path MUST fall back to settings/manual cable-type input.

## Grounding confirmed
- 864 .sor each dir (/tmp/helixspan/A, /tmp/helixspan/B). EOF ~117.32 km fiber 1.
- wavelength field 1539.8 (nominal 1550). 11-15 events/fiber.

## Anchor data shape (existing splice_report xlsx)
- 'Splice Report' sheet header rows give per-closure km AND ft, both directions
  (Splice 1 A->B = 1.79km/5,873ft ...). Those ft are OTDR-fiber-dist-in-ft, NOT sheath footage.
- 15 named splice columns. Anchor table = tech supplies KNOWN sheath footage per closure.

## xlsx style to mirror
- splicereport/splicereportmatchexfo.py:write_xlsx (Calibri 12, header 1F4E79, A-km blue/B-km dark-red).
- BEST template for the QA report: splicereport/acquisition_audit.py:insert_audit_sheet
  (Parameter|Result table, green=match / amber=outlier, indented outlier rows).

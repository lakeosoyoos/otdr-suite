# Vendored libraries

Copied unmodified from the npm packages on cdn.jsdelivr.net, 2026-09-23, so the
Field Capture page works with no network. They ship in the install only. They are
not engine files and do not travel with updates (see `fieldcapture/server.py`).

| File | Package | License |
|---|---|---|
| `exceljs.min.js` | exceljs 4.4.0 `dist/exceljs.min.js` | MIT |
| `jszip.min.js` | jszip 3.10.1 `dist/jszip.min.js` | MIT (dual MIT / GPLv3) |
| `tesseract/tesseract.min.js`, `tesseract/worker.min.js` | tesseract.js 7.0.0 `dist/` | Apache-2.0 |
| `tesseract/core/tesseract-core-{lstm,simd-lstm,relaxedsimd-lstm}.wasm.js` | tesseract.js-core 7.0.0 | Apache-2.0 |
| `tesseract/lang/eng.traineddata.gz` | @tesseract.js-data/eng 1.0.0 `4.0.0_best_int/` | Apache-2.0 |

The reader picks one of the three cores at run time by what the browser
supports (relaxed SIMD, SIMD, or neither). All three must ship.

To update one, replace the file from the same package path and bump the version
in this table.

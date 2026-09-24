/* FQA workbook reader and writer for OTDR Field Capture.

   The FQA Site Survey is Lumen's macro-enabled form. Besides the macros it carries a
   sensitivity label, customXml parts, comments and printer settings, and no spreadsheet
   library round-trips all of that. So this file never opens it as a workbook: it treats
   the .xlsm as a zip of XML parts, rewrites the few parts it has to, and copies every
   other part through untouched. This is the same approach as the FQA Builder's
   xlsx_patch.py, done in the browser with JSZip.

   What a build changes:
     * 'Site Survey Data' section 1.2 cells (rows 51-57 for the A-Location, 59-64 for Z).
       Each cell keeps its style; values go in as inline strings or numbers.
     * The 'Pictures' tab gets the tech's photos, one band per location with a caption,
       below anything already on the tab.
     * xl/workbook.xml gets fullCalcOnLoad="1" so the FAT and Event Log formulas that read
       section 1.2 recalculate when the file is opened.
   The DOM parser keeps namespace prefixes as written, so the sheet XML stays Lumen's. */
'use strict';
(function () {
  const MAIN = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main';
  const PKG_REL = 'http://schemas.openxmlformats.org/package/2006/relationships';
  const REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships';
  const XDR = 'http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing';
  const DML = 'http://schemas.openxmlformats.org/drawingml/2006/main';
  const T_DRAWING = REL + '/drawing';
  const T_IMAGE = REL + '/image';
  const CT_DRAWING = 'application/vnd.openxmlformats-officedocument.drawing+xml';
  const XLSM_MIME = 'application/vnd.ms-excel.sheet.macroEnabled.12';
  const XLSX_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';
  const EMU_PER_PX = 9525;

  const SHEET_SURVEY = 'Site Survey Data';
  const SHEET_PICTURES = 'Pictures';

  // Section 1.2 Fiber Panel Information. The A block has one extra row (54, range of
  // port numbers available) that the Z block does not.
  const SECTION_12 = {
    A: { floor: 'F51', room: 'H51', aisle: 'J51', bay: 'L51', suite: 'N51',
      rmu: 'F52', block: 'I52',
      existing: 'F53', rackSize: 'H53', portCount: 'J53', rmus: 'M53',
      portRange: 'F54', backbone: 'F55',
      termination: 'F56', ospFacing: 'I56', riser: 'K56', panelType: 'M56',
      diverse: 'F57' },
    Z: { floor: 'F59', room: 'H59', aisle: 'J59', bay: 'L59', suite: 'N59',
      rmu: 'F60', block: 'I60',
      existing: 'F61', rackSize: 'H61', portCount: 'J61', rmus: 'M61',
      backbone: 'F62',
      termination: 'F63', ospFacing: 'I63', riser: 'K63', panelType: 'M63',
      diverse: 'F64' },
  };
  const NUMERIC_FIELDS = new Set(['portCount', 'rmus']);
  // Cells the app reads for context: site aliases and the fibre count.
  const CONTEXT_CELLS = { aliasA: 'E10', aliasZ: 'E12', cableSize: 'F95', fibersTested: 'F97', totalFiber: 'H39' };
  // Form prompts such as '<Select>' or '<Enter Suite (as appl)>' are not values.
  const isPrompt = (v) => typeof v === 'string' && /^<[^>]*>$/.test(v.trim());

  // ---------- XML helpers ----------
  function parseXml(text) {
    const doc = new DOMParser().parseFromString(text, 'application/xml');
    if (doc.getElementsByTagName('parsererror').length) throw new Error('The workbook has a part that is not valid XML.');
    return doc;
  }
  // XMLSerializer drops the XML declaration; put back exactly what the part had.
  function serialize(doc, original) {
    let s = new XMLSerializer().serializeToString(doc);
    s = s.replace(/^<\?xml[^>]*\?>\s*/, '');
    const decl = /^<\?xml[^>]*\?>\s*/.exec(original);
    return (decl ? decl[0] : '') + s;
  }
  const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  const dirOf = (p) => p.slice(0, p.lastIndexOf('/') + 1);
  const relsOf = (p) => dirOf(p) + '_rels/' + p.slice(p.lastIndexOf('/') + 1) + '.rels';
  function resolve(base, target) {
    if (target.startsWith('/')) return target.slice(1);
    const out = dirOf(base).split('/').filter(Boolean);
    for (const seg of target.split('/')) {
      if (seg === '..') out.pop(); else if (seg && seg !== '.') out.push(seg);
    }
    return out.join('/');
  }
  function relative(fromPart, toPart) {
    const a = dirOf(fromPart).split('/').filter(Boolean);
    const b = toPart.split('/');
    let i = 0;
    while (i < a.length && i < b.length - 1 && a[i] === b[i]) i++;
    return '../'.repeat(a.length - i) + b.slice(i).join('/');
  }
  function colIndex(col) { let n = 0; for (const ch of col) n = n * 26 + (ch.charCodeAt(0) - 64); return n; }
  function colName(idx) { let s = ''; while (idx > 0) { const r = (idx - 1) % 26; s = String.fromCharCode(65 + r) + s; idx = Math.floor((idx - 1) / 26); } return s; }
  function splitRef(ref) {
    const m = /^([A-Z]+)(\d+)$/.exec(ref);
    if (!m) throw new Error('not a cell reference: ' + ref);
    return [m[1], Number(m[2])];
  }
  const kids = (el, name) => Array.from(el.childNodes).filter((n) => n.nodeType === 1 && n.localName === name);

  // ---------- the package ----------
  // Write one part. JSZip would otherwise add folder entries ("xl/media/") that are not
  // parts of an Excel package and are not in Lumen's file.
  const put = (pkg, name, data) => pkg.zip.file(name, data, { createFolders: false });

  async function openPackage(bytes) {
    if (typeof JSZip === 'undefined') throw new Error('The zip library did not load. Open the app once while online.');
    const zip = await JSZip.loadAsync(bytes);
    const text = async (name) => { const f = zip.file(name); return f ? f.async('string') : null; };
    const wbXml = await text('xl/workbook.xml');
    const relXml = await text('xl/_rels/workbook.xml.rels');
    if (!wbXml || !relXml) throw new Error('This file is not an Excel workbook.');
    const rels = parseXml(relXml);
    const targets = {};
    for (const r of rels.getElementsByTagNameNS(PKG_REL, 'Relationship')) targets[r.getAttribute('Id')] = resolve('xl/workbook.xml', r.getAttribute('Target'));
    const sheets = {};
    const wb = parseXml(wbXml);
    for (const s of wb.getElementsByTagNameNS(MAIN, 'sheet')) {
      const rid = s.getAttributeNS(REL, 'id');
      if (rid && targets[rid]) sheets[s.getAttribute('name')] = targets[rid];
    }
    return { zip, sheets, text };
  }

  async function sharedStrings(pkg) {
    const xml = await pkg.text('xl/sharedStrings.xml');
    if (!xml) return [];
    const doc = parseXml(xml);
    return Array.from(doc.getElementsByTagNameNS(MAIN, 'si')).map((si) => {
      // Phonetic runs (rPh) are furigana, not part of the string.
      let out = '';
      for (const t of si.getElementsByTagNameNS(MAIN, 't')) if (!t.parentNode || t.parentNode.localName !== 'rPh') out += t.textContent;
      return out;
    });
  }

  function cellValue(c, strings) {
    const t = c.getAttribute('t');
    if (t === 'inlineStr') { const is = kids(c, 'is')[0]; return is ? Array.from(is.getElementsByTagNameNS(MAIN, 't')).map((x) => x.textContent).join('') : ''; }
    const v = kids(c, 'v')[0];
    if (!v) return null;
    if (t === 's') return strings[Number(v.textContent)] ?? null;
    if (t === 'str' || t === 'e') return v.textContent;
    if (t === 'b') return v.textContent === '1';
    const n = Number(v.textContent);
    return Number.isFinite(n) ? n : v.textContent;
  }

  async function readCells(pkg, sheetName, refs) {
    const part = pkg.sheets[sheetName];
    if (!part) return {};
    const want = new Set(refs);
    const doc = parseXml(await pkg.text(part));
    const strings = await sharedStrings(pkg);
    const out = {};
    for (const c of doc.getElementsByTagNameNS(MAIN, 'c')) {
      const r = c.getAttribute('r');
      if (want.has(r)) out[r] = cellValue(c, strings);
    }
    return out;
  }

  // What the app needs from a workbook: the site names, the fibre count, and whatever
  // section 1.2 already holds (so the tech starts from the office's values).
  async function describe(bytes) {
    const pkg = await openPackage(bytes);
    if (!pkg.sheets[SHEET_SURVEY]) throw new Error(`This workbook has no '${SHEET_SURVEY}' tab, so it is not the Lumen FQA form.`);
    const refs = Object.values(CONTEXT_CELLS).concat(Object.values(SECTION_12.A), Object.values(SECTION_12.Z));
    const v = await readCells(pkg, SHEET_SURVEY, refs);
    const clean = (x) => (x == null || isPrompt(x) ? '' : String(x).trim());
    const section12 = {};
    for (const site of ['A', 'Z']) {
      section12[site] = {};
      for (const [field, ref] of Object.entries(SECTION_12[site])) section12[site][field] = clean(v[ref]);
    }
    const count = [v[CONTEXT_CELLS.fibersTested], v[CONTEXT_CELLS.cableSize], v[CONTEXT_CELLS.totalFiber]]
      .map((x) => { const m = /(\d{2,5})/.exec(clean(x)); return m ? Number(m[1]) : null; })
      .find((n) => n);
    return {
      aliasA: clean(v[CONTEXT_CELLS.aliasA]), aliasZ: clean(v[CONTEXT_CELLS.aliasZ]),
      fiberCount: count || null, section12, hasPictures: Boolean(pkg.sheets[SHEET_PICTURES]),
    };
  }

  // ---------- cell patching ----------
  function writeValue(doc, c, value) {
    for (const a of Array.from(c.attributes)) if (a.name !== 'r' && a.name !== 's') c.removeAttribute(a.name);
    while (c.firstChild) c.removeChild(c.firstChild);
    if (value == null || value === '') return;
    if (typeof value === 'number') {
      const v = doc.createElementNS(MAIN, 'v');
      v.textContent = String(value);
      c.appendChild(v);
      return;
    }
    c.setAttribute('t', 'inlineStr');
    const is = doc.createElementNS(MAIN, 'is');
    const t = doc.createElementNS(MAIN, 't');
    const text = String(value);
    if (text !== text.trim()) t.setAttributeNS('http://www.w3.org/XML/1998/namespace', 'xml:space', 'preserve');
    t.textContent = text;
    is.appendChild(t);
    c.appendChild(is);
  }

  function patchCells(xml, cells) {
    const doc = parseXml(xml);
    const data = doc.getElementsByTagNameNS(MAIN, 'sheetData')[0];
    if (!data) throw new Error('A worksheet has no cell data.');
    const rows = new Map();
    for (const r of kids(data, 'row')) rows.set(Number(r.getAttribute('r')), r);
    for (const { ref, value } of cells) {
      const [col, rn] = splitRef(ref);
      let row = rows.get(rn);
      if (!row) {
        row = doc.createElementNS(MAIN, 'row');
        row.setAttribute('r', String(rn));
        const after = kids(data, 'row').find((r) => Number(r.getAttribute('r')) > rn);
        data.insertBefore(row, after || null);
        rows.set(rn, row);
      }
      const want = colIndex(col);
      let cell = null;
      for (const c of kids(row, 'c')) {
        const idx = colIndex(splitRef(c.getAttribute('r'))[0]);
        if (idx === want) { cell = c; break; }
        if (idx > want) { cell = doc.createElementNS(MAIN, 'c'); cell.setAttribute('r', ref); row.insertBefore(cell, c); break; }
      }
      if (!cell) { cell = doc.createElementNS(MAIN, 'c'); cell.setAttribute('r', ref); row.appendChild(cell); }
      writeValue(doc, cell, value);
    }
    return serialize(doc, xml);
  }

  // Section 1.2 values -> cells. Only fields the tech filled in are written, so a blank
  // box never erases what the office already put in the workbook.
  function section12Cells(site, panel) {
    const map = SECTION_12[site];
    const out = [];
    for (const [field, ref] of Object.entries(map)) {
      let v = panel[field];
      if (v == null) continue;
      v = String(v).trim();
      if (!v) continue;
      out.push({ ref, value: NUMERIC_FIELDS.has(field) && /^\d+$/.test(v) ? Number(v) : v });
    }
    return out;
  }

  // ---------- workbook.xml: recalculate on open ----------
  function forceFullCalc(xml) {
    const m = /<calcPr\b[^>]*?(\/?)>/.exec(xml);
    if (m) {
      let tag = m[0];
      if (/\bfullCalcOnLoad="[^"]*"/.test(tag)) tag = tag.replace(/\bfullCalcOnLoad="[^"]*"/, 'fullCalcOnLoad="1"');
      else tag = tag.replace(/\s*(\/?)>$/, ' fullCalcOnLoad="1"$1>');
      return xml.slice(0, m.index) + tag + xml.slice(m.index + m[0].length);
    }
    const anchor = /<\/definedNames>/.exec(xml) || /<\/sheets>/.exec(xml);
    if (!anchor) return xml;
    const at = anchor.index + anchor[0].length;
    return xml.slice(0, at) + '<calcPr fullCalcOnLoad="1"/>' + xml.slice(at);
  }

  // A relationship or content type pointing at a part that is not in the package is
  // invalid. The FQA Builder's template dropped calcChain.xml without its references.
  async function dropDanglingCalcChain(pkg) {
    if (pkg.zip.file('xl/calcChain.xml')) return;
    const ct = await pkg.text('[Content_Types].xml');
    const fixedCt = ct.replace(/<Override [^>]*PartName="\/xl\/calcChain\.xml"[^>]*\/>/, '');
    if (fixedCt !== ct) put(pkg, '[Content_Types].xml', fixedCt);
    const rels = await pkg.text('xl/_rels/workbook.xml.rels');
    const fixedRels = rels.replace(/<Relationship [^>]*Target="(?:\/xl\/)?calcChain\.xml"[^>]*\/>/, '');
    if (fixedRels !== rels) put(pkg, 'xl/_rels/workbook.xml.rels', fixedRels);
  }

  // ---------- pictures ----------
  const PHOTO_H_PX = 340;          // every photo is drawn this tall on the Pictures tab
  const GAP_PX = 12;
  const ROW_WIDTH_PX = 1100;       // wrap to a new line of photos past this width
  const COL_PX = 64;               // Excel's default column (8.43 characters)

  async function ensureDefaultType(pkg, ext, type) {
    const ct = await pkg.text('[Content_Types].xml');
    if (new RegExp(`<Default [^>]*Extension="${ext}"`, 'i').test(ct)) return;
    put(pkg, '[Content_Types].xml', ct.replace(/<Types\b[^>]*>/, (m) => `${m}<Default Extension="${ext}" ContentType="${type}"/>`));
  }

  async function nextRelId(relsXml) {
    let max = 0;
    for (const m of relsXml.matchAll(/Id="rId(\d+)"/g)) max = Math.max(max, Number(m[1]));
    return max + 1;
  }
  const emptyRels = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n<Relationships xmlns="' + PKG_REL + '"></Relationships>';
  const addRel = (relsXml, id, type, target) => relsXml.replace(/<\/Relationships>\s*$/, `<Relationship Id="${id}" Type="${type}" Target="${esc(target)}"/></Relationships>`)
    .replace(/<Relationships([^>]*)\/>\s*$/, `<Relationships$1><Relationship Id="${id}" Type="${type}" Target="${esc(target)}"/></Relationships>`);

  // Find the Pictures tab's drawing, creating one if the tab has none.
  async function pictureDrawing(pkg, sheetPart) {
    const sheetRels = relsOf(sheetPart);
    let rels = (await pkg.text(sheetRels)) || emptyRels;
    const m = /<Relationship [^>]*Type="[^"]*\/drawing"[^>]*>/.exec(rels);
    if (m) {
      const target = /Target="([^"]+)"/.exec(m[0])[1];
      return resolve(sheetPart, target);
    }
    let k = 1;
    while (pkg.zip.file(`xl/drawings/drawing${k}.xml`)) k++;
    const part = `xl/drawings/drawing${k}.xml`;
    put(pkg, part, `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n<xdr:wsDr xmlns:xdr="${XDR}" xmlns:a="${DML}"/>`);
    const ct = await pkg.text('[Content_Types].xml');
    put(pkg, '[Content_Types].xml', ct.replace(/<\/Types>\s*$/, `<Override PartName="/${part}" ContentType="${CT_DRAWING}"/></Types>`));
    const rid = 'rId' + (await nextRelId(rels));
    put(pkg, sheetRels, addRel(rels, rid, T_DRAWING, relative(sheetPart, part)));
    // <drawing> has a fixed place in a worksheet: before these elements, or last.
    const sheetXml = await pkg.text(sheetPart);
    const doc = parseXml(sheetXml);
    const ws = doc.documentElement;
    const later = ['legacyDrawing', 'legacyDrawingHF', 'drawingHF', 'picture', 'oleObjects', 'controls', 'webPublishItems', 'tableParts', 'extLst'];
    const before = Array.from(ws.childNodes).find((n) => n.nodeType === 1 && later.includes(n.localName)) || null;
    const d = doc.createElementNS(MAIN, 'drawing');
    d.setAttributeNS(REL, 'r:id', rid);
    ws.insertBefore(d, before);
    put(pkg, sheetPart, serialize(doc, sheetXml));
    return part;
  }

  // The first free row (0-based) below existing pictures and cells on the tab.
  function firstFreeRow(drawingXml, sheetXml) {
    let last = -1;
    for (const m of drawingXml.matchAll(/<xdr:to>[\s\S]*?<xdr:row>(\d+)<\/xdr:row>/g)) last = Math.max(last, Number(m[1]));
    for (const m of drawingXml.matchAll(/<xdr:oneCellAnchor>[\s\S]*?<xdr:row>(\d+)<\/xdr:row>[\s\S]*?<xdr:ext cx="\d+" cy="(\d+)"/g)) {
      last = Math.max(last, Number(m[1]) + Math.ceil(Number(m[2]) / EMU_PER_PX / 17));
    }
    const doc = parseXml(sheetXml);
    for (const r of doc.getElementsByTagNameNS(MAIN, 'row')) {
      const used = kids(r, 'c').some((c) => kids(c, 'v').length || kids(c, 'is').length);
      if (used) last = Math.max(last, Number(r.getAttribute('r')) - 1);
    }
    return last < 0 ? 0 : last + 2;
  }

  function rowHeightPx(sheetXml) {
    const m = /<sheetFormatPr\b[^>]*defaultRowHeight="([\d.]+)"/.exec(sheetXml);
    return m ? Math.round(Number(m[1]) * 96 / 72) : 20;
  }

  // bands: [{caption, photos: [{bytes: Uint8Array, w, h, title}]}]
  async function addPictures(pkg, bands) {
    const sheetPart = pkg.sheets[SHEET_PICTURES];
    if (!sheetPart || !bands.some((b) => b.photos.length)) return 0;
    await ensureDefaultType(pkg, 'jpeg', 'image/jpeg');
    const drawingPart = await pictureDrawing(pkg, sheetPart);
    let drawing = await pkg.text(drawingPart);
    const sheetXml = await pkg.text(sheetPart);
    const rowPx = rowHeightPx(sheetXml);
    const drawingRelsPart = relsOf(drawingPart);
    let drawingRels = (await pkg.text(drawingRelsPart)) || emptyRels;
    let rid = await nextRelId(drawingRels);
    let shapeId = 1;
    for (const m of drawing.matchAll(/<xdr:cNvPr [^>]*id="(\d+)"/g)) shapeId = Math.max(shapeId, Number(m[1]) + 1);
    let mediaNo = 1;
    const mediaName = () => { while (pkg.zip.file(`xl/media/fieldcapture${mediaNo}.jpeg`)) mediaNo++; return `xl/media/fieldcapture${mediaNo}.jpeg`; };

    let row = firstFreeRow(drawing, sheetXml);
    const captions = [];
    let anchors = '';
    let count = 0;
    for (const band of bands) {
      if (!band.photos.length) continue;
      captions.push({ ref: `A${row + 1}`, value: band.caption });
      row += 1;
      const photoRows = Math.ceil(PHOTO_H_PX / rowPx) + 1;
      let x = 0;
      for (const p of band.photos) {
        const wPx = Math.round(PHOTO_H_PX * p.w / p.h);
        if (x > 0 && x + wPx > ROW_WIDTH_PX) { x = 0; row += photoRows; }
        const media = mediaName();
        put(pkg, media, p.bytes);
        const id = 'rId' + rid++;
        drawingRels = addRel(drawingRels, id, T_IMAGE, relative(drawingPart, media));
        const cx = wPx * EMU_PER_PX, cy = PHOTO_H_PX * EMU_PER_PX;
        const col = Math.floor(x / COL_PX), colOff = (x % COL_PX) * EMU_PER_PX;
        const sid = shapeId++;
        anchors += `<xdr:oneCellAnchor xmlns:xdr="${XDR}" xmlns:a="${DML}">`
          + `<xdr:from><xdr:col>${col}</xdr:col><xdr:colOff>${colOff}</xdr:colOff><xdr:row>${row}</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>`
          + `<xdr:ext cx="${cx}" cy="${cy}"/>`
          + `<xdr:pic><xdr:nvPicPr><xdr:cNvPr id="${sid}" name="Field photo ${sid}" descr="${esc(p.title || '')}"/>`
          + '<xdr:cNvPicPr><a:picLocks noChangeAspect="1"/></xdr:cNvPicPr></xdr:nvPicPr>'
          + `<xdr:blipFill><a:blip xmlns:r="${REL}" r:embed="${id}"/><a:stretch><a:fillRect/></a:stretch></xdr:blipFill>`
          + `<xdr:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="${cx}" cy="${cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></xdr:spPr>`
          + '</xdr:pic><xdr:clientData/></xdr:oneCellAnchor>';
        x += wPx + GAP_PX;
        count++;
      }
      row += photoRows + 1;
    }
    if (/<xdr:wsDr\b[^>]*\/>\s*$/.test(drawing)) drawing = drawing.replace(/\/>\s*$/, `>${anchors}</xdr:wsDr>`);
    else drawing = drawing.replace(/<\/xdr:wsDr>\s*$/, `${anchors}</xdr:wsDr>`);
    put(pkg, drawingPart, drawing);
    put(pkg, drawingRelsPart, drawingRels);
    if (captions.length) put(pkg, sheetPart, patchCells(await pkg.text(sheetPart), captions));
    return count;
  }

  // ---------- build ----------
  // job: {sites: {A: panel, Z: panel}, bands: [...]} -> Blob
  async function build(bytes, fileName, job) {
    const pkg = await openPackage(bytes);
    const survey = pkg.sheets[SHEET_SURVEY];
    if (!survey) throw new Error(`This workbook has no '${SHEET_SURVEY}' tab.`);
    const cells = [];
    for (const site of ['A', 'Z']) if (job.sites[site]) cells.push(...section12Cells(site, job.sites[site]));
    if (cells.length) put(pkg, survey, patchCells(await pkg.text(survey), cells));
    const pictures = await addPictures(pkg, job.bands || []);
    put(pkg, 'xl/workbook.xml', forceFullCalc(await pkg.text('xl/workbook.xml')));
    await dropDanglingCalcChain(pkg);
    const macro = /\.xlsm$/i.test(fileName);
    const blob = await pkg.zip.generateAsync({ type: 'blob', mimeType: macro ? XLSM_MIME : XLSX_MIME, compression: 'DEFLATE', compressionOptions: { level: 6 } });
    return { blob, cells: cells.length, pictures, mime: macro ? XLSM_MIME : XLSX_MIME };
  }

  window.FQA = { SECTION_12, describe, build, section12Cells, readCells, openPackage, colName, isPrompt };
})();

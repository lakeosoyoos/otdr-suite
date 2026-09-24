/* OTDR Field Capture
   The tech's phone app for the Lumen FQA Site Survey. For the A-Location and the
   Z-Location it collects tester initials, the rack location and panel details of section
   1.2, a GPS fix and photos. It reads the rack, RMU and panel labels in the photos,
   checks them against what was entered, writes section 1.2 and the photos into the FQA
   workbook and hands the workbook to Mail. It also still exports the capture sheet laid
   out like the production sheet. Everything runs on the phone; there is no server. */
'use strict';
(function () {
  const DB_NAME = 'otdr-field-capture';
  const STORE = 'captures';
  const KV = 'kv';
  const MAX_EDGE = 1280;          // photos are shrunk to this long edge for the workbooks
  const JPEG_QUALITY = 0.72;
  const WORK_QUALITY = 0.9;       // the larger copy kept for reading labels
  const PHOTO_WIDTH_PX = 360;     // width of each photo in the capture sheet's picture well
  const ROW_PX = 20;
  const PHOTO_COL_CHARS = (PHOTO_WIDTH_PX - 5) / 7;
  const XLSX_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';
  const INITIALS_KEY = 'otdr-field-capture.initials';
  const EMAIL_KEY = 'otdr-field-capture.email';
  const BLANK_FORM = 'fqa/FQA_Site_Survey_blank.xlsm';
  const BLANK_NAME = 'Blank Lumen FQA form (built in)';
  const SITE_NAMES = { A: 'A-Location', Z: 'Z-Location', other: 'Other location' };
  const PANEL_FIELDS = ['rmu', 'block', 'existing', 'rackSize', 'portCount', 'rmus', 'portRange', 'backbone', 'termination', 'ospFacing', 'riser', 'panelType', 'diverse'];
  const RACK_FIELDS = ['floor', 'room', 'aisle', 'bay', 'suite'];
  const OFFLINE_ASSETS = ['vendor/tesseract/worker.min.js', 'vendor/tesseract/core/tesseract-core-simd-lstm.wasm.js',
    'vendor/tesseract/core/tesseract-core-lstm.wasm.js', 'vendor/tesseract/core/tesseract-core-relaxedsimd-lstm.wasm.js',
    'vendor/tesseract/lang/eng.traineddata.gz', BLANK_FORM];

  // In OTDR Suite the page is served by fieldcapture/server.py inside the hub
  // (?host=suite). There it saves to a folder on the PC and opens an email draft
  // instead of using a phone's share sheet, and it has no GPS of its own.
  const SUITE = new URLSearchParams(location.search).get('host') === 'suite';
  const $ = (id) => document.getElementById(id);
  const el = {};
  ['fqaName', 'fqaSites', 'fqaOpenBtn', 'fqaBlankBtn', 'fqaInput', 'fqaMsg', 'captureCard', 'formTitle', 'initials', 'siteHint',
    'rackLegend', 'floor', 'room', 'aisle', 'bay', 'suite', 'panelBox', 'portRangeLabel', 'lat', 'lon', 'gpsBtn', 'gpsStatus', 'gpsHint',
    'cameraBtn', 'libraryBtn', 'cameraInput', 'libraryInput', 'thumbs', 'photoHint', 'ocrStatus', 'labelList', 'checkList',
    'saveBtn', 'cancelEditBtn', 'saveMsg', 'savedList', 'savedCount', 'clearBtn', 'sendSummary', 'emailTo', 'fqaBuildBtn', 'sendMsg',
    'exportBtn', 'exportMsg', 'netState', 'viewer', 'viewerTitle', 'viewerClose', 'viewerCanvas', 'viewerMsg',
    'fqaSaveOnlyBtn', 'sendHint', 'sendLinks', 'exportHint', 'stampToggle', 'stampRow'].forEach((id) => { el[id] = $(id); });
  const fieldEl = (f) => (RACK_FIELDS.includes(f) ? el[f] : $('p_' + f));
  const siteRadios = Array.from(document.querySelectorAll('input[name=site]'));

  // The location being filled in.
  const draft = { photos: [], labels: [], gps: null, editingId: null, busy: 0, site: 'A', prefilled: {} };
  let fqa = { name: BLANK_NAME, blob: null, info: null };   // blob null = the built-in blank form
  let savedRecs = [];
  let prepared = null;       // the FQA file ready for the share sheet
  let preparedSheet = null;  // the capture sheet ready for the share sheet
  const images = new Map();  // photo id -> decoded image, for crops and the viewer

  // ---------- storage ----------
  function openDb() {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, 2);
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE, { keyPath: 'id', autoIncrement: true });
        if (!db.objectStoreNames.contains(KV)) db.createObjectStore(KV);
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }
  async function withStore(name, mode, fn) {
    const db = await openDb();
    return new Promise((resolve, reject) => {
      const t = db.transaction(name, mode);
      const req = fn(t.objectStore(name));
      let out;
      if (req) req.onsuccess = () => { out = req.result; };
      t.oncomplete = () => { db.close(); resolve(out); };
      t.onerror = () => { db.close(); reject(t.error); };
      t.onabort = () => { db.close(); reject(t.error || new Error('storage aborted')); };
    });
  }
  const store = {
    all: () => withStore(STORE, 'readonly', (s) => s.getAll()),
    get: (id) => withStore(STORE, 'readonly', (s) => s.get(id)),
    put: (rec) => withStore(STORE, 'readwrite', (s) => s.put(rec)),
    del: (id) => withStore(STORE, 'readwrite', (s) => s.delete(id)),
    clear: () => withStore(STORE, 'readwrite', (s) => s.clear()),
  };
  const kv = {
    get: (k) => withStore(KV, 'readonly', (s) => s.get(k)),
    set: (k, v) => withStore(KV, 'readwrite', (s) => s.put(v, k)),
    del: (k) => withStore(KV, 'readwrite', (s) => s.delete(k)),
  };

  // ---------- small helpers ----------
  const pad = (n) => String(n).padStart(2, '0');
  const localDate = (iso) => { const d = new Date(iso); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; };
  const localTime = (iso) => { const d = new Date(iso); return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`; };
  const stamp = () => { const d = new Date(); return `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}`; };
  const gpsText = (gps) => (gps ? `${gps.lat.toFixed(6)}, ${gps.lon.toFixed(6)}` : '');
  const clean = (s) => (s || '').trim();
  const newId = () => 'p' + Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
  const padDigits = (s, n) => (/^\d+$/.test(s) ? s.padStart(n, '0') : s);
  const safeName = (s) => s.replace(/[\\/:*?"<>|]+/g, ' ').replace(/\s+/g, ' ').trim();
  function setMsg(node, text, kind) { node.textContent = text || ''; node.className = 'msg' + (kind ? ' ' + kind : ''); }
  function blobToDataUrl(blob) {
    return new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => resolve(r.result);
      r.onerror = () => reject(r.error || new Error('could not read photo'));
      r.readAsDataURL(blob);
    });
  }
  let checksTimer = 0;
  const checksSoon = () => { clearTimeout(checksTimer); checksTimer = setTimeout(renderChecks, 150); };

  // ---------- the FQA workbook ----------
  async function blankBytes() {
    const r = await fetch(SUITE ? 'api/blank-form' : BLANK_FORM);
    if (!r.ok) throw new Error('The blank form is not on this phone yet. Open the app once while online.');
    return r.arrayBuffer();
  }
  async function loadFqaState() {
    const saved = await kv.get('fqa').catch(() => null);
    if (saved && saved.blob) fqa = { name: saved.name, blob: saved.blob, info: saved.info };
    else {
      fqa = { name: BLANK_NAME, blob: null, info: null };
      try { fqa.info = await FQA.describe(await blankBytes()); } catch (e) { setMsg(el.fqaMsg, e.message, 'err'); }
    }
    renderFqa();
  }
  function renderFqa() {
    el.fqaName.textContent = fqa.name;
    const i = fqa.info || {};
    const bits = [];
    if (i.aliasA || i.aliasZ) bits.push(`A site: ${i.aliasA || '(not named)'}. Z site: ${i.aliasZ || '(not named)'}.`);
    else bits.push('This workbook names no sites yet, so far-end labels are shown but not compared.');
    if (i.fiberCount) bits.push(`${i.fiberCount} fibers.`);
    el.fqaSites.textContent = bits.join(' ');
    el.fqaBlankBtn.disabled = !fqa.blob;
    siteHint();
  }
  async function useWorkbook(name, bytes) {
    const info = await FQA.describe(bytes);
    const blob = new Blob([bytes]);
    await kv.set('fqa', { name, blob, info });
    fqa = { name, blob, info };
    renderFqa();
    if (draft.editingId == null) prefill(draft.site);
    resetPrepared();
    renderChecks();
    renderSendSummary();
    return info;
  }

  // ---------- location and section 1.2 fields ----------
  function siteHint() {
    const i = fqa.info || {};
    const s = draft.site;
    el.siteHint.textContent = s === 'A' ? `Fills the A-Location rows of section 1.2${i.aliasA ? ` (${i.aliasA})` : ''}.`
      : s === 'Z' ? `Fills the Z-Location rows of section 1.2${i.aliasZ ? ` (${i.aliasZ})` : ''}.`
        : 'Any other location, such as a splice vault. It goes on the capture sheet, not the FQA.';
  }
  function setSite(site, { prefillFields } = {}) {
    draft.site = site;
    siteRadios.forEach((r) => { r.checked = r.value === site; });
    el.captureCard.classList.toggle('site-other', site === 'other');
    el.portRangeLabel.hidden = site !== 'A';
    el.rackLegend.textContent = site === 'other' ? 'Room, Aisle, Bay' : 'Rack Location: Floor, Room, Aisle, Bay';
    // Floor lives in the section 1.2 row; show it only there. Room, aisle and bay stay.
    el.floor.parentElement.hidden = site === 'other';
    siteHint();
    if (prefillFields) prefill(site);
    renderChecks();
  }
  // Start from what the workbook already holds for this site, so the tech confirms the
  // office's values instead of typing them. Values put in this way are cleared again if
  // the tech switches site before touching them.
  function prefill(site) {
    for (const [f, v] of Object.entries(draft.prefilled)) { const n = fieldEl(f); if (n && n.value === v) n.value = ''; }
    draft.prefilled = {};
    const src = site !== 'other' && fqa.info && fqa.info.section12 ? fqa.info.section12[site] : null;
    if (!src) return;
    for (const f of RACK_FIELDS.concat(PANEL_FIELDS)) {
      const n = fieldEl(f);
      if (!n || !src[f] || n.value) continue;
      if (n.tagName === 'SELECT' && !Array.from(n.options).some((o) => o.value === src[f] || o.text === src[f])) continue;
      n.value = src[f];
      draft.prefilled[f] = n.value;
    }
  }
  function readPanel() {
    const p = {};
    for (const f of PANEL_FIELDS.concat(['floor', 'suite'])) p[f] = clean(fieldEl(f).value);
    if (draft.site !== 'A') delete p.portRange;
    return p;
  }
  function clearFields() {
    for (const f of RACK_FIELDS.concat(PANEL_FIELDS)) fieldEl(f).value = '';
    draft.prefilled = {};
  }

  // ---------- photos ----------
  function loadImage(file) {
    return new Promise((resolve, reject) => {
      const url = URL.createObjectURL(file);
      const img = new Image();
      img.onload = () => { URL.revokeObjectURL(url); resolve(img); };
      img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('Could not read ' + (file.name || 'photo'))); };
      img.src = url;
    });
  }
  async function shrinkTo(img, edge, quality, stamp) {
    const scale = Math.min(1, edge / Math.max(img.naturalWidth, img.naturalHeight));
    const w = Math.max(1, Math.round(img.naturalWidth * scale));
    const h = Math.max(1, Math.round(img.naturalHeight * scale));
    const canvas = document.createElement('canvas');
    canvas.width = w; canvas.height = h;
    const g = canvas.getContext('2d');
    g.drawImage(img, 0, 0, w, h);
    if (stamp) drawStamp(g, w, h, stamp);
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', quality));
    canvas.width = canvas.height = 1;   // release the pixels now; phones run out of canvas memory
    if (!blob) throw new Error('Could not encode the photo.');
    return { blob, w, h };
  }
  // ---------- the camera stamp ----------
  // A photo taken with the app's own camera gets a stamp burned into it, the way a
  // timestamp camera app does it: date and time, the phone's GPS fix and its
  // accuracy, the location and the tester. White text on a dark band along the
  // bottom, which is also what the label reader's stamp pass reads. The same values
  // are kept with the photo, so the GPS check never has to read them back.
  const STAMP_KEY = 'otdr-field-capture.stamp';
  let liveFix = null;                      // the newest phone fix: {lat, lon, acc, alt, at}
  function noteFix(pos) {
    liveFix = { lat: pos.coords.latitude, lon: pos.coords.longitude, acc: pos.coords.accuracy,
      alt: pos.coords.altitude, at: new Date(pos.timestamp || Date.now()).toISOString() };
    return liveFix;
  }
  // A fix no older than maxAgeMs, waiting up to waitMs for a new one. null if none.
  function freshFix(maxAgeMs, waitMs) {
    if (liveFix && Date.now() - Date.parse(liveFix.at) < maxAgeMs) return Promise.resolve(liveFix);
    if (!navigator.geolocation) return Promise.resolve(null);
    return new Promise((resolve) => {
      const t = setTimeout(() => resolve(null), waitMs);
      navigator.geolocation.getCurrentPosition((pos) => { clearTimeout(t); resolve(noteFix(pos)); },
        () => { clearTimeout(t); resolve(null); }, { enableHighAccuracy: true, timeout: waitMs, maximumAge: maxAgeMs });
    });
  }
  function stampFor(fix) {
    const i = fqa.info || {};
    const alias = draft.site === 'A' ? i.aliasA : draft.site === 'Z' ? i.aliasZ : '';
    const now = new Date().toISOString();
    return { at: now, lat: fix ? fix.lat : null, lon: fix ? fix.lon : null, acc: fix ? fix.acc : null, alt: fix ? fix.alt : null,
      fixAt: fix ? fix.at : null, site: draft.site, place: `${SITE_NAMES[draft.site]}${alias ? ' ' + alias : ''}`,
      initials: clean(el.initials.value).toUpperCase() };
  }
  function stampLines(st) {
    const lines = [`${localDate(st.at)} ${localTime(st.at)}`];
    if (st.lat != null) {
      const hemi = (v, pos, neg) => `${Math.abs(v).toFixed(6)} ${v < 0 ? neg : pos}`;
      let gps = `${hemi(st.lat, 'N', 'S')}  ${hemi(st.lon, 'E', 'W')}`;
      if (st.acc != null) gps += `  +/-${Math.round(st.acc)} m`;
      if (st.alt != null) gps += `  alt ${Math.round(st.alt * 3.28084)} ft`;
      lines.push(gps);
    } else {
      lines.push('No GPS fix');
    }
    lines.push([st.place, st.initials && `tester ${st.initials}`].filter(Boolean).join(', '));
    return lines;
  }
  function drawStamp(g, w, h, st) {
    const lines = stampLines(st);
    const fs = Math.max(11, Math.round(Math.min(w, h) / 34));
    const lh = Math.round(fs * 1.3), padX = Math.round(fs * 0.7), padY = Math.round(fs * 0.5);
    const bh = lines.length * lh + 2 * padY;
    g.save();
    g.fillStyle = 'rgba(0, 0, 0, 0.6)';
    g.fillRect(0, h - bh, w, bh);
    g.fillStyle = '#ffffff';
    g.textBaseline = 'top';
    g.font = `600 ${fs}px -apple-system, "Helvetica Neue", Helvetica, Arial, sans-serif`;
    lines.forEach((t, k) => g.fillText(t, padX, h - bh + padY + k * lh, w - 2 * padX));
    g.restore();
  }
  const stampOn = () => { try { return localStorage.getItem(STAMP_KEY) !== 'off'; } catch (e) { return true; } };

  // The location a phone camera writes into a JPEG (EXIF GPS). A PC has no GPS of
  // its own, so on the PC this is where a site's coordinates come from. iPhone
  // Safari strips it from uploads, so on the phone this is usually empty.
  function exifGps(buf) {
    try {
      const v = new DataView(buf);
      if (v.getUint16(0) !== 0xFFD8) return null;
      let off = 2;
      while (off + 10 < v.byteLength) {
        const marker = v.getUint16(off), size = v.getUint16(off + 2);
        if ((marker & 0xFF00) !== 0xFF00) return null;
        if (marker === 0xFFE1 && v.getUint32(off + 4) === 0x45786966) {
          const t = off + 10;
          const le = v.getUint16(t) === 0x4949;
          const u16 = (o) => v.getUint16(t + o, le), u32 = (o) => v.getUint32(t + o, le);
          const ifd0 = u32(4);
          let gps = 0;
          for (let i = 0, n = u16(ifd0); i < n; i++) { const e = ifd0 + 2 + i * 12; if (u16(e) === 0x8825) gps = u32(e + 8); }
          if (!gps) return null;
          const tag = {};
          for (let i = 0, n = u16(gps); i < n; i++) {
            const e = gps + 2 + i * 12, id = u16(e), type = u16(e + 2), count = u32(e + 4);
            if ((id === 1 || id === 3) && type === 2) tag[id] = String.fromCharCode(v.getUint8(t + e + 8));
            if ((id === 2 || id === 4) && type === 5 && count === 3) {
              const o = u32(e + 8), r = (k) => u32(o + 8 * k) / (u32(o + 8 * k + 4) || 1);
              tag[id] = r(0) + r(1) / 60 + r(2) / 3600;
            }
          }
          if (tag[2] == null || tag[4] == null || (!tag[2] && !tag[4])) return null;
          const lat = tag[1] === 'S' ? -tag[2] : tag[2], lon = tag[3] === 'W' ? -tag[4] : tag[4];
          return Math.abs(lat) <= 90 && Math.abs(lon) <= 180 ? { lat, lon } : null;
        }
        off += 2 + size;
      }
    } catch (e) { /* not a JPEG we can read: no location */ }
    return null;
  }
  async function makePhoto(file, stamp) {
    let img;
    try {
      img = await loadImage(file);
    } catch (e) {
      if (/\.hei[cf]$/i.test(file.name || '') || /hei[cf]/i.test(file.type || '')) {
        throw new Error(`${file.name} is an HEIC photo, which this browser cannot open. Save it as a JPEG (on the iPhone: Settings > Camera > Formats > Most Compatible), then add it again.`);
      }
      throw e;
    }
    const gps = exifGps(await file.slice(0, 262144).arrayBuffer());
    const small = await shrinkTo(img, MAX_EDGE, JPEG_QUALITY, stamp);
    const work = await shrinkTo(img, Labels.OCR_EDGE, WORK_QUALITY, stamp);
    return { id: newId(), blob: small.blob, w: small.w, h: small.h, work: work.blob, ww: work.w, wh: work.h, exifGps: gps,
      stamp: stamp || null, name: file.name || 'photo.jpg',
      takenAt: stamp ? stamp.at : new Date(file.lastModified || Date.now()).toISOString(), ocr: 'pending' };
  }
  // fromCamera: taken just now with the app's camera button, so the phone's current
  // fix is where the photo was taken and the photo gets the stamp.
  async function addPhotoFiles(files, fromCamera) {
    const list = Array.from(files || []);
    if (!list.length) return;
    let stamp = null;
    if (fromCamera && stampOn()) {
      setMsg(el.saveMsg, 'Getting the GPS for the stamp...');
      stamp = stampFor(await freshFix(120000, 8000));
    }
    draft.busy += 1;
    el.saveBtn.disabled = true;
    setMsg(el.saveMsg, `Adding ${list.length} photo${list.length > 1 ? 's' : ''}...`);
    try {
      for (const f of list) {
        const p = await makePhoto(f, stamp);
        draft.photos.push(p);
        readLabels(p);
        // The stamp's fix was taken here and now: use it when the boxes are empty.
        if (p.stamp && p.stamp.lat != null && !clean(el.lat.value) && !clean(el.lon.value)) {
          draft.gps = { lat: p.stamp.lat, lon: p.stamp.lon, acc: p.stamp.acc, at: p.stamp.fixAt || p.stamp.at, source: 'device' };
          showGps(draft.gps);
        }
        // A photo's own location data was recorded at the site by the phone that
        // took it, so it is the best GPS a location can have. Use it when the
        // boxes are still empty; never overwrite a fix the tech took or typed.
        if (p.exifGps && !clean(el.lat.value) && !clean(el.lon.value)) {
          draft.gps = { lat: p.exifGps.lat, lon: p.exifGps.lon, acc: null, at: p.takenAt, source: 'photo',
            note: `photo ${draft.photos.length}'s location data` };
          showGps(draft.gps);
        }
      }
      setMsg(el.saveMsg, '');
    } catch (e) {
      setMsg(el.saveMsg, e.message, 'err');
    } finally {
      draft.busy -= 1;
      if (!draft.busy) el.saveBtn.disabled = false;
      renderThumbs();
    }
  }
  const photoNo = (id) => draft.photos.findIndex((p) => p.id === id) + 1;
  function photoImage(p) {
    if (!images.has(p.id)) images.set(p.id, Labels.loadImage(p.work || p.blob));
    return images.get(p.id);
  }
  function removePhoto(p) {
    draft.photos = draft.photos.filter((x) => x !== p);
    draft.labels = draft.labels.filter((l) => l.photoId !== p.id);
    images.delete(p.id);
    renderThumbs(); renderLabels(); renderChecks(); renderOcrStatus();
  }
  function renderThumbs() {
    el.thumbs.querySelectorAll('img').forEach((img) => URL.revokeObjectURL(img.src));
    el.thumbs.textContent = '';
    draft.photos.forEach((p, i) => {
      const box = document.createElement('div');
      box.className = 'thumb';
      box.title = 'Open to read a label';
      const img = document.createElement('img');
      img.src = URL.createObjectURL(p.blob);
      img.alt = `Photo ${i + 1}`;
      const n = draft.labels.filter((l) => l.photoId === p.id).length;
      const state = document.createElement('span');
      state.className = 'state';
      state.textContent = p.ocr === 'reading' || p.ocr === 'pending' ? 'Reading labels...'
        : p.ocr === 'failed' || p.ocr === 'off' ? `Photo ${i + 1}: not read`
          : `Photo ${i + 1}: ${n} label${n === 1 ? '' : 's'}`;
      const rm = document.createElement('button');
      rm.type = 'button';
      rm.textContent = '×';
      rm.setAttribute('aria-label', `Remove photo ${i + 1}`);
      rm.addEventListener('click', (e) => { e.stopPropagation(); removePhoto(p); });
      box.addEventListener('click', () => openViewer(p));
      box.append(img, state, rm);
      el.thumbs.appendChild(box);
    });
    const n = draft.photos.length;
    el.photoHint.textContent = n ? `${n} photo${n > 1 ? 's' : ''} attached. Tap a photo to read a label by hand.`
      : 'No photos yet. Photograph the rack label, each panel\'s label and its RMU tag.';
  }

  // ---------- reading labels ----------
  function readLabels(p) {
    p.ocr = 'reading';
    renderOcrStatus();
    Labels.readPhoto(p.work || p.blob).then((found) => {
      p.ocr = 'done';
      if (!draft.photos.includes(p)) return;
      draft.labels = draft.labels.filter((l) => !(l.photoId === p.id && l.source === 'auto'));
      for (const f of found) draft.labels.push({ id: newId(), photoId: p.id, box: f.box, text: f.text, source: 'auto' });
    }).catch((e) => {
      p.ocr = 'failed';
      p.ocrError = e && e.message ? e.message : String(e);
    }).finally(() => { renderThumbs(); renderLabels(); renderChecks(); renderOcrStatus(); });
  }
  function renderOcrStatus() {
    const n = draft.photos.length;
    const reading = draft.photos.filter((p) => p.ocr === 'reading' || p.ocr === 'pending').length;
    const failed = draft.photos.filter((p) => p.ocr === 'failed' || p.ocr === 'off');
    el.ocrStatus.textContent = '';
    if (!n) { el.ocrStatus.textContent = 'The app reads rack, RMU and panel labels from each photo.'; return; }
    if (reading) { el.ocrStatus.textContent = `Reading labels: ${n - reading} of ${n} photos done...`; return; }
    const labels = draft.labels.length;
    el.ocrStatus.append(`Read ${n - failed.length} of ${n} photos; ${labels} label${labels === 1 ? '' : 's'} found. `);
    if (failed.length) {
      const why = failed[0].ocrError ? ` (${failed[0].ocrError})` : '';
      el.ocrStatus.append(`Photo ${failed.map((p) => photoNo(p.id)).join(', ')} not read${why}. `);
      const again = document.createElement('button');
      again.type = 'button';
      again.className = 'inline';
      again.textContent = 'Read again';
      again.addEventListener('click', () => failed.forEach(readLabels));
      el.ocrStatus.append(again);
    }
  }
  async function drawCrop(canvas, p, box) {
    const img = await photoImage(p);
    const W = img.naturalWidth, H = img.naturalHeight;
    const sx = box[0] * W, sy = box[1] * H, sw = Math.max(1, (box[2] - box[0]) * W), sh = Math.max(1, (box[3] - box[1]) * H);
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(112 * dpr); canvas.height = Math.round(64 * dpr);
    const g = canvas.getContext('2d');
    const s = Math.min(canvas.width / sw, canvas.height / sh);
    g.fillStyle = '#000';
    g.fillRect(0, 0, canvas.width, canvas.height);
    g.drawImage(img, sx, sy, sw, sh, (canvas.width - sw * s) / 2, (canvas.height - sh * s) / 2, sw * s, sh * s);
  }
  function chipsFor(text, holder) {
    holder.querySelectorAll('.chip').forEach((c) => c.remove());
    const facts = Labels.parseFacts(text);
    const first = holder.firstChild;
    if (!facts.length) {
      const c = document.createElement('span');
      c.className = 'chip none';
      c.textContent = 'not a label the app knows';
      holder.insertBefore(c, first);
    }
    for (const f of facts) {
      const c = document.createElement('span');
      c.className = 'chip';
      c.textContent = f.meaning || f.text;
      holder.insertBefore(c, first);
    }
  }
  function renderLabels() {
    el.labelList.textContent = '';
    const order = (l) => photoNo(l.photoId) * 10 + l.box[1];
    for (const l of draft.labels.slice().sort((a, b) => order(a) - order(b))) {
      const p = draft.photos.find((x) => x.id === l.photoId);
      if (!p) continue;
      const li = document.createElement('li');
      const canvas = document.createElement('canvas');
      canvas.title = 'Open the photo at this label';
      canvas.addEventListener('click', () => openViewer(p, l.box));
      drawCrop(canvas, p, l.box).catch(() => {});
      const right = document.createElement('div');
      const ta = document.createElement('textarea');
      ta.rows = Math.min(3, Math.max(1, l.text.split('\n').length));
      ta.value = l.text;
      ta.setAttribute('aria-label', `Label text read from photo ${photoNo(p.id)}`);
      ta.placeholder = 'Type what the label says';
      const meta = document.createElement('div');
      meta.className = 'meta';
      const src = document.createElement('span');
      src.textContent = `Photo ${photoNo(p.id)}${l.source === 'tap' ? ', read by hand' : ''}`;
      const rm = document.createElement('button');
      rm.type = 'button';
      rm.textContent = 'Remove';
      rm.addEventListener('click', () => { draft.labels = draft.labels.filter((x) => x !== l); renderLabels(); renderThumbs(); renderChecks(); });
      meta.append(src, rm);
      chipsFor(l.text, meta);
      ta.addEventListener('input', () => { l.text = ta.value; chipsFor(l.text, meta); checksSoon(); });
      right.append(ta, meta);
      li.append(canvas, right);
      el.labelList.appendChild(li);
    }
  }

  // ---------- label checks ----------
  const latest = (recs, site) => recs.filter((r) => r.site === site).sort((a, b) => (a.updatedAt < b.updatedAt ? 1 : -1))[0] || null;
  function contextFor(site, ownId) {
    const i = fqa.info || {};
    const otherSite = site === 'A' ? 'Z' : site === 'Z' ? 'A' : null;
    const other = otherSite ? latest(savedRecs.filter((r) => r.id !== ownId), otherSite) : null;
    return {
      aliasSelf: site === 'A' ? i.aliasA : site === 'Z' ? i.aliasZ : '',
      aliasFar: site === 'A' ? i.aliasZ : site === 'Z' ? i.aliasA : '',
      fiberCount: i.fiberCount || null,
      otherGps: other && other.gps, otherName: otherSite ? SITE_NAMES[otherSite] : '',
    };
  }
  function checksFor(cap, id) {
    const out = Labels.check(cap, contextFor(cap.site, id));
    return cap.site === 'other' ? out.filter((c) => c.key === 'gps' || c.key === 'toward') : out;
  }
  function recordChecks(r) {
    if (!r) return [];
    const no = (pid) => r.photos.findIndex((p) => p.id === pid) + 1;
    return checksFor({ site: r.site || 'other', aisle: r.aisle, bay: r.bay, gps: r.gps, panel: r.panel || {},
      labels: (r.labels || []).map((l) => ({ text: l.text, photoNo: no(l.photoId) })),
      exif: photoFixes(r.photos) }, r.id);
  }
  // Where each photo says it was taken: the app's own stamp first, else the file's location data.
  function photoFixes(photos) {
    return photos.map((p, i) => (p.stamp && p.stamp.lat != null ? { lat: p.stamp.lat, lon: p.stamp.lon, photoNo: i + 1, how: 'stamp' }
      : p.exifGps ? { ...p.exifGps, photoNo: i + 1, how: 'exif' } : null)).filter(Boolean);
  }
  function safeGps() { try { return readGpsFromForm(); } catch (e) { return null; } }
  function draftChecks() {
    return checksFor({ site: draft.site, aisle: clean(el.aisle.value), bay: clean(el.bay.value), gps: safeGps(), panel: readPanel(),
      labels: draft.labels.map((l) => ({ text: l.text, photoNo: photoNo(l.photoId) })),
      exif: photoFixes(draft.photos) }, draft.editingId);
  }
  const MARK = { ok: '✓', bad: '✗', todo: '!', info: 'i' };
  const WORD = { ok: 'Matches', bad: 'Does not match', todo: 'To review', info: 'Note' };
  function renderCheckItem(c, withFix) {
    const li = document.createElement('li');
    li.className = c.status;
    const strong = document.createElement('strong');
    const mark = document.createElement('span');
    mark.className = 'mark';
    mark.textContent = MARK[c.status];
    strong.append(mark, `${c.title}: ${WORD[c.status]}`);
    li.append(strong, c.text);
    if (withFix && c.fix && c.fixLabel) {
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = c.fixLabel;
      b.addEventListener('click', () => applyFix(c.fix));
      li.append(document.createElement('br'), b);
    }
    return li;
  }
  function renderChecks() {
    el.checkList.textContent = '';
    const list = draftChecks();
    if (!list.length) {
      const li = document.createElement('li');
      li.className = 'info';
      li.textContent = draft.site === 'other' ? 'Labels are checked for the A-Location and Z-Location. Here only photo GPS stamps are compared.' : 'Add photos to check the labels.';
      el.checkList.appendChild(li);
      return;
    }
    for (const c of list) el.checkList.appendChild(renderCheckItem(c, true));
  }
  function applyFix(fix) {
    if (fix.aisle != null) el.aisle.value = fix.aisle;
    if (fix.bay != null) el.bay.value = fix.bay;
    if (fix.backbone != null) $('p_backbone').value = fix.backbone;
    if (fix.gps) {
      draft.gps = { lat: fix.gps.lat, lon: fix.gps.lon, acc: null, at: new Date().toISOString(), source: 'photo', note: fix.gps.note };
      showGps(draft.gps);
    }
    renderChecks();
  }

  // ---------- the photo viewer: read a label by hand ----------
  const viewer = { photo: null, img: null, box: null, start: null, dragging: false };
  async function openViewer(p, box) {
    viewer.photo = p; viewer.box = box || null; viewer.img = null;
    el.viewer.hidden = false;
    document.body.style.overflow = 'hidden';
    el.viewerTitle.textContent = `Photo ${photoNo(p.id)}`;
    el.viewerMsg.textContent = 'Drag a box around a label to read it. A tap reads the area around it.';
    viewer.img = await photoImage(p);
    drawViewer();
  }
  function closeViewer() {
    el.viewer.hidden = true;
    document.body.style.overflow = '';
    viewer.photo = null;
  }
  function drawViewer() {
    if (!viewer.img || el.viewer.hidden) return;
    const c = el.viewerCanvas, img = viewer.img;
    const stage = c.parentElement.getBoundingClientRect();
    const s = Math.min(stage.width / img.naturalWidth, stage.height / img.naturalHeight);
    const cssW = Math.max(1, Math.floor(img.naturalWidth * s)), cssH = Math.max(1, Math.floor(img.naturalHeight * s));
    const dpr = window.devicePixelRatio || 1;
    c.style.width = cssW + 'px'; c.style.height = cssH + 'px';
    c.width = Math.round(cssW * dpr); c.height = Math.round(cssH * dpr);
    const g = c.getContext('2d');
    g.drawImage(img, 0, 0, c.width, c.height);
    const rect = (b, color, width) => {
      g.strokeStyle = color; g.lineWidth = width * dpr;
      g.strokeRect(b[0] * c.width, b[1] * c.height, (b[2] - b[0]) * c.width, (b[3] - b[1]) * c.height);
    };
    for (const l of draft.labels) if (l.photoId === viewer.photo.id) rect(l.box, 'rgba(52, 211, 153, .95)', 2);
    if (viewer.box) rect(viewer.box, '#facc15', 3);
  }
  function fracAt(e) {
    const r = el.viewerCanvas.getBoundingClientRect();
    return { x: Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)), y: Math.min(1, Math.max(0, (e.clientY - r.top) / r.height)) };
  }
  el.viewerCanvas.addEventListener('pointerdown', (e) => {
    if (!viewer.img) return;
    e.preventDefault();
    el.viewerCanvas.setPointerCapture(e.pointerId);
    viewer.start = fracAt(e); viewer.dragging = true; viewer.box = null;
  });
  el.viewerCanvas.addEventListener('pointermove', (e) => {
    if (!viewer.dragging) return;
    const p = fracAt(e), s = viewer.start;
    viewer.box = [Math.min(s.x, p.x), Math.min(s.y, p.y), Math.max(s.x, p.x), Math.max(s.y, p.y)];
    drawViewer();
  });
  el.viewerCanvas.addEventListener('pointerup', () => { if (viewer.dragging) { viewer.dragging = false; readViewerBox(); } });
  el.viewerCanvas.addEventListener('pointercancel', () => { viewer.dragging = false; });
  async function readViewerBox() {
    const p = viewer.photo;
    let b = viewer.box;
    if (!b || b[2] - b[0] < 0.02 || b[3] - b[1] < 0.01) {
      const { x, y } = viewer.start;
      const cl = (v) => Math.min(1, Math.max(0, v));
      b = [cl(x - 0.16), cl(y - 0.045), cl(x + 0.16), cl(y + 0.045)];
      viewer.box = b;
      drawViewer();
    }
    el.viewerMsg.textContent = 'Reading...';
    try {
      const text = await Labels.readBox(p.work || p.blob, b);
      const label = { id: newId(), photoId: p.id, box: b, text: text ? Labels.canonical(text) : '', source: 'tap' };
      draft.labels.push(label);
      renderLabels(); renderThumbs(); renderChecks();
      const facts = Labels.parseFacts(text);
      el.viewerMsg.textContent = facts.length ? `Read ${facts.map((f) => f.text).join(', ')}. It is in the labels list; correct it there if it is wrong.`
        : text ? `Read "${text.replace(/\n/g, ' ')}", which is not a rack, RMU or panel label. It is in the labels list so you can correct it.`
          : 'Nothing readable there. The crop is in the labels list: type what the label says.';
      if (viewer.photo === p) drawViewer();
    } catch (e) {
      el.viewerMsg.textContent = 'Could not read the label: ' + (e && e.message ? e.message : e);
    }
  }

  // ---------- GPS ----------
  function currentPosition() {
    return new Promise((resolve, reject) => {
      if (!navigator.geolocation) return reject(new Error('This device has no location service.'));
      navigator.geolocation.getCurrentPosition(resolve, reject, { enableHighAccuracy: true, timeout: 20000, maximumAge: 0 });
    });
  }
  function showGps(gps) {
    el.lat.value = gps ? gps.lat.toFixed(6) : '';
    el.lon.value = gps ? gps.lon.toFixed(6) : '';
    if (!gps) {
      el.gpsHint.textContent = SUITE ? "Filled from a photo's location data when the photos carry it. Otherwise type the site's coordinates, or use this computer's location."
        : 'Tap Get GPS fix, or type the coordinates in.';
      return;
    }
    const acc = gps.acc == null ? '' : gps.acc >= 1000 ? `${(gps.acc / 1000).toFixed(1)} km` : `${Math.round(gps.acc)} m`;
    el.gpsHint.textContent = gps.source === 'device'
      ? (SUITE ? `This computer's location, accurate to about ${acc}, taken ${localTime(gps.at)}. It is where the computer is, which is the site only if the computer is there.${gps.acc > 1000 ? ' That is too rough for a site; use the photos\' GPS or type it in.' : ''}`
        : `Phone GPS, accurate to about ${acc}, taken ${localTime(gps.at)}.`)
      : gps.source === 'photo' ? `Taken from ${gps.note || "a photo's GPS"}.` : 'Typed in by hand.';
  }
  async function getFix() {
    el.gpsBtn.disabled = true;
    el.gpsStatus.textContent = 'Locating...';
    try {
      const pos = await currentPosition();
      noteFix(pos);
      draft.gps = { lat: pos.coords.latitude, lon: pos.coords.longitude, acc: pos.coords.accuracy, at: new Date(pos.timestamp || Date.now()).toISOString(), source: 'device' };
      showGps(draft.gps);
      el.gpsStatus.textContent = 'Fix taken.';
      renderChecks();
    } catch (e) {
      const why = e && e.code === 1 ? 'Location permission was refused. Allow it in Settings > Privacy > Location Services.'
        : e && e.code === 3 ? 'Timed out waiting for a fix. Try again with a clearer view of the sky.'
          : (e && e.message) || 'No fix.';
      el.gpsStatus.textContent = '';
      el.gpsHint.textContent = why;
    } finally {
      el.gpsBtn.disabled = false;
    }
  }
  function readGpsFromForm() {
    const latText = clean(el.lat.value), lonText = clean(el.lon.value);
    if (!latText && !lonText) return null;
    const lat = Number(latText), lon = Number(lonText);
    if (!latText || !lonText || !Number.isFinite(lat) || !Number.isFinite(lon) || Math.abs(lat) > 90 || Math.abs(lon) > 180) {
      throw new Error('GPS needs both a latitude (-90 to 90) and a longitude (-180 to 180).');
    }
    if (draft.gps && draft.gps.source !== 'manual' && draft.gps.lat.toFixed(6) === lat.toFixed(6) && draft.gps.lon.toFixed(6) === lon.toFixed(6)) return draft.gps;
    return { lat, lon, acc: null, at: new Date().toISOString(), source: 'manual' };
  }

  // ---------- the form ----------
  function resetForm() {
    draft.photos = []; draft.labels = []; draft.gps = null; draft.editingId = null;
    images.clear();
    clearFields();
    showGps(null);
    el.gpsStatus.textContent = '';
    el.formTitle.textContent = 'New Location';
    el.saveBtn.textContent = 'Save location';
    el.cancelEditBtn.hidden = true;
    // Default the next location to whichever end is still missing.
    const next = !latest(savedRecs, 'A') ? 'A' : !latest(savedRecs, 'Z') ? 'Z' : draft.site;
    setSite(next, { prefillFields: true });
    renderThumbs(); renderLabels(); renderOcrStatus(); renderChecks();
  }
  async function saveCapture() {
    if (draft.busy) return;
    const initials = clean(el.initials.value).toUpperCase();
    if (!initials) { setMsg(el.saveMsg, 'Tester initials are required.', 'err'); el.initials.focus(); return; }
    let gps;
    try { gps = readGpsFromForm(); } catch (e) { setMsg(el.saveMsg, e.message, 'err'); return; }
    if (Labels.busy()) {
      setMsg(el.saveMsg, 'Finishing reading the labels...');
      el.saveBtn.disabled = true;
      await Labels.idle();
      el.saveBtn.disabled = false;
      setMsg(el.saveMsg, '');
    }
    const site = draft.site;
    if (site !== 'other') {
      // The form's own rules: floor 3 digits, room 4, aisle and bay 3 to 5.
      el.floor.value = padDigits(clean(el.floor.value), 3);
      el.room.value = padDigits(clean(el.room.value), 4);
      el.aisle.value = padDigits(clean(el.aisle.value), 3);
      el.bay.value = padDigits(clean(el.bay.value), 3);
    }
    const missing = [];
    if (!gps) missing.push('no GPS fix');
    if (!draft.photos.length) missing.push('no photos');
    if (missing.length && !window.confirm(`This location has ${missing.join(' and ')}. Save it anyway?`)) return;
    const bad = draftChecks().filter((c) => c.status === 'bad');
    if (bad.length && !window.confirm(`These labels do not match what was entered:\n\n${bad.map((c) => '- ' + c.text).join('\n')}\n\nSave anyway?`)) return;

    const now = new Date().toISOString();
    const rec = {
      createdAt: now, updatedAt: now, initials, site,
      room: clean(el.room.value), aisle: clean(el.aisle.value), bay: clean(el.bay.value),
      panel: site === 'other' ? {} : readPanel(), gps,
      photos: draft.photos.map((p) => ({ id: p.id, blob: p.blob, w: p.w, h: p.h, work: p.work, ww: p.ww, wh: p.wh, exifGps: p.exifGps || null, stamp: p.stamp || null, name: p.name, takenAt: p.takenAt,
        ocr: p.ocr === 'done' ? 'done' : 'off' })),
      labels: draft.labels.map((l) => ({ id: l.id, photoId: l.photoId, box: l.box, text: l.text, source: l.source })),
    };
    if (draft.editingId != null) {
      const old = await store.get(draft.editingId);
      rec.id = draft.editingId;
      rec.createdAt = old ? old.createdAt : now;
    }
    // One A-Location and one Z-Location per job: a new one replaces the old one.
    const replace = site === 'other' ? null : savedRecs.find((r) => r.site === site && r.id !== rec.id);
    if (replace && !window.confirm(`Replace the saved ${SITE_NAMES[site]} from ${localDate(replace.createdAt)} ${localTime(replace.createdAt).slice(0, 5)}?`)) return;
    try {
      await store.put(rec);
      if (replace) await store.del(replace.id);
    } catch (e) {
      setMsg(el.saveMsg, 'Could not save on this phone: ' + (e && e.message ? e.message : e), 'err');
      return;
    }
    try { localStorage.setItem(INITIALS_KEY, initials); } catch (e) { /* private mode */ }
    el.initials.value = initials;
    const wasEdit = draft.editingId != null;
    await renderSaved();
    resetForm();
    setMsg(el.saveMsg, wasEdit ? `${SITE_NAMES[site]} updated.` : `${SITE_NAMES[site]} saved with ${rec.photos.length} photo${rec.photos.length === 1 ? '' : 's'}.`, 'ok');
  }
  async function editCapture(id) {
    const rec = await store.get(id);
    if (!rec) return;
    draft.editingId = id;
    draft.photos = (rec.photos || []).map((p) => ({ ...p, id: p.id || newId(), ocr: p.ocr || 'off' }));
    draft.labels = (rec.labels || []).map((l) => ({ ...l }));
    draft.gps = rec.gps || null;
    images.clear();
    clearFields();
    setSite(rec.site || 'other');
    el.initials.value = rec.initials || '';
    el.room.value = rec.room || ''; el.aisle.value = rec.aisle || ''; el.bay.value = rec.bay || '';
    for (const [f, v] of Object.entries(rec.panel || {})) { const n = fieldEl(f); if (n) n.value = v || ''; }
    showGps(draft.gps);
    el.gpsStatus.textContent = '';
    renderThumbs(); renderLabels(); renderOcrStatus(); renderChecks();
    el.formTitle.textContent = `Editing the Saved ${{ A: 'A-Location', Z: 'Z-Location', other: 'Other Location' }[rec.site || 'other']}`;
    el.saveBtn.textContent = 'Save changes';
    el.cancelEditBtn.hidden = false;
    setMsg(el.saveMsg, '');
    el.captureCard.scrollIntoView({ behavior: 'smooth' });
  }

  // ---------- saved list ----------
  async function sortedRecords() {
    const recs = await store.all();
    recs.sort((a, b) => (a.createdAt < b.createdAt ? -1 : a.createdAt > b.createdAt ? 1 : 0));
    return recs;
  }
  function tally(list) {
    const n = (s) => list.filter((c) => c.status === s).length;
    const parts = [];
    if (n('bad')) parts.push(`${n('bad')} do not match`);
    if (n('todo')) parts.push(`${n('todo')} to review`);
    if (n('ok')) parts.push(`${n('ok')} match`);
    return parts.length ? 'Labels: ' + parts.join(', ') : '';
  }
  async function renderSaved() {
    savedRecs = await sortedRecords();
    resetPrepared();
    el.savedCount.textContent = String(savedRecs.length);
    el.savedList.textContent = '';
    if (!savedRecs.length) {
      const p = document.createElement('p');
      p.className = 'empty';
      p.textContent = 'Nothing saved yet. Saved locations stay on this phone until you delete them.';
      el.savedList.appendChild(p);
    }
    const i = fqa.info || {};
    savedRecs.forEach((r) => {
      const site = r.site || 'other';
      const li = document.createElement('li');
      const main = document.createElement('div');
      main.className = 'item-main';
      const strong = document.createElement('strong');
      const alias = site === 'A' ? i.aliasA : site === 'Z' ? i.aliasZ : '';
      if (site !== 'other') { const tag = document.createElement('span'); tag.className = 'tag'; tag.textContent = site; strong.append(tag); }
      strong.append(`${SITE_NAMES[site]}${alias ? ': ' + alias : ''}, ${r.initials}`);
      const span = document.createElement('span');
      const place = [r.panel && r.panel.floor && `Floor ${r.panel.floor}`, r.room && `Room ${r.room}`, r.aisle && `Aisle ${r.aisle}`, r.bay && `Bay ${r.bay}`, r.panel && r.panel.rmu && `RMU ${r.panel.rmu}`].filter(Boolean).join(', ');
      span.textContent = [`${localDate(r.createdAt)} ${localTime(r.createdAt).slice(0, 5)}`, place, r.gps ? gpsText(r.gps) : 'no GPS',
        `${r.photos.length} photo${r.photos.length === 1 ? '' : 's'}`, tally(recordChecks(r))].filter(Boolean).join(' | ');
      main.append(strong, span);
      const btns = document.createElement('div');
      btns.className = 'item-btns';
      const edit = document.createElement('button');
      edit.type = 'button'; edit.textContent = 'Edit';
      edit.addEventListener('click', () => editCapture(r.id));
      const del = document.createElement('button');
      del.type = 'button'; del.textContent = 'Delete';
      del.addEventListener('click', async () => {
        if (!window.confirm(`Delete the ${SITE_NAMES[site]} saved ${localDate(r.createdAt)} (${r.initials}, ${r.photos.length} photos)?`)) return;
        await store.del(r.id);
        if (draft.editingId === r.id) resetForm();
        renderSaved();
      });
      btns.append(edit, del);
      li.append(main, btns);
      el.savedList.appendChild(li);
    });
    renderSendSummary();
  }

  // ---------- sending the FQA ----------
  function resetPrepared() {
    prepared = null;
    preparedSheet = null;
    el.fqaSaveOnlyBtn.hidden = true;
    el.sendLinks.textContent = '';
    el.fqaBuildBtn.textContent = 'Prepare the FQA';
    el.exportBtn.textContent = 'Export capture sheet (.xlsx)';
  }
  function renderSendSummary() {
    el.sendSummary.textContent = '';
    const i = fqa.info || {};
    for (const site of ['A', 'Z']) {
      const r = latest(savedRecs, site);
      const alias = site === 'A' ? i.aliasA : i.aliasZ;
      const li = document.createElement('li');
      const strong = document.createElement('strong');
      strong.textContent = `${SITE_NAMES[site]}${alias ? ` (${alias})` : ''}`;
      if (!r) {
        li.className = 'todo';
        li.append(strong, 'Not captured yet. Its section 1.2 rows are left as the workbook has them.');
      } else {
        const list = recordChecks(r);
        li.className = list.some((c) => c.status === 'bad') ? 'bad' : list.some((c) => c.status === 'todo') ? 'todo' : 'ok';
        const where = [`aisle ${r.aisle || '?'}`, `bay ${r.bay || '?'}`, r.panel && r.panel.rmu && `RMU ${r.panel.rmu}`].filter(Boolean).join(', ');
        li.append(strong, `${where}, ${r.photos.length} photo${r.photos.length === 1 ? '' : 's'}, by ${r.initials}. ${tally(list)}.`);
      }
      el.sendSummary.appendChild(li);
    }
  }
  function panelFor(r) { return { ...(r.panel || {}), room: r.room, aisle: r.aisle, bay: r.bay }; }
  function fqaFileName() {
    if (fqa.blob) {
      const ext = /\.xlsx$/i.test(fqa.name) ? '.xlsx' : '.xlsm';
      return safeName(`${fqa.name.replace(/\.(xlsm|xlsx)$/i, '')} - field 1.2 ${stamp()}`) + ext;
    }
    const i = fqa.info || {};
    return safeName(`FQA Site Survey - ${i.aliasA || 'A'} to ${i.aliasZ || 'Z'} - field 1.2 ${stamp()}`) + '.xlsm';
  }
  function emailText(A, Z) {
    const i = fqa.info || {};
    const subject = `FQA Site Survey section 1.2: ${i.aliasA || 'A-Location'} to ${i.aliasZ || 'Z-Location'}`;
    const lines = ['Section 1.2 Fiber Panel Information, filled in the field.', ''];
    for (const [site, r] of [['A', A], ['Z', Z]]) {
      const alias = site === 'A' ? i.aliasA : i.aliasZ;
      if (!r) { lines.push(`${SITE_NAMES[site]}${alias ? ` (${alias})` : ''}: not captured.`, ''); continue; }
      const p = r.panel || {};
      lines.push(`${SITE_NAMES[site]}${alias ? ` (${alias})` : ''}, tester ${r.initials}, ${localDate(r.createdAt)} ${localTime(r.createdAt).slice(0, 5)}`);
      lines.push(`Rack: floor ${p.floor || '?'}, room ${r.room || '?'}, aisle ${r.aisle || '?'}, bay ${r.bay || '?'}${p.suite ? `, suite ${p.suite}` : ''}`);
      const panel = [p.rmu && `RMU ${p.rmu}`, p.block && `block ${p.block}`, p.portCount && `${p.portCount} ports`, p.termination, p.panelType, p.backbone && `backbone fibers ${p.backbone}`].filter(Boolean);
      if (panel.length) lines.push('Panel: ' + panel.join(', '));
      if (r.gps) lines.push(`GPS: ${gpsText(r.gps)}`);
      lines.push('Label check:');
      for (const c of recordChecks(r)) lines.push(`- ${WORD[c.status]}. ${c.title}: ${c.text}`);
      lines.push('');
    }
    const nA = A ? A.photos.length : 0, nZ = Z ? Z.photos.length : 0;
    if (nA + nZ) lines.push(`Photos on the Pictures tab: ${nA} from the A-Location, ${nZ} from the Z-Location.`);
    lines.push(`Workbook: ${fqa.name}`);
    return { subject, body: lines.join('\n') };
  }
  async function prepareFqa() {
    if (prepared) { if (SUITE) suiteSend(true); else sendFqa(); return; }
    const A = latest(savedRecs, 'A'), Z = latest(savedRecs, 'Z');
    if (!A && !Z) { setMsg(el.sendMsg, 'Save the A-Location or the Z-Location first.', 'err'); return; }
    const bad = [];
    for (const r of [A, Z]) if (r) for (const c of recordChecks(r)) if (c.status === 'bad') bad.push(`${SITE_NAMES[r.site]}: ${c.text}`);
    if (bad.length && !window.confirm(`These labels do not match what was entered:\n\n${bad.map((t) => '- ' + t).join('\n')}\n\nSend the FQA anyway? The email will list them.`)) return;
    el.fqaBuildBtn.disabled = true;
    setMsg(el.sendMsg, 'Filling in section 1.2...');
    try {
      const bytes = fqa.blob ? await fqa.blob.arrayBuffer() : await blankBytes();
      const i = fqa.info || {};
      const bands = [];
      for (const r of [A, Z]) {
        if (!r || !r.photos.length) continue;
        const alias = r.site === 'A' ? i.aliasA : i.aliasZ;
        const photos = [];
        for (const p of r.photos) photos.push({ bytes: new Uint8Array(await p.blob.arrayBuffer()), w: p.w, h: p.h, title: `${SITE_NAMES[r.site]} ${p.name}` });
        bands.push({ caption: `${SITE_NAMES[r.site]}${alias ? ': ' + alias : ''}, photos by ${r.initials}, ${localDate(r.createdAt)}`, photos });
      }
      const name = fqaFileName();
      const res = await FQA.build(bytes, name, { sites: { A: A && panelFor(A), Z: Z && panelFor(Z) }, bands });
      const file = new File([res.blob], name, { type: res.mime });
      prepared = { file, ...emailText(A, Z) };
      el.fqaBuildBtn.textContent = SUITE ? 'Save and email the FQA' : 'Email the FQA';
      el.fqaSaveOnlyBtn.hidden = !SUITE;
      setMsg(el.sendMsg, `Ready: ${name}. ${res.cells} section 1.2 cells filled, ${res.pictures} photo${res.pictures === 1 ? '' : 's'} added to the Pictures tab.`, 'ok');
    } catch (e) {
      setMsg(el.sendMsg, 'Could not fill in the FQA: ' + (e && e.message ? e.message : e), 'err');
    } finally {
      el.fqaBuildBtn.disabled = false;
    }
  }
  // Called straight from the tap, so iOS still counts it as the tech's action.
  function sendFqa() {
    const { file, subject, body } = prepared;
    const to = clean(el.emailTo.value);
    try { localStorage.setItem(EMAIL_KEY, to); } catch (e) { /* private mode */ }
    if (to && navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(to).catch(() => {});
    if (navigator.canShare && navigator.canShare({ files: [file] })) {
      navigator.share({ files: [file], title: subject, text: body })
        .then(() => setMsg(el.sendMsg, `Handed ${file.name} to the share sheet.${to ? ` ${to} is on the clipboard for the To line.` : ''}`, 'ok'))
        .catch((e) => {
          if (e && e.name === 'AbortError') { setMsg(el.sendMsg, 'Share cancelled. Tap Email the FQA to try again.'); return; }
          download(file);
          setMsg(el.sendMsg, `The share sheet failed (${e && e.message ? e.message : e}). ${file.name} was saved to Downloads instead.`, 'err');
        });
      return;
    }
    download(file);
    if (to) window.location.href = `mailto:${encodeURIComponent(to)}?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
    setMsg(el.sendMsg, `This browser cannot attach files to mail. ${file.name} was saved to Downloads${to ? '; attach it to the email that opened' : ''}.`, 'ok');
  }

  // ---------- saving on the PC (OTDR Suite) ----------
  const postJson = (url, obj) => fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(obj) })
    .then(async (r) => { const out = await r.json().catch(() => ({})); if (!r.ok) throw new Error(out.error || `the app answered ${r.status}`); return out; });
  async function suiteSave(file) {
    const r = await fetch('api/save?name=' + encodeURIComponent(file.name), { method: 'POST', body: file });
    const out = await r.json().catch(() => ({}));
    if (!r.ok || !out.path) throw new Error(out.error || `the app answered ${r.status}`);
    return out.path;
  }
  function showInFolder(holder, path) {
    holder.textContent = '';
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'inline';
    b.textContent = 'Show in folder';
    b.addEventListener('click', () => postJson('api/reveal', { path }).catch(() => {}));
    holder.append(b);
  }
  async function suiteSend(emailIt) {
    const { file, subject, body } = prepared;
    const to = clean(el.emailTo.value);
    try { localStorage.setItem(EMAIL_KEY, to); } catch (e) { /* private mode */ }
    el.fqaBuildBtn.disabled = el.fqaSaveOnlyBtn.disabled = true;
    setMsg(el.sendMsg, 'Saving the FQA...');
    try {
      const path = await suiteSave(file);
      showInFolder(el.sendLinks, path);
      if (!emailIt) { setMsg(el.sendMsg, `Saved ${path}.`, 'ok'); return; }
      const out = await postJson('api/email', { path, to, subject, body });
      if (out.opened) setMsg(el.sendMsg, `Saved ${path}. An email with the FQA attached is open in your mail program${to ? '' : '; add the address'}. Send it from there.`, 'ok');
      else setMsg(el.sendMsg, `Saved ${path} and the email ${out.eml}, but no mail program opened it (${out.error || 'no program for .eml files'}). Open the .eml file to send it.`, 'err');
    } catch (e) {
      setMsg(el.sendMsg, 'Could not save the FQA: ' + (e && e.message ? e.message : e), 'err');
    } finally {
      el.fqaBuildBtn.disabled = el.fqaSaveOnlyBtn.disabled = false;
    }
  }

  // ---------- capture sheet (production-sheet layout) ----------
  async function buildWorkbook(records) {
    if (typeof ExcelJS === 'undefined') throw new Error('The Excel library did not load. Open the app once while online, then try again.');
    const wb = new ExcelJS.Workbook();
    wb.creator = 'OTDR Field Capture';
    wb.created = new Date();
    const sub = wb.addWorksheet('Submissions', { views: [{ state: 'frozen', ySplit: 1 }] });
    sub.columns = [
      { header: '#', key: 'n', width: 5 },
      { header: 'Location', key: 'site', width: 14 },
      { header: 'Date', key: 'date', width: 12 },
      { header: 'Time', key: 'time', width: 10 },
      { header: 'Tester Initials', key: 'initials', width: 15 },
      { header: 'Room', key: 'room', width: 10 },
      { header: 'Aisle', key: 'aisle', width: 10 },
      { header: 'Bay', key: 'bay', width: 10 },
      { header: 'GPS Latitude', key: 'lat', width: 14 },
      { header: 'GPS Longitude', key: 'lon', width: 14 },
      { header: 'GPS Accuracy (m)', key: 'acc', width: 17 },
      { header: 'GPS (Lat, Lon)', key: 'gpsText', width: 26 },
      { header: 'GPS Source', key: 'src', width: 12 },
      { header: 'Photos', key: 'photos', width: 8 },
      { header: 'Sheet', key: 'sheet', width: 10 },
    ];
    sub.getRow(1).font = { bold: true };
    records.forEach((r, i) => {
      sub.addRow({
        n: i + 1, site: SITE_NAMES[r.site || 'other'], date: localDate(r.createdAt), time: localTime(r.createdAt), initials: r.initials,
        room: r.room || null, aisle: r.aisle || null, bay: r.bay || null,
        lat: r.gps ? r.gps.lat : null, lon: r.gps ? r.gps.lon : null,
        acc: r.gps && r.gps.acc != null ? Math.round(r.gps.acc) : null,
        gpsText: gpsText(r.gps) || null, src: gpsSource(r.gps),
        photos: r.photos.length, sheet: `Site ${i + 1}`,
      });
    });
    sub.getColumn('lat').numFmt = '0.000000';
    sub.getColumn('lon').numFmt = '0.000000';
    for (let i = 0; i < records.length; i++) await addSiteSheet(wb, records[i], i + 1);
    return wb.xlsx.writeBuffer();
  }
  const gpsSource = (g) => (!g ? 'No fix' : g.source === 'device' ? 'Phone GPS' : g.source === 'photo' ? 'Photo stamp' : 'Typed in');
  async function addSiteSheet(wb, r, n) {
    const ws = wb.addWorksheet(`Site ${n}`);
    ws.getColumn('C').width = 22;
    ws.getColumn('G').width = 30;
    ws.getColumn('L').width = PHOTO_COL_CHARS;
    const put = (addr, value, props) => { const c = ws.getCell(addr); c.value = value; if (props) Object.assign(c, props); };
    put('C3', 'Field Capture Worksheet', { font: { bold: true, size: 14 } });
    put('L3', 'Pictures Insert Below', { font: { bold: true } });
    put('C5', 'Location Information', { font: { bold: true } });
    put('C6', 'Date'); put('G6', localDate(r.createdAt));
    put('C7', 'Time'); put('G7', localTime(r.createdAt));
    put('C8', 'Address'); put('G8', gpsText(r.gps) || null);
    put('C9', 'GPS Latitude'); put('G9', r.gps ? r.gps.lat : null, { numFmt: '0.000000' });
    put('C10', 'GPS Longitude'); put('G10', r.gps ? r.gps.lon : null, { numFmt: '0.000000' });
    put('C11', 'GPS Accuracy (m)'); put('G11', r.gps && r.gps.acc != null ? Math.round(r.gps.acc) : null);
    put('C12', 'GPS Source'); put('G12', gpsSource(r.gps));
    put('C13', 'Location'); put('G13', SITE_NAMES[r.site || 'other']);
    put('C14', 'Room'); put('G14', r.room || null);
    put('C15', 'Aisle'); put('G15', r.aisle || null);
    put('C16', 'Bay'); put('G16', r.bay || null);
    put('C18', 'Installer Information', { font: { bold: true } });
    put('C19', 'Technician(s)'); put('G19', r.initials);
    put('C21', 'Pictures'); put('G21', r.photos.length);
    let row = 5;
    for (let k = 0; k < r.photos.length; k++) {
      const p = r.photos[k];
      const id = wb.addImage({ base64: await blobToDataUrl(p.blob), extension: 'jpeg' });
      const h = Math.max(1, Math.round(PHOTO_WIDTH_PX * p.h / p.w));
      const rows = h / ROW_PX;
      ws.getCell(row, 12).value = `Photo ${k + 1} of ${r.photos.length}, ${localDate(p.takenAt)} ${localTime(p.takenAt)}`;
      // Two-cell anchor: ExcelJS's one-cell form stamps an attribute the file format does not allow there.
      ws.addImage(id, { tl: { col: 11, row: row }, br: { col: 12, row: row + rows }, editAs: 'oneCell' });
      row += 1 + Math.ceil(rows) + 1;
    }
  }
  function exportFileName(records) {
    const who = (clean(el.initials.value) || (records[0] && records[0].initials) || 'capture').replace(/[^A-Za-z0-9]/g, '').toUpperCase();
    const d = new Date();
    return `OTDR_Field_${who}_${stamp()}_${pad(d.getHours())}${pad(d.getMinutes())}.xlsx`;
  }
  function download(file) {
    const url = URL.createObjectURL(file);
    const a = document.createElement('a');
    a.href = url; a.download = file.name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  }
  async function exportExcel() {
    if (SUITE) {
      if (!savedRecs.length) { setMsg(el.exportMsg, 'Nothing to export yet.', 'err'); return; }
      el.exportBtn.disabled = true;
      setMsg(el.exportMsg, 'Building the workbook...');
      try {
        const file = new File([await buildWorkbook(savedRecs)], exportFileName(savedRecs), { type: XLSX_MIME });
        const path = await suiteSave(file);
        setMsg(el.exportMsg, `Saved ${path}.`, 'ok');
      } catch (e) {
        setMsg(el.exportMsg, 'Export failed: ' + (e && e.message ? e.message : e), 'err');
      } finally {
        el.exportBtn.disabled = false;
      }
      return;
    }
    if (preparedSheet) {
      const file = preparedSheet;
      if (navigator.canShare && navigator.canShare({ files: [file] })) {
        navigator.share({ files: [file], title: file.name })
          .then(() => setMsg(el.exportMsg, `Shared ${file.name}.`, 'ok'))
          .catch((e) => {
            if (e && e.name === 'AbortError') { setMsg(el.exportMsg, 'Share cancelled.'); return; }
            download(file); setMsg(el.exportMsg, `Saved ${file.name} to Downloads.`, 'ok');
          });
      } else {
        download(file);
        setMsg(el.exportMsg, `Saved ${file.name} to this phone's Downloads (Files app).`, 'ok');
      }
      return;
    }
    if (!savedRecs.length) { setMsg(el.exportMsg, 'Nothing to export yet.', 'err'); return; }
    el.exportBtn.disabled = true;
    setMsg(el.exportMsg, 'Building the workbook...');
    try {
      const buf = await buildWorkbook(savedRecs);
      preparedSheet = new File([buf], exportFileName(savedRecs), { type: XLSX_MIME });
      el.exportBtn.textContent = 'Share the capture sheet';
      setMsg(el.exportMsg, `Ready: ${preparedSheet.name}. Tap Share the capture sheet.`, 'ok');
    } catch (e) {
      setMsg(el.exportMsg, 'Export failed: ' + (e && e.message ? e.message : e), 'err');
    } finally {
      el.exportBtn.disabled = false;
    }
  }

  // ---------- offline ----------
  // The label reader is about 15 MB. Fetch it once while online so the service worker
  // keeps it, and the app reads labels in an ILA with no signal.
  async function prefetchOffline() {
    if (!('caches' in window) || !navigator.onLine) return;
    for (const u of OFFLINE_ASSETS) {
      try { if (!(await caches.match(u))) await fetch(u); } catch (e) { /* try again next launch */ }
    }
  }

  // ---------- wiring ----------
  el.fqaOpenBtn.addEventListener('click', () => el.fqaInput.click());
  el.fqaInput.addEventListener('change', async () => {
    const f = el.fqaInput.files && el.fqaInput.files[0];
    el.fqaInput.value = '';
    if (!f) return;
    setMsg(el.fqaMsg, `Opening ${f.name}...`);
    try {
      const info = await useWorkbook(f.name, await f.arrayBuffer());
      setMsg(el.fqaMsg, `Using ${f.name}.${info.section12.A.aisle || info.section12.Z.aisle ? ' Its section 1.2 values fill empty boxes below.' : ''}`, 'ok');
    } catch (e) {
      setMsg(el.fqaMsg, 'Could not use that file: ' + (e && e.message ? e.message : e), 'err');
    }
  });
  el.fqaBlankBtn.addEventListener('click', async () => {
    if (!window.confirm('Go back to the blank Lumen form? The span FQA you opened is removed from this phone (the original file is not touched).')) return;
    await kv.del('fqa');
    await loadFqaState();
    setMsg(el.fqaMsg, 'Using the blank form.', 'ok');
    resetPrepared();
    renderSendSummary();
    renderChecks();
  });
  siteRadios.forEach((r) => r.addEventListener('change', () => { if (r.checked) setSite(r.value, { prefillFields: draft.editingId == null }); }));
  el.gpsBtn.addEventListener('click', getFix);
  el.cameraBtn.addEventListener('click', () => {
    if (stampOn()) freshFix(60000, 15000);   // start the fix now (asks permission once); the photo uses it
    el.cameraInput.click();
  });
  el.stampToggle.checked = stampOn();
  el.stampToggle.addEventListener('change', () => { try { localStorage.setItem(STAMP_KEY, el.stampToggle.checked ? 'on' : 'off'); } catch (e) { /* private mode */ } });
  el.libraryBtn.addEventListener('click', () => el.libraryInput.click());
  for (const input of [el.cameraInput, el.libraryInput]) {
    input.addEventListener('change', async () => { const files = input.files; await addPhotoFiles(files, input === el.cameraInput); input.value = ''; });
  }
  for (const f of RACK_FIELDS.concat(PANEL_FIELDS)) fieldEl(f).addEventListener('input', checksSoon);
  for (const f of PANEL_FIELDS) { const n = fieldEl(f); if (n.tagName === 'SELECT') n.addEventListener('change', checksSoon); }
  el.lat.addEventListener('input', checksSoon);
  el.lon.addEventListener('input', checksSoon);
  el.saveBtn.addEventListener('click', saveCapture);
  el.cancelEditBtn.addEventListener('click', () => { resetForm(); setMsg(el.saveMsg, ''); });
  el.fqaBuildBtn.addEventListener('click', prepareFqa);
  el.fqaSaveOnlyBtn.addEventListener('click', () => { if (prepared) suiteSend(false); });
  el.emailTo.addEventListener('change', () => { try { localStorage.setItem(EMAIL_KEY, clean(el.emailTo.value)); } catch (e) { /* private mode */ } });
  el.exportBtn.addEventListener('click', exportExcel);
  el.viewerClose.addEventListener('click', closeViewer);
  window.addEventListener('resize', drawViewer);
  el.clearBtn.addEventListener('click', async () => {
    const recs = await store.all();
    if (!recs.length) return;
    if (!window.confirm(`Delete all ${recs.length} saved locations from this phone? Send or export first if you have not.`)) return;
    await store.clear();
    await renderSaved();
    resetForm();
  });
  function updateNet() {
    const on = navigator.onLine;
    el.netState.textContent = on ? 'online' : 'offline';
    el.netState.className = 'pill ' + (on ? 'on' : 'off');
  }
  window.addEventListener('online', () => { updateNet(); if (!SUITE) prefetchOffline(); });
  window.addEventListener('offline', updateNet);

  // Startup.
  if (SUITE) {
    document.body.classList.add('suite');
    el.cameraBtn.hidden = true;
    el.stampRow.hidden = true;
    el.gpsBtn.textContent = "Use this computer's location";
    el.libraryBtn.textContent = 'Add photos';
    el.sendHint.textContent = 'Saves the FQA to the folder chosen above this page, then opens an email in your mail program with the FQA attached, ready to send.';
    el.exportHint.textContent = 'Saves to the same folder.';
    fetch('api/config').then((r) => r.json()).then((c) => {
      if (c.dest_dir) el.sendHint.textContent = `Saves the FQA to ${c.dest_dir}, then opens an email in your mail program with the FQA attached, ready to send.`;
    }).catch(() => {});
    // Browser errors reach the shared Slack channel, as the Viewer's do.
    let lastErr = 0;
    const report = (message, stack) => {
      if (Date.now() - lastErr < 60000) return;
      lastErr = Date.now();
      postJson('api/jserror', { message: String(message || ''), stack: String(stack || '') }).catch(() => {});
    };
    window.addEventListener('error', (e) => report(e.message, e.error && e.error.stack));
    window.addEventListener('unhandledrejection', (e) => report(e.reason && e.reason.message ? e.reason.message : e.reason, e.reason && e.reason.stack));
  }
  try {
    el.initials.value = localStorage.getItem(INITIALS_KEY) || '';
    el.emailTo.value = localStorage.getItem(EMAIL_KEY) || '';
  } catch (e) { /* private mode */ }
  updateNet();
  const ready = (async () => {
    await loadFqaState();
    await renderSaved();
    resetForm();
  })();
  if (!SUITE && navigator.permissions && navigator.permissions.query) {
    navigator.permissions.query({ name: 'geolocation' }).then((p) => { if (p.state === 'granted') getFix(); }).catch(() => {});
  }
  if (!SUITE && 'serviceWorker' in navigator && (location.protocol === 'https:' || ['localhost', '127.0.0.1'].includes(location.hostname))) {
    navigator.serviceWorker.register('sw.js').then(() => navigator.serviceWorker.ready).then(() => setTimeout(prefetchOffline, 3000)).catch(() => {});
  }

  // For tests and scripted checks.
  window.FieldCapture = { SUITE, ready, stampLines, freshFix, addPhotoFiles, buildWorkbook, store, kv, draft, renderSaved, saveCapture, sortedRecords, useWorkbook,
    prepareFqa, setSite, openViewer, readViewerBox, viewer, draftChecks, recordChecks, emailText, fqaState: () => fqa, prepared: () => prepared };
})();

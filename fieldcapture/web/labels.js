/* Label reading and label checks for OTDR Field Capture.

   Reading: Tesseract.js runs on the phone (no network, no account). Each photo is read
   whole at up to 3000 px, then every line that looks like part of a label is read again
   from a close crop, enlarged. The second read is what fixes small slips: on Flagler's
   full-size photos the whole-photo pass read "FIBERS 1 - 57" and the crop read
   "FIBERS 1 - 576". The tech can also drag a box over any label the app missed.

   What counts as a label (each is parsed from the text the tech can see and correct):
     rack    RR 100.07         floor/rack label: aisle 100, bay 07
     rmu     RMU 19            rack-unit tag on the rail
     fibers  FIBERS 577 - 1152 panel label: the fibre range on that panel
     toward  WEST TO FLAGLER   panel label: the far end of the span
     gps     39°22'37"N, 102°25'43"W   the GPS stamp a camera app burns into the photo

   Checks compare those against what the tech entered for section 1.2. A check is
     ok    the label and the entry agree
     bad   the label and the entry disagree (the app asks before saving or sending)
     todo  the label has not been read yet; photograph or tap it
     info  nothing to compare against */
'use strict';
(function () {
  const OCR_EDGE = 3000;          // long edge of the copy kept for reading labels
  const LINE_PX = 56;             // crops are enlarged until a text line is about this tall
  const GPS_NEAR_M = 2000;        // a photo stamped further than this from the fix is from somewhere else

  // ---------- the engine ----------
  let enginePromise = null;
  let chain = Promise.resolve();
  let pending = 0;
  const abs = (p) => new URL(p, document.baseURI).href;
  function engine() {
    if (!enginePromise) {
      if (typeof Tesseract === 'undefined') return Promise.reject(new Error('The label reader did not load. Open the app once while online.'));
      enginePromise = Tesseract.createWorker('eng', 1, {
        workerPath: abs('vendor/tesseract/worker.min.js'),
        corePath: abs('vendor/tesseract/core'),
        langPath: abs('vendor/tesseract/lang'),
        workerBlobURL: false,
        gzip: true,
      }).catch((e) => { enginePromise = null; throw e; });
    }
    return enginePromise;
  }
  // One read at a time: the engine is a single worker, and a phone has memory for one.
  function enqueue(fn) {
    pending += 1;
    const p = chain.then(fn, fn);
    chain = p.catch(() => {}).then(() => { pending -= 1; });
    return p;
  }
  const idle = () => chain;
  const busy = () => pending > 0;

  // ---------- images ----------
  function loadImage(blob) {
    return new Promise((resolve, reject) => {
      const url = URL.createObjectURL(blob);
      const img = new Image();
      img.onload = () => { URL.revokeObjectURL(url); resolve(img); };
      img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('Could not open the photo.')); };
      img.src = url;
    });
  }
  function grayCanvas(img, sx, sy, sw, sh, scale, pad, stretch) {
    const w = Math.max(1, Math.round(sw * scale)), h = Math.max(1, Math.round(sh * scale));
    const c = document.createElement('canvas');
    c.width = w + 2 * pad; c.height = h + 2 * pad;
    const g = c.getContext('2d', { willReadFrequently: true });
    g.fillStyle = '#fff';
    g.fillRect(0, 0, c.width, c.height);
    g.imageSmoothingQuality = 'high';
    g.drawImage(img, sx, sy, sw, sh, pad, pad, w, h);
    const d = g.getImageData(0, 0, c.width, c.height), p = d.data;
    for (let k = 0; k < p.length; k += 4) { const y = 0.299 * p[k] + 0.587 * p[k + 1] + 0.114 * p[k + 2]; p[k] = p[k + 1] = p[k + 2] = y; }
    if (stretch) {
      // Stretch the crop's own 2nd..98th percentile to full range: a label behind a
      // clear panel cover is grey on grey until this.
      const hist = new Uint32Array(256);
      for (let k = 0; k < p.length; k += 4) hist[p[k] | 0]++;
      const n = p.length / 4;
      let lo = 0, hi = 255, acc = 0;
      while (lo < 255 && (acc += hist[lo]) < n * 0.02) lo++;
      acc = 0;
      while (hi > 0 && (acc += hist[hi]) < n * 0.02) hi--;
      if (hi - lo > 20) for (let k = 0; k < p.length; k += 4) { const y = Math.max(0, Math.min(255, (p[k] - lo) * 255 / (hi - lo))); p[k] = p[k + 1] = p[k + 2] = y; }
    }
    g.putImageData(d, 0, 0);
    return c;
  }
  const cleanText = (s) => s.split('\n').map((l) => l.replace(/\s+/g, ' ').trim())
    .filter((l) => (l.match(/[A-Za-z0-9]/g) || []).length >= 2).join('\n');

  // Labels are printed in capitals, digits and a little punctuation. Limiting a crop's
  // read to those characters stops the reader turning a scratch into a letter.
  const LABEL_CHARS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-.,:/°\'"# ';
  const config = { whitelist: true, vote: true };
  async function recognize(canvas, psm, crop) {
    const w = await engine();
    await w.setParameters({ tessedit_pageseg_mode: psm, tessedit_char_whitelist: crop && config.whitelist ? LABEL_CHARS : '' });
    const r = await w.recognize(canvas, {}, { text: true, blocks: true });
    return r.data;
  }
  function linesOf(data) {
    const out = [];
    for (const b of data.blocks || []) for (const p of b.paragraphs || []) for (const l of p.lines || []) {
      if (l.text && l.text.trim()) out.push({ text: l.text.trim(), bbox: l.bbox, conf: l.confidence });
    }
    return out;
  }

  // Read one region of an image at one or more enlargements and page layouts. With
  // voting on, every reading is kept and the one whose label facts the other readings
  // agree with most wins; a single confident misread ("RMU 9" for RMU 19) is outvoted.
  async function readRegion(img, box, lineHeights) {
    const [x0, y0, x1, y1] = box;
    // Never enlarge a crop past a size the reader gets through quickly on a phone.
    const cap = Math.max(1, Math.min(1600 / (x1 - x0), 700 / (y1 - y0)));
    const scales = [...new Set(lineHeights.map((h) => Math.round(Math.min(8, cap, Math.max(1, LINE_PX / Math.max(3, h))) * 10) / 10))];
    const reads = [];
    for (const scale of scales) {
      const canvas = grayCanvas(img, x0, y0, x1 - x0, y1 - y0, scale, 20, true);
      for (const psm of ['6', '11', '7']) {
        const data = await recognize(canvas, psm, true);
        const text = cleanText(data.text || '');
        const keys = parseFacts(text).map((f) => f.text);
        reads.push({ text, keys, conf: data.confidence || 0 });
        if (!config.vote && keys.length && data.confidence > 80) return text;
        // Two readings that agree on every fact settle it.
        if (config.vote && keys.length && reads.slice(0, -1).some((r) => r.keys.join('|') === keys.join('|'))) return text;
      }
    }
    const tally = {};
    for (const r of reads) for (const k of new Set(r.keys)) tally[k] = (tally[k] || 0) + 1;
    let best = { text: '', score: -1 };
    for (const r of reads) {
      const score = config.vote ? r.keys.reduce((a, k) => a + tally[k], 0) * 100 + r.conf : r.keys.length * 100 + r.conf;
      if (score > best.score) best = { text: r.text, score };
    }
    return best.text;
  }

  const TRIGGER = /R\s?R\b|\bR\s?[MN]\s?[UV]|F[I1L]B|\bTO\b|°|\bRR|\d{3}\s?[.,]\s?\d{2,3}\b/i;

  // Find the text lines in one area of a photo, then re-read each line from its own
  // enlarged crop. area is in photo pixels. With every=false only lines that look like
  // part of a label are re-read (the whole-photo pass); with every=true all of them are,
  // nearest the middle of the area first (a box the tech drew).
  async function readArea(img, area, every) {
    const [ax0, ay0, ax1, ay1] = area;
    const W = img.naturalWidth, H = img.naturalHeight;
    // Lines are found at the photo's own scale, enlarged only when the whole photo is
    // small (a screenshot, or a copy pulled out of a workbook). Enlarging a crop of a
    // full-size photo makes its text too tall for the reader.
    const up = Math.min(3, Math.max(1, 1800 / Math.max(W, H)));
    const PAD = 20;
    const found = await recognize(grayCanvas(img, ax0, ay0, ax1 - ax0, ay1 - ay0, up, PAD, false), '11');
    const at = (v, o) => o + Math.max(0, v - PAD) / up;
    let lines = linesOf(found).map((l) => ({ ...l, bbox: { x0: at(l.bbox.x0, ax0), y0: at(l.bbox.y0, ay0), x1: at(l.bbox.x1, ax0), y1: at(l.bbox.y1, ay0) } }));
    if (every) {
      const cx = (ax0 + ax1) / 2, cy = (ay0 + ay1) / 2;
      const d = (l) => Math.hypot((l.bbox.x0 + l.bbox.x1) / 2 - cx, (l.bbox.y0 + l.bbox.y1) / 2 - cy);
      lines = lines.filter((l) => TRIGGER.test(l.text) || /\d/.test(l.text)).sort((a, b) => d(a) - d(b)).slice(0, 4);
    } else {
      lines = lines.filter((l) => TRIGGER.test(l.text));
    }
    const boxes = [];
    for (const l of lines) {
      const h = Math.max(4, l.bbox.y1 - l.bbox.y0);
      const box = [Math.max(0, l.bbox.x0 - 1.5 * h), Math.max(0, l.bbox.y0 - 1.6 * h), Math.min(W, l.bbox.x1 + 5 * h), Math.min(H, l.bbox.y1 + 1.6 * h), h, l.text];
      const hit = boxes.find((b) => overlap(b, box) > 0.35);
      if (hit) { hit[0] = Math.min(hit[0], box[0]); hit[1] = Math.min(hit[1], box[1]); hit[2] = Math.max(hit[2], box[2]); hit[3] = Math.max(hit[3], box[3]); hit[5] += '\n' + l.text; }
      else boxes.push(box);
    }
    const out = [];
    for (const b of boxes) {
      const text = await readRegion(img, b, [b[4], b[4] / 1.8]);
      const use = parseFacts(text).length >= parseFacts(cleanText(b[5])).length ? text : cleanText(b[5]);
      if (parseFacts(use).length) out.push({ box: b.slice(0, 4), text: canonical(use) });
    }
    return out;
  }

  // Camera apps burn the GPS into the picture as white text: Timestamp Camera along
  // the bottom, Solocator in a dark bar across the top. Keeping only near-white pixels
  // turns that text black on white. Read that way, the stamp on Flagler's full-size
  // photos comes out exactly ("N 39° 27'42.179", W 103° 1' 16.164""), where the plain
  // pass reads noise off the floor behind it. These stamps matter because such apps
  // blank the location data inside the file: Flagler's photos hold zeros there.
  function whiteMask(img, box, scale) {
    const [x0, y0, x1, y1] = box;
    const c = document.createElement('canvas');
    c.width = Math.max(1, Math.round((x1 - x0) * scale)); c.height = Math.max(1, Math.round((y1 - y0) * scale));
    const g = c.getContext('2d', { willReadFrequently: true });
    g.imageSmoothingQuality = 'high';
    g.drawImage(img, x0, y0, x1 - x0, y1 - y0, 0, 0, c.width, c.height);
    const d = g.getImageData(0, 0, c.width, c.height), p = d.data;
    for (let k = 0; k < p.length; k += 4) { const v = Math.min(p[k], p[k + 1], p[k + 2]) > 235 ? 0 : 255; p[k] = p[k + 1] = p[k + 2] = v; }
    g.putImageData(d, 0, 0);
    return c;
  }
  async function readStamps(img) {
    const W = img.naturalWidth, H = img.naturalHeight;
    const scale = Math.min(3, 1500 / W);
    const out = [];
    for (const [f0, f1] of [[0, 0.16], [0.78, 1]]) {
      const box = [0, f0 * H, W, f1 * H];
      const data = await recognize(whiteMask(img, box, scale), '6', false);
      const gps = parseFacts(cleanText(data.text || '')).filter((f) => f.kind === 'gps');
      if (gps.length) out.push({ box, text: gps.map((f) => f.text).join('\n') });
    }
    return out;
  }

  // Whole-photo pass plus the stamp pass.
  // Returns [{box: [x0, y0, x1, y1] as fractions of the photo, text}].
  function readPhoto(workBlob) {
    return enqueue(async () => {
      const img = await loadImage(workBlob);
      const W = img.naturalWidth, H = img.naturalHeight;
      const found = await readArea(img, [0, 0, W, H], false);
      const gpsOf = (t) => parseFacts(t).filter((f) => f.kind === 'gps');
      const have = found.flatMap((f) => gpsOf(f.text));
      for (const st of await readStamps(img)) {
        // Skip a stamp the whole-photo pass already read (the same place to within 30 m).
        if (gpsOf(st.text).every((g) => have.some((h) => metres(g, h) < 30))) continue;
        found.push(st);
      }
      return found.map((f) => ({ box: [f.box[0] / W, f.box[1] / H, f.box[2] / W, f.box[3] / H], text: f.text }));
    });
  }

  // The tech dragged a box over a label (or tapped, which makes a box around the tap):
  // read just that. box is in fractions. Returns the reading as text.
  function readBox(workBlob, box) {
    return enqueue(async () => {
      const img = await loadImage(workBlob);
      const W = img.naturalWidth, H = img.naturalHeight;
      // A finger-drawn box is rough: give it a margin so a tight box does not clip the
      // label's last digits.
      const bw = (box[2] - box[0]) * W, bh = (box[3] - box[1]) * H;
      const px = [Math.max(0, box[0] * W - 0.08 * bw), Math.max(0, box[1] * H - 0.15 * bh),
        Math.min(W, box[2] * W + 0.08 * bw), Math.min(H, box[3] * H + 0.15 * bh)];
      // First find the lines in the box and zoom on each, as the whole-photo pass does.
      const lines = await readArea(img, px, true);
      if (lines.length) return lines.map((l) => l.text).join('\n');
      // Otherwise read the box as one block, the text lines taken at a half, a third
      // and a fifth of its height.
      const h = px[3] - px[1];
      return readRegion(img, px, [h / 2.2, h / 3.5, h / 5]);
    });
  }

  function overlap(a, b) {
    const ix = Math.max(0, Math.min(a[2], b[2]) - Math.max(a[0], b[0]));
    const iy = Math.max(0, Math.min(a[3], b[3]) - Math.max(a[1], b[1]));
    const inter = ix * iy;
    const small = Math.min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]));
    return small > 0 ? inter / small : 0;
  }

  // The clean reading of a label: one line per fact, in the label's own wording.
  // Raw OCR text around a label carries stray marks; the crop beside it is the evidence.
  const canonical = (text) => { const f = parseFacts(text); return f.length ? f.map((x) => x.text).join('\n') : text; };

  // ---------- parsing ----------
  // OCR swaps look-alike characters inside numbers; only digit groups are repaired.
  const DIG = '[0-9OQDIL|]';
  const digits = (s) => s.replace(/[OQD]/g, '0').replace(/[IL|]/g, '1');
  const up = (s) => s.toUpperCase().replace(/[“”″]/g, '"').replace(/[‘’′`]/g, "'").replace(/[–—−]/g, '-').replace(/[º˚]/g, '°');

  function parseFacts(text) {
    const facts = [];
    if (!text) return facts;
    for (const raw of text.split('\n')) {
      const line = up(raw);
      let m;
      const rack = new RegExp(`(?:^|[^A-Z0-9])[A-Z0-9]?R\\s?[:#-]?\\s*(${DIG}{2,4})\\s?[.,]\\s?(${DIG}{1,4})(?![0-9])`, 'g');
      while ((m = rack.exec(line))) {
        const aisle = digits(m[1]), bay = digits(m[2]);
        if (/^\d+$/.test(aisle) && /^\d+$/.test(bay) && !/°/.test(line)) facts.push({ kind: 'rack', aisle, bay, text: `RR ${aisle}.${bay}`, meaning: `Aisle ${aisle}, bay ${bay}` });
      }
      const rmu = new RegExp(`\\bR\\s?[MN]\\s?[UV]\\s*[#:.-]?\\s*(${DIG}{1,2})(?![0-9])`, 'g');
      while ((m = rmu.exec(line))) {
        const n = digits(m[1]);
        if (/^\d+$/.test(n) && Number(n) > 0) facts.push({ kind: 'rmu', n: Number(n), text: `RMU ${Number(n)}`, meaning: `RMU ${Number(n)}` });
      }
      const fib = new RegExp(`\\bF[I1L]B[A-Z]{0,4}\\s*[:#]?\\s*(${DIG}{1,4})\\s*(?:-|TO|THRU|~)\\s*(${DIG}{1,4})(?![0-9])`, 'g');
      while ((m = fib.exec(line))) {
        const from = Number(digits(m[1])), to = Number(digits(m[2]));
        if (from >= 1 && to >= from && to <= 9999) facts.push({ kind: 'fibers', from, to, text: `FIBERS ${from}-${to}`, meaning: `Fibers ${from} to ${to}` });
      }
      const tow = /\b(EAST|WEST|VEST|NORTH|SOUTH)\s*(?:BOUND)?\s+TO\s+([A-Z][A-Z.' &-]{1,40})/g;
      while ((m = tow.exec(line))) {
        // Labels are printed in capitals and OCR noise is mostly lower case ("BETHUNE ils;"),
        // so when the text has capitals, the name is the run of capitalised words.
        const start = m.index + m[0].length - m[2].length;
        const rawName = raw.length === line.length ? raw.slice(start, start + m[2].length) : m[2];
        const caps = /[A-Z]/.test(rawName);
        const words = [];
        for (const w of rawName.split(/\s+/).filter(Boolean)) {
          if ((caps && !/^[A-Z][A-Z.'&-]*$/.test(w)) || w.length < 2 || /^FIB/i.test(w) || words.length === 3) break;
          words.push(w.toUpperCase());
        }
        const name = words.join(' ').replace(/[.'&-]+$/, '');
        const dir = m[1] === 'VEST' ? 'WEST' : m[1];
        if (name.length >= 3) facts.push({ kind: 'toward', dir, name, text: `${dir} TO ${name}`, meaning: `Far end: ${name}` });
      }
    }
    const gps = parseGps(up(text.replace(/\n/g, ' ')));
    if (gps) {
      const t = `${Math.abs(gps.lat).toFixed(6)}${gps.lat < 0 ? 'S' : 'N'} ${Math.abs(gps.lon).toFixed(6)}${gps.lon < 0 ? 'W' : 'E'}`;
      facts.push({ kind: 'gps', lat: gps.lat, lon: gps.lon, text: t, meaning: `GPS ${gps.lat.toFixed(5)}, ${gps.lon.toFixed(5)}` });
    }
    return facts;
  }

  // Degrees-minutes-seconds with the hemisphere letter before the number (N 39° 27' 42")
  // or after it (39°22'37"N), or decimal with N/S E/W. The two DMS styles are read
  // separately: read together, "N 39° 27' 42" W 103°..." would hand the W to the latitude.
  function parseGps(t) {
    const num = (d, m, s) => Number(d) + Number(m) / 60 + Number(String(s).replace(',', '.')) / 3600;
    const DMS = String.raw`(\d{1,3})\s*°\s*(\d{1,2})\s*['"]\s*(\d{1,2}(?:[.,]\d+)?)`;
    const styles = [
      Array.from(t.matchAll(new RegExp(String.raw`([NSEW])\s*` + DMS, 'g')), (m) => ({ hemi: m[1], v: num(m[2], m[3], m[4]) })),
      Array.from(t.matchAll(new RegExp(DMS + String.raw`\s*"?\s*([NSEW])`, 'g')), (m) => ({ hemi: m[4], v: num(m[1], m[2], m[3]) })),
      Array.from(t.matchAll(/([-+]?\d{1,3}\.\d{3,})\s*°?\s*([NSEW])/g), (m) => ({ hemi: m[2], v: Math.abs(Number(m[1])) })),
    ];
    for (const parts of styles) {
      const lat = parts.find((p) => p.hemi === 'N' || p.hemi === 'S');
      const lon = parts.find((p) => p.hemi === 'E' || p.hemi === 'W');
      if (lat && lon && lat.v <= 90 && lon.v <= 180) return { lat: lat.hemi === 'S' ? -lat.v : lat.v, lon: lon.hemi === 'W' ? -lon.v : lon.v };
    }
    return null;
  }

  // ---------- comparisons ----------
  const sameNum = (a, b) => {
    const x = String(a || '').trim(), y = String(b || '').trim();
    if (/^\d+$/.test(x) && /^\d+$/.test(y)) return Number(x) === Number(y);
    return x.toUpperCase() === y.toUpperCase();
  };
  const letters = (s) => String(s || '').toUpperCase().replace(/[^A-Z]/g, '');
  function lev(a, b) {
    const d = Array.from({ length: a.length + 1 }, (_, i) => [i]);
    for (let j = 1; j <= b.length; j++) d[0][j] = j;
    for (let i = 1; i <= a.length; i++) for (let j = 1; j <= b.length; j++) {
      d[i][j] = Math.min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
    }
    return d[a.length][b.length];
  }
  function sameName(a, b) {
    const x = letters(a), y = letters(b);
    if (!x || !y) return false;
    if (x === y) return true;
    if (Math.min(x.length, y.length) >= 4 && (x.startsWith(y) || y.startsWith(x) || x.includes(y) || y.includes(x))) return true;
    return lev(x, y) <= Math.max(1, Math.floor(Math.min(x.length, y.length) / 5));
  }
  function metres(a, b) {
    const R = 6371000, rad = Math.PI / 180;
    const dLat = (b.lat - a.lat) * rad, dLon = (b.lon - a.lon) * rad;
    const h = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dLon / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(h));
  }
  const distText = (m) => (m >= 1000 ? `${(m / 1000).toFixed(1)} km` : `${Math.round(m)} m`);
  const pad = (s, n) => (/^\d+$/.test(s) ? s.padStart(n, '0') : s);

  function parseRanges(s) {
    const out = [];
    for (const m of String(s || '').matchAll(/(\d{1,4})\s*(?:-|–|TO|THRU)\s*(\d{1,4})/gi)) out.push([Number(m[1]), Number(m[2])]);
    return out;
  }
  function merge(ranges) {
    const r = ranges.slice().sort((a, b) => a[0] - b[0]);
    const out = [];
    for (const [a, b] of r) {
      if (out.length && a <= out[out.length - 1][1] + 1) out[out.length - 1][1] = Math.max(out[out.length - 1][1], b);
      else out.push([a, b]);
    }
    return out;
  }
  const rangesText = (rs) => rs.map(([a, b]) => `${a}-${b}`).join(', ');
  const listText = (ns) => (ns.length <= 1 ? String(ns[0] ?? '') : `${ns.slice(0, -1).join(', ')} and ${ns[ns.length - 1]}`);

  // cap: {site, aisle, bay, gps, panel: {rmu, backbone, portCount}, labels: [{text, photoNo}]}
  // ctx: {aliasSelf, aliasFar, fiberCount, otherGps, otherName}
  function check(cap, ctx) {
    const facts = [];
    for (const l of cap.labels || []) for (const f of parseFacts(l.text)) facts.push({ ...f, photoNo: l.photoNo });
    const of = (k) => facts.filter((f) => f.kind === k);
    const panel = cap.panel || {};
    const out = [];

    // Rack label -> aisle and bay.
    const racks = of('rack');
    if (!racks.length) {
      out.push({ key: 'rack', title: 'Rack label', status: 'todo', text: 'No floor or rack label (RR aisle.bay) read yet. Photograph it, or tap it on a photo.' });
    } else {
      const r0 = racks[0];
      const fix = { aisle: pad(r0.aisle, 3), bay: pad(r0.bay, 3) };
      const fixLabel = `Use aisle ${fix.aisle}, bay ${fix.bay}`;
      if (!cap.aisle && !cap.bay) {
        out.push({ key: 'rack', title: 'Rack label', status: 'todo', text: `Photo ${r0.photoNo} reads ${r0.text}. Aisle and Bay are empty.`, fix, fixLabel });
      } else {
        const match = racks.find((r) => sameNum(r.aisle, cap.aisle) && sameNum(r.bay, cap.bay));
        if (match) out.push({ key: 'rack', title: 'Rack label', status: 'ok', text: `${match.text} on photo ${match.photoNo} matches aisle ${cap.aisle}, bay ${cap.bay}.` });
        else out.push({ key: 'rack', title: 'Rack label', status: 'bad', text: `Photo ${r0.photoNo} reads ${r0.text}, but you entered aisle ${cap.aisle || '(blank)'}, bay ${cap.bay || '(blank)'}.`, fix, fixLabel });
      }
    }

    // RMU tags -> the panel RMU(s). A tag can be hidden behind the panel it belongs to,
    // so a tag not read is left for the tech to review, never called a mismatch.
    const tags = [...new Set(of('rmu').map((f) => f.n))].sort((a, b) => a - b);
    const entered = [...new Set((String(panel.rmu || '').match(/\d{1,2}/g) || []).map(Number))];
    if (!entered.length) {
      out.push({ key: 'rmu', title: 'RMU tags', status: tags.length ? 'todo' : 'info',
        text: tags.length ? `RMU tags read: ${listText(tags)}. Enter the panel RMU (shelf) above.` : 'No panel RMU entered and no RMU tag read.' });
    } else {
      const seen = entered.filter((n) => tags.includes(n));
      const missing = entered.filter((n) => !tags.includes(n));
      if (!missing.length) out.push({ key: 'rmu', title: 'RMU tags', status: 'ok', text: `RMU ${listText(entered)} ${entered.length > 1 ? 'tags were' : 'tag was'} read on the photos.` });
      else {
        const read = tags.length ? ` Tags read: ${listText(tags)}.` : '';
        const got = seen.length ? `RMU ${listText(seen)} read. ` : '';
        out.push({ key: 'rmu', title: 'RMU tags', status: 'todo', text: `${got}RMU ${listText(missing)} not read yet. Tap ${missing.length > 1 ? 'those tags' : 'that tag'} on a photo.${read}` });
      }
    }

    // Panel fibre labels -> backbone fibre numbers, panel size and the span's fibre count.
    const ranges = [];
    for (const f of of('fibers')) if (!ranges.some((r) => r[0] === f.from && r[1] === f.to)) ranges.push([f.from, f.to]);
    ranges.sort((a, b) => a[0] - b[0]);
    if (!ranges.length) {
      out.push({ key: 'fibers', title: 'Panel fiber labels', status: 'todo', text: 'No panel fiber label (FIBERS 1 - 576) read yet.' });
    } else {
      const problems = [];
      for (let i = 1; i < ranges.length; i++) {
        const [a0, a1] = ranges[i - 1], [b0] = ranges[i];
        if (b0 <= a1) problems.push(`labels ${a0}-${a1} and ${ranges[i][0]}-${ranges[i][1]} overlap`);
        else if (b0 > a1 + 1) problems.push(`fibers ${a1 + 1}-${b0 - 1} are on no label`);
      }
      const port = Number(panel.portCount);
      if (port) for (const [a, b] of ranges) if (b - a + 1 > port) problems.push(`label ${a}-${b} holds more than a ${port}-port panel`);
      const union = merge(ranges);
      const unionText = rangesText(union);
      if (ctx.fiberCount && (union[0][0] !== 1 || union[union.length - 1][1] !== ctx.fiberCount) && union.length === 1) {
        problems.push(`the labels cover ${unionText} but the workbook says ${ctx.fiberCount} fibers`);
      }
      const typed = merge(parseRanges(panel.backbone));
      let fix = null;
      if (panel.backbone && typed.length && rangesText(typed) !== unionText) {
        problems.push(`you entered backbone fibers ${panel.backbone}`);
        fix = { backbone: unionText };
      }
      const labelList = ranges.map(([a, b]) => `${a}-${b}`).join(' and ');
      if (problems.length) {
        out.push({ key: 'fibers', title: 'Panel fiber labels', status: 'bad', text: `Panel labels read ${labelList}, but ${problems.join('; ')}.`, fix, fixLabel: fix ? `Use ${unionText}` : null });
      } else if (!panel.backbone) {
        out.push({ key: 'fibers', title: 'Panel fiber labels', status: 'ok', text: `Panel labels read ${labelList}, no gaps.`, fix: { backbone: unionText }, fixLabel: `Fill backbone fibers with ${unionText}` });
      } else {
        out.push({ key: 'fibers', title: 'Panel fiber labels', status: 'ok', text: `Panel labels read ${labelList}, matching backbone fibers ${panel.backbone}.` });
      }
    }

    // "WEST TO FLAGLER" -> the far end of the span.
    const names = [];
    for (const f of of('toward')) if (!names.some((n) => sameName(n.name, f.name))) names.push(f);
    if (names.length) {
      const wrong = names.filter((n) => ctx.aliasFar && !sameName(n.name, ctx.aliasFar));
      const self = names.filter((n) => ctx.aliasSelf && sameName(n.name, ctx.aliasSelf));
      if (self.length) out.push({ key: 'toward', title: 'Far-end label', status: 'bad', text: `Photo ${self[0].photoNo} reads ${self[0].text}, but ${ctx.aliasSelf} is this site. Is the photo from the other end?` });
      else if (wrong.length) out.push({ key: 'toward', title: 'Far-end label', status: 'bad', text: `Photo ${wrong[0].photoNo} reads ${wrong[0].text}, but the workbook's far end is ${ctx.aliasFar}.` });
      else if (ctx.aliasFar) out.push({ key: 'toward', title: 'Far-end label', status: 'ok', text: `Panels read ${names.map((n) => n.text).join(', ')}, which matches the far end, ${ctx.aliasFar}.` });
      else out.push({ key: 'toward', title: 'Far-end label', status: 'info', text: `Panels read ${names.map((n) => n.text).join(', ')}. The workbook names no far-end site to compare.` });
    }

    // Photo locations -> this location's GPS fix. Two sources: the GPS stamp a
    // camera app burns into the picture (read like any label), and the location
    // a phone camera writes into the JPEG file itself (EXIF, read on the PC).
    // (cap.exif carries the photo's own record: the app's camera stamp, or the file's location data.)
    const stamps = of('gps').concat((cap.exif || []).map((e) => ({ kind: 'gps', lat: e.lat, lon: e.lon, photoNo: e.photoNo, exif: e.how !== 'stamp', own: e.how === 'stamp' })));
    const where = (s) => (s.own ? `Photo ${s.photoNo}'s camera stamp` : s.exif ? `Photo ${s.photoNo}'s location data` : `Photo ${s.photoNo}'s GPS stamp`);
    if (stamps.length) {
      if (!cap.gps) {
        const s = stamps[0];
        out.push({ key: 'gps', title: 'Photo GPS', status: 'todo', text: `${where(s)} reads ${s.lat.toFixed(6)}, ${s.lon.toFixed(6)}, and this location has no GPS fix.`, fix: { gps: { lat: s.lat, lon: s.lon, note: s.exif ? `photo ${s.photoNo}'s location data` : `photo ${s.photoNo}'s GPS stamp` } }, fixLabel: 'Use the photo GPS' });
      } else {
        const far = stamps.map((s) => ({ s, d: metres(s, cap.gps) })).sort((a, b) => b.d - a.d)[0];
        if (far.d > GPS_NEAR_M) {
          const nearer = ctx.otherGps && metres(far.s, ctx.otherGps) < far.d ? ` It is closer to the ${ctx.otherName}.` : '';
          out.push({ key: 'gps', title: 'Photo GPS', status: 'bad', text: `${where(far.s)} puts it ${distText(far.d)} from this location's GPS fix.${nearer}` });
        } else {
          out.push({ key: 'gps', title: 'Photo GPS', status: 'ok', text: `The photos' GPS is within ${distText(far.d)} of the fix.` });
        }
      }
    }
    return out;
  }

  window.Labels = { config, readPhoto, readBox, parseFacts, canonical, check, engine, idle, busy, loadImage, OCR_EDGE };
})();

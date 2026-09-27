"""
Surgical cell writer for the Lumen FQA workbook
===============================================

The FQA Site Survey is a macro-enabled Lumen form.  Besides the VBA that
opens and closes its sections, the file carries four customXml parts, a
Microsoft sensitivity label (docMetadata/LabelInfo.xml), cell comments,
printer settings and the drawings that hold the site photos.

openpyxl cannot round-trip any of that.  Loading Span 4's workbook with
keep_vba=True and saving it straight back drops 25 of the 65 zip parts --
including the sensitivity label and the Pictures tab's drawing.  A form
that goes to the customer cannot lose those, so this module never opens
the workbook as a workbook.  It treats the .xlsm as what it is on disk: a
zip of XML parts.  We rewrite the bytes of the sheets we touch and copy
every other part through verbatim.

What a patch does to a sheet part:

  * finds <c r="B12"> in its row, or inserts one in column order
  * keeps the cell's s= style index, so the form still looks like the form
  * drops any <f> formula child -- a literal replaces it
  * writes strings inline (t="inlineStr"), so xl/sharedStrings.xml is
    never touched and its indices stay valid for every cell we did not
    write

Two bookkeeping parts have to move with the values:

  * xl/calcChain.xml is deleted.  It is a cached evaluation order; if a
    cell that used to hold a formula now holds a literal the chain is
    stale, and Excel repairs the file (with a warning dialog) instead of
    opening it.  Excel rebuilds the chain silently when it is absent.
    Its <Override> in [Content_Types].xml and its relationship in
    xl/_rels/workbook.xml.rels go with it, so nothing in the package
    points at a part that is not there.
  * <calcPr fullCalcOnLoad="1"> is set in xl/workbook.xml.  The Submittal
    Checklist's Y/N cells are formulas over the tabs we write, and without
    this they show the values Excel cached before we wrote anything.

A rewritten part keeps Excel's own namespace prefixes (see _serialize).
ElementTree left to itself renames them ns1, ns2 ... and drops the ones
no element uses, which strands the prefixes listed in mc:Ignorable.
Excel for Mac then calls the whole workbook corrupt and will not open
it, repaired or not.

Robert, 2026-09-23.
"""
from __future__ import annotations

import re
import shutil
import threading
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable

import xml.etree.ElementTree as ET

MAIN_NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
REL_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PKG_REL_NS = 'http://schemas.openxmlformats.org/package/2006/relationships'

_Q = '{%s}' % MAIN_NS

CALC_CHAIN = 'xl/calcChain.xml'
_CALC_CHAIN_TYPE = REL_NS + '/calcChain'

# Excel's serial-date epoch.  1900-based workbooks count 1899-12-30 as day 0
# (the off-by-one is Lotus's leap-year bug, which Excel keeps on purpose).
_EPOCH = date(1899, 12, 30)


def _col_to_index(col: str) -> int:
    """'A' -> 1, 'AB' -> 28."""
    n = 0
    for ch in col:
        n = n * 26 + (ord(ch.upper()) - 64)
    return n


def _index_to_col(idx: int) -> str:
    """1 -> 'A', 28 -> 'AB'."""
    out = ''
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        out = chr(65 + rem) + out
    return out


_REF_RE = re.compile(r'^([A-Za-z]+)(\d+)$')


def split_ref(ref: str) -> tuple[str, int]:
    """'AB12' -> ('AB', 12).  Raises on anything else."""
    m = _REF_RE.match(ref.strip())
    if not m:
        raise ValueError(f'not a cell reference: {ref!r}')
    return m.group(1).upper(), int(m.group(2))


# ── writing a part back with its own prefixes ─────────────────────────────
#
# Excel's sheet and workbook parts open like this:
#
#   <worksheet xmlns="...main" xmlns:mc="...markup-compatibility/2006"
#              mc:Ignorable="x14ac xr xr2 xr3" xmlns:x14ac="..."
#              xmlns:xr="..." xmlns:xr2="..." xmlns:xr3="...">
#
# mc:Ignorable names prefixes, not URIs, and the Markup Compatibility rules
# (ECMA-376 Part 3) require each one to be declared.  ElementTree keeps no
# prefixes: it writes ns1:Ignorable="x14ac xr xr2 xr3", renames x14ac to
# ns3, and drops xr2 and xr3 outright because no element uses them.  Four
# undeclared prefixes, and Excel for Mac refuses the file as corrupt.
#
# lxml keeps prefixes, but it is not in the exe, and the fqa modules reach
# installed exes through auto-update: an import the bundle does not carry
# would break the FQA page on every machine until a reinstall.  So this
# stays on the standard library and puts the prefixes back itself.

_DECL_RE = re.compile(
    rb'\sxmlns(?::([A-Za-z_][\w.\-]*))?\s*=\s*(?:"([^"]*)"|\'([^\']*)\')')
_START_TAG_RE = re.compile(
    rb'<[A-Za-z_][^\s/>]*'
    rb'(?:\s+[^\s=/>]+\s*=\s*(?:"[^"]*"|\'[^\']*\'))*\s*/?>')
_ELEMENT_START = re.compile(rb'<(?=[A-Za-z_])')
_ENCODING_RE = re.compile(rb'encoding\s*=\s*["\']([^"\']+)["\']')
_ET_INTERNAL_PREFIX = re.compile(r'ns\d+$')

# ET.register_namespace edits one module-wide table.  Registering a part's
# prefixes and writing it out happen under this lock, so two builds running
# in the hub at once cannot hand each other their prefixes.
_ET_LOCK = threading.Lock()


def _declarations(xml: bytes) -> dict[str, str]:
    """Every xmlns declaration in `xml`, prefix -> URI ('' = default)."""
    out = {}
    for m in _DECL_RE.finditer(xml):
        prefix = (m.group(1) or b'').decode('ascii')
        uri = m.group(2) if m.group(2) is not None else m.group(3)
        out.setdefault(prefix, uri.decode('utf-8'))
    return out


def _root_tag(xml: bytes):
    """The match for the document element's start tag, or None."""
    start = _ELEMENT_START.search(xml)
    return _START_TAG_RE.match(xml, start.start()) if start else None


def _open_tag(tag: bytes) -> ET.Element:
    """Parse a lone start tag as an empty element (name + attributes).

    A root tag declares every prefix its own name and attributes use, so
    it parses by itself; compared as Elements, two tags that differ only
    in prefixes come out equal.
    """
    if not tag.endswith(b'/>'):
        tag = tag[:-1] + b'/>'
    return ET.fromstring(tag)


def _serialize(root: ET.Element, source: bytes) -> bytes:
    """Write `root` back as the part it was parsed from.

    Two steps.  First every prefix the source declares is registered, so
    ElementTree names each namespace the element tree still uses as Excel
    did.  Then the source's prolog and root start tag replace
    ElementTree's: that restores the declarations nothing uses any more
    (xr2, xr3), the standalone="yes" declaration and the attribute order,
    byte for byte.  The root's own name and attributes are never patched,
    and that is checked, not assumed: if they differ, ElementTree's root
    tag is kept and the source's missing declarations are added to it.

    ElementTree moves every declaration to the root, so one that a
    descendant made locally is added to the restored root tag as well.
    """
    with _ET_LOCK:
        for prefix, uri in _declarations(source).items():
            if prefix == 'xml' or _ET_INTERNAL_PREFIX.match(prefix):
                continue
            ET.register_namespace(prefix, uri)
        out = ET.tostring(root, encoding='UTF-8', xml_declaration=True)

    src_m, out_m = _root_tag(source), _root_tag(out)
    if src_m is None or out_m is None:
        return out
    src_tag, out_tag = src_m.group(0), out_m.group(0)
    have, need = _declarations(src_tag), _declarations(out_tag)

    a, b = _open_tag(src_tag), _open_tag(out_tag)
    same_root = (a.tag == b.tag and a.attrib == b.attrib
                 and src_tag.endswith(b'/>') == out_tag.endswith(b'/>')
                 and all(have.get(p, u) == u for p, u in need.items()))
    tag = src_tag if same_root else out_tag
    in_tag = _declarations(tag)
    extra = b''.join(
        b' xmlns%s="%s"' % ((b':' + p.encode('ascii')) if p else b'',
                            u.encode('utf-8'))
        for p, u in {**need, **have}.items() if p not in in_tag)
    if extra:
        cut = len(tag) - (2 if tag.endswith(b'/>') else 1)
        tag = tag[:cut] + extra + tag[cut:]

    # The source's prolog (`<?xml ... standalone="yes"?>` and its CRLF) is
    # only true of our bytes if it declares the UTF-8 we just wrote.
    prolog = source[:src_m.start()]
    enc = _ENCODING_RE.search(prolog)
    if enc and enc.group(1).decode('ascii').lower().replace('-', '') != 'utf8':
        prolog = out[:out_m.start()]
    return prolog + tag + out[out_m.end():]


@dataclass(frozen=True)
class Formula:
    """A value written as a formula rather than a literal.

    The FQA form wires much of itself together -- the Event Log's 'from Z'
    column is $W$8 minus 'from A', the FAT's rack columns point at the
    Site Survey Data tab, the Submittal Checklist reads all of it.  Where
    the form already computes a cell we write its formula back rather than
    a number, so the workbook keeps working when a reviewer edits a value
    by hand.
    """
    text: str          # without the leading '='


@dataclass(frozen=True)
class Cell:
    """One value bound for one cell of one sheet.

    `sheet` is the sheet's display name as the tab shows it, not the part
    name -- the caller should never have to know that 'Event Log' lives in
    xl/worksheets/sheet5.xml.
    """
    sheet: str
    ref: str
    value: object


class WorkbookPatch:
    """Reads an .xlsm, applies cell writes, writes a new .xlsm.

    Every part except the sheets we touched (plus workbook.xml,
    calcChain.xml and the two parts that point at calcChain, which the
    writes force us to touch) is copied through with its original bytes.
    """

    def __init__(self, path: str):
        self.path = path
        with zipfile.ZipFile(path) as z:
            self._names = z.namelist()
            self._parts = {n: z.read(n) for n in self._names}
            self._infos = {i.filename: i for i in z.infolist()}
        self._sheet_parts = self._map_sheets()

    # ── sheet name -> part name ────────────────────────────────────────
    def _map_sheets(self) -> dict[str, str]:
        """Resolve tab names to worksheet parts through workbook.xml's rels.

        The sheet order in workbook.xml is the tab order; the part each one
        lives in comes from r:id, which only xl/_rels/workbook.xml.rels can
        answer.  Sheet parts are NOT numbered in tab order in this file
        (the Event Log is sheet5.xml but the fifth tab is 'Event Log' only
        by coincidence), so the rels lookup is the only correct route.
        """
        wb = ET.fromstring(self._parts['xl/workbook.xml'])
        rels = ET.fromstring(self._parts['xl/_rels/workbook.xml.rels'])
        target = {}
        for rel in rels:
            rid = rel.get('Id')
            tgt = (rel.get('Target') or '').lstrip('/')
            if tgt.startswith('./'):
                tgt = tgt[2:]
            if not tgt.startswith('xl/'):
                tgt = 'xl/' + tgt
            target[rid] = tgt
        out = {}
        rid_attr = '{%s}id' % REL_NS
        for sheet in wb.iter(_Q + 'sheet'):
            rid = sheet.get(rid_attr)
            name = sheet.get('name')
            if name and rid and rid in target:
                out[name] = target[rid]
        return out

    @property
    def sheet_names(self) -> list[str]:
        return list(self._sheet_parts)

    # ── the write ──────────────────────────────────────────────────────
    def set_cells(self, cells: Iterable[Cell]) -> None:
        by_sheet: dict[str, list[Cell]] = {}
        for c in cells:
            by_sheet.setdefault(c.sheet, []).append(c)
        for sheet, group in by_sheet.items():
            part = self._sheet_parts.get(sheet)
            if part is None:
                raise KeyError(
                    f'no sheet named {sheet!r} in {self.path} '
                    f'(have: {", ".join(self.sheet_names)})')
            self._parts[part] = self._patch_sheet(self._parts[part], group)

    def _patch_sheet(self, xml: bytes, cells: list[Cell]) -> bytes:
        root = ET.fromstring(xml)
        data = root.find(_Q + 'sheetData')
        if data is None:
            raise ValueError('worksheet has no sheetData')

        rows = {int(r.get('r')): r for r in data.findall(_Q + 'row') if r.get('r')}

        for cell in cells:
            col, rownum = split_ref(cell.ref)
            row = rows.get(rownum)
            if row is None:
                row = self._insert_row(data, rownum)
                rows[rownum] = row
            c = self._find_or_insert_cell(row, col, rownum)
            self._write_value(c, cell.value)

        return _serialize(root, xml)

    @staticmethod
    def _insert_row(data: ET.Element, rownum: int) -> ET.Element:
        """Add <row r="N"/> keeping sheetData in ascending row order.

        Excel tolerates out-of-order rows in most builds but the ECMA-376
        schema says ascending, and an out-of-order row is one of the things
        that trips the repair dialog, so we place it properly.
        """
        row = ET.Element(_Q + 'row', {'r': str(rownum)})
        kids = list(data)
        for i, existing in enumerate(kids):
            r = existing.get('r')
            if r and int(r) > rownum:
                data.insert(i, row)
                return row
        data.append(row)
        return row

    @staticmethod
    def _find_or_insert_cell(row: ET.Element, col: str, rownum: int) -> ET.Element:
        ref = f'{col}{rownum}'
        want = _col_to_index(col)
        for i, c in enumerate(row.findall(_Q + 'c')):
            cref = c.get('r') or ''
            m = _REF_RE.match(cref)
            if not m:
                continue
            idx = _col_to_index(m.group(1))
            if idx == want:
                return c
            if idx > want:
                new = ET.Element(_Q + 'c', {'r': ref})
                row.insert(i, new)
                return new
        new = ET.Element(_Q + 'c', {'r': ref})
        row.append(new)
        return new

    @staticmethod
    def _write_value(c: ET.Element, value) -> None:
        """Replace a cell's content, keeping its style.

        The s= attribute is the index into xl/styles.xml -- the font, fill,
        border and number format the form author chose.  Dropping it would
        leave our value in the workbook's default style, which on this form
        means a white cell where the tech expects a tan or blue one.  So s=
        survives and everything else about the cell is rebuilt.
        """
        ref = c.get('r')
        style = c.get('s')
        for child in list(c):
            c.remove(child)
        c.attrib.clear()
        if ref:
            c.set('r', ref)
        if style is not None:
            c.set('s', style)

        if value is None or value == '':
            c.attrib.pop('t', None)
            return

        if isinstance(value, Formula):
            # No cached <v>: the workbook is saved with fullCalcOnLoad, so
            # Excel evaluates it on open.  Writing a stale cached value
            # would be worse than writing none.
            f = ET.SubElement(c, _Q + 'f')
            f.text = value.text.lstrip('=')
            return

        if isinstance(value, bool):
            c.set('t', 'b')
            v = ET.SubElement(c, _Q + 'v')
            v.text = '1' if value else '0'
            return

        if isinstance(value, (int, float)):
            v = ET.SubElement(c, _Q + 'v')
            v.text = repr(value) if isinstance(value, float) else str(value)
            return

        if isinstance(value, datetime):
            value = value.date()
        if isinstance(value, date):
            # Written as the serial number the cell's own number format
            # renders.  Writing the ISO text instead would show up as a
            # left-aligned string in a date-formatted cell.
            v = ET.SubElement(c, _Q + 'v')
            v.text = str((value - _EPOCH).days)
            return

        c.set('t', 'inlineStr')
        is_el = ET.SubElement(c, _Q + 'is')
        t = ET.SubElement(is_el, _Q + 't')
        text = str(value)
        if text != text.strip():
            t.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
        t.text = text

    # ── stripping a finished package back to a template ────────────────
    def strip_media(self) -> int:
        """Remove every embedded image, leaving the drawing anchors empty.

        A template is built from somebody's finished submittal, and a
        finished submittal has the site photos in it -- Span 4 carries
        780 KB of Flagler and Bethune ILA.  Shipping those inside a blank
        form would put one customer's site pictures into the next
        customer's package.

        The drawing PARTS stay (emptied), because each one is the target
        of a sheet relationship; deleting them would leave a dangling
        r:id and Excel would offer to repair the file.  Emptying them
        leaves a valid, picture-free drawing.

        Returns the number of image parts removed.
        """
        media = [n for n in self._names if n.startswith('xl/media/')]
        drawings = [n for n in self._names
                    if n.startswith('xl/drawings/drawing')
                    and n.endswith('.xml')]
        empty = (b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
                 b'<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/'
                 b'drawingml/2006/spreadsheetDrawing" xmlns:a="http://'
                 b'schemas.openxmlformats.org/drawingml/2006/main"/>')
        for d in drawings:
            self._parts[d] = empty
        drop = set(media)
        drop |= {n for n in self._names
                 if n.startswith('xl/drawings/_rels/drawing')}
        self._names = [n for n in self._names if n not in drop]
        for n in drop:
            self._parts.pop(n, None)
        return len(media)

    # ── parts, for writers that add more than cell values ─────────────
    def sheet_part(self, sheet: str) -> str:
        part = self._sheet_parts.get(sheet)
        if part is None:
            raise KeyError(
                f'no sheet named {sheet!r} in {self.path} '
                f'(have: {", ".join(self.sheet_names)})')
        return part

    def has_part(self, name: str) -> bool:
        return name in self._parts

    def part(self, name: str) -> bytes:
        return self._parts[name]

    def set_part(self, name: str, data: bytes) -> None:
        """Replace an existing part's bytes."""
        if name not in self._parts:
            raise KeyError(name)
        self._parts[name] = data

    def put_part(self, name: str, data: bytes) -> None:
        """Replace a part, or add it when the package has none by that name.

        OPC part names are case-insensitive, so a new name that differs from
        an existing one only in case is refused rather than written twice."""
        if name not in self._parts:
            if name.lower() in {n.lower() for n in self._names}:
                raise ValueError(f'{name} clashes with an existing part')
            self._names.append(name)
            stamp = self._infos.get(_CONTENT_TYPES) or next(iter(self._infos.values()))
            info = zipfile.ZipInfo(name, date_time=stamp.date_time)
            info.external_attr = stamp.external_attr
            self._infos[name] = info
        self._parts[name] = data

    def new_part_name(self, stem: str, ext: str) -> str:
        """stem + the lowest unused number + ext: 'xl/media/image' + 3 + '.png'."""
        # Numbers are unique across extensions too (no image1.png beside
        # image1.jpeg), which is how Excel names them.
        taken = {n.lower().rsplit('.', 1)[0] for n in self._names}
        n = 1
        while f'{stem}{n}'.lower() in taken:
            n += 1
        return f'{stem}{n}{ext}'

    def ensure_default_content_type(self, ext: str, content_type: str) -> None:
        """Add <Default Extension=ext> to [Content_Types].xml if missing."""
        ct = self._parts[_CONTENT_TYPES]
        root = ET.fromstring(ct)
        for d in root:
            if _local(d.tag) == 'Default' and (d.get('Extension') or '').lower() == ext.lower():
                return
        self._parts[_CONTENT_TYPES] = _insert_first_child(
            ct, b'<Default Extension="%s" ContentType="%s"/>' % (
                ext.encode('ascii'), content_type.encode('ascii')))

    def ensure_override_content_type(self, part: str, content_type: str) -> None:
        ct = self._parts[_CONTENT_TYPES]
        root = ET.fromstring(ct)
        for o in root:
            if _local(o.tag) == 'Override' and \
                    (o.get('PartName') or '').lower() == '/' + part.lower():
                return
        self._parts[_CONTENT_TYPES] = _append_child(
            ct, b'Types', b'<Override PartName="/%s" ContentType="%s"/>' % (
                part.encode('utf-8'), content_type.encode('ascii')))

    def _drawing_of(self, sheet_part: str, create: bool = False) -> str | None:
        """The drawing part a sheet shows, creating and wiring one up when
        the sheet has none and `create` is set."""
        srels = _rels_name(sheet_part)
        if srels in self._parts:
            for m in re.finditer(rb'<Relationship\b[^>]*>', self._parts[srels]):
                attrs = _open_tag(m.group(0)).attrib
                if attrs.get('Type') == _DRAWING_TYPE and \
                        attrs.get('TargetMode') != 'External':
                    target = _resolve(sheet_part, attrs.get('Target', ''))
                    if target in self._parts:
                        return target
        if not create:
            return None

        drawing = self.new_part_name('xl/drawings/drawing', '.xml')
        self.put_part(drawing, _XML_PROLOG +
                      b'<xdr:wsDr xmlns:xdr="%s" xmlns:a="%s"/>' % (
                          SS_DRAWING_NS.encode(), DML_NS.encode()))
        self.ensure_override_content_type(drawing, _DRAWING_CT)

        rels = self._parts.get(srels) or (
            _XML_PROLOG + b'<Relationships xmlns="' + PKG_REL_NS.encode()
            + b'"></Relationships>')
        rid = _next_rid(rels)
        self.put_part(srels, _append_child(
            rels, b'Relationships',
            b'<Relationship Id="%s" Type="%s" Target="%s"/>' % (
                rid.encode(), _DRAWING_TYPE.encode(),
                _relative(sheet_part, drawing).encode())))

        source = self._parts[sheet_part]
        root = ET.fromstring(source)
        old = root.find(_Q + 'drawing')
        if old is not None:                   # a dangling one; repoint it
            old.set('{%s}id' % REL_NS, rid)
        else:
            el = ET.Element(_Q + 'drawing', {'{%s}id' % REL_NS: rid})
            at = len(root)
            for i, child in enumerate(root):
                name = _local(child.tag)
                if child.tag == '{%s}AlternateContent' % _MC_NS:
                    first = [c for c in child.iter() if c is not child
                             and not c.tag.startswith('{%s}' % _MC_NS)]
                    name = _local(first[0].tag) if first else name
                if name in _AFTER_DRAWING:
                    at = i
                    break
            root.insert(at, el)
        self._parts[sheet_part] = _serialize(root, source)
        return drawing

    # ── save ───────────────────────────────────────────────────────────
    def save(self, out_path: str) -> None:
        self._force_full_recalc()
        self._drop_calc_chain()
        with zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED) as z:
            for name in self._names:
                info = self._infos[name]
                # Carry the original timestamp and external attributes so the
                # rebuilt package differs from the source only where we
                # actually changed a part.
                zi = zipfile.ZipInfo(name, date_time=info.date_time)
                zi.compress_type = zipfile.ZIP_DEFLATED
                zi.external_attr = info.external_attr
                z.writestr(zi, self._parts[name])

    def _force_full_recalc(self) -> None:
        """Set <calcPr fullCalcOnLoad="1"/> in xl/workbook.xml.

        Without it the Submittal Checklist keeps the Y/N answers Excel
        cached for whatever span the template was last saved with, which is
        exactly the sort of quietly-wrong cell this whole tool exists to
        stop.
        """
        source = self._parts['xl/workbook.xml']
        root = ET.fromstring(source)
        calc = root.find(_Q + 'calcPr')
        if calc is None:
            calc = ET.SubElement(root, _Q + 'calcPr')
        calc.set('fullCalcOnLoad', '1')
        self._parts['xl/workbook.xml'] = _serialize(root, source)

    def _drop_calc_chain(self) -> None:
        """Delete xl/calcChain.xml and both things that point at it.

        The part alone is not enough: [Content_Types].xml would still
        declare a type for it and workbook.xml.rels would still link to
        it.  Both are edited as bytes, one element each, so the rest of
        those two parts stays exactly as Lumen's form has it.
        """
        self._names = [n for n in self._names if n != CALC_CHAIN]
        self._parts.pop(CALC_CHAIN, None)

        ct = '[Content_Types].xml'
        if ct in self._parts:
            self._parts[ct] = _drop_elements(
                self._parts[ct], b'Override',
                lambda a: a.get('PartName') == '/' + CALC_CHAIN)
        rels = 'xl/_rels/workbook.xml.rels'
        if rels in self._parts:
            self._parts[rels] = _drop_elements(
                self._parts[rels], b'Relationship',
                lambda a: a.get('Type') == _CALC_CHAIN_TYPE)


def _drop_elements(xml: bytes, name: bytes, match) -> bytes:
    """Remove every empty `<name .../>` element whose attributes satisfy
    `match`, leaving every other byte of the part alone."""
    rx = re.compile(rb'<' + re.escape(name) +
                    rb'(?:\s+[^\s=/>]+\s*=\s*(?:"[^"]*"|\'[^\']*\'))*\s*/>')

    def keep(m):
        return b'' if match(_open_tag(m.group(0)).attrib) else m.group(0)
    return rx.sub(keep, xml)


def copy_template(src: str, dst: str) -> None:
    shutil.copyfile(src, dst)


# ── site photos on a sheet ────────────────────────────────────────────────
#
# The FQA form's Pictures tab is a drawing: each photo is an anchor in
# xl/drawings/drawingN.xml whose <a:blip r:embed> names an image part in
# xl/media through the drawing's own .rels.  Adding photos therefore touches
# at most five parts -- the drawing, its .rels, [Content_Types].xml (only
# when an image extension or a new drawing needs declaring), the sheet's
# .rels and the sheet itself (only when it has no drawing yet) -- plus one
# new media part per photo.  Everything else still copies through verbatim.
#
# The drawing and the .rels parts are edited as BYTES: new elements are
# spliced in before the closing tag and nothing already there is re-written.
# A drawing can carry shapes with mc:AlternateContent and a14/a16 extension
# prefixes, and routing it through ElementTree would move or rename those
# declarations (see _serialize); splicing never reads them.
#
# Photo sizes come from the image headers with the standard library.  Pillow
# is used only when it is importable, and only to bake an EXIF rotation into
# the pixels and to shrink a camera original: Excel draws the stored pixels
# and ignores EXIF orientation, so a sideways phone JPEG would land sideways.

SS_DRAWING_NS = ('http://schemas.openxmlformats.org/drawingml/2006/'
                 'spreadsheetDrawing')
DML_NS = 'http://schemas.openxmlformats.org/drawingml/2006/main'
_DRAWING_TYPE = REL_NS + '/drawing'
_IMAGE_TYPE = REL_NS + '/image'
_DRAWING_CT = 'application/vnd.openxmlformats-officedocument.drawing+xml'
_IMAGE_CT = {'jpeg': 'image/jpeg', 'png': 'image/png'}
_EMU_PER_PX = 9525                  # at 96 dpi
_EMU_PER_PT = 12700
_CONTENT_TYPES = '[Content_Types].xml'
_XML_PROLOG = b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'

# A camera original is 4000 px and 3-5 MB; the package goes by email, and a
# photo on this tab is drawn about 400 px across.  Shrunk (when Pillow is
# there to do it) to the long edge the phone capture already uses.
_MAX_LONG_EDGE_PX = 1600


@dataclass(frozen=True)
class Photo:
    """One site photo bound for the Pictures tab.

    `end` is 'A' or 'Z'.  `data` is the image file's bytes (JPEG or PNG).
    `caption` becomes the picture's alt text (Excel's Alt Text pane and the
    screen-reader description); it is not written into a cell, because text
    on the tab is how a reader tells which end a photo belongs to.
    """
    end: str
    data: bytes
    caption: str = ''
    name: str = ''          # where it came from, for error messages only

    @classmethod
    def from_dict(cls, d: dict) -> 'Photo':
        """{'end': 'A'|'Z', 'path': ... or 'data': bytes, 'caption'?: str}."""
        end = str(d.get('end') or '').strip().upper()
        if end not in ('A', 'Z'):
            raise ValueError(f"photo end must be 'A' or 'Z', got {d.get('end')!r}")
        data, name = d.get('data'), ''
        if data is None:
            path = d.get('path')
            if not path:
                raise ValueError('photo needs a path or data')
            with open(path, 'rb') as fh:
                data = fh.read()
            name = str(path)
        return cls(end=end, data=bytes(data), caption=str(d.get('caption') or ''),
                   name=name or str(d.get('name') or ''))


def _u16(b, i, big):
    return int.from_bytes(b[i:i + 2], 'big' if big else 'little')


def _u32(b, i, big):
    return int.from_bytes(b[i:i + 4], 'big' if big else 'little')


def _exif_orientation(tiff: bytes) -> int:
    """The Orientation tag (0x0112) of IFD0 in an Exif TIFF block, or 1."""
    try:
        big = tiff[:2] == b'MM'
        if not big and tiff[:2] != b'II':
            return 1
        off = _u32(tiff, 4, big)
        for k in range(_u16(tiff, off, big)):
            e = off + 2 + 12 * k
            if _u16(tiff, e, big) == 0x0112:
                v = _u16(tiff, e + 8, big)
                return v if 1 <= v <= 8 else 1
    except (IndexError, ValueError):
        pass
    return 1


def image_info(data: bytes) -> tuple[str, int, int, int]:
    """(format, width, height, exif orientation) from the file header.

    format is 'jpeg' or 'png'; width and height are the STORED pixels.
    Raises ValueError for anything else, since Excel's picture support past
    those two is uneven and the phone never produces anything else.
    """
    if data[:8] == b'\x89PNG\r\n\x1a\n' and data[12:16] == b'IHDR':
        return 'png', _u32(data, 16, True), _u32(data, 20, True), 1
    if data[:2] != b'\xff\xd8':
        raise ValueError('not a JPEG or PNG image')
    i, orientation = 2, 1
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            break
        while i < len(data) and data[i] == 0xFF:        # fill bytes
            i += 1
        marker = data[i]
        i += 1
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:     # no length
            continue
        if marker in (0xD9, 0xDA):                       # end / scan data
            break
        seglen = _u16(data, i, True)
        seg = data[i + 2:i + seglen]
        if marker == 0xE1 and seg[:6] == b'Exif\x00\x00':
            orientation = _exif_orientation(seg[6:])
        elif 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            return 'jpeg', _u16(seg, 3, True), _u16(seg, 1, True), orientation
        i += seglen
    raise ValueError('JPEG has no frame header')


def _normalise_image(data: bytes) -> tuple[bytes, str, int, int]:
    """(bytes, format, displayed width px, displayed height px).

    With Pillow: an EXIF rotation is baked into the pixels and an oversize
    original is shrunk.  Without it the bytes go in as they are and the
    displayed size is the stored one, which is what Excel will draw.
    """
    fmt, w, h, orient = image_info(data)
    if orient == 1 and max(w, h) <= _MAX_LONG_EDGE_PX:
        return data, fmt, w, h
    try:
        import io
        from PIL import Image, ImageOps
    except Exception:                        # noqa: BLE001 - optional
        return data, fmt, w, h
    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
        if max(img.size) > _MAX_LONG_EDGE_PX:
            img.thumbnail((_MAX_LONG_EDGE_PX, _MAX_LONG_EDGE_PX))
        buf = io.BytesIO()
        if fmt == 'jpeg':
            img.convert('RGB').save(buf, 'JPEG', quality=88)
        else:
            img.save(buf, 'PNG')
        return buf.getvalue(), fmt, img.size[0], img.size[1]
    except Exception:                        # noqa: BLE001 - keep original
        return data, fmt, w, h


def fit_box(w: int, h: int, box_w: int, box_h: int) -> tuple[int, int]:
    """Scale (w, h) to fit inside the box, keeping the aspect ratio.
    Never enlarges past the box; a small image is scaled UP to fill it,
    because a thumbnail-sized photo is useless to a reviewer."""
    s = min(box_w / w, box_h / h)
    return max(1, round(w * s)), max(1, round(h * s))


def _rels_name(part: str) -> str:
    d, f = part.rsplit('/', 1)
    return f'{d}/_rels/{f}.rels'


def _resolve(part: str, target: str) -> str:
    import posixpath
    if target.startswith('/'):
        return target.lstrip('/')
    return posixpath.normpath(posixpath.join(posixpath.dirname(part), target))


def _relative(from_part: str, to_part: str) -> str:
    import posixpath
    return posixpath.relpath(to_part, posixpath.dirname(from_part))


def _xml_attr(text: str) -> str:
    return (text.replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;')
            .replace('\r', '&#13;').replace('\n', '&#10;'))


def _append_child(xml: bytes, root_local: bytes, fragment: bytes) -> bytes:
    """Insert `fragment` as the last children of the root element, touching
    no other byte.  A self-closing root (`<x:wsDr .../>`, how the template
    ships its empty drawings) is opened to take them."""
    m = _root_tag(xml)
    if m is None:
        raise ValueError('part has no root element')
    tag = m.group(0)
    if tag.endswith(b'/>'):
        name = re.match(rb'<([^\s/>]+)', tag).group(1)
        opened = tag[:-2].rstrip() + b'>'
        return (xml[:m.start()] + opened + fragment + b'</' + name + b'>'
                + xml[m.end():])
    close = re.compile(rb'</(?:[A-Za-z_][\w.\-]*:)?' + re.escape(root_local)
                       + rb'\s*>\s*$')
    cm = close.search(xml)
    if cm is None:
        raise ValueError('part has no closing root tag')
    return xml[:cm.start()] + fragment + xml[cm.start():]


def _insert_first_child(xml: bytes, fragment: bytes) -> bytes:
    """Insert `fragment` right after the root's start tag.  [Content_Types]
    lists its Defaults before its Overrides, so a new Default goes first."""
    m = _root_tag(xml)
    if m is None or m.group(0).endswith(b'/>'):
        raise ValueError('part has no open root element')
    return xml[:m.end()] + fragment + xml[m.end():]


def _prefix_for(decls: dict[str, str], uri: str, want: str) -> tuple[str, bytes]:
    """The prefix `decls` already binds to `uri`, or `want` plus the
    declaration the inserted fragment has to carry itself."""
    for p, u in decls.items():
        if u == uri and p:
            return p, b''
    p = want
    while p in decls:
        p += '_'
    return p, b' xmlns:%s="%s"' % (p.encode('ascii'), uri.encode('utf-8'))


def _next_rid(rels_xml: bytes) -> str:
    used = {int(n) for n in re.findall(rb'\bId\s*=\s*"rId(\d+)"', rels_xml)}
    n = 1
    while n in used:
        n += 1
    return f'rId{n}'


class _SheetGeometry:
    """Column widths and row heights of one sheet, in EMU.

    Used to stack photos down the tab without overlapping and to fill the
    a:off hint.  Widths follow Excel's own rule for the stored <col width>
    (characters of the default font's maximum digit width, 7 px for the
    Calibri/Arial 10-11 this form uses, plus 5 px of padding)."""

    def __init__(self, sheet_xml: bytes):
        root = ET.fromstring(sheet_xml)
        fmt = root.find(_Q + 'sheetFormatPr')
        self.default_row_pt = float(fmt.get('defaultRowHeight', 15)) if fmt is not None else 15.0
        dcw = fmt.get('defaultColWidth') if fmt is not None else None
        self.default_col_px = (self._chars_to_px(float(dcw)) if dcw else 64)
        self.cols: dict[int, int] = {}            # 0-based col -> px
        cols = root.find(_Q + 'cols')
        if cols is not None:
            for c in cols.findall(_Q + 'col'):
                lo, hi = int(c.get('min')), int(c.get('max'))
                px = (0 if c.get('hidden') in ('1', 'true')
                      else self._chars_to_px(float(c.get('width', 8.43))))
                for i in range(lo - 1, min(hi, lo + 200)):
                    self.cols[i] = px
        self.rows: dict[int, float] = {}          # 0-based row -> pt
        data = root.find(_Q + 'sheetData')
        if data is not None:
            for r in data.findall(_Q + 'row'):
                if r.get('ht') and r.get('r'):
                    self.rows[int(r.get('r')) - 1] = (
                        0.0 if r.get('hidden') in ('1', 'true') else float(r.get('ht')))

    @staticmethod
    def _chars_to_px(width: float) -> int:
        return int(width * 7 + 0.5)

    def col_emu(self, i: int) -> int:
        return self.cols.get(i, self.default_col_px) * _EMU_PER_PX

    def row_emu(self, i: int) -> int:
        return int(self.rows.get(i, self.default_row_pt) * _EMU_PER_PT)

    def x_of(self, col: int) -> int:
        return sum(self.col_emu(i) for i in range(col))

    def y_of(self, row: int) -> int:
        return sum(self.row_emu(i) for i in range(row))

    def row_at_or_below(self, y: int, start: int = 0) -> int:
        """The first row whose top edge is at or below y."""
        r, top = start, self.y_of(start)
        while top < y:
            top += self.row_emu(r)
            r += 1
        return r

    def free_row(self, row: int, row_off: int, cy: int) -> int:
        """The first row wholly below a picture cy EMU tall anchored at
        (row, row_off)."""
        return self.row_at_or_below(self.y_of(row) + row_off + cy, row)


_ANCHOR_RE = re.compile(
    rb'<(?:[A-Za-z_][\w.\-]*:)?(twoCellAnchor|oneCellAnchor)\b.*?'
    rb'</(?:[A-Za-z_][\w.\-]*:)?\1\s*>', re.S)
_NUM = rb'<(?:[\w.\-]+:)?%s>\s*(-?\d+)\s*</'


def _anchor_bottoms(drawing_xml: bytes, geo: _SheetGeometry) -> list[tuple[int, int]]:
    """(from col, first row wholly below it) of every cell-anchored shape
    already drawn."""
    out = []
    for m in _ANCHOR_RE.finditer(drawing_xml):
        body = m.group(0)
        cols = [int(x) for x in re.findall(_NUM % b'col', body)]
        rows = [int(x) for x in re.findall(_NUM % b'row', body)]
        offs = [int(x) for x in re.findall(_NUM % b'rowOff', body)]
        if not cols or not rows:
            continue
        if m.group(1) == b'twoCellAnchor' and len(rows) >= 2:
            out.append((cols[0], rows[1] + 1))
        else:
            ext = re.search(rb'<(?:[\w.\-]+:)?ext\s[^>]*\bcy\s*=\s*"(\d+)"', body)
            cy = int(ext.group(1)) if ext else 0
            out.append((cols[0], geo.free_row(rows[0], offs[0] if offs else 0, cy)))
    return out


# Where a <drawing> element may go in a worksheet: before the first of
# these (CT_Worksheet's sequence, ECMA-376 Part 1, 18.3.1.99).
_AFTER_DRAWING = ('legacyDrawing', 'legacyDrawingHF', 'drawingHF', 'picture',
                  'oleObjects', 'controls', 'webPublishItems', 'tableParts',
                  'extLst')
_MC_NS = 'http://schemas.openxmlformats.org/markup-compatibility/2006'


def _local(tag: str) -> str:
    return tag.rsplit('}', 1)[-1]


def add_photos(patch: 'WorkbookPatch', photos, *, sheet: str,
               a_col: str, z_col: str, first_row: int,
               box_px: tuple[int, int] = (400, 400),
               gap_rows: int = 1) -> int:
    """Put `photos` on `sheet` of `patch`: A-end photos stacked down column
    `a_col`, Z-end photos down `z_col`, starting at 1-based row `first_row`
    (or below any picture already in that column), each fitted into
    `box_px` (width, height) keeping its aspect ratio, `gap_rows` blank rows
    apart.  The box is narrowed so an A photo never runs into the Z column.

    Appends to the sheet's drawing when it has one (existing shapes are left
    byte for byte), or creates the drawing and wires it to the sheet when it
    has none.  Returns the number of photos placed.
    """
    photos = [p if isinstance(p, Photo) else Photo.from_dict(p) for p in photos]
    if not photos:
        return 0
    sheet_part = patch.sheet_part(sheet)
    drawing = patch._drawing_of(sheet_part, create=True)
    drels = _rels_name(drawing)
    geo = _SheetGeometry(patch.part(sheet_part))

    dxml = patch.part(drawing)
    decls = _declarations(dxml[:_root_tag(dxml).end()])
    xdr, xdr_decl = _prefix_for(decls, SS_DRAWING_NS, 'xdr')
    a, a_decl = _prefix_for(decls, DML_NS, 'a')
    ns_decl = xdr_decl + a_decl

    used_ids = [int(x) for x in re.findall(rb'<(?:[\w.\-]+:)?cNvPr\s[^>]*?\bid\s*=\s*"(\d+)"', dxml)]
    next_id = max(used_ids + [1]) + 1

    ca, cz = _col_to_index(a_col) - 1, _col_to_index(z_col) - 1
    left, right = sorted((ca, cz))
    gutter = 8 * _EMU_PER_PX
    lane_emu = sum(geo.col_emu(i) for i in range(left, right)) - gutter
    box_w = min(box_px[0], max(1, lane_emu // _EMU_PER_PX))
    box_h = box_px[1]

    bottoms = _anchor_bottoms(dxml, geo)
    next_row = {}
    for end, col in (('A', ca), ('Z', cz)):
        below = [r + gap_rows for c, r in bottoms if c == col]
        next_row[end] = max([first_row - 1] + below)

    rels_xml = patch.part(drels) if patch.has_part(drels) else (
        _XML_PROLOG + b'<Relationships xmlns="' + PKG_REL_NS.encode()
        + b'"></Relationships>')

    frags = []
    for n, photo in enumerate(photos):
        if photo.end not in ('A', 'Z'):
            raise ValueError(f"photo end must be 'A' or 'Z', got {photo.end!r}")
        try:
            data, fmt, w, h = _normalise_image(photo.data)
        except ValueError as exc:
            raise ValueError(f'photo {photo.name or n + 1}: {exc}') from None
        media = patch.new_part_name('xl/media/image', '.' + fmt)
        patch.put_part(media, data)
        patch.ensure_default_content_type(fmt, _IMAGE_CT[fmt])

        rid = _next_rid(rels_xml)
        rels_xml = _append_child(
            rels_xml, b'Relationships',
            b'<Relationship Id="%s" Type="%s" Target="%s"/>' % (
                rid.encode(), _IMAGE_TYPE.encode(),
                _relative(drawing, media).encode()))

        pw, ph = fit_box(w, h, box_w, box_h)
        cx, cy = pw * _EMU_PER_PX, ph * _EMU_PER_PX
        col = ca if photo.end == 'A' else cz
        row = next_row[photo.end]
        next_row[photo.end] = geo.free_row(row, 0, cy) + gap_rows

        pid = next_id
        next_id += 1
        caption = _xml_attr(photo.caption)
        X, A = xdr.encode(), a.encode()
        frags.append(
            b'<%s:oneCellAnchor%s>' % (X, ns_decl)
            + b'<%s:from><%s:col>%d</%s:col><%s:colOff>0</%s:colOff>'
              b'<%s:row>%d</%s:row><%s:rowOff>0</%s:rowOff></%s:from>'
              % (X, X, col, X, X, X, X, row, X, X, X, X)
            + b'<%s:ext cx="%d" cy="%d"/>' % (X, cx, cy)
            + b'<%s:pic><%s:nvPicPr><%s:cNvPr id="%d" name="Picture %d"%s%s/>'
              % (X, X, X, pid, pid - 1,
                 (b' descr="%s"' % caption.encode('utf-8')) if caption else b'',
                 (b' title="%s"' % caption.encode('utf-8')) if caption else b'')
            + b'<%s:cNvPicPr><%s:picLocks noChangeAspect="1"/></%s:cNvPicPr>'
              b'</%s:nvPicPr>' % (X, A, X, X)
            + b'<%s:blipFill><%s:blip xmlns:r="%s" r:embed="%s"/>'
              b'<%s:stretch><%s:fillRect/></%s:stretch></%s:blipFill>'
              % (X, A, REL_NS.encode(), rid.encode(), A, A, A, X)
            + b'<%s:spPr><%s:xfrm><%s:off x="%d" y="%d"/>'
              b'<%s:ext cx="%d" cy="%d"/></%s:xfrm>'
              % (X, A, A, geo.x_of(col), geo.y_of(row), A, cx, cy, A)
            + b'<%s:prstGeom prst="rect"><%s:avLst/></%s:prstGeom></%s:spPr>'
              % (A, A, A, X)
            + b'</%s:pic><%s:clientData/></%s:oneCellAnchor>' % (X, X, X))

    patch.set_part(drawing, _append_child(dxml, b'wsDr', b''.join(frags)))
    patch.put_part(drels, rels_xml)
    return len(photos)

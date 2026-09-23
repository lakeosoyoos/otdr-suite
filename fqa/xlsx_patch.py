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

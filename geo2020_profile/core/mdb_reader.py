# -*- coding: utf-8 -*-
"""
Read-only Microsoft Access (.mdb, Jet 4 / Access 2000+) reader in pure Python.

Geo2020 (經濟部地質調查及礦業管理中心「工程地質探勘資料庫」) stores its data in a
Jet 4 database.  QGIS on Windows usually has no 64-bit Access ODBC driver, so the
plugin ships this small dependency-free reader.  It supports what Geo2020 uses:
text / memo / numeric / date / OLE columns, usage maps and long-value (LVAL) pages.

If the pure-Python reader fails, ``open_mdb`` falls back to GDAL's ODBC / MDB
drivers (e.g. Windows + Microsoft Access Database Engine).
"""
import datetime
import os
import struct

__all__ = ["MdbError", "JetReader", "open_mdb"]


class MdbError(Exception):
    pass


def _u8(b, o):
    return b[o]


def _u16(b, o):
    return struct.unpack_from("<H", b, o)[0]


def _u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def _decode_text(data):
    """Jet4 text: UCS-2LE, optionally 'compressed' (prefix 0xFF 0xFE)."""
    if not data:
        return ""
    if len(data) >= 2 and data[0] == 0xFF and data[1] == 0xFE:
        out = []
        compressed = True
        i = 2
        n = len(data)
        while i < n:
            c = data[i]
            if c == 0:
                compressed = not compressed
                i += 1
                continue
            if compressed:
                out.append(chr(c))
                i += 1
            else:
                if i + 1 >= n:
                    break
                out.append(chr(c | (data[i + 1] << 8)))
                i += 2
        return "".join(out)
    if len(data) % 2:
        data = data[:-1]
    return data.decode("utf-16-le", errors="replace")


class _Column(object):
    __slots__ = ("name", "type", "num", "var_idx", "fixed_ofs", "length", "flags", "misc")

    @property
    def is_fixed(self):
        return bool(self.flags & 0x01)


class JetReader(object):
    """Minimal Jet 4 reader.  Usage::

        r = JetReader(path)
        r.table_names()
        rows = r.read_table('dr_coord')   # list of dict
    """

    PG = 4096

    def __init__(self, path):
        self.path = path
        with open(path, "rb") as f:
            self.buf = f.read()
        if len(self.buf) < self.PG * 3:
            raise MdbError("File too small to be an Access database")
        if self.buf[4:19] != b"Standard Jet DB" and self.buf[4:19] != b"Standard ACE DB":
            raise MdbError("Not a Jet/ACE database")
        ver = self.buf[0x14]
        if ver == 0:
            raise MdbError("Jet 3 (Access 97) database is not supported by the built-in reader")
        self._tdefs = {}
        self._catalog = None

    # ------------------------------------------------------------------ pages
    def page(self, n):
        o = n * self.PG
        if o < 0 or o + self.PG > len(self.buf):
            raise MdbError("Page %d out of range" % n)
        return self.buf[o:o + self.PG]

    def _row_bounds(self, pg, idx):
        nrows = _u16(pg, 12)
        if idx >= nrows:
            return None
        off = _u16(pg, 14 + 2 * idx)
        start = off & 0x1FFF
        if idx == 0:
            end = self.PG
        else:
            end = _u16(pg, 14 + 2 * (idx - 1)) & 0x1FFF
        return start, end, off

    def _get_row_raw(self, pgno, idx, follow=True):
        pg = self.page(pgno)
        b = self._row_bounds(pg, idx)
        if b is None:
            return None
        start, end, flags = b
        if flags & 0x4000 and follow:
            return None  # deleted row
        data = pg[start:end]
        if flags & 0x8000 and follow and len(data) == 4:
            # "lookup" row that only holds a pointer to the real row
            ptr = _u32(data, 0)
            try:
                return self._get_row_raw(ptr >> 8, ptr & 0xFF, follow=False)
            except MdbError:
                return None
        return data

    # ------------------------------------------------------------------- tdef
    def _read_tdef_bytes(self, pgno):
        pg = self.page(pgno)
        if pg[0] != 0x02:
            raise MdbError("Page %d is not a table definition" % pgno)
        chunks = [pg]
        nxt = _u32(pg, 4)
        guard = 0
        while nxt and guard < 1000:
            p = self.page(nxt)
            chunks.append(p[8:])
            nxt = _u32(p, 4)
            guard += 1
        return b"".join(chunks)

    def _tdef(self, pgno):
        if pgno in self._tdefs:
            return self._tdefs[pgno]
        t = self._read_tdef_bytes(pgno)
        num_var = _u16(t, 43)
        num_cols = _u16(t, 45)
        num_ridx = _u32(t, 51)
        usage_ptr = _u32(t, 55)
        cols = []
        pos = 63 + num_ridx * 12
        for _ in range(num_cols):
            c = _Column()
            c.type = t[pos]
            c.num = _u16(t, pos + 5)
            c.var_idx = _u16(t, pos + 7)
            c.misc = _u16(t, pos + 11)
            c.flags = t[pos + 15]
            c.fixed_ofs = _u16(t, pos + 21)
            c.length = _u16(t, pos + 23)
            cols.append(c)
            pos += 25
        for c in cols:
            ln = _u16(t, pos)
            pos += 2
            c.name = t[pos:pos + ln].decode("utf-16-le", errors="replace")
            pos += ln
        cols_sorted = sorted(cols, key=lambda c: c.num)
        info = {"cols": cols_sorted, "usage": usage_ptr, "num_var": num_var}
        self._tdefs[pgno] = info
        return info

    def _usage_pages(self, usage_ptr):
        m = self._get_row_raw(usage_ptr >> 8, usage_ptr & 0xFF)
        pages = []
        if not m:
            return pages
        typ = m[0]
        if typ == 0:
            start = _u32(m, 1)
            bm = m[5:]
            for i, byte in enumerate(bm):
                if not byte:
                    continue
                for bit in range(8):
                    if byte & (1 << bit):
                        pages.append(start + i * 8 + bit)
        elif typ == 1:
            per = (self.PG - 4) * 8
            for i in range((len(m) - 1) // 4):
                mp = _u32(m, 1 + 4 * i)
                if not mp:
                    continue
                bm = self.page(mp)[4:]
                for j, byte in enumerate(bm):
                    if not byte:
                        continue
                    for bit in range(8):
                        if byte & (1 << bit):
                            pages.append(i * per + j * 8 + bit)
        return pages

    def _scan_pages(self, tdef_pg):
        res = []
        for n in range(len(self.buf) // self.PG):
            o = n * self.PG
            if self.buf[o] == 0x01 and self.buf[o + 1] == 0x01 and _u32(self.buf, o + 4) == tdef_pg:
                res.append(n)
        return res

    # ------------------------------------------------------------- long value
    def _read_lval(self, data, binary):
        if len(data) < 12:
            return b"" if binary else ""
        hdr = _u32(data, 0)
        ln = hdr & 0x3FFFFFFF
        flag = hdr & 0xC0000000
        if flag == 0x80000000:
            raw = data[12:12 + ln]
        elif flag == 0x40000000:
            ptr = _u32(data, 4)
            r = self._get_row_raw(ptr >> 8, ptr & 0xFF, follow=False) or b""
            raw = r[:ln]
        else:
            ptr = _u32(data, 4)
            parts = []
            got = 0
            guard = 0
            while ptr and got < ln and guard < 100000:
                r = self._get_row_raw(ptr >> 8, ptr & 0xFF, follow=False)
                if not r:
                    break
                ptr = _u32(r, 0)
                chunk = r[4:]
                parts.append(chunk)
                got += len(chunk)
                guard += 1
            raw = b"".join(parts)[:ln]
        return raw if binary else _decode_text(raw)

    # ------------------------------------------------------------- row values
    def _value(self, c, data):
        t = c.type
        if t == 2:
            return data[0]
        if t == 3:
            return struct.unpack_from("<h", data)[0]
        if t == 4:
            return struct.unpack_from("<i", data)[0]
        if t == 5:
            return struct.unpack_from("<q", data)[0] / 10000.0
        if t == 6:
            return struct.unpack_from("<f", data)[0]
        if t == 7:
            return struct.unpack_from("<d", data)[0]
        if t == 8:
            d = struct.unpack_from("<d", data)[0]
            try:
                return datetime.datetime(1899, 12, 30) + datetime.timedelta(days=d)
            except (OverflowError, ValueError):
                return None
        if t == 10:
            return _decode_text(data)
        if t == 12:
            return self._read_lval(data, False)
        if t == 11:
            return self._read_lval(data, True)
        if t == 9:
            return bytes(data)
        if t == 15:
            return "{%08X-%04X-%04X-%s-%s}" % (
                _u32(data, 0), _u16(data, 4), _u16(data, 6),
                data[8:10].hex().upper(), data[10:16].hex().upper())
        if t == 16:
            # numeric: scale in misc high byte, 1 sign byte + 16 byte magnitude
            neg = data[0] & 0x80
            words = [_u32(data, 1 + 4 * i) for i in range(4)]
            # stored as 4 little-endian dwords, most significant dword last? use mdbtools order
            val = 0
            for w in (words[1], words[0], words[3], words[2]):
                val = (val << 32) | w
            scale = (c.misc >> 8) & 0xFF
            v = val / (10 ** scale) if scale else val
            return -v if neg else v
        return bytes(data)

    def _parse_row(self, row, cols):
        n = len(row)
        if n < 4:
            return None
        num_cols = _u16(row, 0)
        mlen = (num_cols + 7) // 8
        mask = row[n - mlen:]
        var_len = _u16(row, n - mlen - 2)
        offs = [_u16(row, n - mlen - 4 - 2 * i) for i in range(var_len + 1)]
        out = {}
        for c in cols:
            if c.num >= num_cols:
                out[c.name] = None
                continue
            bit = (mask[c.num // 8] >> (c.num % 8)) & 1 if c.num // 8 < len(mask) else 0
            if c.type == 1:
                out[c.name] = bool(bit)
                continue
            if not bit:
                out[c.name] = None
                continue
            try:
                if c.is_fixed:
                    s = 2 + c.fixed_ofs
                    data = row[s:s + c.length]
                else:
                    if c.var_idx >= var_len:
                        out[c.name] = None
                        continue
                    s, e = offs[c.var_idx], offs[c.var_idx + 1]
                    data = row[s:e]
                    if not data and c.type not in (10, 12):
                        out[c.name] = None
                        continue
                out[c.name] = self._value(c, data)
            except Exception:  # pragma: no cover - corrupt value
                out[c.name] = None
        return out

    def _rows_of_tdef(self, tdef_pg):
        info = self._tdef(tdef_pg)
        try:
            pages = self._usage_pages(info["usage"])
        except Exception:
            pages = []
        if not pages:
            pages = self._scan_pages(tdef_pg)
        rows = []
        for pn in pages:
            try:
                pg = self.page(pn)
            except MdbError:
                continue
            if pg[0] != 0x01 or _u32(pg, 4) != tdef_pg:
                continue
            for i in range(_u16(pg, 12)):
                raw = self._get_row_raw(pn, i)
                if raw is None:
                    continue
                r = self._parse_row(raw, info["cols"])
                if r is not None:
                    rows.append(r)
        return rows

    # ------------------------------------------------------------------ public
    def _load_catalog(self):
        if self._catalog is None:
            cat = {}
            for r in self._rows_of_tdef(2):
                if r.get("Type") == 1 and r.get("Name") is not None and r.get("Id") is not None:
                    cat[r["Name"]] = {"page": r["Id"] & 0x00FFFFFF, "flags": r.get("Flags") or 0}
            self._catalog = cat
        return self._catalog

    def table_names(self, include_system=False):
        cat = self._load_catalog()
        names = []
        for k, v in cat.items():
            if not include_system and (k.startswith("MSys") or (v["flags"] & 0x80000002)):
                continue
            names.append(k)
        return names

    def _find(self, name):
        cat = self._load_catalog()
        if name in cat:
            return cat[name]
        low = name.lower()
        for k, v in cat.items():
            if k.lower() == low:
                return v
        return None

    def has_table(self, name):
        return self._find(name) is not None

    def columns(self, name):
        t = self._find(name)
        if t is None:
            raise MdbError("Table not found: %s" % name)
        return [c.name for c in self._tdef(t["page"])["cols"]]

    def read_table(self, name):
        t = self._find(name)
        if t is None:
            raise MdbError("Table not found: %s" % name)
        return self._rows_of_tdef(t["page"])


# ----------------------------------------------------------------- fallbacks
class _OgrReader(object):
    """GDAL ODBC (Windows) / MDB / PGeo driver."""

    def __init__(self, path):
        from osgeo import gdal
        gdal.UseExceptions()
        self.ds = None
        for drv in ("ODBC", "MDB", "PGeo"):
            try:
                d = gdal.OpenEx(path, gdal.OF_VECTOR, allowed_drivers=[drv])
            except Exception:
                d = None
            if d is not None and d.GetLayerCount() > 0:
                self.ds = d
                break
        if self.ds is None:
            raise MdbError("GDAL cannot open MDB (no Access ODBC driver)")

    def _layer(self, name):
        lyr = self.ds.GetLayerByName(name)
        if lyr is None:
            for i in range(self.ds.GetLayerCount()):
                item_ = self.ds.GetLayer(i)
                if item_.GetName().lower() == name.lower():
                    return item_
        return lyr

    def has_table(self, name):
        return self._layer(name) is not None

    def table_names(self, include_system=False):
        return [self.ds.GetLayer(i).GetName() for i in range(self.ds.GetLayerCount())]

    def read_table(self, name):
        lyr = self._layer(name)
        if lyr is None:
            raise MdbError("Table not found: %s" % name)
        defn = lyr.GetLayerDefn()
        names = [defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())]
        out = []
        lyr.ResetReading()
        for f in lyr:
            out.append({n: f.GetField(n) for n in names})
        return out


def open_mdb(path):
    """Return a reader object with ``has_table``, ``table_names``, ``read_table``."""
    if not os.path.isfile(path):
        raise MdbError("File not found: %s" % path)
    errors = []
    for cls in (JetReader, _OgrReader):
        try:
            r = cls(path)
            r.table_names()
            return r
        except Exception as e:  # try next backend
            errors.append("%s: %s" % (cls.__name__, e))
    raise MdbError("Unable to read MDB:\n" + "\n".join(errors))

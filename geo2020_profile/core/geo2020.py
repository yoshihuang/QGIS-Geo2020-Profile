# -*- coding: utf-8 -*-
"""
Load borehole data from a Geo2020 MDB (工程地質探勘資料庫) and locate core-box photos.

Tables used (資料作業規範 A1 ~ A4):
  dr_coord / measured_attribu / case_holes : coordinates, collar elevation
  drill_attributes                         : total depth, dates
  geol_graphic_log                         : 地質圖元符號 (stratum code per depth interval)
  geol_rock_soil_d                         : 岩石或土壤性質描述
  geol_weather_deg                         : 岩心風化程度 (code1..code6 = Y/N)
  geol_rqds                                : RQD
  geol_rock_discon                         : 岩石不連續面 (depth, type, angle)
  std_proct_compac                         : 標準貫入試驗 SPT
  water_tables                             : 鑽孔水位
  drill_image                              : 岩心照片
  kind (category 06)                       : 不連續面類別代碼
"""
import glob
import os
import re

from .mdb_reader import open_mdb

WEATHER_NAMES = {1: "新鮮", 2: "輕度風化", 3: "中度風化", 4: "高度風化", 5: "完全風化", 6: "殘餘土壤"}
DISCON_TYPES = {"00": "斷層帶", "01": "斷層", "02": "節理", "03": "劈理", "08": "葉理",
                "09": "層理", "11": "岩脈"}
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp")
DEFAULT_BOX_LENGTH = 7.0  # m of core per box when drill_image has no depth


def _f(v, default=None):
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        try:
            return float(str(v).strip())
        except ValueError:
            return default


def _s(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


class Borehole(object):
    def __init__(self, proj_no, hole_no):
        self.proj_no = proj_no
        self.hole_no = hole_no
        self.x = None
        self.y = None
        self.elev_mdb = None
        self.depth = None
        self.water_depth = None
        self.date_begin = ""
        self.date_end = ""
        self.layers = []   # dict(top, bottom, code, desc, weather)
        self.rqd = []      # (top, bottom, rqd)
        self.discon = []   # (depth, type_code, type_name, angle)
        self.spt = []      # dict
        self.photos = []   # dict(image_no, top, bottom, path)
        self.coord_system = ""  # 1=TM2(TWD67) 2=UTM 3=TM2(TWD97)

    @property
    def key(self):
        return (self.proj_no, self.hole_no)

    @property
    def label(self):
        return self.hole_no

    def bottom_depth(self):
        d = [item_["bottom"] for item_ in self.layers if item_["bottom"] is not None]
        if self.depth:
            d.append(self.depth)
        return max(d) if d else 0.0

    def __repr__(self):
        return "<Borehole %s/%s (%s,%s) z=%s n=%d>" % (
            self.proj_no, self.hole_no, self.x, self.y, self.elev_mdb, len(self.layers))


def find_mdb_files(folder):
    if not folder or not os.path.isdir(folder):
        return []
    res = []
    for fn in sorted(os.listdir(folder)):
        if fn.lower().endswith((".mdb", ".accdb")):
            res.append(os.path.join(folder, fn))
    # Prefer the standard Geo2020 file name
    res.sort(key=lambda p: (0 if os.path.basename(p).lower().startswith("ngeoext") else 1, p))
    return res


class Geo2020Data(object):
    def __init__(self, mdb_path):
        self.mdb_path = mdb_path
        self.folder = os.path.dirname(os.path.abspath(mdb_path))
        self.reader = open_mdb(mdb_path)
        self.projects = {}   # proj_no -> name
        self.holes = {}      # key -> Borehole
        self.discon_names = dict(DISCON_TYPES)
        self.warnings = []
        self._load()

    # ------------------------------------------------------------------
    def _rows(self, table):
        try:
            if self.reader.has_table(table):
                return self.reader.read_table(table)
        except Exception as e:  # keep going with what we can read
            self.warnings.append("讀取資料表 %s 失敗：%s" % (table, e))
        return []

    def _hole(self, r, create=True):
        p = _s(r.get("proj_no"))
        h = _s(r.get("hole_no") if r.get("hole_no") is not None else r.get("hole_point_no"))
        if not h:
            return None
        k = (p, h)
        if k not in self.holes:
            if not create:
                return None
            self.holes[k] = Borehole(p, h)
        return self.holes[k]

    def _load(self):
        for r in self._rows("drill_projects"):
            self.projects[_s(r.get("proj_no"))] = _s(r.get("proj_name"))
        for r in self._rows("kind"):
            if _s(r.get("kind")) == "06" and r.get("code") is not None:
                self.discon_names[_s(r.get("code"))] = _s(r.get("technology")) or _s(r.get("desc"))

        # coordinates / elevation
        for r in self._rows("dr_coord"):
            b = self._hole(r)
            if b is None:
                continue
            b.x = _f(r.get("coord_x"), b.x)
            b.y = _f(r.get("coord_y"), b.y)
            b.depth = _f(r.get("depth"), b.depth)
            if _s(r.get("proj_name")) and b.proj_no not in self.projects:
                self.projects[b.proj_no] = _s(r.get("proj_name"))
        for r in self._rows("measured_attribu"):
            b = self._hole(r)
            if b is None:
                continue
            if _f(r.get("coord_x")) is not None and _f(r.get("coord_y")) is not None:
                b.x = _f(r.get("coord_x"))
                b.y = _f(r.get("coord_y"))
            if _f(r.get("elevation")) is not None:
                b.elev_mdb = _f(r.get("elevation"))
            if _s(r.get("coord_system")):
                b.coord_system = _s(r.get("coord_system"))
        for r in self._rows("case_holes"):
            b = self._hole(r, create=False)
            if b is None:
                continue
            if b.elev_mdb is None:
                b.elev_mdb = _f(r.get("elevation"))
            if b.x is None:
                b.x, b.y = _f(r.get("coord_x")), _f(r.get("coord_y"))
            if b.depth is None:
                b.depth = _f(r.get("total_depth"))
        for r in self._rows("drill_attributes"):
            b = self._hole(r)
            if b is None:
                continue
            b.depth = _f(r.get("total_depth"), b.depth)
            b.date_begin = _s(r.get("date_drill_begin"))
            b.date_end = _s(r.get("date_drill_end"))

        # strata
        for r in self._rows("geol_graphic_log"):
            b = self._hole(r)
            if b is None:
                continue
            code = _s(r.get("code"))
            if code.isdigit() and len(code) < 3:
                code = code.zfill(3)
            b.layers.append({"top": _f(r.get("top_depth"), 0.0), "bottom": _f(r.get("bottom_depth"), 0.0),
                             "code": code, "desc": "", "weather": ""})
        desc = {}
        for r in self._rows("geol_rock_soil_d"):
            desc.setdefault((_s(r.get("proj_no")), _s(r.get("hole_no"))), []).append(
                (_f(r.get("top_depth"), 0.0), _f(r.get("bottom_depth"), 0.0), _s(r.get("description"))))
        weather = {}
        for r in self._rows("geol_weather_deg"):
            names = [WEATHER_NAMES[i] for i in range(1, 7) if _s(r.get("code%d" % i)).upper() == "Y"]
            weather.setdefault((_s(r.get("proj_no")), _s(r.get("hole_no"))), []).append(
                (_f(r.get("top_depth"), 0.0), _f(r.get("bottom_depth"), 0.0), "～".join(names)))
        for b in self.holes.values():
            b.layers.sort(key=lambda item_: (item_["top"], item_["bottom"]))
            for item_ in b.layers:
                item_["desc"] = _best_overlap(desc.get(b.key, []), item_["top"], item_["bottom"])
                item_["weather"] = _best_overlap(weather.get(b.key, []), item_["top"], item_["bottom"])

        for r in self._rows("geol_rqds"):
            b = self._hole(r, create=False)
            if b is not None and _f(r.get("rqd")) is not None:
                b.rqd.append((_f(r.get("top_depth"), 0.0), _f(r.get("bottom_depth"), 0.0), _f(r.get("rqd"))))
        for r in self._rows("geol_rock_discon"):
            b = self._hole(r, create=False)
            if b is None or _f(r.get("depth")) is None:
                continue
            t = _s(r.get("type"))
            if t.isdigit() and len(t) == 1:
                t = "0" + t
            b.discon.append((_f(r.get("depth")), t, self.discon_names.get(t, t), _f(r.get("angle"))))
        for r in self._rows("std_proct_compac"):
            b = self._hole(r, create=False)
            if b is None:
                continue
            b.spt.append(_spt_record(r))
        for r in self._rows("water_tables"):
            b = self._hole(r, create=False)
            if b is None or _f(r.get("wt_depth")) is None:
                continue
            # keep the latest measurement
            d = _s(r.get("date_of_measure"))
            if b.water_depth is None or d >= getattr(b, "_wt_date", ""):
                b.water_depth = _f(r.get("wt_depth"))
                b._wt_date = d
        self._images = self._rows("drill_image")

        for b in self.holes.values():
            b.rqd.sort()
            b.discon.sort()
            b.spt.sort(key=lambda s: s["top"])

    # ------------------------------------------------------------------
    def usable_holes(self, proj_filter=None):
        out = []
        for b in self.holes.values():
            if proj_filter and b.proj_no != proj_filter:
                continue
            if b.x is None or b.y is None or not b.layers:
                continue
            out.append(b)
        out.sort(key=lambda b: (b.proj_no, b.hole_no))
        return out

    def suggested_crs_authid(self):
        """EPSG code implied by the Geo2020 coord_system code (資料作業規範 A21.2)."""
        codes = [b.coord_system for b in self.holes.values() if b.coord_system]
        if not codes:
            return "EPSG:3826"
        c = max(set(codes), key=codes.count)
        return {"1": "EPSG:3828", "2": "EPSG:32651", "3": "EPSG:3826"}.get(c, "EPSG:3826")

    # ------------------------------------------------------------------
    def scan_photos(self, folder=None):
        """Attach core-box photos to boreholes.

        Looks for a per-hole folder (e.g. ``<proj_no>_Image/<hole>/`` as written by
        Geo2020, or any sub-folder named after the hole) inside the Geo2020 folder.
        Returns (n_holes_with_photos, n_photos).
        """
        folder = folder or self.folder
        hole_dirs = _find_hole_dirs(folder, set(b.hole_no for b in self.holes.values()),
                                    set(b.proj_no for b in self.holes.values()))
        n_holes = 0
        n_photos = 0
        img_rows = {}
        for r in self._images:
            img_rows.setdefault((_s(r.get("proj_no")), _s(r.get("hole_no"))), []).append(r)
        for b in self.holes.values():
            b.photos = []
            dirs = hole_dirs.get((b.proj_no, b.hole_no)) or hole_dirs.get(("", b.hole_no)) or []
            if not dirs:
                continue
            files = []
            for d in dirs:
                for fn in sorted(os.listdir(d)):
                    if fn.lower().endswith(IMAGE_EXT):
                        files.append(os.path.join(d, fn))
            if not files:
                continue
            used = set()
            rows = sorted(img_rows.get(b.key, []), key=lambda r: (_f(r.get("top_depth"), 0), _s(r.get("image_no"))))
            for r in rows:
                base = _s(r.get("image_path")).replace("\\", "/").split("/")[-1]
                ino = _s(r.get("image_no"))
                match = None
                for f in files:
                    if base and os.path.basename(f).lower() == base.lower():
                        match = f
                        break
                if match is None and ino:
                    pat = re.compile(r"^%s(?:[_\-. ]|$)" % re.escape(ino), re.I)
                    for f in files:
                        if f not in used and pat.match(os.path.basename(f)):
                            match = f
                            break
                if match is None:
                    continue
                used.add(match)
                b.photos.append({"image_no": ino or os.path.splitext(os.path.basename(match))[0],
                                 "top": _f(r.get("top_depth")), "bottom": _f(r.get("bottom_depth")),
                                 "path": match})
            # photos without drill_image rows: parse "孔號-箱號" and assume fixed box length
            for f in files:
                if f in used:
                    continue
                m = re.search(r"(?<![A-Za-z0-9])%s[-_ ](\d+)" % re.escape(b.hole_no), os.path.basename(f), re.I)
                if not m:
                    continue
                box = int(m.group(1))
                top = (box - 1) * DEFAULT_BOX_LENGTH
                bot = min(box * DEFAULT_BOX_LENGTH, b.bottom_depth() or box * DEFAULT_BOX_LENGTH)
                b.photos.append({"image_no": "%s-%d" % (b.hole_no, box), "top": top, "bottom": bot, "path": f})
            b.photos.sort(key=lambda p: (p["top"] if p["top"] is not None else 1e9, p["image_no"]))
            if b.photos:
                n_holes += 1
                n_photos += len(b.photos)
        return n_holes, n_photos


def _best_overlap(items, top, bottom):
    best, best_ov = "", 0.0
    for t, b, v in items:
        ov = min(b, bottom) - max(t, top)
        if ov > best_ov + 1e-9 or (abs(t - top) < 1e-6 and not best):
            best, best_ov = v, max(ov, best_ov)
    return best


def _spt_record(r):
    top = _f(r.get("top_depth"), 0.0)
    bot = _f(r.get("bottom_depth"), top)
    ns = [_f(r.get("n1")), _f(r.get("n2")), _f(r.get("n3"))]
    ds = [_f(r.get("n1_depth")), _f(r.get("n2_depth")), _f(r.get("n3_depth"))]
    parts = []
    refusal = False
    for i, (n, d) in enumerate(zip(ns, ds)):
        if n is None:
            break
        if i > 0 and n == 0 and (refusal or not d):
            break
        ni = int(round(n))
        if d is not None and 0 < d < 15:
            parts.append("%d/%dcm" % (ni, int(round(d))))
            refusal = True
            break
        parts.append("%d" % ni)
        if ni >= 50:
            refusal = True
            break
    nval = _f(r.get("n"))
    if not nval:
        if not refusal and ns[1] is not None and ns[2] is not None:
            nval = ns[1] + ns[2]
        elif refusal:
            nval = 50
    return {"top": top, "bottom": bot,
            "n1": None if ns[0] is None else int(ns[0]),
            "n2": None if ns[1] is None else int(ns[1]),
            "n3": None if ns[2] is None else int(ns[2]),
            "n": None if nval is None else int(nval),
            "pen": None if ds[0] is None else int(ds[0]),
            "label": "+".join(parts)}


def _find_hole_dirs(folder, hole_nos, proj_nos):
    """Return {(proj_no or '', hole_no): [dirs]} for per-hole photo folders."""
    res = {}
    if not os.path.isdir(folder):
        return res
    # 1) Geo2020 layout: <proj_no>_Image/<hole>/
    for p in proj_nos:
        for cand in glob.glob(os.path.join(folder, "%s*" % p)):
            if os.path.isdir(cand):
                for h in hole_nos:
                    d = os.path.join(cand, h)
                    if os.path.isdir(d):
                        res.setdefault((p, h), []).append(d)
    if res:
        return res
    # 2) any directory (depth <= 3) whose name equals a hole number
    base_depth = folder.rstrip(os.sep).count(os.sep)
    for root, dirs, files in os.walk(folder):
        if root.count(os.sep) - base_depth >= 3:
            dirs[:] = []
            continue
        for d in dirs:
            if d in hole_nos:
                res.setdefault(("", d), []).append(os.path.join(root, d))
    return res

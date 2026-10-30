# -*- coding: utf-8 -*-
"""
Geometry of one geological section (地質剖面).

Profile coordinates follow the template project 「地質剖面A_宜蘭」:
  X = chainage along the section line from its first vertex (m)
  Y = elevation (m), vertical scale 1:1
Each borehole is drawn as a 2 m wide column centred on its projected chainage,
RQD bars (3 m = 100 %) to the right of the column, SPT points 0.15 m right of the
column, discontinuities on the column axis.
"""
import math

from qgis.core import (QgsCoordinateTransform, QgsFeatureRequest, QgsGeometry, QgsPointXY,
                       QgsProject, QgsRasterLayer)

from .structure import StructureBuilder, apparent_dip, azimuth
from .symbology import code_name, normalize_code


class SectionError(Exception):
    pass


def _interp(xs, ys, x):
    """Linear interpolation, constant extrapolation."""
    if not xs:
        return None
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    lo, hi = 0, len(xs) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if xs[mid] <= x:
            lo = mid
        else:
            hi = mid
    x0, x1 = xs[lo], xs[hi]
    if x1 == x0:
        return ys[lo]
    t = (x - x0) / (x1 - x0)
    return ys[lo] + t * (ys[hi] - ys[lo])


class TerrainSampler(object):
    """Sample elevations along a section line from a DEM raster or contour lines."""

    def __init__(self, layer, section_crs, band=1, elev_field=None, transform_context=None):
        self.layer = layer
        self.band = band or 1
        self.elev_field = elev_field
        ctx = transform_context or QgsProject.instance().transformContext()
        self.xform = QgsCoordinateTransform(section_crs, layer.crs(), ctx)
        self.back = QgsCoordinateTransform(layer.crs(), section_crs, ctx)
        self.is_raster = isinstance(layer, QgsRasterLayer)

    def profile(self, line_geom, length, step):
        """Return (chainages, elevations, n_missing)."""
        n = max(2, int(math.ceil(length / step)) + 1)
        ch = [min(length, i * step) for i in range(n)]
        if ch[-1] < length:
            ch.append(length)
        if self.is_raster:
            return self._raster_profile(line_geom, ch)
        return self._contour_profile(line_geom, length, ch)

    def value_at(self, pt):
        """Elevation at a point (section CRS)."""
        if self.is_raster:
            return self._sample(self.xform.transform(pt))
        return None

    def _sample(self, p):
        """Bilinear interpolation of the DEM at point p (layer CRS)."""
        prov = self.layer.dataProvider()
        ext = self.layer.extent()
        rx = self.layer.rasterUnitsPerPixelX()
        ry = self.layer.rasterUnitsPerPixelY()

        def val(x, y):
            try:
                v, ok = prov.sample(QgsPointXY(x, y), self.band)
            except Exception:
                return None
            return float(v) if ok and v is not None and v == v else None

        if not rx or not ry:
            return val(p.x(), p.y())
        col = (p.x() - ext.xMinimum()) / rx - 0.5
        row = (ext.yMaximum() - p.y()) / ry - 0.5
        c0, r0 = int(math.floor(col)), int(math.floor(row))
        fx, fy = col - c0, row - r0
        vs = []
        for dr in (0, 1):
            for dc in (0, 1):
                vs.append(val(ext.xMinimum() + (c0 + dc + 0.5) * rx, ext.yMaximum() - (r0 + dr + 0.5) * ry))
        if any(v is None for v in vs):
            return val(p.x(), p.y())
        top = vs[0] * (1 - fx) + vs[1] * fx
        bot = vs[2] * (1 - fx) + vs[3] * fx
        return top * (1 - fy) + bot * fy

    def _raster_profile(self, line_geom, ch):
        zs = []
        missing = 0
        for c in ch:
            p = line_geom.interpolate(c).asPoint()
            try:
                v = self._sample(self.xform.transform(QgsPointXY(p)))
            except Exception:
                v = None
            if v is None:
                zs.append(None)
                missing += 1
            else:
                zs.append(round(float(v), 3))
        # fill gaps by interpolation
        good = [(c, z) for c, z in zip(ch, zs) if z is not None]
        if len(good) < 2:
            raise SectionError("DEM 在剖面線範圍內無有效高程值")
        gx = [g[0] for g in good]
        gz = [g[1] for g in good]
        zs = [z if z is not None else round(_interp(gx, gz, c), 3) for c, z in zip(ch, zs)]
        return ch, zs, missing

    def _contour_profile(self, line_geom, length, ch):
        lg = QgsGeometry(line_geom)
        lg.transform(self.xform)
        req = QgsFeatureRequest().setFilterRect(lg.boundingBox())
        pts = []
        for f in self.layer.getFeatures(req):
            z = f[self.elev_field]
            try:
                z = float(z)
            except (TypeError, ValueError):
                continue
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            g = QgsGeometry(g)
            g.transform(self.back)
            inter = line_geom.intersection(g)
            if inter is None or inter.isEmpty():
                continue
            for v in inter.vertices():
                c = line_geom.lineLocatePoint(QgsGeometry.fromPointXY(QgsPointXY(v.x(), v.y())))
                if c >= 0:
                    pts.append((c, z))
        if len(pts) < 2:
            raise SectionError("剖面線與等高線交點不足 2 點，無法內插地形（等高距太大或等高線未涵蓋剖面線，建議改用 DEM）")
        pts.sort()
        xs, zs = [], []
        for c, z in pts:
            if xs and abs(c - xs[-1]) < 1e-6:
                continue
            xs.append(c)
            zs.append(z)
        return ch, [round(_interp(xs, zs, c), 3) for c in ch], 0


class SectionBuilder(object):
    """Compute all profile features of one section."""

    def __init__(self, name, line_geom, boreholes, hole_xform, terrain, settings):
        self.name = name
        g = QgsGeometry(line_geom)
        if g.isMultipart():
            merged = g.mergeLines()
            if merged and not merged.isEmpty() and not merged.isMultipart():
                g = merged
            else:
                parts = g.asMultiPolyline()
                pts = [p for part in parts for p in part]
                g = QgsGeometry.fromPolylineXY(pts)
        self.line = g
        self.length = g.length()
        if self.length <= 0:
            raise SectionError("剖面線長度為 0")
        self.boreholes = boreholes
        self.hole_xform = hole_xform
        self.terrain = terrain
        s = settings
        self.buffer = s.get("buffer", 10.0)
        self.step = s.get("step", 0.25)
        self.col_w = s.get("column_width", 2.0)
        self.rqd_w = s.get("rqd_width", 3.0)
        self.elev_source = s.get("elev_source", "dem")
        self.rock_att = s.get("rock_attitude")        # (dip_dir, dip) or None
        self.fault_att = s.get("fault_attitude")      # (dip_dir, dip) or None
        self.use_core_angle = s.get("use_core_angle", False)
        self.correlate = s.get("correlate", False)
        self._struct = None
        self.begin = s.get("begin_label") or name
        self.end = s.get("end_label") or (name + "'")
        self.warnings = []
        self.holes = []      # list of dict(b, ch, dist, z)

    # ---------------------------------------------------------------- compute
    def compute(self):
        self.ch, self.topo, missing = self.terrain.profile(self.line, self.length, self.step)
        if missing:
            self.warnings.append("剖面 %s：DEM 有 %d 個取樣點無值，已以內插補齊" % (self.name, missing))
        for b in self.boreholes:
            p = QgsPointXY(b.x, b.y)
            if self.hole_xform is not None:
                p = self.hole_xform.transform(p)
            pg = QgsGeometry.fromPointXY(p)
            dist = self.line.distance(pg)
            if dist > self.buffer + 1e-9:
                continue
            ch = self.line.lineLocatePoint(pg)
            if ch < 0:
                continue
            z_dem = _interp(self.ch, self.topo, ch)
            if self.elev_source == "mdb":
                if b.elev_mdb is None:
                    self.warnings.append("鑽孔 %s 無 MDB 井口高程，改用地形高程" % b.hole_no)
                    z = z_dem
                else:
                    z = b.elev_mdb
            else:
                z = z_dem
            self.holes.append({"b": b, "ch": ch, "dist": dist, "z": round(z, 3), "z_dem": z_dem})
        self.holes.sort(key=lambda h: h["ch"])
        for h1, h2 in zip(self.holes, self.holes[1:]):
            if h2["ch"] - h1["ch"] < self.col_w:
                self.warnings.append("剖面 %s：鑽孔 %s 與 %s 投影位置僅相距 %.1f m，柱狀圖重疊，兩孔間不做地層對比" % (
                    self.name, h1["b"].hole_no, h2["b"].hole_no, h2["ch"] - h1["ch"]))
        return self

    # ---------------------------------------------------------------- structure
    def az_at(self, c):
        """Map azimuth of the section line at chainage c (local segment)."""
        pts = self.line.asPolyline()
        acc = 0.0
        for i in range(len(pts) - 1):
            seg = math.hypot(pts[i + 1].x() - pts[i].x(), pts[i + 1].y() - pts[i].y())
            if c <= acc + seg or i == len(pts) - 2:
                return azimuth((pts[i].x(), pts[i].y()), (pts[i + 1].x(), pts[i + 1].y()))
            acc += seg
        return 0.0

    def frame_bottom(self):
        zmin, zmax = self.z_range()
        return zmin - max(1.0, 0.06 * max(zmax - zmin, 1.0))

    def structure(self):
        """Faults, rock contacts, apparent-dip form lines and discontinuity ticks (see structure.py)."""
        if self._struct is not None:
            return self._struct
        holes = []
        for h in self.holes:
            b = h["b"]
            holes.append({"x": h["ch"], "z": h["z"], "bottom_z": h["z"] - b.bottom_depth(), "name": b.hole_no,
                          "contacts": _rock_contacts(b.layers, h["z"]),
                          "discon": [(d, c, n, a) for d, c, n, a in b.discon]})
        sr = self._soil_rock_line()
        sr = [(p.x(), p.y()) for p in sr] if sr else None
        top = max(self.topo) + 5.0
        # one effective rock attitude for contacts, form lines, symbols and the layout note
        st = StructureBuilder(holes, (self.ch, self.topo), sr, (0.0, self.frame_bottom(), self.length, top),
                              self.az_at, self.effective_rock_att(), self.fault_att, False, self.col_w,
                              self.correlate).build()
        for n in st.notes:
            self.warnings.append("剖面 %s：%s" % (self.name, n))
        self._struct = st
        return st

    def effective_rock_att(self):
        """(dip_dir, dip) actually used for drawing.

        With 「改用 MDB 實測傾角」 the dip is the mean of the MDB bedding / cleavage / foliation
        angles of the boreholes in this section (dip direction stays the representative one),
        so rock contacts, apparent-dip lines, symbols and the layout note all use the same value.
        """
        if not self.rock_att:
            return None
        if self.use_core_angle:
            from .structure import FABRIC_TYPES
            angs = [a for h in self.holes for d, c, n, a in h["b"].discon if c in FABRIC_TYPES and a is not None]
            if angs:
                return (self.rock_att[0], sum(angs) / len(angs))
        return self.rock_att

    def _core_dip_note(self):
        eff = self.effective_rock_att()
        if eff and self.use_core_angle and abs(eff[1] - self.rock_att[1]) > 1e-6:
            return "（傾角採 MDB 層理／劈理／片理實測平均 %.1f°）" % eff[1]
        return ""

    def attitude_note(self):
        """Lines for the layout note: attitudes, section azimuth and apparent dips."""
        az = self.az_at(self.length / 2.0)
        out = ["剖面方位 N%.0f°（%s → %s）" % (az, self.begin, self.end)]

        def side(a):
            return "水平" if abs(a) < 0.05 else "向 %s 傾斜" % (self.end if a > 0 else self.begin)
        if self.rock_att:
            eff = self.effective_rock_att()
            a = apparent_dip(eff[0], eff[1], az)
            out.append("岩層代表位態 %03.0f°/%.0f°（傾向方位角/傾角）" % self.rock_att)
            out.append("地層視傾角 %.1f°，%s" % (abs(a), side(a)))
            if self._core_dip_note():
                out.append(self._core_dip_note())
        else:
            out.append("未輸入岩層代表位態（地層界線水平延伸）")
        if self.fault_att:
            a = apparent_dip(self.fault_att[0], self.fault_att[1], az)
            out.append("斷層代表位態 %03.0f°/%.0f°（傾向方位角/傾角），視傾角 %.1f°，%s" % (
                self.fault_att[0], self.fault_att[1], abs(a), side(a)))
        return out

    def attitude_summary(self):
        if not self.rock_att:
            return ""
        az = self.az_at(self.length / 2.0)
        eff = self.effective_rock_att()
        a = apparent_dip(eff[0], eff[1], az)
        side = "%s側" % (self.end if a > 0 else self.begin) if abs(a) > 0.05 else "水平"
        return "剖面方位 %.0f°，岩層位態 %03.0f/%.0f → 視傾角 %.1f°（向 %s傾斜）%s" % (
            az, eff[0], eff[1], abs(a), side, self._core_dip_note())

    # ---------------------------------------------------------------- unit labels
    def unit_bands(self, x):
        """Strata of the first (left-most) borehole with their bounding lines at chainage x.

        Returns [(name, top_z, bottom_z)] where the bounds are evaluated on the actual drawn
        lines: terrain, soil/rock interface, rock contacts (地層界線) and the frame bottom.
        A bound is None when that line does not reach x.
        """
        if not self.holes:
            return []
        h0 = self.holes[0]
        b = h0["b"]
        units = []                      # [name, kind, code, top_depth, bottom_depth]
        for lay in sorted(b.layers, key=lambda q: q["top"]):
            code = normalize_code(lay["code"])
            rock = code == "010" or (code[:1].isdigit() and code[:1] in "345678")
            name = _unit_name(lay)
            if units and units[-1][1] == ("rock" if rock else "soil") and (not rock or units[-1][2] == code):
                u = units[-1]
                if not rock and (lay["bottom"] - lay["top"]) > u[5]:
                    u[0] = name     # overburden: name of the thickest soil layer
                    u[5] = lay["bottom"] - lay["top"]
                u[4] = lay["bottom"]
            else:
                units.append([name, "rock" if rock else "soil", code, lay["top"], lay["bottom"],
                              lay["bottom"] - lay["top"]])
        lines = [(a, pts) for typ, pts, a in self.structure().lines
                 if typ == "地層界線" and h0["b"].hole_no in str(a.get("孔號", "")).split("、")]
        sr = self._soil_rock_line()
        sr = [(p.x(), p.y()) for p in sr] if sr else None

        def z_on(pts, xx):
            pts = sorted(pts)
            if not pts or xx < pts[0][0] - 1e-6 or xx > pts[-1][0] + 1e-6:
                return None
            for (x0, z0), (x1, z1) in zip(pts, pts[1:]):
                if x0 - 1e-9 <= xx <= x1 + 1e-9:
                    return z0 if x1 == x0 else z0 + (xx - x0) / (x1 - x0) * (z1 - z0)
            return pts[-1][1]

        out = []
        prev_bottom = self.topo_at(x)       # top of the first unit = ground surface
        for i, u in enumerate(units):
            if i == len(units) - 1:
                bottom = self.frame_bottom()
            else:
                nxt = units[i + 1]
                if u[1] == "soil" and nxt[1] == "rock":
                    bottom = z_on(sr, x) if sr else None
                else:
                    bottom = None
                    for a, pts in lines:
                        if a.get("上層") == u[2] and a.get("下層") == nxt[2]:
                            bottom = z_on(pts, x)
                            break
            out.append((u[0], prev_bottom, bottom))
            prev_bottom = bottom
        return out

    # ---------------------------------------------------------------- outputs
    def strata_features(self):
        """[(geom, attrs)] for 鑽孔柱狀圖 polygons."""
        out = []
        fid = 1
        hw = self.col_w / 2.0
        for h in self.holes:
            b, x, z = h["b"], h["ch"], h["z"]
            for item_ in b.layers:
                top, bot = item_["top"], item_["bottom"]
                if bot is None or top is None or bot <= top:
                    continue
                zt, zb = z - top, z - bot
                geom = QgsGeometry.fromPolygonXY([[QgsPointXY(x + hw, zb), QgsPointXY(x + hw, zt),
                                                   QgsPointXY(x - hw, zt), QgsPointXY(x - hw, zb),
                                                   QgsPointXY(x + hw, zb)]])
                code = normalize_code(item_["code"])
                desc = item_["desc"] or ""
                unit = desc.split("：")[0].split(":")[0].strip() if ("：" in desc or ":" in desc) else ""
                if not unit or len(unit) > 12:
                    unit = code_name(code) or code
                photos = [p["path"] for p in b.photos
                          if p["top"] is None or (p["top"] < bot - 1e-6 and (p["bottom"] or 1e9) > top + 1e-6)]
                out.append((geom, {
                    "id": fid, "type": unit, "孔號": b.hole_no, "頂深": top, "底深": bot,
                    "頂高程": round(zt, 3), "底高程": round(zb, 3), "地層代碼": code,
                    "圖元名稱": code_name(code), "風化程度": item_["weather"] or "", "描述": desc,
                    "孔口高程MDB": b.elev_mdb, "計畫編號": b.proj_no, "照片": ";".join(photos)}))
                fid += 1
        return out

    def rqd_features(self):
        out = []
        x0off = self.col_w / 2.0 + 0.15
        for h in self.holes:
            b, x, z = h["b"], h["ch"], h["z"]
            if not b.rqd:
                continue
            x0 = x + x0off
            tmin = min(r[0] for r in b.rqd)
            bmax = max(r[1] for r in b.rqd)
            out.append((self._rect(x0, x0 + self.rqd_w, z - bmax, z - tmin),
                        {"孔號": b.hole_no, "頂深": tmin, "底深": bmax, "RQD": 100.0, "kind": "RQD100%"}))
            for t, bt, v in b.rqd:
                w = max(0.0, min(100.0, v)) / 100.0 * self.rqd_w
                if w <= 0:
                    w = 0.001
                out.append((self._rect(x0, x0 + w, z - bt, z - t),
                            {"孔號": b.hole_no, "頂深": t, "底深": bt, "RQD": v, "kind": "RQD"}))
        return out

    def spt_features(self):
        out = []
        xoff = self.col_w / 2.0 + 0.15
        for h in self.holes:
            b, x, z = h["b"], h["ch"], h["z"]
            for i, s in enumerate(b.spt, 1):
                y = z - (s["top"] + s["bottom"]) / 2.0
                out.append((QgsGeometry.fromPointXY(QgsPointXY(x + xoff, y)), {
                    "孔號": b.hole_no, "樣號": "S-%d" % i, "頂深": s["top"], "底深": s["bottom"],
                    "N1": s["n1"], "N2": s["n2"], "N3": s["n3"], "N": s["n"], "貫入cm": s["pen"],
                    "標註": s["label"]}))
        return out

    def discon_features(self):
        out = []
        for h in self.holes:
            b, x, z = h["b"], h["ch"], h["z"]
            for depth, code, name, dip in b.discon:
                y = z - depth
                app = self.structure().discon_app(x, code, dip)
                app = None if app is None else round(app, 1)
                out.append((QgsGeometry.fromPointXY(QgsPointXY(x, y)), {
                    "X": round(x, 2), "井口高程": z, "深度": depth, "Y": round(y, 2), "DIP": dip,
                    "TYPE": name, "代碼": code, "孔號": b.hole_no, "視傾角": app}))
        return out

    def terrain_features(self):
        out = []
        pts = [QgsPointXY(c, z) for c, z in zip(self.ch, self.topo)]
        out.append((QgsGeometry.fromMultiPolylineXY([pts]), {"id": 1, "type": "地形"}))
        sr = self._soil_rock_line()
        if sr:
            out.append((QgsGeometry.fromMultiPolylineXY([sr]), {"id": 2, "type": "地層界線"}))
        wt = self._water_line()
        if wt:
            out.append((QgsGeometry.fromMultiPolylineXY([wt]), {"id": 3, "type": "地下水位面"}))
        if sr:
            out[1][1]["類別"] = "土岩界面"
        fid = 10
        for typ, pts, attrs in self.structure().lines:
            a = {"id": fid, "type": typ}
            a.update(attrs)
            out.append((QgsGeometry.fromMultiPolylineXY([[QgsPointXY(x, z) for x, z in pts]]), a))
            fid += 1
        return out

    def paths_features(self):
        pts = [QgsPointXY(c, z) for c, z in zip(self.ch, self.topo)]
        return [(QgsGeometry.fromPolylineXY(pts), {"begin": self.begin, "end": self.end})]

    def photo_rows(self):
        rows = []
        for h in self.holes:
            b = h["b"]
            for p in b.photos:
                rows.append({"孔號": b.hole_no, "計畫編號": b.proj_no, "箱號": p["image_no"],
                             "頂深": p["top"], "底深": p["bottom"], "路徑": p["path"]})
        return rows

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _rect(x0, x1, y0, y1):
        return QgsGeometry.fromPolygonXY([[QgsPointXY(x1, y0), QgsPointXY(x1, y1), QgsPointXY(x0, y1),
                                           QgsPointXY(x0, y0), QgsPointXY(x1, y0)]])

    def topo_at(self, c):
        return _interp(self.ch, self.topo, c)

    def _soil_rock_line(self):
        """Interface between soil (codes 0xx/1xx/2xx) and rock (3xx~8xx, 010).

        Linear between boreholes, extended to both ends of the section keeping the
        interface depth below ground of the nearest borehole (same rule as the template).
        """
        ctrl = []
        for h in self.holes:
            b = h["b"]
            depth = None
            for item_ in b.layers:
                c = normalize_code(item_["code"])
                if c == "010" or (c[:1].isdigit() and c[:1] in "345678"):
                    depth = item_["top"]
                    break
            if depth is None or depth <= 0:
                continue
            ctrl.append((h["ch"], depth - (h["z"] - self.topo_at(h["ch"])), h["z"] - depth))
        if not ctrl:
            return None
        pts = []
        first, last = ctrl[0], ctrl[-1]
        pts.append(QgsPointXY(0.0, self.topo_at(0.0) - first[1]))
        for c, d, z in ctrl:
            if 0 < c < self.length:
                pts.append(QgsPointXY(c, z))
        pts.append(QgsPointXY(self.length, self.topo_at(self.length) - last[1]))
        # densify the end extensions so they follow the terrain
        dense = []
        for i in range(len(pts) - 1):
            a, b2 = pts[i], pts[i + 1]
            seg_is_end = (i == 0 and len(ctrl) and a.x() < ctrl[0][0]) or (i == len(pts) - 2 and b2.x() > ctrl[-1][0])
            dense.append(a)
            if seg_is_end:
                d = first[1] if i == 0 else last[1]
                n = int((b2.x() - a.x()) / max(self.step * 2, 0.5))
                for k in range(1, n):
                    xx = a.x() + (b2.x() - a.x()) * k / n
                    dense.append(QgsPointXY(xx, self.topo_at(xx) - d))
        dense.append(pts[-1])
        return dense

    def _water_line(self):
        ctrl = [(h["ch"], h["b"].water_depth, h["z"]) for h in self.holes if h["b"].water_depth is not None]
        if not ctrl:
            return None
        ext = 8.0
        x0 = max(0.0, ctrl[0][0] - ext)
        x1 = min(self.length, ctrl[-1][0] + ext)
        xs = [c[0] for c in ctrl]
        ds = [c[1] for c in ctrl]
        # offset between collar and terrain (MDB elevation option)
        offs = [c[2] - self.topo_at(c[0]) for c in ctrl]
        pts = []
        n = max(2, int((x1 - x0) / 0.5) + 1)
        for i in range(n):
            x = x0 + (x1 - x0) * i / (n - 1)
            z = self.topo_at(x) + _interp(xs, offs, x) - _interp(xs, ds, x)
            pts.append(QgsPointXY(x, round(z, 3)))
        return pts

    # ------------------------------------------------------------- extents
    def z_range(self):
        zs = list(self.topo)
        for h in self.holes:
            zs.append(h["z"])
            zs.append(h["z"] - h["b"].bottom_depth())
        return min(zs), max(zs)

    def summary(self):
        return "剖面 %s：長 %.1f m，納入鑽孔 %s" % (
            self.name, self.length,
            "、".join("%s(距 %.1f m)" % (h["b"].hole_no, h["dist"]) for h in self.holes) or "無")


def _rock_contacts(layers, z_collar):
    from .structure import rock_contacts
    return rock_contacts([dict(lay, code=normalize_code(lay["code"])) for lay in layers], z_collar)


def _unit_name(lay):
    """Short stratum name: text before '：' in the description, else the symbol name."""
    desc = lay.get("desc") or ""
    unit = desc.split("：")[0].split(":")[0].strip() if ("：" in desc or ":" in desc) else ""
    if not unit or len(unit) > 6:
        unit = code_name(lay["code"]) or lay["code"]
    return unit

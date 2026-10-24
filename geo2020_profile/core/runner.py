# -*- coding: utf-8 -*-
"""Validation and execution of a batch of geological sections (GUI independent)."""
import os
import re

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject,
                       QgsRasterLayer, QgsVectorLayer)

from .compat import geometry_kind, log_exception
from .geo2020 import Geo2020Data
from .project_writer import ProjectBuilder
from .section import SectionBuilder, SectionError, TerrainSampler

GPKG_NAME = "GroProfile.gpkg"
_BAD_CHARS = re.compile(r'[\\/:*?"<>|]')


class RunConfig(object):
    def __init__(self, **kw):
        self.folder = kw.get("folder", "")
        self.mdb_path = kw.get("mdb_path", "")
        self.proj_filter = kw.get("proj_filter")          # None = all projects
        self.hole_crs = kw.get("hole_crs") or QgsCoordinateReferenceSystem("EPSG:3826")
        self.terrain_layer = kw.get("terrain_layer")
        self.terrain_band = kw.get("terrain_band", 1)
        self.contour_field = kw.get("contour_field")
        self.elev_source = kw.get("elev_source", "dem")     # 'dem' | 'mdb'
        self.section_layer = kw.get("section_layer")
        self.name_field = kw.get("name_field")
        self.selected_only = kw.get("selected_only", False)
        self.buffer = float(kw.get("buffer", 10.0))
        self.out_dir = kw.get("out_dir", "")
        self.prefix = kw.get("prefix", "")
        self.suffix = kw.get("suffix", "")
        self.step = float(kw.get("step", 0.25))
        self.column_width = float(kw.get("column_width", 2.0))
        self.colored = kw.get("colored", True)
        self.geo = kw.get("geo")                            # cached Geo2020Data
        self.transform_context = kw.get("transform_context") or QgsProject.instance().transformContext()

    @property
    def gpkg_path(self):
        return os.path.join(self.out_dir, GPKG_NAME)


def section_features(cfg):
    lyr = cfg.section_layer
    if lyr is None:
        return []
    if cfg.selected_only:
        return list(lyr.getSelectedFeatures())
    return list(lyr.getFeatures())


def section_name(feat, field):
    v = feat[field]
    if v is None:
        return ""
    s = str(v).strip()
    if s.upper() == "NULL":
        return ""
    return s


def planned_outputs(cfg):
    out = []
    for f in section_features(cfg):
        n = section_name(f, cfg.name_field)
        out.append((n, os.path.join(cfg.out_dir, "%s%s%s.qgz" % (cfg.prefix, n, cfg.suffix)), f))
    return out


def validate(cfg):
    """Return a list of (setting, message) problems; empty list = OK."""
    errs = []
    # 1. Geo2020
    if not cfg.folder or not os.path.isdir(cfg.folder):
        errs.append(("1. Geo2020 資料夾", "請選擇存在的 Geo2020 MDB 資料夾"))
    elif not cfg.mdb_path or not os.path.isfile(cfg.mdb_path):
        errs.append(("1. Geo2020 資料夾", "資料夾中找不到 MDB 檔 (*.mdb)"))
    else:
        try:
            if cfg.geo is None or cfg.geo.mdb_path != cfg.mdb_path:
                cfg.geo = Geo2020Data(cfg.mdb_path)
                cfg.geo.scan_photos(cfg.folder)
            if not cfg.geo.usable_holes(cfg.proj_filter):
                errs.append(("1. Geo2020 資料夾", "MDB 中沒有同時具備座標與地層資料的鑽孔"))
        except Exception as e:
            errs.append(("1. Geo2020 資料夾", "無法讀取 MDB：%s" % e))
    if cfg.hole_crs is None or not cfg.hole_crs.isValid():
        errs.append(("1. 鑽孔座標系統", "請指定鑽孔座標的坐標參考系統"))
    # 2. terrain
    t = cfg.terrain_layer
    if t is None or not t.isValid():
        errs.append(("2. 數值地形", "請選擇數值地形 DEM 或等高線圖層"))
    elif isinstance(t, QgsRasterLayer):
        if cfg.terrain_band < 1 or cfg.terrain_band > t.bandCount():
            errs.append(("2. 數值地形", "DEM 波段設定錯誤"))
    elif isinstance(t, QgsVectorLayer):
        if geometry_kind(t) != "Line":
            errs.append(("2. 數值地形", "等高線圖層必須為線圖層"))
        elif not cfg.contour_field or t.fields().indexOf(cfg.contour_field) < 0:
            errs.append(("2. 數值地形", "請選擇等高線的高程欄位"))
        elif not t.fields().field(cfg.contour_field).isNumeric():
            errs.append(("2. 數值地形", "等高線高程欄位必須為數值欄位"))
    # terrain must cover the section lines
    if t is not None and t.isValid() and cfg.section_layer is not None and cfg.section_layer.isValid() \
            and not any(k == "2. 數值地形" for k, _m in errs):
        try:
            xf = QgsCoordinateTransform(t.crs(), cfg.section_layer.crs(), cfg.transform_context)
            text = xf.transformBoundingBox(t.extent())
            uncovered = []
            for f in section_features(cfg):
                g = f.geometry()
                if g is not None and not g.isEmpty() and not text.intersects(g.boundingBox()):
                    uncovered.append(section_name(f, cfg.name_field) if cfg.name_field else str(f.id()))
            if uncovered:
                errs.append(("2. 數值地形", "地形圖層範圍未涵蓋剖面線：%s（請確認選擇正確的 DEM / 等高線圖層與座標系統）"
                             % "、".join(uncovered)))
        except Exception as e:  # coverage check is advisory only
            log_exception("地形範圍檢查略過", e)
    # 3. elevation source
    if cfg.elev_source not in ("dem", "mdb"):
        errs.append(("3. 井口高程", "請選擇井口高程來源"))
    elif cfg.elev_source == "mdb" and cfg.geo is not None:
        miss = [b.hole_no for b in cfg.geo.usable_holes(cfg.proj_filter) if b.elev_mdb is None]
        if miss and len(miss) == len(cfg.geo.usable_holes(cfg.proj_filter)):
            errs.append(("3. 井口高程", "MDB 中所有鑽孔都沒有井口高程，請改用數值地形"))
    # 4. section lines
    s = cfg.section_layer
    if s is None or not s.isValid():
        errs.append(("4. 剖面線", "請選擇剖面線圖層"))
    else:
        if geometry_kind(s) != "Line":
            errs.append(("4. 剖面線", "剖面線圖層必須為線圖層"))
        if t is not None and s is t:
            errs.append(("4. 剖面線", "剖面線圖層與數值地形圖層不可為同一圖層"))
        if s.crs().isGeographic():
            errs.append(("4. 剖面線", "剖面線圖層須為投影座標 (公尺)，例如 EPSG:3826"))
        if not cfg.name_field or s.fields().indexOf(cfg.name_field) < 0:
            errs.append(("4. 剖面線名稱欄位", "請選擇剖面線名稱欄位"))
        else:
            feats = section_features(cfg)
            if not feats:
                errs.append(("4. 執行範圍", "沒有選取的剖面線" if cfg.selected_only else "剖面線圖層沒有圖徵"))
            names = [section_name(f, cfg.name_field) for f in feats]
            if any(not n for n in names):
                errs.append(("4. 剖面線名稱欄位", "有剖面線的名稱欄位為空值"))
            bad = [n for n in names if n and _BAD_CHARS.search(n)]
            if bad:
                errs.append(("4. 剖面線名稱欄位", "名稱含檔名不允許的字元：%s" % "、".join(bad)))
            dup = sorted(set(n for n in names if n and names.count(n) > 1))
            if dup:
                errs.append(("4. 剖面線名稱欄位", "剖面名稱重複：%s（輸出檔名會相同）" % "、".join(dup)))
            for f in feats:
                g = f.geometry()
                if g is None or g.isEmpty() or g.length() <= 0:
                    errs.append(("4. 剖面線", "剖面 %s 幾何無效" % section_name(f, cfg.name_field)))
    # 6. distance
    if not (5 <= cfg.buffer <= 50):
        errs.append(("5. 鑽孔與剖面距離", "距離需介於 5 ~ 50 公尺"))
    # 7. output
    if not cfg.out_dir:
        errs.append(("6. 輸出資料夾", "請選擇輸出資料夾"))
    else:
        try:
            os.makedirs(cfg.out_dir, exist_ok=True)
            test = os.path.join(cfg.out_dir, ".geo2020_write_test")
            with open(test, "w") as fh:
                fh.write("ok")
            os.remove(test)
        except Exception as e:
            errs.append(("6. 輸出資料夾", "無法寫入輸出資料夾：%s" % e))
    if _BAD_CHARS.search(cfg.prefix or "") or _BAD_CHARS.search(cfg.suffix or ""):
        errs.append(("6. 檔名前綴", "檔名前綴/後綴含不允許的字元"))
    return errs


def existing_outputs(cfg):
    ex = [p for _n, p, _f in planned_outputs(cfg) if os.path.exists(p)]
    if os.path.exists(cfg.gpkg_path):
        ex.append(cfg.gpkg_path)
    return ex


def run(cfg, progress=None, log=None, is_canceled=None):
    """Generate all sections.  Returns list of dict(name, qgz, holes, warnings)."""
    log = log or (lambda m: None)
    progress = progress or (lambda v: None)
    geo = cfg.geo
    if geo is None:
        geo = cfg.geo = Geo2020Data(cfg.mdb_path)
        geo.scan_photos(cfg.folder)
    photos_enabled = any(b.photos for b in geo.holes.values())
    holes = geo.usable_holes(cfg.proj_filter)
    sec_crs = cfg.section_layer.crs()
    hole_xform = None
    if cfg.hole_crs != sec_crs:
        hole_xform = QgsCoordinateTransform(cfg.hole_crs, sec_crs, cfg.transform_context)
    terrain = TerrainSampler(cfg.terrain_layer, sec_crs, cfg.terrain_band, cfg.contour_field,
                             cfg.transform_context)
    pb = ProjectBuilder(cfg.out_dir, cfg.gpkg_path, sec_crs, cfg.colored)
    plans = planned_outputs(cfg)
    results = []
    for w in geo.warnings:
        log("⚠ " + w)
    for i, (name, qgz, feat) in enumerate(plans):
        if is_canceled and is_canceled():
            log("已取消")
            break
        log("── 剖面 %s ──" % name)
        settings = {"buffer": cfg.buffer, "step": cfg.step, "column_width": cfg.column_width,
                    "elev_source": cfg.elev_source, "begin_label": name, "end_label": name + "'"}
        try:
            sec = SectionBuilder(name, feat.geometry(), holes, hole_xform, terrain, settings).compute()
            log(sec.summary())
            if not sec.holes:
                log("⚠ 剖面 %s 沒有鑽孔位於 %.0f m 範圍內，只輸出地形剖面" % (name, cfg.buffer))
            for w in sec.warnings:
                log("⚠ " + w)
            if cfg.elev_source == "mdb":
                for h in sec.holes:
                    if h["z_dem"] is not None and abs(h["z"] - h["z_dem"]) > 2.0:
                        log("⚠ 鑽孔 %s MDB 井口高程 %.2f m 與地形高程 %.2f m 相差 %.1f m" % (
                            h["b"].hole_no, h["z"], h["z_dem"], h["z"] - h["z_dem"]))
            counts = pb.write_section_layers(sec, cfg.transform_context)
            title = "%s%s%s" % (cfg.prefix, name, cfg.suffix)
            pid = sorted(set(h["b"].proj_no for h in sec.holes))
            pname = "、".join(geo.projects.get(p, "") for p in pid if geo.projects.get(p))
            if pname:
                title = "%s（%s）" % (title, pname)
            pb.build_project(sec, qgz, title, photos_enabled)
            log("✔ 已輸出 %s（地層 %d、RQD %d、SPT %d、不連續面 %d）" % (
                os.path.basename(qgz), counts["strata"], counts["rqd"], counts["spt"], counts["discon"]))
            results.append({"name": name, "qgz": qgz, "holes": [h["b"].hole_no for h in sec.holes],
                            "warnings": sec.warnings, "ok": True, "section": sec})
        except (SectionError, Exception) as e:
            log("✖ 剖面 %s 失敗：%s" % (name, e))
            results.append({"name": name, "qgz": qgz, "ok": False, "error": str(e)})
        progress(int(100 * (i + 1) / max(1, len(plans))))
    return results

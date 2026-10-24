# -*- coding: utf-8 -*-
"""Write section layers to GroProfile.gpkg and build the per-section .qgz project."""
import math
import os

from qgis.PyQt.QtGui import QFont, QFontDatabase
from qgis.PyQt.QtXml import QDomDocument
from qgis.core import (QgsAction, QgsFeature, QgsLayoutItem,
                       QgsLayoutItemLabel, QgsLayoutItemLegend, QgsLayoutItemMap,
                       QgsLayoutItemScaleBar, QgsLayoutPoint, QgsLayoutSize, QgsPrintLayout,
                       QgsProject, QgsReadWriteContext, QgsRectangle, QgsTextFormat,
                       QgsVectorFileWriter, QgsVectorLayer)

from qgis.core import Qgis

from .compat import enum_value, make_field
from .symbology import discon_renderer, strata_renderer

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STYLE_DIR = os.path.join(PLUGIN_DIR, "styles")
TEMPLATE_QPT = os.path.join(PLUGIN_DIR, "templates", "section_layout.qpt")
PLUGIN_KEY = "geo2020_profile"


def _enum(owner, scoped, name):
    """owner.name (Qt5 style) or owner.scoped.name (Qt6 scoped enums)."""
    if hasattr(owner, name):
        return getattr(owner, name)
    return getattr(getattr(owner, scoped), name)


# ---------------------------------------------------------------- layer specs
LAYER_SPECS = {
    "strata": {
        "suffix": "鑽孔柱狀圖", "title": "地層", "geom": "MultiPolygon", "qml": "strata.qml",
        "fields": [("id", "int"), ("type", "string"), ("孔號", "string"), ("頂深", "double"),
                   ("底深", "double"), ("頂高程", "double"), ("底高程", "double"), ("地層代碼", "string"),
                   ("圖元名稱", "string"), ("風化程度", "string"), ("描述", "string"),
                   ("孔口高程MDB", "double"), ("計畫編號", "string"), ("照片", "string")]},
    "rqd": {
        "suffix": "RQD", "title": "RQD", "geom": "MultiPolygon", "qml": "rqd.qml",
        "fields": [("孔號", "string"), ("頂深", "double"), ("底深", "double"), ("RQD", "double"),
                   ("kind", "string")]},
    "spt": {
        "suffix": "SPT", "title": "SPT (N1+N2+N3)", "geom": "Point", "qml": "spt.qml",
        "fields": [("孔號", "string"), ("樣號", "string"), ("頂深", "double"), ("底深", "double"),
                   ("N1", "int"), ("N2", "int"), ("N3", "int"), ("N", "int"), ("貫入cm", "int"),
                   ("標註", "string")]},
    "discon": {
        "suffix": "不連續面", "title": "不連續面", "geom": "Point", "qml": "discon.qml",
        "fields": [("X", "double"), ("井口高程", "double"), ("深度", "double"), ("Y", "double"),
                   ("DIP", "double"), ("TYPE", "string"), ("代碼", "string"), ("孔號", "string")]},
    "terrain": {
        "suffix": "地形剖面", "title": "地形剖面", "geom": "MultiLineString", "qml": "terrain.qml",
        "fields": [("id", "int"), ("type", "string")]},
    "paths": {
        "suffix": "paths", "title": "paths", "geom": "LineString", "qml": "paths.qml",
        "fields": [("begin", "string"), ("end", "string")]},
    "photos": {
        "suffix": "岩心照片", "title": "岩心照片", "geom": "None", "qml": None,
        "fields": [("孔號", "string"), ("計畫編號", "string"), ("箱號", "string"), ("頂深", "double"),
                   ("底深", "double"), ("路徑", "string"), ("相對路徑", "string")]},
}
# drawing order in the layer tree (top first)
TREE_ORDER = ["spt", "discon", "rqd", "strata", "terrain", "paths"]


class WriteError(Exception):
    pass


def gpkg_layer_name(section, key):
    return "%s_%s" % (section, LAYER_SPECS[key]["suffix"])


def write_layer(gpkg, layer_name, key, crs, feats, transform_context=None):
    spec = LAYER_SPECS[key]
    uri = spec["geom"] if spec["geom"] != "None" else "None"
    if spec["geom"] != "None" and crs.authid():
        uri += "?crs=" + crs.authid()
    mem = QgsVectorLayer(uri, layer_name, "memory")
    if spec["geom"] != "None":
        mem.setCrs(crs)
    pr = mem.dataProvider()
    pr.addAttributes([make_field(n, k) for n, k in spec["fields"]])
    mem.updateFields()
    fields = mem.fields()
    out = []
    for geom, attrs in feats:
        f = QgsFeature(fields)
        if geom is not None and spec["geom"] != "None":
            g = geom
            if spec["geom"].startswith("Multi"):
                g = geom.__class__(geom)
                g.convertToMultiType()
            f.setGeometry(g)
        for n, _k in spec["fields"]:
            if n in attrs:
                f[n] = attrs[n]
        out.append(f)
    pr.addFeatures(out)
    mem.updateExtents()

    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG"
    opts.layerName = layer_name
    opts.fileEncoding = "UTF-8"
    if os.path.exists(gpkg):
        opts.actionOnExistingFile = _enum(QgsVectorFileWriter, "ActionOnExistingFile", "CreateOrOverwriteLayer")
    else:
        opts.actionOnExistingFile = _enum(QgsVectorFileWriter, "ActionOnExistingFile", "CreateOrOverwriteFile")
    from qgis.core import QgsCoordinateTransformContext
    ctx = transform_context or QgsCoordinateTransformContext()
    res = QgsVectorFileWriter.writeAsVectorFormatV3(mem, gpkg, ctx, opts)
    err = res[0]
    no_err = _enum(QgsVectorFileWriter, "WriterError", "NoError")
    if err != no_err:
        raise WriteError("寫入 %s 失敗：%s" % (layer_name, res[1]))
    return len(out)


# ---------------------------------------------------------------- utilities
def nice_step(span, target, allow_quarter=True):
    if span <= 0:
        return 1.0
    raw = span / float(target)
    mag = 10 ** math.floor(math.log10(raw))
    for m in ((1, 2, 2.5, 5, 10) if allow_quarter else (1, 2, 5, 10)):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def _font_family():
    try:
        fams = set(QFontDatabase.families())          # Qt6: static
    except TypeError:
        fams = set(QFontDatabase().families())        # Qt5: instance method
    except Exception:
        fams = set()
    for f in ("Microsoft JhengHei", "微軟正黑體", "PMingLiU", "新細明體", "Noto Sans CJK TC",
              "Noto Sans TC", "PingFang TC", "Heiti TC", "Inter"):
        if f in fams:
            return f
    return QFont().defaultFamily()


def _text_format(size, family, bold=False):
    fmt = QgsTextFormat()
    font = QFont(family)
    font.setBold(bold)
    fmt.setFont(font)
    fmt.setSize(size)  # points (default unit)
    return fmt


def _add_label(layout, text, pos_mm, ref, size, family, bold=False):
    lab = QgsLayoutItemLabel(layout)
    lab.setText(text)
    fmt = _text_format(size, family, bold)
    if hasattr(lab, "setTextFormat"):
        lab.setTextFormat(fmt)
    else:  # pragma: no cover - QGIS < 3.24
        lab.setFont(fmt.font())
    lab.setMargin(0.5)
    layout.addLayoutItem(lab)
    lab.adjustSizeToText()
    lab.setReferencePoint(_enum(QgsLayoutItem, "ReferencePoint", ref))
    lab.attemptMove(QgsLayoutPoint(pos_mm[0], pos_mm[1]))
    return lab


# ---------------------------------------------------------------- project
class ProjectBuilder(object):
    def __init__(self, out_dir, gpkg_path, crs, colored=True):
        self.out_dir = out_dir
        self.gpkg = gpkg_path
        self.crs = crs
        self.colored = colored

    def write_section_layers(self, sec, transform_context=None):
        """Write all layers of a SectionBuilder into the GeoPackage."""
        data = {
            "strata": sec.strata_features(),
            "rqd": sec.rqd_features(),
            "spt": sec.spt_features(),
            "discon": sec.discon_features(),
            "terrain": sec.terrain_features(),
            "paths": sec.paths_features(),
        }
        rows = sec.photo_rows()
        gdir = os.path.dirname(os.path.abspath(self.gpkg))
        photo_feats = []
        for r in rows:
            r = dict(r)
            try:
                r["相對路徑"] = os.path.relpath(r["路徑"], gdir)
            except ValueError:  # different drive on Windows
                r["相對路徑"] = ""
            photo_feats.append((None, r))
        data["photos"] = photo_feats
        counts = {}
        for key, feats in data.items():
            counts[key] = write_layer(self.gpkg, gpkg_layer_name(sec.name, key), key, self.crs, feats,
                                      transform_context)
        self._data = data
        return counts

    def build_project(self, sec, qgz_path, title=None, photos_enabled=True):
        prj = QgsProject()
        prj.setCrs(self.crs)
        prj.setTitle(title or "地質剖面%s" % sec.name)
        prj.writeEntry("Paths", "/Absolute", False)
        root = prj.layerTreeRoot()
        layers = {}
        for key in TREE_ORDER + ["photos"]:
            spec = LAYER_SPECS[key]
            src = "%s|layername=%s" % (self.gpkg, gpkg_layer_name(sec.name, key))
            title_ = spec["title"] if key != "paths" else "%s — paths" % sec.name
            lyr = QgsVectorLayer(src, title_, "ogr")
            if not lyr.isValid():
                raise WriteError("無法載入圖層 %s" % src)
            if spec["qml"]:
                qml = os.path.join(STYLE_DIR, spec["qml"])
                if os.path.exists(qml):
                    lyr.loadNamedStyle(qml)
            layers[key] = lyr

        strata = layers["strata"]
        used = sorted(set(a["地層代碼"] for _g, a in self._data["strata"]))
        strata.setRenderer(strata_renderer(used, self.colored))
        strata.setLabelsEnabled(False)
        strata.setMapTipTemplate(
            '<b>[% "孔號" %]</b>　[% format_number("頂深",2) %]～[% format_number("底深",2) %] m<br/>'
            '[% "地層代碼" %] [% "圖元名稱" %]　[% "風化程度" %]<br/>[% "描述" %]<br/>'
            '[% array_to_string(array_foreach(string_to_array(coalesce("照片",\'\'),\';\'),'
            ' \'<img src="file:///\' || replace(@element,\'\\\\\',\'/\') || \'" width="320"/><br/>\'),\'\') %]')
        strata.setDisplayExpression('"孔號" || \' \' || "頂深" || \'-\' || "底深" || \'m \' || "圖元名稱"')
        self._add_photo_action(strata)
        types = sorted(set(a["TYPE"] for _g, a in self._data["discon"]))
        if types:
            layers["discon"].setRenderer(discon_renderer(types))

        for key in TREE_ORDER:
            prj.addMapLayer(layers[key], False)
            root.addLayer(layers[key])
        prj.addMapLayer(layers["photos"], False)  # kept in project, not in the legend
        prj.writeEntry(PLUGIN_KEY, "photo_layer", layers["photos"].id())
        prj.writeEntry(PLUGIN_KEY, "strata_layer", strata.id())
        prj.writeEntry(PLUGIN_KEY, "photos_enabled", bool(photos_enabled))
        prj.writeEntry(PLUGIN_KEY, "section", sec.name)

        layout = self._build_layout(prj, sec, title or "地質剖面%s" % sec.name)
        prj.layoutManager().addLayout(layout)
        if not prj.write(qgz_path):
            raise WriteError("無法寫入專案檔 %s：%s" % (qgz_path, prj.error()))
        return prj

    def _add_photo_action(self, layer):
        code = (
            "from qgis.utils import plugins, iface\n"
            "p = plugins.get('%s')\n"
            "if p is not None:\n"
            "    p.show_photos_for_feature('[%% @layer_id %%]', [%% $id %%])\n"
            "else:\n"
            "    iface.messageBar().pushWarning('岩心照片', '請先安裝 Geo2020 地質剖面產製外掛')\n" % PLUGIN_KEY)
        if hasattr(Qgis, "AttributeActionType"):
            atype = Qgis.AttributeActionType.GenericPython
        else:  # QGIS < 3.30
            atype = enum_value(QgsAction, "ActionType", "GenericPython")
        act = QgsAction(atype, "顯示岩心照片", code, "", False, "岩心照片", {"Feature", "Canvas"})
        layer.actions().addAction(act)

    # ------------------------------------------------------------ layout
    def _build_layout(self, prj, sec, name):
        layout = QgsPrintLayout(prj)
        layout.initializeDefaults()
        with open(TEMPLATE_QPT, "r", encoding="utf-8") as f:
            txt = f.read()
        doc = QDomDocument()
        doc.setContent(txt)
        # remove default page then load template
        items, ok = layout.loadFromTemplate(doc, QgsReadWriteContext(), True)
        layout.setName(name)
        mp = legend = sb = None
        for it in layout.items():
            if isinstance(it, QgsLayoutItemMap):
                mp = it
            elif isinstance(it, QgsLayoutItemLegend):
                legend = it
            elif isinstance(it, QgsLayoutItemScaleBar):
                sb = it
        if mp is None:
            raise WriteError("版面範本缺少地圖物件")
        family = _font_family()

        L = sec.length
        pad = max(0.2, 0.01 * L)
        xmin, xmax = -pad, L + pad
        zmin, zmax = sec.z_range()
        zr = max(zmax - zmin, 1.0)
        W = 292.7
        k = W / (xmax - xmin)  # mm per m

        if legend is not None:
            legend.setAutoUpdateModel(True)
            legend.setLinkedMap(mp)
            legend.setAutoUpdateModel(False)
            hide = [prj.mapLayersByName("%s — paths" % sec.name)]
            hide_ids = set(l.id() for group in hide for l in group)
            lroot = legend.model().rootGroup()
            for node in list(lroot.findLayers()):
                if node.layerId() in hide_ids:
                    node.parent().removeChildNode(node)
            legend.updateLegend()
            legend.adjustBoxSize()
            leg_h = legend.rect().height()
        else:
            leg_h = 42.0
        head_mm = max(leg_h, 14.0) + 14.0
        bottom = zmin - max(1.0, 0.06 * zr)
        top = zmax + head_mm / k
        H = (top - bottom) * k
        page_h = H + 4.3
        page = layout.pageCollection().page(0)
        if page is not None:
            page.setPageSize(QgsLayoutSize(297.0, page_h))
        mp.attemptMove(QgsLayoutPoint(2.15, 2.15))
        mp.attemptResize(QgsLayoutSize(W, H))
        mp.setExtent(QgsRectangle(xmin, bottom, xmax, top))
        mp.setKeepLayerSet(False)
        mp.setLayers([])
        gstep_x = nice_step(xmax - xmin, 16, False)
        gstep_y = nice_step(top - bottom, 8, False)
        g = mp.grid()
        if g is not None:
            g.setIntervalX(gstep_x)
            g.setIntervalY(gstep_y)
            g.setOffsetX(0)
            g.setOffsetY(0)
        if sb is not None:
            seg = nice_step(xmax - xmin, 16)
            sb.setLinkedMap(mp)
            sb.setUnitsPerSegment(seg)
            sb.setNumberOfSegments(2)
            sb.setNumberOfSegmentsLeft(0)
            sb.update()
        if legend is not None:
            legend.updateLegend()
            legend.adjustBoxSize()
            # keep the legend clear of the scale bar
            x_leg = 72.9
            if sb is not None:
                sb.refreshItemSize()
                x_leg = max(x_leg, sb.pos().x() + sb.rect().width() + 8.0)
            legend.attemptMove(QgsLayoutPoint(x_leg, 8.4))

        def to_layout(x, y):
            return (2.15 + (x - xmin) * k, 2.15 + (top - y) * k)

        # section end labels
        px, py = to_layout(0, sec.topo_at(0))
        _add_label(layout, sec.begin, (9.0, py - 4.0),
                   "LowerLeft", 20, family)
        px, py = to_layout(L, sec.topo_at(L))
        _add_label(layout, sec.end, (2.15 + W - 8.0, py - 4.0),
                   "LowerRight", 20, family)
        # borehole names
        for h in sec.holes:
            lx, ly = to_layout(h["ch"], max(h["z"], sec.topo_at(h["ch"])))
            _add_label(layout, h["b"].hole_no, (lx, ly - 1.5), "LowerMiddle", 14, family)
        # stratum names at the left end (from the first borehole)
        if sec.holes:
            b = sec.holes[0]["b"]
            groups = []
            for l in b.layers:
                desc = l["desc"] or ""
                unit = desc.split("：")[0].split(":")[0].strip() if ("：" in desc or ":" in desc) else ""
                from .symbology import code_name
                if not unit or len(unit) > 12:
                    unit = code_name(l["code"]) or l["code"]
                # merge weathering variants of the same rock into one name
                base = code_name(l["code"]) or unit
                if groups and groups[-1][0] == base:
                    groups[-1][2] = l["bottom"]
                else:
                    groups.append([base, l["top"], l["bottom"], unit])
            h0 = sec.holes[0]
            z0 = sec.topo_at(0) + (h0["z"] - sec.topo_at(h0["ch"]))
            last_y = -1e9
            for base, t, bt, unit in groups:
                lx, ly = to_layout(0, z0 - (t + bt) / 2.0)
                if ly - last_y < 7 or ly > 2.15 + H - 3:
                    continue
                _add_label(layout, unit if len(unit) <= 6 else base, (4.0, ly), "MiddleLeft", 17, family)
                last_y = ly
        return layout


def legend_bottom(legend):
    if legend is None:
        return 0.0
    try:
        return legend.pos().y() + legend.rect().height()
    except Exception:
        return 0.0

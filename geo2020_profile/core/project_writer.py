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

from .compat import enum_value, log_exception, make_field
from .symbology import adjust_terrain_renderer, discon_renderer, label_terrain_lines, strata_renderer

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
                   ("DIP", "double"), ("TYPE", "string"), ("代碼", "string"), ("孔號", "string"),
                   ("視傾角", "double")]},
    "terrain": {
        "suffix": "地形剖面", "title": "地形剖面", "geom": "MultiLineString", "qml": "terrain.qml",
        "fields": [("id", "int"), ("type", "string"), ("類別", "string"), ("孔號", "string"),
                   ("深度", "double"), ("上層", "string"), ("下層", "string"), ("視傾角", "double"),
                   ("終止", "string")]},
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


def _add_label(layout, text, pos_mm, ref, size, family, bold=False, grow=1.0):
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
    if grow != 1.0:  # multi-line notes: avoid unwanted wrapping when font metrics differ at render time
        lab.attemptResize(QgsLayoutSize(lab.rect().width() * grow, lab.rect().height() * 1.08))
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
        adjust_terrain_renderer(layers["terrain"], set(a["type"] for _g, a in self._data["terrain"]))
        label_terrain_lines(layers["terrain"], _font_family())

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
        view = getattr(self, "_view", None)
        if view is not None:
            try:
                from qgis.core import QgsReferencedRectangle
                prj.viewSettings().setDefaultViewExtent(QgsReferencedRectangle(view, self.crs))
            except Exception as e:
                log_exception("預設檢視範圍", e)
        if not prj.write(qgz_path):
            raise WriteError("無法寫入專案檔 %s：%s" % (qgz_path, prj.error()))
        if view is not None:
            _set_canvas_extent(qgz_path, view, self.crs)
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
        W = 292.7
        k = W / (xmax - xmin)  # mm per m

        if legend is not None:
            _legend_font(legend, family)
            legend.setAutoUpdateModel(True)
            legend.setLinkedMap(mp)
            legend.setAutoUpdateModel(False)
            hide = [prj.mapLayersByName("%s — paths" % sec.name)]
            hide_ids = set(item_.id() for group in hide for item_ in group)
            lroot = legend.model().rootGroup()
            for node in list(lroot.findLayers()):
                if node.layerId() in hide_ids:
                    node.parent().removeChildNode(node)
            legend.updateLegend()
            _leg_w, leg_h = _fit_legend(layout, legend)
        else:
            leg_h = 42.0
        note_h = 6.0 + 4.6 * len(sec.attitude_note())   # attitude note under the scale bar
        head_mm = max(leg_h, 14.0 + note_h) + 14.0
        bottom = sec.frame_bottom()
        top = zmax + head_mm / k
        H = (top - bottom) * k
        page_h = H + 4.3
        page = layout.pageCollection().page(0)
        if page is not None:
            page.setPageSize(QgsLayoutSize(297.0, page_h))
        mp.attemptMove(QgsLayoutPoint(2.15, 2.15))
        mp.attemptResize(QgsLayoutSize(W, H))
        mp.setExtent(QgsRectangle(xmin, bottom, xmax, top))
        # default canvas view: extent of the 地形剖面 layer (+5 % margin)
        zt = max(sec.topo)
        m = 0.05 * max(L, zt - bottom)
        self._view = QgsRectangle(-m, bottom - m, L + m, zt + m)
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
            _fit_legend(layout, legend)
            # keep the legend clear of the scale bar
            x_leg = 72.9
            # keep the legend to the right of the attitude note
            note_w = max([_text_width_mm(t, 9) for t in sec.attitude_note()] or [0.0])
            x_leg = max(x_leg, 11.0 + note_w + 6.0)
            if sb is not None:
                sb.refreshItemSize()
                x_leg = max(x_leg, sb.pos().x() + sb.rect().width() + 8.0)
            legend.attemptMove(QgsLayoutPoint(x_leg, 8.4))

        def to_layout(x, y):
            return (2.15 + (x - xmin) * k, 2.15 + (top - y) * k)

        # section end labels (kept clear of the legend box)
        leg_box = None
        if legend is not None:
            leg_box = (legend.pos().x(), legend.pos().y(), legend.pos().x() + legend.rect().width(),
                       legend.pos().y() + legend.rect().height())

        note_lines = sec.attitude_note()
        y_note = 22.0
        if sb is not None:
            y_note = sb.pos().y() + sb.rect().height() + 4.0
        note_w = max([_text_width_mm(t, 9) for t in note_lines] or [0.0])
        obstacles = [b for b in (leg_box, (11.0, y_note, 11.0 + note_w + 2.0, y_note + 4.6 * len(note_lines) + 2.0)) if b]

        def clear_of_legend(x0, x1, y_bottom, h=9.0):
            for ob in obstacles:
                if x1 > ob[0] and x0 < ob[2] and y_bottom > ob[1] and y_bottom - h < ob[3]:
                    y_bottom = ob[3] + h + 1.0
            return y_bottom
        px, py = to_layout(0, sec.topo_at(0))
        lab_a = _add_label(layout, sec.begin, (9.0, clear_of_legend(9.0, 25.0, py - 4.0)),
                           "LowerLeft", 20, family)
        px, py = to_layout(L, sec.topo_at(L))
        xr = 2.15 + W - 8.0
        lab_b = _add_label(layout, sec.end, (xr, clear_of_legend(xr - 16.0, xr, py - 4.0)),
                           "LowerRight", 20, family)
        for lab in (lab_a, lab_b):
            r = lab.sceneBoundingRect()
            obstacles.append((r.left(), r.top(), r.right(), r.bottom()))
        # attitude / apparent-dip note under the scale bar
        if note_lines:
            _add_label(layout, "\n".join(note_lines), (11.0, y_note), "UpperLeft", 9, family, grow=1.5)
        # borehole names
        for h in sec.holes:
            lx, ly = to_layout(h["ch"], max(h["z"], sec.topo_at(h["ch"])))
            lab = _add_label(layout, h["b"].hole_no, (lx, ly - 1.5), "LowerMiddle", 14, family)
            r = lab.sceneBoundingRect()
            obstacles.append((r.left(), r.top(), r.right(), r.bottom()))
        # stratum names at the left end, centred between the unit's bounding lines.
        # Thin units get a smaller font; if even 8 pt does not fit (or it would collide with
        # another name) the name is placed nearby with a leader arrow (call-out).
        if sec.holes:
            x_lab = xmin + (4.0 + 14.0 - 2.15) / k     # chainage under the label centre
            h0 = sec.holes[0]
            items = []
            for name, ztop, zbot in sec.unit_bands(x_lab):
                xx, anchor, lx = x_lab, "MiddleLeft", 4.0
                if ztop is None or zbot is None:
                    # the unit does not reach the section edge: label it beside the first borehole
                    xx = h0["ch"] - sec.col_w
                    bands = dict((n, (t, b2)) for n, t, b2 in sec.unit_bands(xx))
                    ztop, zbot = bands.get(name, (None, None))
                    anchor, lx = "MiddleRight", to_layout(xx, 0)[0]
                    if ztop is None or zbot is None:
                        continue
                y_top = to_layout(xx, max(ztop, zbot))[1]
                y_bot = to_layout(xx, min(ztop, zbot))[1]
                tx = to_layout(xx, 0)[0]      # call-out arrows point at the band centre at this x
                items.append((name, lx, anchor, y_top, y_bot, tx))
            _place_unit_labels(layout, items, family, 2.15 + 3.0, 2.15 + H - 3.0, obstacles)
        return layout


def _text_height_mm(pt):
    return pt * 0.3528 * 1.25


def _place_unit_labels(layout, items, family, y_min, y_max, obstacles, fs_max=17.0, fs_min=8.0, fs_call=10.0):
    """items: [(name, x_mm, anchor 'MiddleLeft'|'MiddleRight', band_top_mm, band_bottom_mm)] top to bottom."""
    placed = [tuple(o) for o in obstacles if o]          # (x0, y0, x1, y1)

    def box(x, anchor, yc, w, h):
        x0 = x if anchor == "MiddleLeft" else x - w
        return (x0, yc - h / 2.0, x0 + w, yc + h / 2.0)

    def free(bx):
        if bx[1] < y_min or bx[3] > y_max:
            return False
        return not any(bx[0] < o[2] and o[0] < bx[2] and bx[1] < o[3] and o[1] < bx[3] for o in placed)

    for name, x, anchor, y_top, y_bot, tx in items:
        y_top, y_bot = max(y_top, y_min - 50), min(y_bot, y_max + 50)
        band_h = y_bot - y_top
        yc = (y_top + y_bot) / 2.0
        # 1) inside the band, shrinking the font to fit between the two boundary lines
        fs = min(fs_max, (band_h * 0.85) / (0.3528 * 1.25))
        fs = int(fs * 2) / 2.0
        if fs >= fs_min and y_min <= yc <= y_max:
            bx = box(x, anchor, yc, _text_width_mm(name, fs) + 1.0, _text_height_mm(fs))
            if free(bx):
                _add_label(layout, name, (x, yc), anchor, fs, family)
                placed.append(bx)
                continue
        # 2) call-out: put the name in free space above / below the band with a leader arrow
        h = _text_height_mm(fs_call)
        w = _text_width_mm(name, fs_call) + 1.0
        shift = 9.0 if anchor == "MiddleLeft" else -9.0
        # arrow tip at the band centre where the band was measured; the last leader segment runs
        # horizontally along the centre line so the arrow head stays between the two boundaries
        target = (tx, min(max(yc, y_min), y_max))
        side = 1.0 if anchor == "MiddleLeft" else -1.0
        elbow = (tx + side * 3.5, target[1])
        head_w = max(0.5, min(1.4, band_h * 0.5))
        cands = []
        for i in range(6):
            cands.append(y_top - h / 2.0 - 2.5 - i * (h + 1.0))
            cands.append(y_bot + h / 2.0 + 2.5 + i * (h + 1.0))
        for ty in cands:
            bx = box(x + shift, anchor, ty, w, h)
            if free(bx):
                _add_label(layout, name, (x + shift, ty), anchor, fs_call, family)
                placed.append(bx)
                start = ((bx[0] + bx[2]) / 2.0, bx[3] if ty < target[1] else bx[1])
                _add_leader(layout, [start, elbow, target], head_w)
                break


def _add_leader(layout, pts, head_w=1.4):
    """Thin leader polyline with an arrow head pointing at the last point (layout mm)."""
    from qgis.PyQt.QtCore import QPointF
    from qgis.PyQt.QtGui import QPolygonF
    from qgis.core import QgsLayoutItemPolyline, QgsLineSymbol
    line = QgsLayoutItemPolyline(QPolygonF([QPointF(p[0], p[1]) for p in pts]), layout)
    line.setSymbol(QgsLineSymbol.createSimple({"line_color": "40,40,40", "line_width": "0.25",
                                               "line_width_unit": "MM"}))
    try:
        line.setEndMarker(enum_value(QgsLayoutItemPolyline, "MarkerMode", "ArrowHead"))
        line.setArrowHeadWidth(head_w)
    except Exception as e:
        log_exception("引線箭頭", e)
    layout.addLayoutItem(line)
    return line


def _text_width_mm(text, pt):
    """Rough rendered width of a text line (CJK full width, Latin about half)."""
    em = pt * 0.3528
    return sum(em if ord(ch) > 0x2E80 else 0.56 * em for ch in text)


def _set_canvas_extent(qgz_path, rect, crs):
    """Store a map-canvas extent in the .qgz so QGIS opens the project zoomed to the section."""
    import zipfile
    from qgis.PyQt.QtCore import QSize
    from qgis.core import QgsMapSettings
    ms = QgsMapSettings()
    ms.setDestinationCrs(crs)
    ms.setOutputSize(QSize(1600, 900))
    ms.setExtent(rect)
    doc = QDomDocument()
    el = doc.createElement("mapcanvas")
    el.setAttribute("name", "theMapCanvas")
    el.setAttribute("annotationsVisible", "1")
    doc.appendChild(el)
    ms.writeXml(el, doc)
    xml = doc.toString()
    with zipfile.ZipFile(qgz_path, "r") as z:
        items = [(i, z.read(i.filename)) for i in z.infolist()]
    out = []
    for info, data in items:
        if info.filename.lower().endswith(".qgs"):
            txt = data.decode("utf-8")
            if "<mapcanvas" not in txt:
                txt = txt.replace("</qgis>", xml + "</qgis>")
            data = txt.encode("utf-8")
        out.append((info, data))
    with zipfile.ZipFile(qgz_path, "w", zipfile.ZIP_DEFLATED) as z:
        for info, data in out:
            z.writestr(info, data)


def _fit_legend(layout, legend):
    """Size the legend from QgsLegendRenderer (adjustBoxSize() can keep a stale size). Returns (w, h) mm."""
    legend.adjustBoxSize()
    w, h = legend.rect().width(), legend.rect().height()
    try:
        from qgis.core import QgsLayoutUtils, QgsLegendRenderer
        ctx = QgsLayoutUtils.createRenderContextForLayout(layout, None, 300)
        size = QgsLegendRenderer(legend.model(), legend.legendSettings()).minimumSize(ctx)
        if size.width() > 0 and size.height() > 0:
            w, h = max(w, size.width()), max(h, size.height())
            legend.setResizeToContents(False)
            legend.attemptResize(QgsLayoutSize(w, h))
    except Exception as e:
        log_exception("圖例尺寸計算", e)
    return w, h


def _legend_font(legend, family):
    """Use a CJK-capable font in every legend text style so the box is measured correctly."""
    from qgis.core import QgsLegendStyle
    for name in ("Title", "Group", "Subgroup", "SymbolLabel"):
        try:
            key = enum_value(QgsLegendStyle, "Style", name)
            st = legend.style(key)
            if hasattr(st, "textFormat"):
                fmt = st.textFormat()
                f = fmt.font()
                f.setFamily(family)
                fmt.setFont(f)
                st.setTextFormat(fmt)
            else:  # QGIS < 3.30
                f = st.font()
                f.setFamily(family)
                st.setFont(f)
            legend.setStyle(key, st)
        except Exception as e:  # keep the template style
            log_exception("圖例字型設定", e)


def legend_bottom(legend):
    if legend is None:
        return 0.0
    try:
        return legend.pos().y() + legend.rect().height()
    except Exception:
        return 0.0

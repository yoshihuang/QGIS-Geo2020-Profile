# -*- coding: utf-8 -*-
"""Borehole location layer (鑽孔位置) built from a Geo2020 MDB."""
from qgis.PyQt.QtGui import QColor, QFont
from qgis.core import (QgsFeature, QgsGeometry, QgsMarkerSymbol, QgsPalLayerSettings,
                       QgsPointXY, QgsSingleSymbolRenderer, QgsTextBufferSettings,
                       QgsTextFormat, QgsVectorLayer, QgsVectorLayerSimpleLabeling)

from .compat import make_field

LAYER_PROP = "geo2020_profile/holes_layer"
FIELDS = [("計畫編號", "string"), ("計畫名稱", "string"), ("孔號", "string"), ("X", "double"),
          ("Y", "double"), ("井口高程", "double"), ("孔深", "double"), ("地下水位", "double"),
          ("開孔日期", "string"), ("完孔日期", "string"), ("地層數", "int"), ("岩心照片數", "int")]


def hole_attrs(geo, b):
    return {"計畫編號": b.proj_no, "計畫名稱": geo.projects.get(b.proj_no, ""), "孔號": b.hole_no,
            "X": b.x, "Y": b.y, "井口高程": b.elev_mdb, "孔深": b.bottom_depth(),
            "地下水位": b.water_depth, "開孔日期": b.date_begin, "完孔日期": b.date_end,
            "地層數": len(b.layers), "岩心照片數": len(b.photos)}


def build_holes_layer(geo, crs, proj_filter=None, name=None):
    """Return a memory point layer with all boreholes that have coordinates."""
    uri = "Point?crs=%s" % crs.authid() if crs.authid() else "Point"
    lyr = QgsVectorLayer(uri, name or "Geo2020 鑽孔位置", "memory")
    lyr.setCrs(crs)
    pr = lyr.dataProvider()
    pr.addAttributes([make_field(n, k) for n, k in FIELDS])
    lyr.updateFields()
    feats = []
    for b in sorted(geo.holes.values(), key=lambda h: (h.proj_no, h.hole_no)):
        if b.x is None or b.y is None:
            continue
        if proj_filter and b.proj_no != proj_filter:
            continue
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(b.x, b.y)))
        for k, v in hole_attrs(geo, b).items():
            f[k] = v
        feats.append(f)
    pr.addFeatures(feats)
    lyr.updateExtents()
    style_holes_layer(lyr)
    lyr.setCustomProperty(LAYER_PROP, geo.mdb_path)
    return lyr


def style_holes_layer(lyr):
    sym = QgsMarkerSymbol.createSimple({"name": "circle", "color": "255,255,255", "outline_color": "0,0,0",
                                        "outline_width": "0.5", "size": "3.2"})
    cross_sym = QgsMarkerSymbol.createSimple({"name": "cross", "color": "0,0,0", "outline_color": "0,0,0",
                                              "outline_width": "0.4", "size": "3.2"})
    sym.appendSymbolLayer(cross_sym.symbolLayer(0).clone())
    lyr.setRenderer(QgsSingleSymbolRenderer(sym))

    s = QgsPalLayerSettings()
    s.fieldName = "孔號"
    s.enabled = True
    fmt = QgsTextFormat()
    font = QFont()
    font.setBold(True)
    fmt.setFont(font)
    fmt.setSize(10)
    fmt.setColor(QColor(200, 0, 0))
    buf = QgsTextBufferSettings()
    buf.setEnabled(True)
    buf.setSize(1.0)
    buf.setColor(QColor(255, 255, 255))
    fmt.setBuffer(buf)
    s.setFormat(fmt)
    try:
        from qgis.core import Qgis
        s.placement = Qgis.LabelPlacement.OrderedPositionsAroundPoint
    except AttributeError:  # QGIS < 3.26
        s.placement = QgsPalLayerSettings.OrderedPositionsAroundPoint
    s.dist = 1.5
    lyr.setLabeling(QgsVectorLayerSimpleLabeling(s))
    lyr.setLabelsEnabled(True)
    lyr.setMapTipTemplate('<b>鑽孔 [% "孔號" %]</b><br/>計畫：[% "計畫名稱" %]<br/>'
                          '井口高程：[% "井口高程" %] m　孔深：[% "孔深" %] m<br/>'
                          '地下水位：[% "地下水位" %] m　岩心照片：[% "岩心照片數" %] 張')

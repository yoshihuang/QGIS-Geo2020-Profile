# -*- coding: utf-8 -*-
"""
工程地質圖元符號 (資料作業規範 附錄B「工程地質圖元符號對照表」及
「簡化地層使用工程地質圖元一覽表」) -> QGIS symbols.

All 131 Geo2020 stratum codes ship as SVG tiles in ``symbols/rule2008``.  They are
embedded into the generated projects as base64 so the .qgz files stay portable
(they render on computers without this plugin).
"""
import base64
import json
import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.core import (QgsCategorizedSymbolRenderer, QgsFillSymbol, QgsLineSymbol, QgsMarkerSymbol,
                       QgsProperty, QgsRendererCategory, QgsSimpleFillSymbolLayer,
                       QgsSimpleMarkerSymbolLayer, QgsSVGFillSymbolLayer, QgsSymbolLayer,
                       QgsUnitTypes)

from qgis.core import Qgis

from .compat import enum_value

if hasattr(Qgis, "RenderUnit"):
    _MM = Qgis.RenderUnit.Millimeters
else:  # QGIS < 3.30
    _MM = enum_value(QgsUnitTypes, "RenderUnit", "RenderMillimeters")

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYMBOL_DIR = os.path.join(PLUGIN_DIR, "symbols", "rule2008")
CODES_FILE = os.path.join(PLUGIN_DIR, "symbols", "rule2008_codes.json")

# pale background colour per 岩類大類 (first digit of the code)
GROUP_COLORS = {
    "0": "#ffffff",
    "1": "#fff7d6", "2": "#fbefc4",          # 土壤或沉積物
    "3": "#e3f1fb", "4": "#d4e8f7",          # 沉積岩
    "5": "#e5f4e0", "6": "#d5ecd0",          # 變質岩
    "7": "#fbe3e6", "8": "#f6d3d8",          # 火成岩
}

_codes = None


def codes():
    """{code: {'code','zh','en','group','group_name','simplified',...}}"""
    global _codes
    if _codes is None:
        with open(CODES_FILE, "r", encoding="utf-8") as f:
            _codes = {c["code"]: c for c in json.load(f)}
    return _codes


def normalize_code(code):
    c = (code or "").strip()
    if c.isdigit() and len(c) < 3:
        c = c.zfill(3)
    return c


def code_name(code):
    c = codes().get(normalize_code(code))
    if not c:
        return ""
    return c["zh"].split(";")[0].split("；")[0]


def svg_path(code):
    p = os.path.join(SYMBOL_DIR, normalize_code(code) + ".svg")
    return p if os.path.exists(p) else None


def svg_base64(code):
    p = svg_path(code)
    if not p:
        return None
    with open(p, "rb") as f:
        return "base64:" + base64.b64encode(f.read()).decode("ascii")


def strata_symbol(code, colored=True, pattern_width_mm=6.0, embed=True):
    code = normalize_code(code)
    sym = QgsFillSymbol()
    sym.deleteSymbolLayer(0)
    bg = QgsSimpleFillSymbolLayer()
    bg.setColor(QColor(GROUP_COLORS.get(code[:1], "#ffffff") if colored else "#ffffff"))
    bg.setStrokeColor(QColor(35, 35, 35))
    bg.setStrokeWidth(0.3)
    bg.setStrokeWidthUnit(_MM)
    sym.appendSymbolLayer(bg)
    src = svg_base64(code) if embed else svg_path(code)
    if src:
        pat = QgsSVGFillSymbolLayer(src, pattern_width_mm, 0.0)
        pat.setPatternWidthUnit(_MM)
        pat.setSvgFillColor(QColor(0, 0, 0))
        pat.setSvgStrokeWidth(0)
        sym.appendSymbolLayer(pat)
        # keep an outline on top of the pattern
        ol = QgsSimpleFillSymbolLayer()
        ol.setColor(QColor(0, 0, 0, 0))
        ol.setBrushStyle(Qt.BrushStyle.NoBrush)
        ol.setStrokeColor(QColor(35, 35, 35))
        ol.setStrokeWidth(0.3)
        ol.setStrokeWidthUnit(_MM)
        sym.appendSymbolLayer(ol)
    return sym


def strata_renderer(used_codes, colored=True, field="地層代碼"):
    cats = []
    for c in sorted(set(normalize_code(x) for x in used_codes if x)):
        info = codes().get(c)
        label = "%s %s" % (c, code_name(c)) if info else "%s (未定義圖元)" % c
        cats.append(QgsRendererCategory(c, strata_symbol(c, colored), label))
    r = QgsCategorizedSymbolRenderer(field, cats)
    return r


# 不連續面: rotated tick showing the (apparent) dip in the core
DISCON_STYLE = {
    "斷層帶": ("#d7191c", 1.0, 6.0),
    "斷層": ("#d7191c", 0.8, 6.0),
    "節理": ("#000000", 0.4, 4.0),
    "劈理": ("#2b83ba", 0.4, 4.0),
    "葉理": ("#7b3294", 0.4, 4.0),
    "層理": ("#1a9641", 0.5, 4.0),
    "岩脈": ("#e66101", 0.8, 4.0),
}


if hasattr(QgsSymbolLayer, "Property") and hasattr(QgsSymbolLayer.Property, "Angle"):
    _ANGLE_PROP = QgsSymbolLayer.Property.Angle
else:  # QGIS < 3.30
    _ANGLE_PROP = enum_value(QgsSymbolLayer, "Property", "PropertyAngle")


def discon_renderer(type_names, field="TYPE", dip_field="DIP"):
    cats = []
    for name in sorted(set(t for t in type_names if t), key=lambda n: list(DISCON_STYLE).index(n)
                       if n in DISCON_STYLE else 99):
        color, width, size = DISCON_STYLE.get(name, ("#555555", 0.4, 4.0))
        sym = QgsMarkerSymbol()
        sym.deleteSymbolLayer(0)
        try:
            shape = QgsSimpleMarkerSymbolLayer.decodeShape("line")[0]
        except Exception:
            shape = None
        lyr = QgsSimpleMarkerSymbolLayer()
        if shape is not None:
            lyr.setShape(shape)
        lyr.setSize(size)
        lyr.setSizeUnit(_MM)
        lyr.setStrokeColor(QColor(color))
        lyr.setColor(QColor(color))
        lyr.setStrokeWidth(width)
        lyr.setStrokeWidthUnit(_MM)
        lyr.setDataDefinedProperty(_ANGLE_PROP,
                                   QgsProperty.fromExpression(
                                       'coalesce(90 + "視傾角", 90 - coalesce("%s", 0))' % dip_field))
        sym.appendSymbolLayer(lyr)
        cats.append(QgsRendererCategory(name, sym, name))
    return QgsCategorizedSymbolRenderer(field, cats)


# 地形剖面 line types (drawing order of the legend follows the construction order)
TERRAIN_ORDER = ["地形", "地層界線", "不整合面", "斷層", "地層視傾角", "剪力面", "岩脈", "節理", "裂隙", "張裂隙",
                 "開挖面", "地下水位面"]
TERRAIN_EXTRA_STYLE = {   # types not in the template style: (color, width mm, pen style)
    "剪力面": ("#d7191c", 0.45, "dash dot"),
    "岩脈": ("#e66101", 0.7, "solid"),
    "節理": ("#000000", 0.3, "solid"),
    "裂隙": ("#666666", 0.3, "solid"),
    "張裂隙": ("#666666", 0.3, "dash"),
}


def adjust_terrain_renderer(layer, present):
    """Keep only line types that have data; add categories for discontinuity types missing in the template."""
    r = layer.renderer()
    if not isinstance(r, QgsCategorizedSymbolRenderer):
        return
    present = set(present) | {"地形"}
    cats = {c.value(): c for c in r.categories()}
    new = []
    for t in TERRAIN_ORDER + sorted(present - set(TERRAIN_ORDER)):
        if t not in present:
            continue
        if t in cats:
            c = cats[t]
            new.append(QgsRendererCategory(c.value(), c.symbol().clone(), c.label(), True))
        else:
            color, width, style = TERRAIN_EXTRA_STYLE.get(t, ("#555555", 0.3, "solid"))
            sym = QgsLineSymbol.createSimple({"line_color": color, "line_width": str(width),
                                              "line_width_unit": "MM", "line_style": style})
            new.append(QgsRendererCategory(t, sym, t, True))
    layer.setRenderer(QgsCategorizedSymbolRenderer(r.classAttribute(), new))


def label_terrain_lines(layer, family):
    """Label 地層視傾角 and 斷層 lines with their apparent dip (parallel to the line)."""
    from qgis.core import (QgsPalLayerSettings, QgsRuleBasedLabeling, QgsTextBufferSettings,
                           QgsTextFormat)
    from qgis.PyQt.QtGui import QFont

    def settings(color):
        st = QgsPalLayerSettings()
        st.isExpression = True
        st.fieldName = "'視傾角 ' || format_number(abs(\"視傾角\"), 1) || '°'"
        fmt = QgsTextFormat()
        fmt.setFont(QFont(family))
        fmt.setSize(7)
        fmt.setColor(QColor(color))
        buf = QgsTextBufferSettings()
        buf.setEnabled(True)
        buf.setSize(0.8)
        buf.setColor(QColor(255, 255, 255))
        fmt.setBuffer(buf)
        st.setFormat(fmt)
        if hasattr(Qgis, "LabelPlacement"):
            st.placement = Qgis.LabelPlacement.Line
        else:  # QGIS < 3.26
            st.placement = enum_value(QgsPalLayerSettings, "Placement", "Line")
        st.dist = 0.6
        return st

    root = QgsRuleBasedLabeling.Rule(None)
    for typ, color in (("地層視傾角", "#555555"), ("斷層", "#c00000")):
        r = QgsRuleBasedLabeling.Rule(settings(color))
        r.setFilterExpression("\"type\" = '%s' AND \"視傾角\" IS NOT NULL" % typ)
        r.setDescription(typ)
        root.appendChild(r)
    layer.setLabeling(QgsRuleBasedLabeling(root))
    layer.setLabelsEnabled(True)

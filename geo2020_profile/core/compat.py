# -*- coding: utf-8 -*-
"""Small helpers so the plugin runs on QGIS 3.28+ (Qt5) and QGIS 4.x (Qt6)."""
from qgis.core import Qgis, QgsField, QgsWkbTypes

try:  # Qt6 / QGIS 4
    from qgis.PyQt.QtCore import QMetaType
except ImportError:  # pragma: no cover
    QMetaType = None
try:
    from qgis.PyQt.QtCore import QVariant
except ImportError:  # pragma: no cover
    QVariant = None

QGIS_VERSION_INT = Qgis.QGIS_VERSION_INT if hasattr(Qgis, "QGIS_VERSION_INT") else 30000

try:
    from qgis.PyQt.QtGui import QAction  # Qt6
except ImportError:  # Qt5
    from qgis.PyQt.QtWidgets import QAction  # noqa: F401


def _type(kind):
    use_meta = QMetaType is not None and (QGIS_VERSION_INT >= 33800 or QVariant is None
                                          or not hasattr(QVariant, "Double"))
    if use_meta:
        return {"int": QMetaType.Type.Int, "double": QMetaType.Type.Double,
                "string": QMetaType.Type.QString}[kind]
    return {"int": QVariant.Int, "double": QVariant.Double, "string": QVariant.String}[kind]


def make_field(name, kind, length=0):
    """kind: 'int' | 'double' | 'string'."""
    f = QgsField(name, _type(kind))
    if length:
        f.setLength(length)
    return f


def geometry_kind(layer):
    """Return 'Point' / 'Line' / 'Polygon' / 'Unknown' for a vector layer."""
    gt = layer.geometryType()
    try:
        G = Qgis.GeometryType
        m = {G.Point: "Point", G.Line: "Line", G.Polygon: "Polygon"}
    except AttributeError:  # QGIS < 3.30
        m = {QgsWkbTypes.PointGeometry: "Point", QgsWkbTypes.LineGeometry: "Line",
             QgsWkbTypes.PolygonGeometry: "Polygon"}
    for k, v in m.items():
        if gt == k:
            return v
    return "Unknown"


def is_numeric_field(field):
    try:
        return field.isNumeric()
    except Exception:
        return False

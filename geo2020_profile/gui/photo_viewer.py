# -*- coding: utf-8 -*-
"""岩心照片：map tool + dock window."""
import os

from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices, QPixmap
from qgis.PyQt.QtWidgets import (QDockWidget, QHBoxLayout, QLabel, QPushButton, QScrollArea,
                                 QSlider, QVBoxLayout, QWidget)
from qgis.core import QgsProject, QgsVectorLayer
from qgis.gui import QgsMapToolIdentify

from ..core.compat import enum_value

PLUGIN_KEY = "geo2020_profile"
REQUIRED = ("孔號", "頂深", "底深")


def _is_strata_layer(layer):
    if not isinstance(layer, QgsVectorLayer):
        return False
    names = layer.fields().names()
    return all(n in names for n in REQUIRED)


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _layer_gpkg_dir(layer):
    src = layer.source().split("|")[0]
    return os.path.dirname(src) if src else ""


def photos_for_feature(layer, feat):
    """Return dict(title, subtitle, photos=[(caption, path)], message)."""
    hole = feat["孔號"]
    top, bot = _f(feat["頂深"]), _f(feat["底深"])
    names = layer.fields().names()
    title = "鑽孔 %s　深度 %s～%s m" % (hole, "%.2f" % top if top is not None else "?",
                                     "%.2f" % bot if bot is not None else "?")
    sub = []
    for n in ("地層代碼", "圖元名稱", "風化程度"):
        if n in names and feat[n] not in (None, ""):
            sub.append(str(feat[n]))
    if "描述" in names and feat["描述"]:
        sub.append(str(feat["描述"]))
    prj = QgsProject.instance()
    enabled, ok = prj.readBoolEntry(PLUGIN_KEY, "photos_enabled", True)
    photos = []
    lid, ok = prj.readEntry(PLUGIN_KEY, "photo_layer", "")
    plyr = prj.mapLayer(lid) if lid else None
    if plyr is None:
        # try the sibling table <section>_岩心照片 in the same GeoPackage
        src = layer.source()
        if "layername=" in src:
            lname = src.split("layername=")[1].split("|")[0]
            sec = lname.rsplit("_", 1)[0]
            cand = QgsVectorLayer("%s|layername=%s_岩心照片" % (src.split("|")[0], sec), "photos", "ogr")
            if cand.isValid():
                plyr = cand
    if plyr is not None:
        base = _layer_gpkg_dir(plyr)
        for f in plyr.getFeatures():
            if str(f["孔號"]) != str(hole):
                continue
            t, b = _f(f["頂深"]), _f(f["底深"])
            if top is not None and bot is not None and t is not None and b is not None:
                if not (t < bot - 1e-6 and b > top + 1e-6):
                    continue
            path = None
            rel = f["相對路徑"] if "相對路徑" in f.fields().names() else None
            if rel and base:
                cand = os.path.normpath(os.path.join(base, rel))
                if os.path.exists(cand):
                    path = cand
            if path is None and f["路徑"] and os.path.exists(str(f["路徑"])):
                path = str(f["路徑"])
            cap = "%s　(%s～%s m)" % (f["箱號"], "%.2f" % t if t is not None else "?",
                                      "%.2f" % b if b is not None else "?")
            photos.append((cap, path or str(f["路徑"] or "")))
    elif "照片" in names and feat["照片"]:
        for p in str(feat["照片"]).split(";"):
            if p:
                photos.append((os.path.basename(p), p))
    msg = ""
    if not enabled:
        msg = "此剖面產製時 Geo2020 資料夾內沒有岩心箱照片資料夾，岩心照片功能停用。"
    elif not photos:
        msg = "此深度範圍沒有對應的岩心箱照片。"
    return {"title": title, "subtitle": "　".join(sub), "photos": photos, "message": msg}


class PhotoMapTool(QgsMapToolIdentify):
    def __init__(self, canvas, plugin):
        super(PhotoMapTool, self).__init__(canvas)
        self.plugin = plugin
        self._mode = enum_value(QgsMapToolIdentify, "IdentifyMode", "TopDownStopAtFirst")
        self._ltype = enum_value(QgsMapToolIdentify, "Type", "VectorLayer")

    def canvasReleaseEvent(self, e):  # noqa: N802
        layers = [item_ for item_ in self.canvas().layers() if _is_strata_layer(item_)]
        if not layers:
            self.plugin.iface.messageBar().pushWarning(
                "岩心照片", "目前地圖中沒有地層（鑽孔柱狀圖）圖層。請開啟本外掛產製的地質剖面專案。")
            return
        try:
            pos = e.pos()
            res = self.identify(pos.x(), pos.y(), layers, self._mode)
        except TypeError:
            res = self.identify(e.x(), e.y(), self._mode, layers, self._ltype)
        if not res:
            return
        r = res[0]
        self.plugin.show_photos_for_feature(r.mLayer.id(), r.mFeature.id())


class _ClickLabel(QLabel):
    def __init__(self, path, parent=None):
        super(_ClickLabel, self).__init__(parent)
        self.path = path
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("點擊以系統看圖程式開啟原圖\n%s" % path)

    def mouseReleaseEvent(self, e):  # noqa: N802
        if self.path and os.path.exists(self.path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.path))


class PhotoDock(QDockWidget):
    def __init__(self, parent=None):
        super(PhotoDock, self).__init__("岩心照片", parent)
        self.setObjectName("Geo2020PhotoDock")
        w = QWidget()
        lay = QVBoxLayout(w)
        self.title = QLabel("請以「顯示岩心照片」工具點選地層圖徵")
        self.title.setWordWrap(True)
        self.title.setStyleSheet("font-weight:bold;")
        self.sub = QLabel("")
        self.sub.setWordWrap(True)
        lay.addWidget(self.title)
        lay.addWidget(self.sub)
        zl = QHBoxLayout()
        zl.addWidget(QLabel("縮放"))
        self.zoom = QSlider(Qt.Orientation.Horizontal)
        self.zoom.setRange(30, 300)
        self.zoom.setValue(100)
        self.zoom.valueChanged.connect(self._relayout)
        zl.addWidget(self.zoom)
        lay.addLayout(zl)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.inner = QWidget()
        self.inner_lay = QVBoxLayout(self.inner)
        self.inner_lay.addStretch(1)
        self.scroll.setWidget(self.inner)
        lay.addWidget(self.scroll, 1)
        btn = QPushButton("清除")
        btn.clicked.connect(lambda: self.show_info({"title": "", "subtitle": "", "photos": [], "message": ""}))
        lay.addWidget(btn)
        self.setWidget(w)
        self._items = []
        self.setMinimumWidth(320)

    def show_info(self, info):
        self.title.setText(info.get("title", ""))
        sub = info.get("subtitle", "")
        if info.get("message"):
            sub = (sub + "\n" if sub else "") + "⚠ " + info["message"]
        self.sub.setText(sub)
        while self.inner_lay.count() > 1:
            it = self.inner_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self._items = []
        for cap, path in info.get("photos", []):
            c = QLabel(cap)
            c.setStyleSheet("color:#444;")
            img = _ClickLabel(path)
            pm = QPixmap(path) if path and os.path.exists(path) else QPixmap()
            if pm.isNull():
                img.setText("（找不到照片檔）\n%s" % path)
            self.inner_lay.insertWidget(self.inner_lay.count() - 1, c)
            self.inner_lay.insertWidget(self.inner_lay.count() - 1, img)
            self._items.append((img, pm))
        self._relayout()

    def _relayout(self, *args):
        w = max(100, self.scroll.viewport().width() - 40)
        w = int(w * self.zoom.value() / 100.0)
        for img, pm in self._items:
            if not pm.isNull():
                img.setPixmap(pm.scaledToWidth(w, Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, e):  # noqa: N802
        super(PhotoDock, self).resizeEvent(e)
        self._relayout()

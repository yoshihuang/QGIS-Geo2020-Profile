# -*- coding: utf-8 -*-
"""Plugin entry point: toolbar / menu actions."""
import os

from qgis.PyQt.QtGui import QIcon
from qgis.core import QgsProject

from .core.compat import QAction

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
MENU = "Geo2020 地質剖面產製"


def icon(name):
    return QIcon(os.path.join(PLUGIN_DIR, "icons", name))


class Geo2020ProfilePlugin(object):
    def __init__(self, iface):
        self.iface = iface
        self.actions = []
        self.toolbar = None
        self.dialog = None
        self.photo_tool = None
        self.photo_dock = None

    # ------------------------------------------------------------------ GUI
    def initGui(self):  # noqa: N802
        self.toolbar = self.iface.addToolBar(MENU)
        self.toolbar.setObjectName("Geo2020ProfileToolbar")

        a = QAction(icon("profile.svg"), "地質剖面產製設定…", self.iface.mainWindow())
        a.setToolTip("開啟設定畫面，由 Geo2020 MDB 產製地質剖面 QGIS 專案檔")
        a.triggered.connect(self.show_dialog)
        self._register(a)

        a = QAction(icon("photo.svg"), "顯示岩心照片", self.iface.mainWindow())
        a.setToolTip("點選地層（鑽孔柱狀圖）圖層，於照片視窗顯示岩心箱照片")
        a.setCheckable(True)
        a.triggered.connect(self.toggle_photo_tool)
        self.photo_action = a
        self._register(a)

        a = QAction(icon("help.svg"), "程式說明", self.iface.mainWindow())
        a.setToolTip("套件開發者資訊及操作說明")
        a.triggered.connect(self.show_help)
        self._register(a)

    def _register(self, action):
        self.toolbar.addAction(action)
        self.iface.addPluginToMenu(MENU, action)
        self.actions.append(action)

    def unload(self):
        for a in self.actions:
            self.iface.removePluginMenu(MENU, a)
        if self.photo_tool is not None:
            try:
                if self.iface.mapCanvas().mapTool() is self.photo_tool:
                    self.iface.mapCanvas().unsetMapTool(self.photo_tool)
            except Exception:
                pass
            self.photo_tool = None
        if self.photo_dock is not None:
            self.iface.removeDockWidget(self.photo_dock)
            self.photo_dock.deleteLater()
            self.photo_dock = None
        if self.dialog is not None:
            self.dialog.close()
            self.dialog.deleteLater()
            self.dialog = None
        if self.toolbar is not None:
            self.toolbar.deleteLater()
            self.toolbar = None
        self.actions = []

    # -------------------------------------------------------------- actions
    def show_dialog(self):
        from .gui.main_dialog import MainDialog
        if self.dialog is None:
            self.dialog = MainDialog(self.iface, self, self.iface.mainWindow())
        self.dialog.refresh_layers()
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def show_help(self, parent=None):
        from .gui.help_dialog import HelpDialog
        dlg = HelpDialog(parent or self.iface.mainWindow())
        dlg.exec()

    def _ensure_photo_dock(self):
        from .gui.photo_viewer import PhotoDock
        if self.photo_dock is None:
            self.photo_dock = PhotoDock(self.iface.mainWindow())
            try:
                from qgis.PyQt.QtCore import Qt
                area = Qt.DockWidgetArea.RightDockWidgetArea
            except AttributeError:  # pragma: no cover
                area = 2
            self.iface.addDockWidget(area, self.photo_dock)
        self.photo_dock.show()
        self.photo_dock.raise_()
        return self.photo_dock

    def toggle_photo_tool(self, checked):
        from .gui.photo_viewer import PhotoMapTool
        canvas = self.iface.mapCanvas()
        if not checked:
            if self.photo_tool is not None and canvas.mapTool() is self.photo_tool:
                canvas.unsetMapTool(self.photo_tool)
            return
        if self.photo_tool is None:
            self.photo_tool = PhotoMapTool(canvas, self)
            self.photo_tool.setAction(self.photo_action)
        canvas.setMapTool(self.photo_tool)
        self._ensure_photo_dock()
        self.iface.messageBar().pushInfo("岩心照片", "請在地圖上點選地層（鑽孔柱狀圖）圖徵")

    def show_photos_for_feature(self, layer_id, fid):
        """Called by the tool and by the '顯示岩心照片' layer action in generated projects."""
        from .gui.photo_viewer import photos_for_feature
        layer = QgsProject.instance().mapLayer(layer_id)
        if layer is None:
            return
        feat = layer.getFeature(int(fid))
        info = photos_for_feature(layer, feat)
        dock = self._ensure_photo_dock()
        dock.show_info(info)

# -*- coding: utf-8 -*-
"""主設定畫面：Geo2020 MDB → 地質剖面 QGIS 專案檔."""
import os
import time

from qgis.PyQt.QtCore import QCoreApplication, Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDialog,
                                 QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                                 QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
                                 QPlainTextEdit, QProgressBar, QPushButton, QRadioButton,
                                 QScrollArea, QSlider, QSpinBox, QTabWidget, QVBoxLayout, QWidget)
from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsProject, QgsRasterLayer, QgsRectangle, QgsSettings, QgsVectorLayer)
from qgis.gui import (QgsFieldComboBox, QgsFileWidget, QgsMapLayerComboBox,
                      QgsProjectionSelectionWidget)

from ..core import runner
from ..core.compat import geometry_kind, log_exception
from ..core.geo2020 import Geo2020Data, find_mdb_files
from ..core.holes_layer import LAYER_PROP, build_holes_layer

SKEY = "geo2020_profile/"


def _e(owner, scoped, name):
    if hasattr(owner, name):
        return getattr(owner, name)
    return getattr(getattr(owner, scoped), name)


def _layer_filter(*names):
    """QgsMapLayerProxyModel filter flags (Qgis.LayerFilter on QGIS >= 3.34)."""
    try:
        from qgis.core import Qgis
        F = Qgis.LayerFilter
        val = getattr(F, names[0])
        for n in names[1:]:
            val = val | getattr(F, n)
        return val
    except AttributeError:
        from qgis.core import QgsMapLayerProxyModel as P
        val = getattr(P, names[0])
        for n in names[1:]:
            val = val | getattr(P, n)
        return val


def _numeric_field_filter():
    try:
        from qgis.core import QgsFieldProxyModel
        return _e(QgsFieldProxyModel, "Filter", "Numeric")
    except Exception:  # pragma: no cover
        return None


class MainDialog(QDialog):
    def __init__(self, iface, plugin, parent=None):
        super(MainDialog, self).__init__(parent)
        self.iface = iface
        self.plugin = plugin
        self.geo = None
        self.photos_ok = False
        self.holes_layer_id = None
        self._running = False
        self._cancel = False
        self.setWindowTitle("Geo2020 地質剖面產製 — 設定")
        self.resize(760, 820)
        self._build_ui()
        self._restore()
        self._connect()
        self._smart_defaults()
        self._update_terrain_widgets()
        self._update_selection_info()

    # ================================================================ UI
    def _build_ui(self):
        outer = QVBoxLayout(self)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        lay = QVBoxLayout(body)
        scroll.setWidget(body)
        self.tabs.addTab(scroll, "設定")

        # 1 ------------------------------------------------------- Geo2020
        g1 = QGroupBox("1. Geo2020 MDB 資料夾")
        f1 = QFormLayout(g1)
        self.folder_w = QgsFileWidget()
        self.folder_w.setStorageMode(_e(QgsFileWidget, "StorageMode", "GetDirectory"))
        self.folder_w.setDialogTitle("選擇 Geo2020 MDB 所在資料夾")
        f1.addRow("資料夾：", self.folder_w)
        self.mdb_cb = QComboBox()
        f1.addRow("MDB 檔：", self.mdb_cb)
        self.proj_cb = QComboBox()
        self.proj_cb.setToolTip("MDB 內含多個計畫時，可只使用其中一個計畫的鑽孔")
        f1.addRow("計畫：", self.proj_cb)
        self.crs_w = QgsProjectionSelectionWidget()
        self.crs_w.setCrs(QgsCoordinateReferenceSystem("EPSG:3826"))
        self.crs_w.setToolTip("鑽孔座標 (MDB coord_x / coord_y) 的坐標參考系統；"
                              "預設依 MDB coord_system 判定：1=TWD67 TM2、2=UTM、3=TWD97 TM2")
        f1.addRow("鑽孔座標系統：", self.crs_w)
        row = QHBoxLayout()
        self.reload_btn = QPushButton("讀取 MDB 並加入鑽孔圖層")
        row.addWidget(self.reload_btn)
        row.addStretch(1)
        f1.addRow("", row)
        self.mdb_info = QLabel("尚未選擇資料夾")
        self.mdb_info.setWordWrap(True)
        f1.addRow("狀態：", self.mdb_info)
        self.photo_info = QLabel("")
        self.photo_info.setWordWrap(True)
        f1.addRow("岩心照片：", self.photo_info)
        lay.addWidget(g1)

        # 2 ------------------------------------------------------- terrain
        g2 = QGroupBox("2. 數值地形（DEM 網格或等高線）")
        f2 = QFormLayout(g2)
        self.terrain_cb = QgsMapLayerComboBox()
        self.terrain_cb.setFilters(_layer_filter("RasterLayer", "LineLayer"))
        self.terrain_cb.setAllowEmptyLayer(True)
        f2.addRow("地形圖層：", self.terrain_cb)
        self.band_cb = QComboBox()
        self.band_lbl = QLabel("DEM 波段：")
        f2.addRow(self.band_lbl, self.band_cb)
        self.contour_field = QgsFieldComboBox()
        nf = _numeric_field_filter()
        if nf is not None:
            self.contour_field.setFilters(nf)
        self.contour_lbl = QLabel("等高線高程欄位：")
        f2.addRow(self.contour_lbl, self.contour_field)
        lay.addWidget(g2)

        # 3 ------------------------------------------------------- collar elevation
        g3 = QGroupBox("3. 井口高程來源")
        h3 = QVBoxLayout(g3)
        self.elev_dem = QRadioButton("使用數值地形（DEM / 等高線）於鑽孔位置的高程（柱狀圖頂端貼齊地形線）")
        self.elev_mdb = QRadioButton("使用 Geo2020 MDB 的井口高程 (measured_attribu.elevation)")
        self.elev_grp = QButtonGroup(self)
        self.elev_grp.addButton(self.elev_dem)
        self.elev_grp.addButton(self.elev_mdb)
        self.elev_dem.setChecked(True)
        h3.addWidget(self.elev_dem)
        h3.addWidget(self.elev_mdb)
        lay.addWidget(g3)

        # 4 ------------------------------------------------------- section lines
        g4 = QGroupBox("4. 剖面線")
        f4 = QFormLayout(g4)
        self.sec_cb = QgsMapLayerComboBox()
        self.sec_cb.setFilters(_layer_filter("LineLayer"))
        self.sec_cb.setAllowEmptyLayer(True)
        f4.addRow("剖面線圖層：", self.sec_cb)
        self.name_field = QgsFieldComboBox()
        self.name_field.setToolTip("欄位值作為輸出 QGZ 專案檔名，以及剖面兩端標示 (如 A、A')")
        f4.addRow("剖面名稱欄位：", self.name_field)
        h4 = QHBoxLayout()
        self.run_all = QRadioButton("全部剖面")
        self.run_sel = QRadioButton("只執行選取 (Selected) 的剖面")
        self.scope_grp = QButtonGroup(self)
        self.scope_grp.addButton(self.run_all)
        self.scope_grp.addButton(self.run_sel)
        self.run_all.setChecked(True)
        h4.addWidget(self.run_all)
        h4.addWidget(self.run_sel)
        h4.addStretch(1)
        f4.addRow("執行範圍：", h4)
        self.sel_info = QLabel("")
        f4.addRow("", self.sel_info)
        lay.addWidget(g4)

        # 5 ------------------------------------------------------- distance
        g5 = QGroupBox("5. 鑽孔與剖面線距離")
        h5 = QHBoxLayout(g5)
        self.dist_slider = QSlider(Qt.Orientation.Horizontal)
        self.dist_slider.setRange(5, 50)
        self.dist_slider.setValue(10)
        self.dist_slider.setTickInterval(5)
        self.dist_slider.setTickPosition(_e(QSlider, "TickPosition", "TicksBelow"))
        self.dist_spin = QSpinBox()
        self.dist_spin.setRange(5, 50)
        self.dist_spin.setValue(10)
        self.dist_spin.setSuffix(" m")
        h5.addWidget(QLabel("5 m"))
        h5.addWidget(self.dist_slider, 1)
        h5.addWidget(QLabel("50 m"))
        h5.addWidget(self.dist_spin)
        lay.addWidget(g5)
        tip = QLabel("鑽孔位置與剖面線的最近距離在此值以內者，投影到剖面上繪製。")
        tip.setStyleSheet("color:#666;")
        lay.addWidget(tip)

        # 6 ------------------------------------------------------- attitudes
        g_att = QGroupBox("6. 代表位態（岩層界線、地層視傾角、斷層的延伸方向）")
        v_att = QVBoxLayout(g_att)
        self.rock_box = QGroupBox("岩層代表位態（層理／劈理）")
        self.rock_box.setCheckable(True)
        self.rock_box.setChecked(False)
        h_r = QHBoxLayout(self.rock_box)
        self.rock_dd = self._angle_spin(360.0, "傾向（dip direction）")
        self.rock_dip = self._angle_spin(90.0, "傾角（dip）")
        h_r.addWidget(QLabel("傾向方位角"))
        h_r.addWidget(self.rock_dd)
        h_r.addWidget(QLabel("傾角"))
        h_r.addWidget(self.rock_dip)
        h_r.addStretch(1)
        v_att.addWidget(self.rock_box)
        self.core_angle = QCheckBox("傾角改用 MDB 層理／劈理／片理實測傾角的平均值（傾向仍依代表位態；地層界線與視傾角線一致）")
        v_att.addWidget(self.core_angle)
        self.correlate = QCheckBox("相鄰鑽孔有相同上下岩層的界面時直接連線對比（不勾選：每孔界面皆依視傾角延伸）")
        v_att.addWidget(self.correlate)
        self.fault_box = QGroupBox("斷層代表位態（MDB 有斷層／斷層帶紀錄時用來延伸斷層線）")
        self.fault_box.setCheckable(True)
        self.fault_box.setChecked(False)
        h_f = QHBoxLayout(self.fault_box)
        self.fault_dd = self._angle_spin(360.0, "斷層傾向")
        self.fault_dip = self._angle_spin(90.0, "斷層傾角")
        self.fault_dip.setValue(70.0)
        h_f.addWidget(QLabel("傾向方位角"))
        h_f.addWidget(self.fault_dd)
        h_f.addWidget(QLabel("傾角"))
        h_f.addWidget(self.fault_dip)
        h_f.addStretch(1)
        v_att.addWidget(self.fault_box)
        tip2 = QLabel("位態以方位角表示（例：傾向 120°、傾角 35°）。程式依各剖面的方位計算視傾角，決定岩層向哪一側傾斜；"
                      "未輸入時，岩層界線以水平延伸，且不繪製地層視傾角線與斷層延伸線。")
        tip2.setWordWrap(True)
        tip2.setStyleSheet("color:#666;")
        v_att.addWidget(tip2)
        lay.addWidget(g_att)

        # 7 ------------------------------------------------------- output
        g6 = QGroupBox("7. 輸出資料夾（QGZ 專案檔 + %s）" % runner.GPKG_NAME)
        f6 = QFormLayout(g6)
        self.out_w = QgsFileWidget()
        self.out_w.setStorageMode(_e(QgsFileWidget, "StorageMode", "GetDirectory"))
        self.out_w.setDialogTitle("選擇地質剖面輸出資料夾")
        f6.addRow("輸出資料夾：", self.out_w)
        h6 = QHBoxLayout()
        self.prefix = QLineEdit()
        self.prefix.setPlaceholderText("例：地質剖面")
        self.suffix = QLineEdit()
        self.suffix.setPlaceholderText("例：_宜蘭")
        h6.addWidget(QLabel("前綴"))
        h6.addWidget(self.prefix)
        h6.addWidget(QLabel("後綴"))
        h6.addWidget(self.suffix)
        f6.addRow("檔名（選填）：", h6)
        self.colored = QCheckBox("地層圖元加上岩類底色（土壤黃、沉積岩藍、變質岩綠、火成岩紅）")
        self.colored.setChecked(True)
        f6.addRow("", self.colored)
        self.open_after = QCheckBox("完成後開啟第一個剖面專案")
        f6.addRow("", self.open_after)
        lay.addWidget(g6)
        lay.addStretch(1)

        # log + results --------------------------------------------------
        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("執行訊息")
        bl.addWidget(self.log, 3)
        bl.addWidget(QLabel("產出的專案（雙擊開啟）："))
        rl = QHBoxLayout()
        self.results = QListWidget()
        self.results.setSelectionMode(_e(QAbstractItemView, "SelectionMode", "SingleSelection"))
        self.results.setMinimumHeight(120)
        rl.addWidget(self.results, 1)
        rb = QVBoxLayout()
        self.open_prj_btn = QPushButton("開啟所選專案")
        self.open_dir_btn = QPushButton("開啟輸出資料夾")
        rb.addWidget(self.open_prj_btn)
        rb.addWidget(self.open_dir_btn)
        rb.addStretch(1)
        rl.addLayout(rb)
        bl.addLayout(rl, 1)
        self.progress = QProgressBar()
        self.progress.setValue(0)
        bl.addWidget(self.progress)
        self.tabs.addTab(bottom, "執行結果")

        btns = QHBoxLayout()
        self.help_btn = QPushButton("程式說明")
        self.run_btn = QPushButton("開始繪製地質剖面")
        self.run_btn.setDefault(True)
        self.run_btn.setStyleSheet("font-weight:bold; padding:6px 18px;")
        self.cancel_btn = QPushButton("取消執行")
        self.cancel_btn.setEnabled(False)
        self.close_btn = QPushButton("關閉")
        btns.addWidget(self.help_btn)
        btns.addStretch(1)
        btns.addWidget(self.run_btn)
        btns.addWidget(self.cancel_btn)
        btns.addWidget(self.close_btn)
        outer.addLayout(btns)

    @staticmethod
    def _angle_spin(maximum, tip):
        sp = QDoubleSpinBox()
        sp.setRange(0.0, maximum)
        sp.setDecimals(1)
        sp.setSingleStep(1.0)
        sp.setSuffix("°")
        sp.setToolTip(tip)
        sp.setMinimumWidth(90)
        return sp

    def _connect(self):
        self.folder_w.fileChanged.connect(self._folder_changed)
        self.mdb_cb.activated.connect(lambda *_: self.load_mdb())
        self.reload_btn.clicked.connect(lambda: self.load_mdb(force=True))
        self.proj_cb.activated.connect(lambda *_: self._add_holes_layer())
        self.crs_w.crsChanged.connect(lambda *_: self._add_holes_layer() if self.geo else None)
        self.terrain_cb.layerChanged.connect(self._update_terrain_widgets)
        self.sec_cb.layerChanged.connect(self._section_layer_changed)
        self.run_all.toggled.connect(self._update_selection_info)
        self.dist_slider.valueChanged.connect(self.dist_spin.setValue)
        self.dist_spin.valueChanged.connect(self.dist_slider.setValue)
        self.run_btn.clicked.connect(self.start)
        self.cancel_btn.clicked.connect(self._do_cancel)
        self.close_btn.clicked.connect(self.close)
        self.help_btn.clicked.connect(lambda: self.plugin.show_help(self))
        self.open_dir_btn.clicked.connect(self._open_out_dir)
        self.open_prj_btn.clicked.connect(self._open_selected_project)
        self.results.itemDoubleClicked.connect(lambda *_: self._open_selected_project())
        self._section_layer_changed(self.sec_cb.currentLayer())

    # ============================================================ settings
    def _restore(self):
        s = QgsSettings()
        self.dist_slider.setValue(int(s.value(SKEY + "buffer", 10)))
        self.dist_spin.setValue(int(s.value(SKEY + "buffer", 10)))
        self.prefix.setText(s.value(SKEY + "prefix", ""))
        self.suffix.setText(s.value(SKEY + "suffix", ""))
        self.colored.setChecked(s.value(SKEY + "colored", True, type=bool))
        self.open_after.setChecked(s.value(SKEY + "open_after", False, type=bool))
        self.rock_box.setChecked(s.value(SKEY + "rock_on", False, type=bool))
        self.rock_dd.setValue(float(s.value(SKEY + "rock_dd", 0.0)))
        self.rock_dip.setValue(float(s.value(SKEY + "rock_dip", 0.0)))
        self.fault_box.setChecked(s.value(SKEY + "fault_on", False, type=bool))
        self.fault_dd.setValue(float(s.value(SKEY + "fault_dd", 0.0)))
        self.fault_dip.setValue(float(s.value(SKEY + "fault_dip", 70.0)))
        self.core_angle.setChecked(s.value(SKEY + "core_angle", False, type=bool))
        self.correlate.setChecked(s.value(SKEY + "correlate", False, type=bool))
        if s.value(SKEY + "elev", "dem") == "mdb":
            self.elev_mdb.setChecked(True)
        out = s.value(SKEY + "out_dir", "")
        if out:
            self.out_w.setFilePath(out)
        folder = s.value(SKEY + "folder", "")
        if folder and os.path.isdir(folder):
            self.folder_w.blockSignals(True)
            self.folder_w.setFilePath(folder)
            self.folder_w.blockSignals(False)
            self._fill_mdb_list(folder)

    def _save(self):
        s = QgsSettings()
        s.setValue(SKEY + "buffer", self.dist_spin.value())
        s.setValue(SKEY + "prefix", self.prefix.text())
        s.setValue(SKEY + "suffix", self.suffix.text())
        s.setValue(SKEY + "colored", self.colored.isChecked())
        s.setValue(SKEY + "open_after", self.open_after.isChecked())
        s.setValue(SKEY + "elev", "mdb" if self.elev_mdb.isChecked() else "dem")
        s.setValue(SKEY + "rock_on", self.rock_box.isChecked())
        s.setValue(SKEY + "rock_dd", self.rock_dd.value())
        s.setValue(SKEY + "rock_dip", self.rock_dip.value())
        s.setValue(SKEY + "fault_on", self.fault_box.isChecked())
        s.setValue(SKEY + "fault_dd", self.fault_dd.value())
        s.setValue(SKEY + "fault_dip", self.fault_dip.value())
        s.setValue(SKEY + "core_angle", self.core_angle.isChecked())
        s.setValue(SKEY + "correlate", self.correlate.isChecked())
        s.setValue(SKEY + "out_dir", self.out_w.filePath())
        s.setValue(SKEY + "folder", self.folder_w.filePath())

    def refresh_layers(self):
        """Called each time the dialog is shown (layers may have changed)."""
        self._smart_defaults()
        self._update_terrain_widgets()
        self._update_selection_info()

    def _smart_defaults(self):
        """Pick likely layers (剖面線 / DEM) instead of whatever layer happens to be first."""
        layers = list(QgsProject.instance().mapLayers().values())

        def pick(combo, pred_list):
            cur = combo.currentLayer()
            for pred in pred_list:
                if cur is not None and pred(cur):
                    return
                for lyr in layers:
                    try:
                        if pred(lyr) and combo.model().indexFromLayer(lyr).isValid():
                            combo.setLayer(lyr)
                            return
                    except Exception:
                        if pred(lyr):
                            combo.setLayer(lyr)
                            return

        def is_line(item_):
            return isinstance(item_, QgsVectorLayer) and geometry_kind(item_) == "Line"
        pick(self.sec_cb, [lambda item_: is_line(item_) and ("剖面" in item_.name() or "section" in item_.name().lower()
                                                    or "profile" in item_.name().lower())])
        pick(self.terrain_cb, [lambda item_: isinstance(item_, QgsRasterLayer) and ("dem" in item_.name().lower()
                                                                         or "dtm" in item_.name().lower()
                                                                         or "地形" in item_.name()),
                               lambda item_: isinstance(item_, QgsRasterLayer),
                               lambda item_: is_line(item_) and ("等高" in item_.name() or "contour" in item_.name().lower()
                                                         or "cntr" in item_.name().lower())])
        if self.terrain_cb.currentLayer() is not None and \
                self.terrain_cb.currentLayer() is self.sec_cb.currentLayer():
            self.terrain_cb.setLayer(None)

    # ============================================================ Geo2020
    def _fill_mdb_list(self, folder):
        self.mdb_cb.clear()
        files = find_mdb_files(folder)
        for p in files:
            self.mdb_cb.addItem(os.path.basename(p), p)
        if not files:
            self.mdb_info.setText("<span style='color:#c00'>資料夾中找不到 MDB 檔 (*.mdb)</span>")
            self.photo_info.setText("")
        return files

    def _folder_changed(self, folder):
        self.geo = None
        if not folder or not os.path.isdir(folder):
            self.mdb_cb.clear()
            self.mdb_info.setText("<span style='color:#c00'>資料夾不存在</span>")
            return
        if self._fill_mdb_list(folder):
            self.load_mdb()
        if not self.out_w.filePath():
            self.out_w.setFilePath(os.path.join(folder, "地質剖面"))

    def current_mdb(self):
        return self.mdb_cb.currentData() or ""

    def load_mdb(self, force=False):
        path = self.current_mdb()
        folder = self.folder_w.filePath()
        if not path:
            return False
        if self.geo is not None and self.geo.mdb_path == path and not force:
            self._add_holes_layer()
            return True
        QCoreApplication.processEvents()
        try:
            self.setCursor(Qt.CursorShape.WaitCursor)
            t0 = time.time()
            geo = Geo2020Data(path)
            n_holes_ph, n_ph = geo.scan_photos(folder)
        except Exception as e:  # noqa
            self.unsetCursor()
            self.geo = None
            self.mdb_info.setText("<span style='color:#c00'>無法讀取 MDB：%s</span>" % e)
            QMessageBox.critical(self, "Geo2020 MDB", "無法讀取 MDB 檔：\n%s\n\n%s" % (path, e))
            return False
        finally:
            self.unsetCursor()
        self.geo = geo
        usable = geo.usable_holes()
        self.proj_cb.blockSignals(True)
        self.proj_cb.clear()
        projs = sorted(set(b.proj_no for b in usable))
        if len(projs) != 1:
            self.proj_cb.addItem("全部計畫 (%d 個)" % len(projs), None)
        for p in projs:
            n = len([b for b in usable if b.proj_no == p])
            self.proj_cb.addItem("%s %s（%d 孔）" % (p, geo.projects.get(p, ""), n), p)
        self.proj_cb.blockSignals(False)
        self.crs_w.blockSignals(True)
        self.crs_w.setCrs(QgsCoordinateReferenceSystem(geo.suggested_crs_authid()))
        self.crs_w.blockSignals(False)
        no_xy = [b.hole_no for b in geo.holes.values() if b.x is None or b.y is None]
        no_layers = [b.hole_no for b in geo.holes.values() if b.x is not None and not b.layers]
        msg = "已讀取 %d 個鑽孔，其中 %d 孔有座標與地層資料 (%.1f 秒)" % (len(geo.holes), len(usable), time.time() - t0)
        if no_xy:
            msg += "<br/><span style='color:#b60'>無座標：%s</span>" % "、".join(sorted(no_xy))
        if no_layers:
            msg += "<br/><span style='color:#b60'>無地層資料：%s</span>" % "、".join(sorted(no_layers))
        for w in geo.warnings:
            msg += "<br/><span style='color:#b60'>%s</span>" % w
        self.mdb_info.setText(msg)
        self.photos_ok = n_ph > 0
        if self.photos_ok:
            self.photo_info.setText("<span style='color:#080'>找到 %d 孔、共 %d 張岩心箱照片；"
                                    "產出的專案可點選地層顯示照片。</span>" % (n_holes_ph, n_ph))
        else:
            self.photo_info.setText("<span style='color:#c00'>資料夾中沒有各鑽孔的岩心箱照片資料夾，"
                                    "顯示岩心照片功能將停用 (Unable)。</span>")
            QMessageBox.warning(
                self, "岩心照片",
                "Geo2020 資料夾中沒有找到各鑽孔的岩心箱照片資料夾。\n\n"
                "照片資料夾的建議結構：\n"
                "  <MDB 資料夾>\\<計畫編號>_Image\\<孔號>\\<孔號>-<箱號>*.jpg\n"
                "  或 <MDB 資料夾>\\...\\<孔號>\\*.jpg\n\n"
                "仍可繼續產製地質剖面，但「顯示岩心照片」功能將無法使用。")
        self._add_holes_layer()
        return True

    def _add_holes_layer(self):
        if self.geo is None:
            return
        prj = QgsProject.instance()
        # replace a previous borehole layer from this plugin
        for lyr in list(prj.mapLayers().values()):
            if lyr.customProperty(LAYER_PROP):
                prj.removeMapLayer(lyr.id())
        crs = self.crs_w.crs()
        name = "Geo2020 鑽孔位置（%s）" % os.path.basename(self.geo.mdb_path)
        lyr = build_holes_layer(self.geo, crs, self.proj_cb.currentData(), name)
        if lyr.featureCount() == 0:
            return
        prj.addMapLayer(lyr)
        self.holes_layer_id = lyr.id()
        self._zoom_to(lyr)

    def _zoom_to(self, lyr):
        canvas = self.iface.mapCanvas()
        ext = lyr.extent()
        dst = canvas.mapSettings().destinationCrs()
        if dst.isValid() and dst != lyr.crs():
            try:
                ext = QgsCoordinateTransform(lyr.crs(), dst, QgsProject.instance()).transformBoundingBox(ext)
            except Exception as e:
                log_exception("鑽孔範圍座標轉換失敗", e)
        geographic = dst.isGeographic() if dst.isValid() else lyr.crs().isGeographic()
        minw = 0.002 if geographic else 150.0
        if ext.width() < minw or ext.height() < minw:
            c = ext.center()
            ext = QgsRectangle(c.x() - minw / 2, c.y() - minw / 2, c.x() + minw / 2, c.y() + minw / 2)
        ext.scale(1.3)
        canvas.setExtent(ext)
        canvas.refresh()

    # ============================================================ terrain / sections
    def _update_terrain_widgets(self, *args):
        lyr = self.terrain_cb.currentLayer()
        is_r = isinstance(lyr, QgsRasterLayer)
        is_v = isinstance(lyr, QgsVectorLayer)
        self.band_cb.clear()
        if is_r:
            for i in range(1, lyr.bandCount() + 1):
                self.band_cb.addItem("%d %s" % (i, lyr.bandName(i)), i)
        self.band_cb.setVisible(is_r)
        self.band_lbl.setVisible(is_r)
        self.contour_field.setVisible(is_v)
        self.contour_lbl.setVisible(is_v)
        if is_v:
            self.contour_field.setLayer(lyr)
            for cand in ("ELEV", "elev", "Elevation", "ELEVATION", "Z", "z", "CONTOUR", "高程", "HEIGHT", "height"):
                if lyr.fields().indexOf(cand) >= 0:
                    self.contour_field.setField(cand)
                    break

    def _section_layer_changed(self, lyr):
        old = getattr(self, "_sel_layer", None)
        if old is not None:
            try:
                old.selectionChanged.disconnect(self._update_selection_info)
            except (TypeError, RuntimeError) as e:  # already disconnected / layer deleted
                log_exception("剖面線圖層訊號解除", e)
        self._sel_layer = lyr if isinstance(lyr, QgsVectorLayer) else None
        self.name_field.setLayer(self._sel_layer)
        if self._sel_layer is not None:
            self._sel_layer.selectionChanged.connect(self._update_selection_info)
            for cand in ("Name", "name", "NAME", "剖面", "剖面名稱", "名稱", "id"):
                if self._sel_layer.fields().indexOf(cand) >= 0:
                    self.name_field.setField(cand)
                    break
        self._update_selection_info()

    def _update_selection_info(self, *args):
        lyr = self.sec_cb.currentLayer()
        if not isinstance(lyr, QgsVectorLayer):
            self.sel_info.setText("")
            return
        n_all = lyr.featureCount()
        n_sel = lyr.selectedFeatureCount()
        self.sel_info.setText("圖層共 %d 條剖面線，已選取 %d 條" % (n_all, n_sel))
        if self.run_sel.isChecked() and n_sel == 0:
            self.sel_info.setText(self.sel_info.text() + "　<span style='color:#c00'>（尚未選取剖面線）</span>")

    # ============================================================ run
    def config(self):
        terrain = self.terrain_cb.currentLayer()
        return runner.RunConfig(
            folder=self.folder_w.filePath(),
            mdb_path=self.current_mdb(),
            proj_filter=self.proj_cb.currentData(),
            hole_crs=self.crs_w.crs(),
            terrain_layer=terrain,
            terrain_band=self.band_cb.currentData() or 1,
            contour_field=self.contour_field.currentField() if isinstance(terrain, QgsVectorLayer) else None,
            elev_source="mdb" if self.elev_mdb.isChecked() else "dem",
            section_layer=self.sec_cb.currentLayer(),
            name_field=self.name_field.currentField(),
            selected_only=self.run_sel.isChecked(),
            buffer=self.dist_spin.value(),
            out_dir=self.out_w.filePath(),
            prefix=self.prefix.text().strip(),
            suffix=self.suffix.text().strip(),
            colored=self.colored.isChecked(),
            rock_attitude=(self.rock_dd.value(), self.rock_dip.value()) if self.rock_box.isChecked() else None,
            fault_attitude=(self.fault_dd.value(), self.fault_dip.value()) if self.fault_box.isChecked() else None,
            use_core_angle=self.core_angle.isChecked(),
            correlate=self.correlate.isChecked(),
            geo=self.geo,
            transform_context=QgsProject.instance().transformContext())

    def _log(self, msg):
        self.log.appendPlainText(msg)
        QCoreApplication.processEvents()

    def _progress(self, v):
        self.progress.setValue(v)
        QCoreApplication.processEvents()

    def _do_cancel(self):
        self._cancel = True

    def start(self):
        if self._running:
            return
        self.log.clear()
        self.results.clear()
        self.progress.setValue(0)
        cfg = self.config()
        # 1. validate
        errs = runner.validate(cfg)
        if errs:
            self.tabs.setCurrentIndex(0)
            text = "\n".join("• [%s] %s" % (k, m) for k, m in errs)
            self._log("設定檢查未通過：\n" + text)
            QMessageBox.warning(self, "設定有誤", "下列設定需重新設定後再執行：\n\n" + text)
            return
        self.geo = cfg.geo
        self._save()
        plans = runner.planned_outputs(cfg)
        if len(plans) > 10:
            yes = _e(QMessageBox, "StandardButton", "Yes")
            no = _e(QMessageBox, "StandardButton", "No")
            if QMessageBox.question(self, "剖面數量", "將產製 %d 個地質剖面（圖層「%s」），確定執行？" % (
                    len(plans), cfg.section_layer.name()), yes | no, no) != yes:
                return
        # 2. overwrite
        exist = runner.existing_outputs(cfg)
        if exist:
            shown = "\n".join(os.path.basename(p) for p in exist[:15])
            if len(exist) > 15:
                shown += "\n… 共 %d 個" % len(exist)
            yes = _e(QMessageBox, "StandardButton", "Yes")
            no = _e(QMessageBox, "StandardButton", "No")
            r = QMessageBox.question(
                self, "覆蓋輸出檔",
                "輸出資料夾中已有下列檔案：\n\n%s\n\n是否覆蓋？\n"
                "（%s 中同名剖面的圖層會被取代，其他剖面的圖層保留）" % (shown, runner.GPKG_NAME),
                yes | no, no)
            if r != yes:
                self._log("使用者取消：不覆蓋既有檔案")
                return
            if self._outputs_open_in_project(cfg, plans):
                QMessageBox.warning(self, "檔案使用中",
                                    "目前開啟的 QGIS 專案正在使用要覆蓋的 %s 圖層。\n"
                                    "請先開新專案或移除這些圖層後再執行。" % runner.GPKG_NAME)
                return
        # 3. run
        self.tabs.setCurrentIndex(1)
        self._running = True
        self._cancel = False
        self.run_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self._log("開始產製 %d 個地質剖面 → %s" % (len(plans), cfg.out_dir))
        self._log("井口高程：%s；距離門檻：%d m；鑽孔座標：%s" % (
            "數值地形" if cfg.elev_source == "dem" else "Geo2020 MDB", cfg.buffer, cfg.hole_crs.authid()))
        if not self.photos_ok:
            self._log("⚠ 沒有岩心箱照片資料夾：產出專案的顯示岩心照片功能停用")
        try:
            res = runner.run(cfg, progress=self._progress, log=self._log, is_canceled=lambda: self._cancel)
        except Exception as e:  # noqa
            import traceback
            self._log(traceback.format_exc())
            QMessageBox.critical(self, "執行失敗", str(e))
            res = []
        finally:
            self._running = False
            self.run_btn.setEnabled(True)
            self.cancel_btn.setEnabled(False)
        ok = [r for r in res if r.get("ok")]
        bad = [r for r in res if not r.get("ok")]
        for r in ok:
            it = QListWidgetItem("✔ %s　（鑽孔：%s）" % (os.path.basename(r["qgz"]), "、".join(r["holes"]) or "無"))
            it.setData(Qt.ItemDataRole.UserRole, r["qgz"])
            self.results.addItem(it)
        for r in bad:
            it = QListWidgetItem("✖ %s：%s" % (r["name"], r.get("error", "")))
            self.results.addItem(it)
        if self.results.count():
            self.results.setCurrentRow(0)
        self._log("完成：成功 %d、失敗 %d" % (len(ok), len(bad)))
        if ok:
            self.iface.messageBar().pushSuccess(
                "Geo2020 地質剖面", "已產製 %d 個地質剖面專案於 %s" % (len(ok), cfg.out_dir))
        if bad:
            QMessageBox.warning(self, "部分剖面失敗", "\n".join("%s：%s" % (r["name"], r.get("error")) for r in bad))
        if ok:
            self._persist_holes_layer(cfg.gpkg_path)
        if ok and self.open_after.isChecked():
            self._open_project(ok[0]["qgz"])

    def _persist_holes_layer(self, gpkg):
        """The borehole layer starts as a memory layer (lost when the project is reopened):
        save it into the output GeoPackage and point the map layer at the saved copy."""
        lyr = QgsProject.instance().mapLayer(self.holes_layer_id) if self.holes_layer_id else None
        if lyr is None or lyr.providerType() != "memory":
            return
        try:
            from qgis.core import QgsVectorFileWriter
            opts = QgsVectorFileWriter.SaveVectorOptions()
            opts.driverName = "GPKG"
            opts.layerName = "鑽孔位置"
            opts.fileEncoding = "UTF-8"
            opts.actionOnExistingFile = _e(QgsVectorFileWriter, "ActionOnExistingFile", "CreateOrOverwriteLayer")
            res = QgsVectorFileWriter.writeAsVectorFormatV3(lyr, gpkg, QgsProject.instance().transformContext(), opts)
            if res[0] != _e(QgsVectorFileWriter, "WriterError", "NoError"):
                raise RuntimeError(res[1])
            lyr.setDataSource("%s|layername=鑽孔位置" % gpkg, lyr.name(), "ogr")
            self._log("鑽孔位置圖層已存入 %s（鑽孔位置）" % os.path.basename(gpkg))
        except Exception as e:  # keep the memory layer
            log_exception("鑽孔位置圖層存檔", e)
            self._log("⚠ 鑽孔位置圖層仍為暫存圖層，存專案前請另存：%s" % e)

    def _outputs_open_in_project(self, cfg, plans):
        gp = os.path.normcase(os.path.abspath(cfg.gpkg_path))
        for lyr in QgsProject.instance().mapLayers().values():
            if lyr.customProperty(LAYER_PROP):
                continue  # the plugin's own borehole layer only reads the GeoPackage
            src = lyr.source().split("|")[0]
            if src and os.path.normcase(os.path.abspath(src)) == gp:
                return True
        cur = QgsProject.instance().fileName()
        if cur:
            for _n, q, _f in plans:
                if os.path.normcase(os.path.abspath(q)) == os.path.normcase(os.path.abspath(cur)):
                    return True
        return False

    # ============================================================ results
    def _open_out_dir(self):
        d = self.out_w.filePath()
        if d and os.path.isdir(d):
            QDesktopServices.openUrl(QUrl.fromLocalFile(d))

    def _open_selected_project(self):
        it = self.results.currentItem()
        if it is None:
            return
        path = it.data(Qt.ItemDataRole.UserRole)
        if path:
            self._open_project(path)

    def _open_project(self, path):
        prj = QgsProject.instance()
        if prj.isDirty():
            save = _e(QMessageBox, "StandardButton", "Save")
            disc = _e(QMessageBox, "StandardButton", "Discard")
            canc = _e(QMessageBox, "StandardButton", "Cancel")
            r = QMessageBox.question(self, "開啟剖面專案",
                                     "目前的專案有未儲存的變更。開啟剖面專案前是否儲存？",
                                     save | disc | canc, canc)
            if r == canc:
                return
            if r == save:
                if not prj.fileName():
                    self.iface.actionSaveProjectAs().trigger()
                else:
                    prj.write()
        prj.setDirty(False)
        self.iface.addProject(path)

    def closeEvent(self, e):  # noqa: N802
        if self._running:
            self._cancel = True
            e.ignore()
            return
        self._save()
        super(MainDialog, self).closeEvent(e)

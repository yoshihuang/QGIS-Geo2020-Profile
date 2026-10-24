# -*- coding: utf-8 -*-
"""程式說明：開發者資訊及操作說明."""
import configparser
import os

from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import QDialog, QHBoxLayout, QPushButton, QTextBrowser, QVBoxLayout

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def metadata():
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(os.path.join(PLUGIN_DIR, "metadata.txt"), encoding="utf-8")
    return dict(cp.items("general")) if cp.has_section("general") else {}


class HelpDialog(QDialog):
    def __init__(self, parent=None):
        super(HelpDialog, self).__init__(parent)
        self.setWindowTitle("Geo2020 地質剖面產製 — 程式說明")
        self.resize(820, 760)
        lay = QVBoxLayout(self)
        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(True)
        lay.addWidget(self.browser, 1)
        md = metadata()
        with open(os.path.join(PLUGIN_DIR, "help", "index.html"), "r", encoding="utf-8") as f:
            html = f.read()
        for k in ("name", "version", "author", "email", "homepage", "repository", "tracker", "qgisminimumversion"):
            html = html.replace("{{%s}}" % k, md.get(k, ""))
        self.browser.setSearchPaths([os.path.join(PLUGIN_DIR, "help"), os.path.join(PLUGIN_DIR, "icons")])
        self.browser.setHtml(html)
        row = QHBoxLayout()
        b = QPushButton("專案網站")
        b.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(md.get("homepage", ""))))
        b.setEnabled(bool(md.get("homepage")))
        row.addWidget(b)
        b2 = QPushButton("回報問題")
        b2.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(md.get("tracker", ""))))
        b2.setEnabled(bool(md.get("tracker")))
        row.addWidget(b2)
        row.addStretch(1)
        c = QPushButton("關閉")
        c.clicked.connect(self.accept)
        row.addWidget(c)
        lay.addLayout(row)

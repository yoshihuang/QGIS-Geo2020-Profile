# -*- coding: utf-8 -*-
"""Geo2020 地質剖面產製 (Geo2020 Geological Profile Builder) - QGIS plugin."""


def classFactory(iface):  # noqa: N802
    from .plugin import Geo2020ProfilePlugin
    return Geo2020ProfilePlugin(iface)

# Geo2020 地質剖面產製 (QGIS Plugin)

由 Geo2020 工程地質探勘資料庫 (MDB) 批次產製地質剖面 QGIS 專案檔 (.qgz) 與 `GroProfile.gpkg`。

Builds geological cross-section QGIS projects from Taiwan Geo2020 borehole databases (Access MDB).

## 功能
- 選擇 Geo2020 MDB 資料夾 → 自動讀取（內建純 Python Access 讀取器，免 ODBC）→ 地圖加入鑽孔位置圖層、標示孔號並縮放。
- 偵測各鑽孔岩心箱照片資料夾；沒有照片時提醒並停用照片功能。
- 數值地形可用 DEM 網格或等高線；井口高程可選數值地形或 MDB。
- 剖面線圖層 + 名稱欄位（=輸出檔名），可只執行選取的剖面。
- 鑽孔與剖面距離 5–50 m（預設 10 m）。
- 每條剖面一個 QGZ，所有圖層存入同一個 `GroProfile.gpkg`；版面依「地質剖面A_宜蘭」範本。
- 地層圖元依「工程地質探勘資料庫資料作業規範」(2008) 附錄B，內含全部 131 個圖元。
- 岩心照片檢視工具（點選地層顯示照片）、程式說明。

## 安裝
QGIS → 外掛 → 管理與安裝外掛 → 從 ZIP 安裝 → 選擇 `geo2020_profile.zip`。

支援 QGIS 3.28 以上（含 QGIS 4 / Qt6）。

## 授權
GPL-2.0-or-later

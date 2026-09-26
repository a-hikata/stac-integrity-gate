# Track F — GeoParquet Semantic Integrity: Report

Branch: `feature/geoparquet-integrity` (from RC `a6e23c0`). Date: 2026-09-26.

**Verdict: IMPLEMENTED (experimental, optional extra)**
**Recommendation: MERGE_WITH_CHANGES**. Merge it as an experimental opt-in. Before the final release, reconcile the one-line `audit.py` hook with Track E (Zarr) and Track G. The `NO_RASTER_ASSETS` message wording should also become format-neutral.

## 1. Evidence (F1)

### 実データでの計測（一次エビデンス）
NRP public-data STAC カタログ `https://s3-west.nrp-nautilus.io/public-data/stac/catalog.json` を匿名で巡回した（深さ3、426 ドキュメント）。そのうえで、単一ファイルの Parquet アセットについては HTTP Range リクエストでフッターだけを読んだ。

- Parquet/Table アセットは 1031 件。うち `table:columns` 付きが 1010 件、hive glob href (`hex/h0=*/data_0.parquet`) が 481 件。
- 単一ファイルで `table:columns` 付きのものは 258 件。フッターを読めたのは 257 件で、1 件は directory href (`public-inat/taxon/`)。
- **宣言と実ファイルの食い違い（実例）**:
  1. `table:row_count` の不一致: <https://s3-west.nrp-nautilus.io/public-wyoming/blm-sma/stac-collection.json> の asset `blm-sma-parquet` は **439,200** 行と宣言している。フッターの `num_rows` は **865,059**（`blm-sma.parquet`、last-modified 2026-03-07）。asset レベルの宣言なので ERROR とした。
  2. 列名が大文字小文字だけ違う: <https://s3-west.nrp-nautilus.io/public-high-seas/rfmo/stac-collection.json> の `vme-parquet` は宣言が `surface`、実際の列は `SURFACE`。Arrow/pandas では列が見つからない。DuckDB では見つかる。そのため WARN とした。
  3. Collection レベルで宣言された列が asset に存在しない: `pad-us` collection の `fee-parquet` / `easement-parquet` で `Access`, `d_Own_Type` など 5 列。複数アセットで共有するスキーマの可能性があるので WARN とした。これが「継承された宣言は WARN」というルールの根拠になっている。
  4. 型の食い違い: 61 件。例えば CDC SVI では int32/int16 の列を `double` と宣言している。`hydrothermal-vents` では string 列を `float64` と、BLM の各データセットでは string 列を `int32` / `date` と宣言している。Table 拡張の型語彙は自由記述なので WARN とした。
  5. CRS: 229 ファイルが `geo` を持つ。内訳は EPSG:4326 PROJJSON が 129、crs 省略（= OGC:CRS84）が 100。`nwi-v2.parquet` は `proj:epsg: 4326` を宣言していて、`geo` の crs は省略されている。これは**素朴に比較すると誤 ERROR になる罠**なので、実装で同値として扱うようにした。CRS の実不一致は 0 件。
  6. `table:primary_geometry` を宣言しているのは 5 件で、全件 `geo.primary_column` と一致した。
- 計測スクリプトと要約 JSON: `research/geoparquet-evidence/`（`crawl.py`, `footers.py`, `analyze.py`, `sweep.py`, `nrp-sweep-2026-09-26.json`）。`footers.py` だけは研究用に fsspec を使っているが、パッケージ本体は fsspec に依存しない。

### 公開 issue（二次エビデンス）
- <https://github.com/portolan-sdi/portolan-cli/issues/883>: Iceberg backend が `table:columns` の type に `geometry(EPSG:4326)` を書いていて、Table 拡張の語彙と食い違っている。型語彙が揃っていないことを示す例なので、型比較は WARN にとどめた。
- <https://github.com/OvertureMaps/stac/issues/130>: Overture STAC の `table:columns` には name しかない（type も description もない）。
- `table:row_count` や列欠落を直接報告している公開 issue は見つからなかった。**公開 issue としてのエビデンスはない。実データの計測で 1 件の明確な ERROR 級の不一致（blm-sma の row_count）を確認した。**

## 2. Invariants と severity (F2/F4)

詳細は `docs/geoparquet-design.md` にある。原則は次のとおり。ERROR にするのは、フッターを読めていて、値が厳密に比較でき、かつ宣言が asset 自身に書かれている場合だけ。Item properties や Collection から継承した宣言は、複数アセットを記述している可能性があるので WARN にする。

| Code | Severity |
|---|---|
| `TABLE_COLUMN_MISSING` | ERROR (asset) / WARN (継承) |
| `TABLE_ROW_COUNT_MISMATCH` | ERROR (asset) / WARN (継承) |
| `PRIMARY_GEOMETRY_MISSING` | ERROR (asset) / WARN (継承) |
| `CRS_MISMATCH` | ERROR（asset レベルで、両者が別の EPSG コードに解決される場合のみ。CRS84↔4326 は同値扱い） |
| `CRS_MISMATCH_UNCERTAIN`, `GEOPARQUET_CRS_UNDEFINED`, `CRS_UNPARSEABLE`, `GEOPARQUET_CRS_UNPARSEABLE` | WARN |
| `TABLE_COLUMN_CASE_MISMATCH`, `TABLE_COLUMN_PARTITION_KEY`, `TABLE_COLUMN_TYPE_MISMATCH`, `TABLE_COLUMNS_INVALID`, `TABLE_ROW_COUNT_INVALID` | WARN |
| `PRIMARY_GEOMETRY_NOT_GEOMETRY`, `PRIMARY_GEOMETRY_DIFFERS`, `GEOPARQUET_METADATA_MISSING`, `GEOPARQUET_METADATA_INVALID` | WARN |
| `PARQUET_PARTITIONED_UNVERIFIED`（glob / directory href）, `GEOPARQUET_DEPENDENCY_MISSING` | WARN（asset は skip） |
| `ASSET_UNREADABLE` | WARN。ローカルカタログでローカルファイルが欠けている場合だけ ERROR（raster と同じ規則） |
| 宣言されていない余分な列 | finding なし |

実装していないもの: `geo.bbox` と Item bbox の比較、`geometry_types`、glob の展開、行データの検査。

## 3. Implementation (F6)
- 新規 `stac_integrity/geoparquet_checks.py`。pyarrow は関数内で lazy import している。HTTP(S) のフッターは標準ライブラリだけで書いた Range reader で読む（suffix range で 64 KiB を 1 リクエスト）。`s3://` などは pyarrow.fs に任せる。
- `audit.py` への hook は約 15 行。raster ではなく Parquet と判定された asset だけを `audit_parquet_asset` に回す。raster の判定と処理経路は変えていない。
- Collection assets は既存の collection 監査（pseudo item）から自動で対象になる。

## 4. Dependencies (F3)
- `[project.optional-dependencies] geoparquet = ["pyarrow>=14.0.1"]`。14.0.1 を下限にしたのは CVE-2023-47248 の修正版だから。これは信頼できない Parquet/IPC を読むと任意コードが実行されうる脆弱性で、リモートのカタログ資産はまさに信頼できない入力にあたる。
- コア依存は rasterio のまま。pyarrow が無い環境では Parquet asset に `GEOPARQUET_DEPENDENCY_MISSING` (WARN) を出して skip する。

## 5. Tests / QA (F5/F7)
- `tests/test_geoparquet.py` は 30 テスト。すべて tmp_path に小さな Parquet を書き出して使う。known-good、列欠落、継承された列欠落、case 違い、partition key、余分な列、row_count 不一致とその継承版と不正値、primary geometry の欠落 / 非 geometry / 不一致、geo の欠落 / 不正、CRS の不一致 / 継承 / 4326↔CRS84 / PROJJSON 同値 / null、型の曖昧さ（WARN）、互換な語彙、glob、ローカル欠損、壊れたファイル、ローカル HTTP Range サーバー経由のフッター読み取り、raster 挙動が変わらないこと、をカバーしている。
- `pytest -q`（pyarrow あり）: **95 passed**（既存 65 + 新規 30）。
- クリーン venv（pyarrow なし）: **68 passed, 27 skipped**。
- `python -m build` は OK。`twine check dist/*` は 2 件とも PASSED。
- クリーン venv への wheel インストール:
  - extra なし: blm-sma collection は PASS。`GEOPARQUET_DEPENDENCY_MISSING=2` の WARN が出る。
  - `[geoparquet]` あり（pyarrow 25.0.1）: FAIL。`TABLE_ROW_COUNT_MISMATCH=1` と `PARQUET_PARTITIONED_UNVERIFIED=1` が出る。
- NRP 全体の sweep（364 ドキュメント、Parquet 638 asset を検査）で、Parquet 由来の ERROR は 1 件（blm-sma）だけだった。WARN の内訳は partitioned 491、型 61、継承された列欠落 10、case 違い 1。

## 6. False-positive risk
- 行数: データを再エクスポートした後に metadata を更新し忘れると ERROR になる。ただし利用者が誤った値に基づいて動くのは事実なので、ERROR を維持する。
- asset レベルの `table:columns` を兄弟 asset（PMTiles など）からコピーした場合は、列欠落 ERROR になりうる。NRP では 0 件。
- partitioned データセットは現状まったく検証していない（NRP の table asset の約半数）。偽陰性は多いが、偽陽性は出ない。
- 型の WARN は publisher によってはノイズになりうる（NRP で 61 件）。

## 7. Shared files touched
- `stac_integrity/audit.py`: import 1 行と dispatch hook（+14 行）。Track E (Zarr) も同じ位置に hook を入れる見込みなので、マージ時にコンフリクトしうる。両方を `elif` の並びにまとめれば解消できる。
- `pyproject.toml`: extra を 1 つとコメントを追加。
- `README.md`: Known limitations の GeoParquet の行を 1 行置き換えた。

## 8. Side observation (Track G 向け, 未修正)
NRP の sweep で、既存の raster チェックが `NODATA_MISMATCH` ERROR を出した。宣言は `-3.4e+38`、ヘッダは float32 の `-3.3999999521443642e+38` で、float32 の丸め誤差によるものと思われる。`plant-richness` 系の 4 asset が該当する。`_number_equal` の rel_tol 1e-9 は float32 の nodata には厳しすぎる可能性があるので、Track G で確認してほしい。

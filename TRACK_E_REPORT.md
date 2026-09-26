# Track E — Zarr Semantic Integrity 報告

- ブランチ: `feature/zarr-integrity`（RC a6e23c0 から）
- 実施日: 2026-09-26
- **Verdict: IMPLEMENTED（prototype 品質・optional extra）**
- **Recommendation: MERGE_WITH_CHANGES**（下記「マージ前の条件」参照）

## 1. Evidence（公開情報・read-only で確認）

| # | URL | 内容 | 2026-09-26 時点の live 状態 |
|---|---|---|---|
| E-1 | https://github.com/EOPF-Sample-Service/eopf-stac/issues/82 | `sentinel-2-l2a-zarr3` の STAC `raster:scale/offset` が Zarr 配列の CF `scale_factor/add_offset` と重複。xarray（CF デコード）+ raster 拡張の両方を適用すると反射率が ≈ −0.1 の定数に潰れる | `sentinel-2-l2a-zarr3` の最新 item は **raster:scale/offset を削除済み**（issue は open のまま）。ツールで 9 asset を検査し finding 0 |
| E-2 | https://github.com/EOPF-Explorer/data-pipeline/issues/384 | 本番 `sentinel-2-l2a`（CPM 2.7.0）でも同じ重複。titiler-openeo 0.17（PR #322）が STAC scale をデフォルト適用するようになり顕在化。`data_type`/`nodata` も「エンコード側」を記述している点を指摘 | **再現する**。item `S2A_MSIL2A_20260925T140231_N0513_R010_T26WPC_20260925T200952`（https://stac.core.eopf.eodc.eu/collections/sentinel-2-l2a）で B04_10m 等 14 asset が `raster:scale=0.0001, raster:offset=-0.1`、配列 `.zattrs` も `scale_factor=0.0001, add_offset=-0.1` → `ZARR_DOUBLE_SCALING_RISK` ×14 |
| E-3 | https://github.com/EOPF-Explorer/data-model/issues/195 | S1 RTC の VV/VH data asset が同一 group href、item の意味（単一 datetime）とストア範囲（全時系列）が不一致 | asset は `gamma0-rtc-backscatter-{asc,desc}` に再編済みだが、`border-mask-asc` と同じ orbit group href を共有 → 既存 `DUPLICATE_DATA_HREF` WARN ×2。bands `vv/vh` は GeoZarr multiscale の下位（`ascending/r10m/vv`）にあり group 直下で解決できない → `ZARR_GROUP_UNRESOLVED` WARN |
| E-4 | https://github.com/EOPF-Sample-Service/eopf-stac/issues/70 | S2 の `quality/l2a_quicklook` サブグループ消失 | 本番 item の `TCI_10m` href（`…/quality/l2a_quicklook/r10m/tci`）は `.zarray`/`.zgroup` とも 404 → `ASSET_UNREADABLE`（リモートなので WARN） |
| E-5 | https://github.com/EOPF-Sample-Service/eopf-stac/issues/34 | HTTP オブジェクトストアはディレクトリ一覧不可 | 実際 `array_keys()` は `[]` を返した → band 名解決を「一覧」ではなく「子パスを直接 open」に変更した根拠 |

Live 手動チェック: `https://stac.core.eopf.eodc.eu`（匿名 OK）と `https://api.explorer.eopf.copernicus.eu/stac`（匿名 OK）。ストアは `data.eodc.eu` / `s3.explorer.eopf.copernicus.eu` から HTTPS で匿名取得可。zarr 3.4 と zarr 2.18 の両方で同じ結果（本番 l2a: DOUBLE_SCALING_RISK 14 + UNREADABLE 1、ERROR 0）。1 item あたり 18〜24 秒（asset ごとに逐次メタデータ取得）。

**結論**: #82 型の二重スケーリングは現行の本番コレクションで今も再現し、構造的 STAC バリデータは検出できない（`raster:scale` は schema 上正しい）。Zarr チェックを追加する根拠は十分。

## 2. Invariant 定義（詳細は `docs/zarr-design.md`）

| ID | 比較 | 判定 |
|---|---|---|
| I1 | `proj:shape` vs 配列の最後の 2 次元（2-D、または dim 名が y/x 等） | 不一致 ERROR `SHAPE_MISMATCH`／次元が特定できない WARN `ZARR_SHAPE_UNVERIFIED` |
| I2 | `data_type` vs 格納 dtype | 不一致 ERROR `DATA_TYPE_MISMATCH`。ただし宣言 float・格納 int・CF デコード属性あり → WARN `ZARR_DATA_TYPE_DECODED` |
| I3 | `nodata` vs CF `_FillValue` / `fill_value` | `_FillValue` と不一致 ERROR `NODATA_MISMATCH`／`fill_value` のみと不一致 WARN `ZARR_FILL_VALUE_DIFFERS`／どちらも無し WARN `ZARR_NODATA_NOT_IN_STORE` |
| I4 | STAC scale/offset（band `scale`/`raster:scale`、asset `raster:scale`）vs CF `scale_factor/add_offset` | 両方 active・同値 WARN `ZARR_DOUBLE_SCALING_RISK`／両方 active・異値 WARN `ZARR_SCALE_CONFLICT`／片側のみ・恒等（1/0）は finding なし |
| I5 | CRS | 設計のみ（未実装） |
| I6 | スコープ（asset ↔ store 範囲） | 既存 `DUPLICATE_DATA_HREF` のみ。datacube 拡張ベースは将来 |

asset → Zarr 対応: 配列 href は直接比較。group href は `bands[].name` を子配列に解決（完全一致 → 小文字 → 大文字の順に直接 open、一覧可能なら一意な大小無視一致）。band も比較可能フィールドも宣言しない group（`product` 等）は finding なし。

## 3. Severity の選択理由

- **二重スケーリングを ERROR にしない理由**: 同値の場合、STAC と CF は「同じ格納整数 → 物理量」の同じ写像を記述しており、文書同士は矛盾していない。壊れるのは両方を適用するクライアント（xarray デフォルト + titiler-openeo ≥ 0.17 デフォルト）で、生値を読み STAC のみ適用するクライアント（GDAL raw 等）は正しい。どのクライアントが使うかはゲートから見えない。異値の場合も、raster 拡張には「スケールが生値に対するものかデコード後に対するものか」を表すフィールドがないため、第 2 段スケールとしての意図的宣言を誤りと証明できない。→ どちらも WARN。
- ERROR は「読めた配列のメタデータが宣言と明白に矛盾」する shape / dtype / CF `_FillValue` に限定。
- アクセス失敗は既存 `ASSET_UNREADABLE` と同じ規則（ローカル catalog + ローカル欠落ストアのみ ERROR）。

## 4. 依存関係

- `pyproject.toml`: `zarr = ["zarr>=2.16", "fsspec>=2023.6", "aiohttp"]`。core は rasterio のみのまま。
- zarr 3.x は v2/v3 両形式を読めるが Python ≥ 3.11 必須。Python 3.10 では pip が zarr 2.18 を解決（v2 形式のみ）。使用 API（`zarr.open(mode="r")`、`Array.shape/dtype/fill_value/attrs`、`Group[name]`、`array_keys()`）は両メジャーに存在し、テストも両方で通過。
- fsspec + aiohttp は HTTPS ストア用（公開カタログは HTTPS）。s3fs/gcsfs は含めない（開けなければ WARN）。
- `zarr` は `zarr_checks.zarr_module()` 内で遅延 import。extra 無しでは Zarr asset は skipped に計上し、item ごとに 1 件 WARN `ZARR_SUPPORT_UNAVAILABLE`（インストール方法付き）。

## 5. 実装（変更ファイル）

- 新規 `stac_integrity/zarr_checks.py`（検出・メタデータ読取・純粋比較関数 `compare_array`）
- 新規 `tests/test_zarr.py`（29 テスト）、新規 `docs/zarr-design.md`
- 共有ファイル（最小・局所的）:
  - `stac_integrity/audit.py` +18 行: `audit_item_dict` 内で Zarr 判定を raster 判定の直前に挿入、未導入時 WARN を集約。循環 import 回避のため関数内 import。
  - `pyproject.toml` +4 行（extra 追加）
  - `README.md` 1 行置換（Known limitations の Zarr 行）

## 6. テスト / QA

| 環境 | 結果 |
|---|---|
| 開発 venv（zarr 3.4, py3.12） | 94 passed |
| クリーン venv・wheel のみ（zarr 無し） | 83 passed, 11 skipped（ストアを作るテストのみ `importorskip`） |
| クリーン venv・wheel + `[zarr]` extra（ネット経由 install, zarr 3.4.0） | 94 passed |
| クリーン venv・wheel + zarr 2.18.7 | 89 passed, 5 skipped（v3 形式パラメータのみ skip） |
| `python -m build` / `twine check dist/*` | 成功 / PASSED（wheel に `zarr_checks.py` 含む） |

フィクスチャはテスト内で `tmp_path` に生成（バイナリはコミットしない）: EOPF 本番型（配列ごと asset + raster:scale → DOUBLE_SCALING_RISK）、#82 修正後型（group + bands、scale 無し → finding 0）、group 上の dtype/shape 矛盾（ERROR）、大文字 band 名の解決、S1 RTC 型の orbit group（UNRESOLVED + DUPLICATE_DATA_HREF）、欠落ローカルストア（ERROR）。v2/v3 両形式でパラメータ化。

## 7. False positive リスク

- item レベル `proj:shape` を複数解像度 Zarr asset が継承する場合の `SHAPE_MISMATCH`（GeoTIFF 経路と同じリスク。EOPF は asset に置いている）。
- 大文字小文字違いの band 名解決で別配列を拾う可能性（`B04` と `b04` が併存し、完全一致が無い場合のみ）。
- `ZARR_FILL_VALUE_DIFFERS` は fill_value を nodata として意図しないストアで出る（WARN のみ）。
- `ZARR_DOUBLE_SCALING_RISK` は「両方適用しないクライアント」しか使わない利用者にはノイズになり得る（WARN のみ・値付き）。
- live 3 item で ERROR は 0 件。

## 8. マージ前の条件（MERGE_WITH_CHANGES の中身）

1. Track G の `audit.py` 変更との衝突解消（挿入箇所は asset ループ内の raster 判定直前 1 か所と末尾 1 か所のみ）。
2. `NO_RASTER_ASSETS` のメッセージ「GeoTIFF/COG」を「GeoTIFF/COG/Zarr」に直すかどうか決める（今回は共有ファイル最小化のため未変更）。
3. CHANGELOG 追記（今回は未編集）。
4. 将来課題: GeoZarr `multiscales` 下位レベルへの解決、`cube:variables` を使った shape/dimension 比較、CRS（zarr-conventions `proj:code` / CF `grid_mapping`）、asset メタデータ取得の並列化（1 item 約 20 秒）。

# Track H — Benchmark / Regression Platform

Branch `feature/benchmark-platform`（RC commit a6e23c0 から）。パッケージ本体 `stac_integrity/` は無変更。

## Recommendation: **MERGE**

- 開発専用ツールのみを追加した。wheel と sdist には入らない（MANIFEST.in の `prune benchmark`。build した成果物で確認済み）
- ランタイム依存は増やしていない。使うのは rasterio と numpy（rasterio 依存）と標準ライブラリだけ
- `pytest -q` は完全オフラインのまま（72 passed）。sdist から実行したときはベンチマークのテストが skip になる
- live は明示的な opt-in（`--live` / `--mode live|all`）にしている

## 作ったもの

| path | 内容 |
|---|---|
| `benchmark/__init__.py` | `benchmark` をパッケージ化（`python -m benchmark.run` 用）。docstring のみ |
| `benchmark/fixtures/builders.py` | fixture registry。`@fixture("name")` で builder を登録する。実行時に数ピクセルの GeoTIFF と STAC JSON を決定的に生成する。バイナリは commit しない（NRP のみ既存の `demo_bad/`・`demo_collection_bad/` を流用） |
| `benchmark/manifest.json` | 18 cases（offline 12 / live 6）。各 case に provenance を付けた |
| `benchmark/run.py` | regression runner（offline 実行・live 実行・baseline diff・JSON と Markdown 出力） |
| `benchmark/baseline/offline.json` | RC の挙動から生成した offline baseline |
| `tests/test_benchmark_regressions.py` | 7 tests（offline・0.2 秒未満） |
| `benchmark/README.md` | 使い方・schema・diff の意味・case の追加手順・baseline の更新手順 |

## 登録した case

| id | family | role | 期待値 |
|---|---|---|---|
| e84-s2-legacy-aot | grid-resolution-mismatch | known-bad | SHAPE ERROR, TRANSFORM ERROR, SCALE WARN / exit 1 |
| pc-3dep-2013-pixel-size | pixel-size-mismatch | known-bad | TRANSFORM ERROR, CRS_VERTICAL_UNVERIFIED WARN / 1 |
| pc-3dep-compound-crs | compound-crs | control | CRS_VERTICAL_UNVERIFIED WARN / 0 |
| pc-3dep-2019-shape-off-by-one | shape-off-by-one | known-bad | SHAPE ERROR, CRS_VERTICAL WARN / 1 |
| nasa-ghg-float-epsg | numeric-epsg-normalization | control | なし / 0 |
| dea-nidem-crs-nodata | crs-mismatch | known-bad | CRS_MISMATCH ERROR, NODATA_NOT_IN_HEADER WARN / 1 |
| dea-mstp-shape-off-by-one | shape-off-by-one | known-bad | SHAPE ERROR / 1 |
| dlr-terrabyte-remote-file-href | access-not-semantic | control | ASSET_UNREADABLE WARN / 0（remote URL をローカル JSON から配信して模擬） |
| nrp-rap-pfg-band-count-item / -collection | band-count-mismatch | known-bad | BAND_COUNT_MISMATCH ERROR / 1 |
| e84-s2-c1-red-control | clean-control | control | なし / 0（bbox・scale・offset・nodata をすべて一致させた） |
| cop-dem-pixel-is-point-control | clean-control | control | なし / 0（AREA_OR_POINT=Point。書き込んだ grid を独立に宣言） |
| live-* ×6 | 上記の live 版（E84 legacy AOT・C1 red・cop-dem・NRP（上流修正済みのため今は control）・DEA nidem・DLR terrabyte） | | `expected.signal`（findings は subset で照合） |

3DEP は SAS 署名が必要で、署名用の planetary-computer は依存に入れられない。そのため 3DEP の live case は登録しておらず、offline のみ。

## Manifest schema（v1）

`id`, `family`, `role`（known-bad|control）, `kind`（offline-fixture|live）, `source`{publisher, collection, asset, wave, reference, observed}, `fixture.builder` | `live`{target: targets.json 名 | インライン target}, `expected`{findings[{code, severity}], exit_code | signal, match?}, `notes`。

`load_manifest` は次を検証する: id の重複、kind と role の値、builder と exit_code または signal の有無、control が ERROR を期待していないこと。

## Runner

```bash
python -m benchmark.run                                             # offline（既定）
python -m benchmark.run --baseline benchmark/baseline/offline.json  # 差分
python -m benchmark.run --write-baseline benchmark/baseline/offline.json
python -m benchmark.run --live [--http-stats]                        # opt-in
python -m benchmark.run --mode all --case <id> ...
```

- offline は実 CLI（`cli.main([... "--json"])`）をプロセス内で実行する。`urllib.request.urlopen` を差し替えており、fixture が提供していない URL を取得しようとすると `OfflineViolation` になり case が失敗する
- live は `scripts/public_benchmark.run_target` と `targets.json` をそのまま再利用する（`AWS_NO_SIGN_REQUEST=YES` が既定）。manifest の `expected.signal` が targets.json の値より優先される
- 出力は `benchmark/results/regression/results.json|md`（git-ignored）。finding は code・severity・asset・field のみ保存し、message・href・ローカルパスは残さない（テストで検証済み）
- exit code: 0 は全 case が期待を満たし、かつ regression が 0。1 はそれ以外。2 は manifest エラーまたは usage エラー

## Baseline diff の意味

case id ごとに比較する。

regression として数えるもの:
- new false positives: 期待外の ERROR（control では任意の ERROR）で、baseline になかったもの
- new false negatives: 期待していた ERROR が消えたもの
- changed severities: 同じ code の severity の集合が変わったもの
- exit code または live signal の変化
- PASS から FAIL/ERROR への変化
- case の消失（`--case` 指定時と mode が異なる場合は除外）

timing regression は「audit 時間が baseline の 2.0 倍を超え、かつ 0.25 秒以上増えた」case を報告する。count に入るのは `--fail-on-timing` 指定時だけ。

improvements は resolved FP/FN と FAIL から PASS への変化で、info 扱い。

**結果: offline 12/12 PASS、baseline との差分は Regressions 0、improvements 0。**

## Performance（H5）

- case ごとに `build_s`（fixture 生成）と `audit_s`（CLI 実行）を記録している。offline suite 全体で約 0.03 秒
- `--http-stats`（live のみ）は GDAL の `CPL_DEBUG` ログを rasterio logger で拾い、次を数える: `GetFileSize` probe（206 応答の数も含む）、`Downloading a-b` の ranged GET とその byte 範囲、urllib による JSON fetch
- 実測例: E84 legacy AOT は JSON 1、probe 1、range GET 1（16 KiB）。cop-dem（/vsis3/）は probe 10 がすべて 206 で、range GET は 0
- 限界: 206 の probe の body サイズはログに出ないため、bytes は下限値にとどまる。GDAL のキャッシュ、GDAL のバージョンによるメッセージ形式の違いもあり、数値は目安として扱う。正確な計測（GDAL の `/vsicurl` 統計 API や proxy 経由の計測）は今後の課題

## Tests / CI（H6）

**CI の変更は加えていない。** 既存の `pytest -q` に offline benchmark の検証を含めた。理由は次の 3 点。

1. 所要時間が 0.2 秒未満
2. 完全オフライン
3. workflow ファイル（共有ファイル）を触らずに済み、matrix（3.10/3.12/3.13）でもそのまま動く

追加したテスト:
- manifest の整合性
- 全 offline case が PASS すること
- baseline との差分で regression が 0 であること
- 結果にローカルパスが含まれないこと
- offline で network がブロックされること
- diff の分類（FP・FN・severity・timing・逆方向の改善）
- **検出器を故意に壊す end-to-end 検証**: `_check_crs` を無効化すると new FN と newly failing が検出されること

Markdown レポートを CI artifact にしたくなった場合は、`python -m benchmark.run --baseline ...` を別 step に追加すれば済む。

## QA（H8）

| 項目 | 結果 |
|---|---|
| `pytest -q` | **72 passed**（既存 65 + 新規 7） |
| sdist を展開して pytest | 65 passed, 1 skipped（benchmark/ が無いため module を skip） |
| offline benchmark と baseline の差分 | 12/12 PASS, **Regressions: 0** |
| live（opt-in 動作確認、結果は commit していない） | 6/6 PASS |
| `python -m build` / `twine check dist/*` | 両方 PASSED |
| wheel の中身 | `stac_integrity/*.py` と dist-info のみ |
| sdist の中身 | `benchmark/` は含まれない（grep で 0 件） |

## Limitations

- fixture は縮小版の合成データで、実データの byte 構造（COG の overview やタイル構成など）までは再現しない。検証しているのはヘッダの意味論だけ
- baseline の timing は生成したマシン（macOS arm64、GDAL 3.12.4）に依存する。CI では timing を regression として数えない（既定）
- live の結果は上流の変化（修正や WAF）で変わる。NRP は既に上流で修正済みのため control として登録した
- 3DEP の live は署名が必要なため未登録

## 触った共有ファイル

- `benchmark/README.md`（追記。既存セクションは保持）
- `tests/test_benchmark_regressions.py`（新規）

`README.md`、`pyproject.toml`、`MANIFEST.in`、CI workflow、`stac_integrity/` は無変更。conflict のリスクは低い。他の track が `benchmark/README.md` を編集していれば、その部分だけ軽微な conflict になりうる。

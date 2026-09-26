# Track G — Additional STAC Invariants

Branch `feature/additional-invariants`（RC commit a6e23c0 から）。原則は「ERROR を増やさない」。偽陽性を防ぐことを新しい ERROR より優先した。**このトラックで追加した ERROR はない。** 新しいコードはすべて WARN で、うち 1 つは opt-in。

## 候補ごとの判定

| # | 候補 | 判定 | 実装コード / 重大度 | 根拠 | 仕様参照 |
|---|---|---|---|---|---|
| G1 | asset の `eo:bands` 数とラスタのバンド数 | **WARN_ONLY（実装）** | `EO_BAND_COUNT_MISMATCH` / WARN | eo 拡張 v1.0/v1.1 では、asset 上の `eo:bands` はその asset に含まれるバンドを列挙する。一方 Item properties 上の `eo:bands` は全 asset のバンドをまとめて記述するので、asset ごとに比較してはいけない（→ asset レベルだけを見る）。`eo:bands` は「スペクトル」バンドの記述で、alpha などの非スペクトルバンドを列挙するかは生産者によって異なる。STAC 1.1 で deprecated にもなっている。以上から ERROR にするだけの証拠はない。`bands`/`raster:bands` がある場合は既存の `BAND_COUNT_MISMATCH`（ERROR）が優先されるので評価しない。末尾の alpha バンド（RGBA visual COG）は無視する。BDC のように eo:bands しか宣言しないカタログで、比較できるものが初めてできる | eo v1.1.0 README「Item fields / Asset fields」、STAC 1.1 common metadata `bands`（eo:bands deprecated） |
| G2 | `file:size` と実サイズ | **WARN_ONLY・opt-in（実装）** | `FILE_SIZE_MISMATCH` / WARN（`--check-file-size`、`check_file_size=True`） | ローカルは `os.stat` で取れて安い。HTTP(S) は `Range: bytes=0-0` の GET を 1 回送り、`Content-Range` の total を使う（pre-signed GET URL は HEAD を拒否することが多いので HEAD は使わない。リダイレクトは urllib が追従する。`Accept-Encoding: identity` を付け、`Content-Encoding` が identity 以外なら判定しない）。Range を無視して 200 が返った場合は、未エンコードの `Content-Length` だけを使い、本文は読まない。s3:// と gs:// は stdlib だけでは取得できないので黙ってスキップする。total が `*` の場合、ヘッダが無い場合、403/タイムアウトもスキップし、追加の WARN は出さない。バイトサイズは意味的なラスタ宣言ではなく、再圧縮・再タイル化は意味上無害なので ERROR にはしない。asset ごとにネットワーク往復が 1 回増えるため既定は OFF | file 拡張 v2.1.0 `file:size` |
| G3 | `file:checksum`（multihash） | **HOLD** | — | 全バイトのダウンロードが必要で、COG の部分読みという前提と両立しない（大容量ではコストが大きい）。検証対象はバイト完全性で、ヘッダと宣言の意味的整合性というこのツールの中核概念とは別の問題。multihash のアルゴリズムは多様（sha2-256, blake3 など）で、stdlib で扱えないものもある。将来やるなら別サブコマンド（例 `stac-integrity verify-checksums`）として分けるべき | file 拡張 v2.1.0 `file:checksum`、multiformats multihash |
| G4 | 同一 href で band 宣言が異なる asset | **WARN_ONLY（実装）** | `DUPLICATE_HREF_DIFFERENT_BANDS` / WARN（そのグループでは `DUPLICATE_DATA_HREF` の代わりに出す） | 2 つの asset が同じファイルを指しながら異なるバンド（`name`、`eo:common_name`/`common_name`、`sar:polarizations`）を宣言している場合、両方が正しいことはあり得ない（例: VV と VH が 1 ファイルに潰れた公開ミス）。ただし意図的な alias の可能性もあり、どちらが誤りかも判定できないので WARN に留める。比較は「両 asset が同じ種類の識別子を宣言している場合」に限る（片方が `name`、もう片方が `common_name` だけなら比較しない）。1 つの問題に 1 finding とし、WARN の件数は増やさない | STAC best practices（asset per band）、sar 拡張 `sar:polarizations`、eo `common_name` |
| G5 | 値域・統計（`statistics` と GDAL `STATISTICS_*` タグ） | **HOLD** | — | ヘッダの統計タグは任意で、多くは approx（オーバービューから計算）。nodata を除外するかどうかや計算方法が生産者によって違うため、許容誤差を決められない。ヘッダにタグが無ければ全画素スキャンが必要で、コスト原則に反する。偽陽性のリスクが大きく、得られる価値は小さい | raster 拡張 `statistics`、STAC 1.1 `bands[].statistics`、GDAL `STATISTICS_MINIMUM/MAXIMUM/MEAN/STDDEV` |

KILL の候補はない。G3 と G5 は、別モードや別サブコマンドとして将来再評価する余地があるので HOLD とした。

## 実装の概要

- `stac_integrity/audit.py`: 独立した関数を追加した（`_check_eo_bands`、`_declared_size`、`_remote_size`、`_check_file_size`、`_band_identity`、`_different_band_semantics`、`_duplicate_href_finding`）。既存コードへの変更は次だけ。
  - `import os`
  - `audit_item_dict` / `audit_item` に `check_file_size: bool = False` キーワード引数
  - 重複 href の finding 生成を `_duplicate_href_finding` の呼び出しに置き換え
  - `checked += 1` の直後に opt-in の file-size 呼び出し 1 行
  - `_check_bands` の直後に `_check_eo_bands` 1 行
- `stac_integrity/collection.py`: `check_file_size` を受け取り、`audit_item_dict` の 2 か所に渡す（+3 行）。
- `stac_integrity/cli.py`: `_common` に `--check-file-size` を追加し、item/collection の両方に渡す（+7 行）。
- `README.md`: Checks 表に 3 行を追加し、Limitations の 1 文を更新。`CHANGELOG.md`: 先頭に `## Unreleased` を追加。

## テスト（`tests/test_additional_invariants.py`、24 件）

- G1: known-good（eo:bands 2 = 2 バンド）、known-bad（3 ≠ 2 で WARN、`ok`）。偽陽性ガード: item-level の eo:bands（13 本）は評価しない、raster:bands がある場合は eo:bands を無視する、RGBA の末尾 alpha を無視する。
- G2: ローカルの一致・不一致（WARN、`ok`）、既定 OFF、不正な宣言（文字列、負数、float、bool、null）はスキップ、CLI の exit code（flag あり 0 / `--strict` で 1 / flag なしの `--strict` で 0）。リモートは urlopen をモックして検証: Content-Range 一致、不一致で WARN、曖昧なケース 4 種（`/*`、gzip、ヘッダなし、403）はスキップ。
- G4: 同一 band は従来どおり `DUPLICATE_DATA_HREF`、polarization が異なる場合と band name が異なる場合は `DUPLICATE_HREF_DIFFERENT_BANDS`（WARN）。偽陽性ガード: 識別子の種類が異なる場合は昇格しない。

QA: `pytest -q` で **89 passed**（既存 65 + 新規 24）。`python -m build` OK。`twine check dist/*` PASSED。クリーンな venv に wheel をインストールして smoke（`--version`、`--check-file-size` の help、新関数の import）も OK。

## 偽陽性リスク

- `EO_BAND_COUNT_MISMATCH`: asset に Item 全体の eo:bands リストをそのままコピーしているカタログでは WARN が出る（実際にメタデータ品質の問題なので WARN が妥当）。exit code には影響しない（`--strict` の場合を除く）。
- `DUPLICATE_HREF_DIFFERENT_BANDS`: 既存の WARN を置き換えるだけで、件数は増えない。`DUPLICATE_DATA_HREF` でフィルタしている利用者はコードの変化に注意が必要（CHANGELOG に記載済み）。
- `FILE_SIZE_MISMATCH`: opt-in。サイズが曖昧に取れた場合は必ずスキップする。

## 共有ファイルへの変更とコンフリクトリスク

- `audit.py`: 関数シグネチャへの kwarg 追加と、ループ内の 2 行。Track B（resolver 引数）や Track D（open 周りのキャッシュ）と隣接行で**機械的なコンフリクトが起きる可能性がある**が、解決は容易（両方の kwarg と行を残せばよい）。Track E/F の dispatch hook とは位置が異なる見込み。
- `collection.py` / `cli.py`: Track B が同じ呼び出し箇所に resolver を通す場合、同じく些細なコンフリクトになる。
- README の Checks 表と CHANGELOG の先頭: 他トラックも追記する場合は行単位でマージが必要。

## 推奨

**MERGE**（コンフリクトがあれば機械的に解決する。ERROR の追加はなく、新しいコードはすべて WARN。file-size は opt-in）。

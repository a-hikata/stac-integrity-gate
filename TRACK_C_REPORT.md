# Track C — CI / DevEx / machine-readable output

Branch `feature/ci-devex`, based on RC commit `a6e23c0`.

## 推奨: **MERGE**

変更は加算的です。既存の 65 テストは無修正で pass します。runtime 依存は増えていません（stdlib の `json` と `xml.etree` のみ）。

## 何をしたか・なぜ

| ID | 内容 |
|---|---|
| C1 | `--format sarif` で SARIF 2.1.0 を出力します。rule は finding code ごとに 1 つです（既知 19 コードのカタログを常に出力し、未知コードも rule を自動生成します）。level は ERROR→`error`、WARN→`warning` です。artifactLocation はローカルなら repo 相対の POSIX パス、URL ならそのままです。ローカル JSON では asset キーの行を `region.startLine` に best-effort で入れます。`logicalLocations` は `<item>/assets/<asset>/<field>` です。`partialFingerprints` は item/asset/field/code の SHA-256 で、code scanning の重複排除を安定させます。properties には declared/actual/item_id/asset/field を入れます。 |
| C2 | `--format junit` で JUnit XML を出力します。構造は `<testsuites>` → testsuite 1 つ（item または collection）→ **Item ごとに testcase 1 つ**です（Collection-level assets は `collection:<id>` の testcase になります）。ERROR→`<failure>`。WARN のみなら pass し、内容は `<system-out>` に出します。`--strict` 時は WARN も `<failure>` にし、exit code と一致させます。検査できた asset が 0（unreadable / NO_RASTER_ASSETS）なら `<skipped>` です。per-asset にしなかったのは、結果モデルが合格 asset を列挙しないからです（audit.py を変えると他 track と衝突します）。 |
| C3 | JSON に `schema_version: "1.0"`、`status: pass/fail`（exit code と同じ判定で `--strict` を反映）、`strict`、`summary.by_severity/by_code` を**追加のみ**で加えました。既存キーの削除・改名はありません。`ok` は従来どおり「ERROR なし」の意味です。JSON Schema は `docs/report.schema.json` として同梱しました（sdist に含まれます）。 |
| C4 | exit code 0/1/2 は全フォーマットで同一です（parametrize テストで確認）。`--output` に書けない場合は exit 2 です。 |
| C5 | composite action は**作りません**。CLI は 1 コマンドなので、wrapper を作っても公開 API を増やすだけです。`examples/github-actions.yml` を SARIF upload 付きの例に更新しました（`if: always() && hashFiles(...)`、`security-events: write`）。GitLab の junit 例は docs に載せています。 |
| C6 | `--format text\|json\|sarif\|junit` と `--output PATH` を追加しました。`--output` 指定時はファイルにレポートを書き、stdout には text サマリを出します（CI ログを人が読めるままにするため）。`--json` は `--format json` の alias として残し、後に書いたものが勝ちます。`--summary-only` は json に反映されます。 |
| C7 | `tests/test_report_formats.py` に 24 テストを追加しました（known-good/bad × 各フォーマット、strict、mixed collection、`--output`、書き込み失敗、exit code の parametrize、JSON Schema の required キー照合）。 |

## CLI / API の変更
- CLI: `--format`、`--output` を追加しました（`item` / `collection` 共通の `_common()`）。
- API: 新モジュール `stac_integrity/report.py` を追加しました（`render(result, fmt, strict=, include_items=)`、`report_dict`、`sarif_dict`、`junit_element`、`gate_failed`、`summary`、`RULES`、`SCHEMA_VERSION`）。`audit.py` / `collection.py` / `__init__.py` は無変更です。
- text 出力は `report.render_text()` に移しました。出力文字列は同一です。

## 後方互換の根拠
- `tests/test_cli.py`（テキスト/JSON 契約と exit code）は**無修正**で pass します。
- JSON は旧キーを全保持し、テストで旧キー集合と finding キー集合を明示的に確認しています。
- default の format は text で、出力文字列は旧実装と同一です（`test_default_text_output_unchanged`）。

## QA
- `pytest -q`: **89 passed**（既存 65 + 新規 24）。
- `python -m build`: sdist と wheel を作成しました。`twine check dist/*`: PASSED。
- クリーンな venv に wheel を入れ、`stac-integrity item demo_bad/item.json --format sarif` を実行しました。SARIF 2.1.0 が出て ruleId=BAND_COUNT_MISMATCH、exit 1 です。junit も exit 1 です。
- 公式 SARIF JSON schema はオフラインでは取得しません。代わりに構造アサーション（version、$schema、driver.name/version/rules、ruleId/ruleIndex 整合、level、message.text、locations）で検証しています。

## 依存
- 追加なし（runtime / test とも）。

## 触った共有ファイル（衝突リスク）
- `stac_integrity/cli.py`: **中〜低**。`_common()` の `--json` 行を置き換えて `--format` / `--output` を追加しました。`main()` の出力ブロックは削除し、try の後に render/emit を 1 ブロック置いています。`audit_item(...)` / `audit_collection(...)` の呼び出し行は触っていません。Track B（`--resolver`）と Track D（`--sample` など）が `_common()` や audit 呼び出しに引数を足すと、隣接行の単純な衝突が起きる可能性がありますが、解消は機械的です。旧コードの `args.as_json` を参照する変更が他 track にあれば `args.format == "json"` への置き換えが必要です。
- `README.md`、`CHANGELOG.md`、`MANIFEST.in`、`examples/github-actions.yml`: 追記のみで、衝突リスクは低いです。
- 新規: `stac_integrity/report.py`、`tests/test_report_formats.py`、`docs/output-formats.md`、`docs/report.schema.json`。
- 注意: 他 track が新しい finding code を足した場合は、`report.RULES` にも説明を追加するのが望ましいです（未追加でも汎用 rule が生成されるので壊れません）。

## 未対応・判断メモ
- exit 2（読み込み失敗）ではレポートを出しません（stderr のみ）。upload step は `hashFiles` でガードする例にしています。
- JUnit の `time` は 0 です（計測していないため）。

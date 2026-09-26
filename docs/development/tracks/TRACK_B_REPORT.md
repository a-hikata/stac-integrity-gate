# Track B — Authentication / Signed Assets レポート

ブランチ: `feature/authenticated-assets`（base: RC `a6e23c0`）
設計書: [docs/auth-design.md](docs/auth-design.md)

## 推奨: **MERGE**

コア（semantic 比較）は変更なし、既定動作は identity で完全互換、新依存はオプション extra のみ。
既存 65 テストはそのまま通過し、27 件の新テストを含め計 **92 passed**（完全オフライン）。

## アーキテクチャ

(a) コアに認証を内蔵する案と (b) resolver コールバックで transport/auth を分離する案を比較し、**(b) を採用**。

```
declared href ──_resolve_href──▶ href ──resolver(href, context)──▶ open_href ──rasterio.open──▶ header
                                   │                                                          │
STAC 宣言 (proj:*, bands) ─────────┴──────────────── semantic 比較 ◀────────────────────────────┘
```

- resolver は「開く文字列」だけを変える。比較対象は常に STAC 宣言。
- raster 判定（拡張子/type）と `DUPLICATE_DATA_HREF` は宣言 href で行うため、署名の有無で検査対象が変わらない。
- resolver は実際に開く asset（raster + data-role フィルタ後）にだけ、1 asset 1 回呼ばれる。
- resolved href は結果オブジェクトに一切保存しない。

## 公開 API（追加のみ）

- `audit_item(..., resolver=None)`, `audit_item_dict(..., resolver=None)`, `audit_collection(..., resolver=None)`
- `stac_integrity.HrefContext(asset_key, item_id, source, asset)`（frozen、`asset` は read-only MappingProxy）
- `stac_integrity.HrefResolver`（Protocol: `(href: str, context: HrefContext) -> str`）
- `stac_integrity.resolvers`: `identity`, `planetary_computer_resolver()`, `alternate_resolver(name)`, `load_resolver(spec)`, `ResolverUnavailableError`
- `stac_integrity.redaction`: `redact(text)`, `redact_value(value)`, `REDACTED`
- CLI: `--resolver SPEC`（`item` / `collection` 共通）。SPEC = `planetary-computer` | `alternate-<name>` | `identity` | `module:function`
- 新 finding code: `ASSET_RESOLVE_FAILED`

### 失敗時の severity

| 状況 | code | severity |
|---|---|---|
| resolver が例外 / 非 str / 空文字を返す | `ASSET_RESOLVE_FAILED` | `unreadable_severity`（既定 WARN、`--fail-unreadable` 時のみ ERROR） |
| 開けない（403/409/404/timeout） | `ASSET_UNREADABLE` | 従来通り（remote は WARN。local catalog + local 宣言 + local 解決先で開けない時のみ ERROR） |

`ASSET_UNREADABLE` を再利用せず新 code にした理由: 「署名・認証設定が壊れている（ユーザー側で直す）」と「サーバーが拒否/ファイルが無い」は対処が異なり、CI 集計で区別したい。いずれも semantic ERROR にはならない（ERROR になるのは明示的 opt-in の `--fail-unreadable` のみ）。
resolver が local href を remote に変えた場合も、開けなければ WARN（ローカル欠落の証明にならないため）。

## オプション依存

- `pyproject.toml`: `planetary-computer = ["planetary-computer>=1"]` extra を追加。コア依存は rasterio のみのまま。
- `planetary_computer_resolver()` は生成時に lazy import。未インストールなら `ResolverUnavailableError`（`pip install "stac-integrity-gate[planetary-computer]"` を案内）を**生成時に**送出 → CLI は exit 2。asset ごとの WARN 大量発生にはしない。

## セキュリティ

GDAL のエラーメッセージは URL 全体を含むため、対策なしでは SAS `sig=` 等が `Finding.message` → JSON → CI ログへ漏れる。

1. `redact()`: 機微 query param の値を `REDACTED` に置換。対象: Azure SAS（`sig se st sp sv sr spr srt ss sip skoid sktid skt ske sks skv saoid suoid scid sdd ses`）、`X-Amz-*`、`X-Goog-*`、CloudFront `Signature/Policy/Key-Pair-Id`、`token access_token id_token refresh_token key api_key apikey password secret client_secret credential session_token jwt code auth authorization` など（大小文字無視）。URL userinfo (`https://user:pass@`)、`Authorization: ...`、`Bearer xxx` も置換。benign な URL は無変更（テスト済み）。
2. 適用箇所: `ASSET_UNREADABLE` / `ASSET_RESOLVE_FAILED` の message、`declared`（元の STAC href。catalog 自体が署名済み href を公開している場合もあるため redact）、`DUPLICATE_DATA_HREF` の `actual`、`Finding.to_dict()`（多重防御）、`AuditResult.to_dict()` / `CollectionAuditResult.to_dict()` の `source`、CLI の stderr 運用エラー行。
3. `declared` は常に元の未署名 STAC href。resolved href は保持しない。
4. 環境変数・HTTP ヘッダーは読まない・ログしない・シリアライズしない。認証情報は GDAL の設定にのみ存在。
5. `--resolver module:function` は任意モジュールを import（= コード実行）する。信頼レベルは「自分で選んだ Python スクリプトを実行する」のと同等。spec は CLI 引数からのみ受け取り、STAC 文書からは決して読まない。

残余リスク: ユーザー resolver 自身のログ出力、`CPL_DEBUG=ON` / `CPL_CURL_VERBOSE=YES` による GDAL の stderr 出力はツールの管理外（設計書に明記）。1 文字の SAS 値（`sr=c` 等）は置換されるが、同じ文字が URL の他の部分に偶然現れるのは漏洩ではない。

## 対応プロバイダ

| プロバイダ | 方法 | 検証 |
|---|---|---|
| Microsoft Planetary Computer | `--resolver planetary-computer`（extra） | テストは stub。**ライブ QA（pytest 外）**: sentinel-2-l2a の Item で、未署名 → `ASSET_UNREADABLE` WARN（HTTP 409）×2、署名後 → 2 asset を実検査し findings 0、JSON 出力に `sig=` 0 件。実 SAS URL を `redact()` に通して全 12 パラメータが `REDACTED` になることも確認 |
| 事前署名 URL 全般（S3 presign 等） | ユーザー resolver が返す / catalog に直接記載 | オフラインテスト |
| STAC `alternate` を持つ catalog（CDSE `alternate.https` 等） | `--resolver alternate-https` + GDAL env（`GDAL_HTTP_BEARER` 等） | オフラインテスト（ライブ未検証: トークン必要） |

## 非対応（意図的に組み込みフローなし）

CDSE（S3 キー / OIDC）、NASA VEDA / GHG Center（Earthdata / STS）、USGS LandsatLook（EROS ログイン）、Earth Search NAIP（requester-pays）。
いずれも GDAL の env（`AWS_REQUEST_PAYER=requester`, `AWS_S3_ENDPOINT`, `AWS_VIRTUAL_HOSTING=FALSE`, `AWS_ACCESS_KEY_ID/SECRET/SESSION_TOKEN`, `GDAL_HTTP_BEARER`, `GDAL_HTTP_HEADERS`）かユーザー resolver で対応。手順は `docs/auth-design.md`。資格情報フローを内蔵しない理由: 秘密情報の保持責任・ネットワーク依存テスト・プロバイダ仕様変更への追従コスト。

## テスト（`tests/test_resolvers.py`, 27 件, オフライン）

- identity 既定: known-good / known-bad とも resolver なしと完全一致
- resolver の出力が開かれる・context の内容・asset が read-only・skip 対象 asset では呼ばれない
- known-bad（band 数不一致）は resolver 経由でも `BAND_COUNT_MISMATCH` ERROR
- resolver 例外 / None / "" / 非 str → `ASSET_RESOLVE_FAILED` WARN、`unreadable_severity="ERROR"` 時 ERROR
- 403 を返す `rasterio.open`（署名 URL をメッセージに含む）→ WARN、Finding / JSON / text CLI / stderr のいずれにも秘密値なし
- 署名後 open 失敗でも `declared` は未署名の元 href
- local catalog + remote へ解決して失敗 → WARN
- `audit_collection` へ resolver が伝播
- redaction 単体（Azure SAS, AWS SigV4, userinfo, Bearer, access_token, benign URL 無変更）
- PC resolver: `sys.modules` stub で署名、未インストールで `ResolverUnavailableError`、E2E
- alternate resolver: 相対 alternate の解決（local / http source）、fallback、E2E
- CLI: `module:function`、`planetary-computer`（stub）、不正 spec / 未インストール / import 失敗 → exit 2、stderr redaction

QA: `pytest -q` → 92 passed。`python -m build` OK、`twine check dist/*` PASSED。
クリーン venv（/private/tmp）に wheel をインストール: コアのみで demo_bad → exit 1、`--resolver planetary-computer` → 案内付きで exit 2。`[planetary-computer]` extra 付き（planetary-computer 1.0.0 がネットワーク経由でインストール可能）→ resolver 生成・CLI 実行とも OK。

## 共有ファイルの変更（マージ競合リスク）

| ファイル | 変更 | 競合リスク |
|---|---|---|
| `stac_integrity/audit.py` | import 2 行、`Finding.to_dict` / `AuditResult.to_dict` の redaction、`audit_item_dict` / `audit_item` に `resolver` kwarg、asset open ループ内（resolver 呼び出し + severity 計算の位置移動 + redact） | **中**: Track G（invariants 追加）が同ファイル。`_check_*` 関数群には触れていないため、G が新しい `_check_*` を `with rasterio.open(...)` ブロックに追加する形なら軽微。open ループ自体を書き換える場合は手動解決が必要 |
| `stac_integrity/collection.py` | import 2 行、`to_dict` の `source` redact、`audit_collection` に `resolver` kwarg と 2 箇所の転送 | **中**: Track D（sampling / concurrency）が `audit_collection` の引数・`run()` を触る可能性大。追加は 1 行ずつなので解決は容易 |
| `stac_integrity/cli.py` | `_common()` に `--resolver`、`load_resolver` 1 行、2 呼び出しに `resolver=`、stderr を `redact()` | **中**: Track C（出力形式）。text 出力部分には触れていない。C が新しい出力経路（SARIF 等）を追加する場合は `Finding.to_dict()` 経由なら自動で redaction される。`finding.message` を直接使う経路も Finding 生成時に redact 済み |
| `stac_integrity/__init__.py` | `HrefContext`, `HrefResolver` を export | 低 |
| `pyproject.toml` | extra 1 行 | 低 |
| `README.md` | 「Supported formats」直下の 1 段落と Known limitations の 1 行を置換 | 低〜中 |

CHANGELOG は競合回避のため未更新（マージ時に 1 行追記を推奨）。

## リスク / 未解決

- CDSE / NASA のライブ検証は未実施（資格情報が必要）。
- redaction はパターンベース。未知の独自パラメータ名（例 `?x_my_sig=`）は検出しない → 独自 resolver 利用者には「resolver 内でログしない」ことを README / 設計書で案内済み。
- `code` / `key` を機微扱いにしたため、それらを正当な非秘密パラメータとして使う URL は表示上 `REDACTED` になる（安全側の選択）。

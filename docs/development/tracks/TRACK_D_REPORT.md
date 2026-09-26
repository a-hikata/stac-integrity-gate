# Track D — Collection Scale Audit レポート

- ブランチ: `feature/collection-scale`(RC commit `a6e23c0` から)
- 推奨: **MERGE_WITH_CHANGES**(下記「マージ時の注意」を参照。コード上のブロッカーはなし)
- テスト: `pytest -q` → **96 passed**(既存 65 + 新規 31、すべてオフライン)
- 新しいランタイム依存: **なし**(stdlib のみ。`rasterio` は既存)

## 1. 設計

新規モジュール `stac_integrity/netpolicy.py`(stdlib のみ)に運用系の部品を分離し、`collection.py` から組み立てる。

| 部品 | 役割 |
|---|---|
| `RateLimiter` | スレッドセーフな token bucket(burst=1)。ロック内でトークンを「予約」(残高は負になりうる)し、ロック外で sleep するので FIFO で 1/rate 間隔に並ぶ。`clock` / `sleep` 注入可能(テストはフェイク時計)。STAC JSON リクエストと実際の raster open が同じバケットを共有 |
| `JsonClient` | STAC JSON 取得。リトライは transient のみ: timeout / connection reset / HTTP 429(`Retry-After` 秒・HTTP-date を尊重、上限 60s 超なら諦める)/ HTTP 5xx(500,502,503,504)。指数バックオフ 1s→2s…(上限 30s)、既定 `retries=2`(計 3 試行)。4xx は即失敗。**HTTP 202 + 空 body は `WafChallengeError` として 1 回で打ち切り**(リトライしない)。失敗はすべて `OperationalFetchError` |
| `HeaderCache` | 解決済み href → ヘッダースナップショットのスレッドセーフな single-flight キャッシュ。同一 href は 1 run で 1 回だけ open。open 失敗もキャッシュし、参照する各アセットで `ASSET_UNREADABLE` を再報告。`BoundedSemaphore` で同時 open 数を制限 |
| `RasterHeader`(audit.py) | チェックが使う属性(crs, width, height, transform, bounds, count, dtypes, nodatavals, scales, offsets)の immutable スナップショット。dataset オブジェクトはキャッシュしない |

`audit_item_dict` に `header_reader=` 引数(既定 `read_raster_header`)を追加し、チェックは常にスナップショットに対して実行する。例外処理の範囲(open + チェック中の例外 → `ASSET_UNREADABLE`)は従来と同一。

### サンプリング (D1/D7)
- `first`(既定・従来動作): 公開者の並び順で先頭 `--limit` 件。
- `random`: **候補プール = 公開者の並び順で先頭 `--scan-limit` 件(既定 1000)**。その上で Algorithm R(reservoir sampling)により `--limit` 件を一様抽出。`random.Random(seed)` を使用し、`--seed` 省略時は `SystemRandom` で生成して必ず報告。
- **バイアス**: プールを使い切った場合(`sampling.pool_exhausted: true`)のみコレクション全体に対して一様。そうでなければ「サーバーが先に返す N 件」(多くは最新 Item)に偏る。text 出力でも `whole collection` / `first-N pool only` を明示。
- **再現性の限界**: 同じ seed で同じサンプルになるのは、公開者の並び順とプールが変わらない間だけ。ライブ検証で実際に確認済み(下記 D9: earth-search は新規 ingest で先頭 1000 件がずれ、同 seed でも 5 件中 3 件が変化)。
- 静的カタログでは `rel=item` リンク(href のみ)でプールを作り、**選ばれた Item の JSON だけ**をワーカー内で取得する(従来は全 Item を逐次取得)。
- STAC API では random 時に既定で `?limit=100`(`--page-size`)を最初の items リクエストに付与。`next` リンクはサーバーのものをそのまま辿る。サーバーが上限を持っていてもページングは正しい。
- **Stratified は実装しない**: コレクション横断で汎用かつ正直な層定義がない(時刻・グリッドタイル・プラットフォームはコレクション依存)、STAC API のページング順序も保証されない。特定の層化は `--asset` / 利用者側のフィルタ URL で行うのが誠実。

### 並列度 (D2)
- `--workers` 既定を **8 → 4**(API `audit_collection(workers=...)` 既定も 4)。DEA explorer が並列クロールで AWS WAF challenge(HTTP 202 空 body)に切り替わった実績があり、公開 API に対して保守的にするため。
- 同時 raster open 数 `max_concurrent_opens`(API のみ、既定 = workers)。ワーカー内でアセットは逐次 open なので実質 ≤ workers だが、コレクションレベルアセット等も含めて明示的に制限。

### レート制限 (D3)
- `--max-rps N` / `--delay S`(排他)。STAC JSON リクエスト(リトライ含む各試行)と実際の raster open(キャッシュヒットは消費しない)で 1 トークン。1 回の open で GDAL は複数の range read を発行しうる点は README に明記。既定は無制限(従来動作)。

### リトライ (D4)
- JSON: 上記の通り。セマンティック結果は一切リトライしない。
- raster open: remote href の `rasterio.Env` に `GDAL_HTTP_MAX_RETRY=2`, `GDAL_HTTP_RETRY_DELAY=1` を追加(`item` コマンドにも適用される。挙動は保守的な方向のみ)。

### 失敗の扱い
- コレクション文書自体の取得失敗: 従来通り例外 → exit 2。
- items ページ / Item 文書の取得失敗(WAF 202 含む): 監査を中断せず、取得できた分だけ監査し `operational_errors` に記録、`complete=false`。CLI は stderr に `incomplete audit: ...` を出し、**ERROR が無ければ exit 2**(不完全な監査を PASS にしない)。ERROR があれば exit 1。ネットワーク失敗をセマンティック ERROR には絶対にしない。
- 挙動変更: 以前は items ページ取得失敗で `audit_collection` が例外を投げていたが、現在は部分結果を返す(API 利用者は `result.complete` を確認する必要あり)。

### サマリー (D6)
- text: 既存 1 行目・`findings:` 行はそのまま。追加で
  - `sampling: random seed=1 pool=1000 (first-N pool only, scan-limit=1000) | limit: 5 | selected: 5`
  - `summary: items | assets-opened | cache-hits | errors | warnings | unreadable | http req/retries | complete`
- JSON: 既存キーはすべて不変。`sampling`(mode, seed, limit, scan_limit, page_size, strategy, pool_size, pool_exhausted, pages_fetched, selected)と `summary`(items_checked, failing_items, assets_checked/skipped/opened, asset_cache_hits, unreadable, errors, warnings, finding_codes, http_requests, http_retries, rate_limit_wait_s, complete, operational_errors)を追加。`schema_version` は Track C 担当なので未追加。

## 2. フラグ / API

CLI `collection`(新規 argument group "sampling and politeness"):
`--sample {first,random}` `--seed N` `--scan-limit N`(1000) `--page-size N` `--max-rps F` | `--delay S` `--retries N`(2)。`--workers` 既定 4。

Python API: `audit_collection(..., workers=4, sample="first", seed=None, scan_limit=1000, page_size=None, max_rps=None, delay=None, retries=2, max_concurrent_opens=None, client=None, header_reader=None)`。
`CollectionAuditResult` に `sampling`, `stats`, `operational_errors`, `complete`, `unreadable_count`, `summary()` を追加(すべて既定値付きで後方互換)。
`audit.RasterHeader`, `audit.read_raster_header`, `audit.REMOTE_GDAL_ENV`, `audit_item_dict(header_reader=...)`。
`netpolicy`: `RateLimiter`, `JsonClient`, `HeaderCache`, `OperationalFetchError`, `WafChallengeError`, `RawResponse`, `urllib_opener`。
`collection._load_collection_items` は互換ヘルパーとして残置、`_fetch_json` も未使用だが残置(他トラックの参照に備え)。

## 3. テスト (D8) — `tests/test_collection_scale.py`(31 件)
- first 既定・不変 / random+seed 決定性(workers 数に依存しない)/ seed 自動生成→再利用で再現 / scan-limit によるプール上限 / 不正 mode
- フェイク opener による STAC API ページング + random + page-size 付与、items ページの WAF-202 → operational(ERROR 0、リトライなし、CLI exit 2)
- RateLimiter: フェイク時計で 0/0.5/0.5/0.5 秒、idle 後の burst 上限、`--delay` 換算、20 スレッドで FIFO 0.1s 間隔、CLI の排他フラグ
- リトライ: 429+Retry-After→成功、5xx バックオフ 1s/2s→成功、timeout→成功、上限到達、4xx(400/401/403/404/410)リトライなし、非 transient 例外リトライなし、202 空 body リトライなし、Retry-After 上限超過で打ち切り、コレクション文書 WAF → exit 2
- キャッシュ: 8 Item が同一 tif を参照 → `rasterio.open` 1 回(workers=4)、open 失敗もキャッシュしつつ 3 件の `ASSET_UNREADABLE`、同時 open 上限 ≤2
- サマリー: JSON 既存キー保持 + `sampling`/`summary`、text 行の内容
- localhost `ThreadingHTTPServer`: 実 urllib 経路で 429→リトライ、items → 302 → `/search` 追従、202 空 body、404 リトライなし
- 既存 65 件はすべて無変更で通過(後方互換)

## 4. ライブ確認 (D9, pytest 外)

```
$ stac-integrity collection https://earth-search.aws.element84.com/v1/collections/sentinel-2-c1-l2a --limit 5 --sample random --seed 1 --asset red
PASS sentinel-2-c1-l2a | collection-assets: PASS | items: 5 | failing-items: 0 | assets: 5 | errors: 0 | warnings: 0
sampling: random seed=1 pool=1000 (first-N pool only, scan-limit=1000) | limit: 5 | selected: 5
summary: items: 5 | assets-opened: 5 | cache-hits: 0 | errors: 0 | warnings: 0 | unreadable: 0 | http: 11 req / 0 retries | complete: yes
exit 0, 約 22 秒
```

- page-size 自動拡大前はサーバー既定 10 件/ページで 101 リクエスト・約 99 秒 → `?limit=100` 既定化で 11 リクエスト・約 22 秒。
- 同 seed で 2 回連続実行すると 5 件中 3 件が別 Item になった(新規 ingest で先頭 1000 件がずれるため)。これが「再現性はプールが安定している間だけ」という注意書きの根拠。
- `--scan-limit 50 --page-size 50 --max-rps 5` では 2 リクエスト・約 3.5 秒。

## 5. 性能メモ
- 静的カタログ: Item JSON 取得がワーカー並列になり、random では選ばれた Item のみ取得。
- 重複 href(同一 COG を参照する Item 群、コレクションアセットと Item の共有など)は 1 回だけ open。
- random のプール走査コストは `scan_limit / page_size` リクエスト。大きいコレクションでは `--scan-limit` を下げるか `--max-rps` と組み合わせる。

## 6. QA (D10)
- `pytest -q`: 96 passed
- `python -m build`: sdist + wheel OK(`netpolicy.py` 同梱確認)
- `twine check dist/*`: PASSED ×2
- クリーン venv に wheel をインストール → `stac-integrity --version` = 0.3.0rc1、`collection demo_collection_bad/... --sample random --seed 2` → exit 1、新サマリー行出力

## 7. 触った共有ファイル(コンフリクトリスク)
| ファイル | 変更 | リスク |
|---|---|---|
| `stac_integrity/audit.py` | `RasterHeader` / `read_raster_header` / `REMOTE_GDAL_ENV` 追加、`audit_item_dict` に `header_reader` 引数、open ブロックを 1 行呼び出しに置換 | **中**。Track B(`--resolver`)が href 解決・open 周りを触るなら同じ try ブロックで衝突しうる。解決方針: resolver で href を変換した後に `header_reader(href)` を呼ぶ形にすれば両立 |
| `stac_integrity/cli.py` | `_collection_scale()` 追加、`--workers` 既定、`audit_collection` 呼び出しに引数追加、text 出力に `_print_collection_summary`、incomplete → exit 2 | **中**。Track C(`--format/--output`)が collection の出力分岐を書き換えるなら `print(...)` 部分で衝突。Track B は `_common` に追加するなら衝突小 |
| `stac_integrity/collection.py` | 大幅書き換え | 他トラックが触らなければ低 |
| `README.md` | 例の出力 2 行、新節「Large collections」、exit 2 の説明 1 セル | 低〜中(節単位の追加) |
| `CHANGELOG.md` | Collections 節に 3 行 | 低 |
| 新規: `stac_integrity/netpolicy.py`, `tests/test_collection_scale.py`, `TRACK_D_REPORT.md` | — | なし |

## 8. 推奨: MERGE_WITH_CHANGES
理由と条件:
1. `--workers` 既定 8→4 と「items ページ失敗時に例外ではなく部分結果 + exit 2」は利用者から見える挙動変更。CHANGELOG に記載済みだが、RC 後の変更として許容するか判断が必要。
2. Track B/C とのマージ順により `audit.py` の open ブロックと `cli.py` の collection 出力部で手動解決が必要になる可能性。
3. Track C の `schema_version` 導入時に `sampling` / `summary` ブロックをスキーマに含めること。
コード自体は全テスト・ビルド・ライブ確認を通過しており、ブロッカーはない。

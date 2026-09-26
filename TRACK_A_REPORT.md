# Track A — Release / Distribution report

Branch: `feature/release-distribution` (base: RC commit `a6e23c0`, 0.3.0rc1)
Date: 2026-09-26

## 実施内容

### A1. CI (`.github/workflows/ci.yml`)
- `permissions: contents: read`（ワークフロー全体、最小権限）、`concurrency` で同一 ref の古い実行をキャンセル。
- `test` job: Python 3.10 / 3.11 / 3.12 / 3.13 の matrix（従来は 3.11 が欠落）、`fail-fast: false`、`pip install -e ".[test]"`、`pytest -q`。
- `lint` job: `ruff check .`（ruff は CI 上でのみ install、パッケージ依存にしない）。
- `build` job: `python -m build` → `twine check --strict dist/*` → クリーン venv に wheel を install して `stac-integrity --version` とチェックアウト外からの import を確認 → `dist/` を artifact 化（7 日保持）。
- actions は major 版固定（`actions/checkout@v4`, `actions/setup-python@v5`, `actions/upload-artifact@v4`）、checkout は `persist-credentials: false`。

### A2. Release 自動化（`.github/workflows/release.yml` + `docs/releasing.md`）
**判断: release.yml を作成する（安全に書けるため）。docs/releasing.md も併記。**
- トリガは `v*` タグ push のみ。ブランチ push / PR では publish しない。ローカル repo では当然実行されない（ファイルのみ）。
- `test`（3.10–3.13）→ `build`（タグ == `v` + `__version__` を検証、build、`twine check --strict`、クリーン venv smoke）→ `publish`。
- `publish` job のみ `id-token: write`。GitHub `environment: pypi` で実行し、`pypa/gh-action-pypi-publish@release/v1` で Trusted Publishing（OIDC）。API token は一切保存しない。PEP 740 attestations は action の既定で生成・upload。
- publish job はプロジェクトコードを実行せず、build job の artifact をそのまま upload（再ビルドしない）。
- フェイルクローズ: PyPI 側に Trusted Publisher（repo + `release.yml` + env `pypi`）が登録されるまで upload は PyPI に拒否される。さらに `pypi` environment に required reviewers + タグ規則 `v*` を設定するよう docs で必須化（手動承認ゲート）。
- 自動 publish を完全に避けたい場合は release.yml を削除しても docs/releasing.md の Manual fallback で運用可能。

### A3. メタデータ
- `pyproject.toml` は**変更なし**。build 結果の METADATA を確認: `Metadata-Version: 2.4`、`License-Expression: MIT`、`Description-Content-Type: text/markdown`、classifiers（3.10–3.13, Beta, GIS, QA）、keywords、`Requires-Python: >=3.10`、`Requires-Dist: rasterio>=1.3` はいずれも適切。SPDX license expression 使用中のため License classifier は追加しない（併用は非推奨/エラー）。
- `[project.urls]` は公開 repo URL 未定のため追加しない（placeholder 禁止）。checklist に「repo 作成後に追加」として記載。
- README の相対リンク（`docs/validation-evidence.md` 等）は PyPI 上で解決しない → 既知の制限として checklist に記載（URL 未定のため今は修正不可）。

### A4. lint
- **ruff を dev-only で導入**（`ruff.toml` 単独ファイル。pyproject を触らずマージ衝突を回避）。
- 根拠: 既存コードは ruff の既定ルール（`E4,E7,E9,F`）で `scripts/public_benchmark.py` の意図的な E402（sys.path 挿入後の import）2 件のみ → per-file-ignore で 0 件。コード変更ゼロで CI に入れられ、未使用 import 等の回帰を安価に防げる。
- `benchmark/`・`research/`・`release/` は非出荷の研究ツールのため除外（benchmark には F401/E741 が計 4 件あるが対象外）。
- `ruff format` は 9 ファイルを再整形してしまうため**強制しない**。pre-commit は導入しない（単独メンテナ、CI の lint job で十分、フック追加は開発者環境依存を増やすだけ）。

### A5. `docs/release-checklist.md`
身元（`release/rewrite-author-identity.sh` をメインリポジトリのローカルで実行、コピーはしない）、secret/personal-data scan、version/changelog、クリーンビルド・twine check・wheel/sdist 内容確認・クリーン install・sdist からのテスト、`[project.urls]`、Trusted Publisher / environment 設定、push・タグ・承認・PyPI 検証、公開後の後片付け、の 8 セクション。

## 変更ファイル
- M `.github/workflows/ci.yml`
- A `.github/workflows/release.yml`
- A `docs/releasing.md`
- A `docs/release-checklist.md`
- A `ruff.toml`
- A `TRACK_A_REPORT.md`

## 依存の追加
| 依存 | 分類 | 備考 |
|---|---|---|
| なし | core | rasterio のみのまま |
| なし | optional / test | `test = ["pytest","numpy"]` 変更なし |
| `ruff>=0.15,<0.16` | dev (CI only) | CI の lint job で install。pyproject には記載しない |
| `build`, `twine` | dev (CI/release only) | CI/release job とローカル QA でのみ使用 |
| `pypa/gh-action-pypi-publish@release/v1` | release (GitHub Action) | publish job のみ |

## QA 結果（ローカル, Python 3.12.x, macOS arm64）
- `pytest -q`: **65 passed**, 48 warnings（rasterio 由来の PendingDeprecationWarning、既知）
- `python -m build`: `stac_integrity_gate-0.3.0rc1.tar.gz` と `stac_integrity_gate-0.3.0rc1-py3-none-any.whl` を生成
- `twine check --strict dist/*`: 2/2 PASSED
- クリーン venv（python3.12）に wheel install → チェックアウト外で `stac-integrity --version` = `stac-integrity 0.3.0rc1`、`demo_bad/item.json` で exit 1（期待通り）
- 展開した sdist 内で `pytest -q`: 65 passed
- sdist 40 ファイル。`benchmark/`・`research/`・`release/`・ruff.toml・workflows は含まれない（`scripts/public_benchmark.py` は MANIFEST.in の意図通り含まれる）
- dist 内の個人情報スキャン（`/Users/`, 個人 e-mail）: 0 件
- `ruff check .`: All checks passed
- workflow YAML: PyYAML で両ファイル parse 成功、`permissions` 構造を確認（top-level `contents: read`、`id-token: write` は release の `publish` のみ）。`actionlint` は未インストールのため未実施。
- タグ/バージョン照合の one-liner をローカルで実行し `0.3.0rc1` を取得することを確認。

## 既知の制限
- GitHub Actions 上での実行は未検証（remote なし）。actionlint 未実施。
- PyPI project page 表示用 `environment.url` は PyPI のパッケージ URL（repo URL ではない）。
- rc タグ（`v0.3.0rc1`）も PyPI に pre-release として publish される。TestPyPI 経路は docs の手順のみ（workflow には入れていない）。
- Author identity の blocker は未解決のまま（本トラックの範囲外、指示通り）。
- CONTRIBUTING.md に ruff の記載は追加していない（衝突回避。必要ならマージ後に 1 行追加）。
- pip の検証用に `.venv` へ PyYAML を追加 install した（ローカル venv のみ、リポジトリには影響なし）。

## マージ衝突リスク
- 共有ファイル（README.md / pyproject.toml / CHANGELOG.md）は**一切変更していない**。
- `.github/workflows/ci.yml` は全面書き換え。他トラックが ci.yml を触る場合は衝突するが、内容は小さく解消は容易。
- `docs/` には新規ファイル追加のみ。

## 判定
- Verdict: **READY_AFTER_AUTHOR_ID**
- Recommendation: **MERGE**

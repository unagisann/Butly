# Old（凍結ドキュメント）

**日本語** | [English](README.md)

> ⚠️ **このディレクトリの文書は凍結済みです。現行仕様の正ではありません。**

ここには「かつて正本だったが、現在は他の文書やコードに置き換わった」ドキュメントを置きます。
内容は書かれた当時のスナップショットで、**更新しません**。

## 正の優先順位

1. 現行コード
2. テスト（`tests/`）
3. [`docs/reference/`](../reference/) / [`docs/guides/`](../guides/)
4. （参考）`docs/planning/archived/`
5. （参考）このディレクトリ

## 収録文書

| 文書 | 凍結時点 | 後継 |
|---|---|---|
| [project_status.ja.md](project_status.ja.md) / [.md](project_status.md) | 2026-05-24 | ルート [README.ja.md](../../README.ja.md) と [`docs/reference/`](../reference/) |
| [recent_changes.ja.md](recent_changes.ja.md) / [.md](recent_changes.md) | 2026-05-24 | `git log`（変更履歴の正本）と [`docs/planning/`](../planning/) |

### なぜ凍結したか

- **project_status**: Streamlit 単独 UI・Stage 3 未実装・settings 層なしを前提とした構成図で、
  Tauri desktop frontend / `butly_api` / Stage 3 Knowledge Maturation / `butly_core/settings/` を
  含む現行アーキテクチャと食い違う。役割はルート README と `docs/reference/` に吸収済み。
- **recent_changes**: 2026-05-24 以降の変更が入っていない手書き changelog。
  Conventional Commits 運用に移行したため、変更履歴は `git log` を正とする。

## 削除ではなくここに置く理由

設計判断の経緯（なぜその形になったか）を追う時の一次資料として残しています。
実装の根拠として引用しないでください。

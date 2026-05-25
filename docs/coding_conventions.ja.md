# コーディング規約

🌐 **日本語** | [English](coding_conventions.md)

> Butly プロジェクトの軽量な規約集。一人プロジェクトなのでメモ書きに近いが、新規コードでは理由がない限り従う。

## 例外処理

### 原則
- **例外を黙って握り潰さない。** `except Exception: pass`（や bare `except: pass`）はバグを隠蔽し、回帰の調査を不可能にする。
- **狭く捕捉する。** `except FileNotFoundError`、`except json.JSONDecodeError`、`except yaml.YAMLError` などを優先し、`except Exception` は避ける。
- **どうしても広く捕捉する必要がある場合**（例: サードパーティ SDK の例外階層が不安定、バックグラウンドタスクの分離）は、必ず `logger.exception(...)` か `print(...)` でトレースを残す。**`pass` は禁止。**
- **`BaseException` の捕捉は禁止。** すぐに re-raise する場合を除く（`KeyboardInterrupt` / `SystemExit` を含むため）。

### 広い捕捉を許容するケース
- バックグラウンドデーモン・監視スレッドで、1回の失敗でループを止めてはいけない場合（`sleeptime.py`、`main.py:_watch_parent` 等）。
- 設定ファイル読み込みのフォールバックで、JSON 破損が起動を止めてはいけない場合 — ただし**必ず**ログを残す。
- デバッグログ・テレメトリの保存失敗が応答に影響してはいけない場合（`ChatService` の debug_logs 等）。

### 既存コード
- 2026-05 時点で `except Exception:` は約 168 箇所存在。一括置換は予定なし。**触る箇所だけ漸進的に**直す方針。

## ファイル書き込み

### 原則
- **インプレースで上書きされるファイルは必ず `butly_core.io_utils.atomic_write_text`（または `atomic_write_bytes`）を使う。** 書き込み中のクラッシュで元ファイルが空・破損状態にならないようにする。
- インスタンス新規作成時の単発書き込み（`InstanceManager.create_instance`）は対象外。失敗しても「インスタンス未作成」状態でリトライ可能。
- デバッグ・一時ログ（`ChatService` の `latest.json` など）は判断で対象外。ローテーション付きで冗長・再構築可能なため。

### アトミック書き込み必須箇所（2026-05 時点）
- `butly_core/core/memory.py` — glossary、セッションターン JSON、session digest
- `butly_core/core/key_memory.py` — `Key_Memory.yaml`、提案 JSON
- `butly_core/core/gatekeeper/session_state.py` — `session_state.json`（毎ターン書き込み）
- `butly_core/core/instance_manager.py` — `config.json` の更新、`system_instruction.txt` の更新、rename 時の波及更新
- `butly_core/search/usage_tracker.py` — `search_usage.json`

## 型ヒント

- `butly_core/` の公開関数シグネチャには型ヒントを付ける。本体から型が自明な内部ヘルパは省略可。
- `mypy` の CI 強制は未導入。型ヒントはあくまでドキュメント目的。
- `Optional[T]` を `T | None` より優先（既存コードがほぼ前者なので統一）。

## 設定参照

- 新規コードでは `butly_core.config.AI_CONFIG` / `SYSTEM_CONFIG` を直接参照しない。Phase 1 の間は既存コード互換のため残すが、新規・改修コードは `butly_core.settings.get_settings()` を使う。
- テストで設定を差し替える場合は `override_settings()` または `get_settings.cache_clear()` / `clear_settings_cache()` を使う。legacy global の直接 mutation は段階移行まで既存テストの互換用途に限定する。

## コメント

- デフォルトはコメントなし。**名前で「何をするか」**を伝える。
- コメントを書くのは**「なぜ」が自明でない場合**: 回避策、繊細な不変条件、既知の落とし穴、期限付きハック等。
- 実装を逐一説明するコメント（「リストをループして加算」など）は禁止。コード自体が説明しているから。

## logging vs print

- `print("[Module] ...")` はトップレベルの起動・終了・単発イベントなら許容。
- ホットパス内、もしくはレベルフィルタリングが有用な箇所（ターン毎の診断、gatekeeper のトレース、プロバイダー呼び出し等）は Python の `logging` を使う。
- 新規モジュールは最初から `logging` を推奨。既存の `print` ベースのモジュールは触るまで放置可。

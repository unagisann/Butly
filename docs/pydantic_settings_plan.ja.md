# 設定統合計画書: pydantic-settings 導入

🌐 **日本語** | [English](pydantic_settings_plan.md)

> 起票: 2026-05-24 / 想定スパン: 段階的、急がない / 利用者: 自分のみ（破壊的変更可）

## 1. なぜやるか（モチベーション）

現状の設定は 5 つのレイヤーが手作業で重なっており、以下の問題がある:

1. **型安全性ゼロ**: `AI_CONFIG["chat"]["generation_config"]["temperature"]` のタイポは無音で `KeyError`、もしくは未定義キーが None を返すだけ。IDE 補完も効かない。
2. **`AI_CONFIG` / `SYSTEM_CONFIG` が mutable な module global**: テストが `cfg_mod.AI_CONFIG["chat"] = ...` で直接書き換える（[tests/test_settings_model_candidates.py:62](../tests/test_settings_model_candidates.py#L62)）。テスト分離の事故源。実際に「単独実行で通る・スイートで落ちる」test (`test_embedding_role_excludes_chat_only`) を抱えている。
3. **import 時の副作用**: [butly_core/config.py:268-289](../butly_core/config.py#L268-L289) が import 時に `user_config.json` を読みに行くため、テストごとの差し替えが困難。さらに循環 import 回避のため約 40 箇所で関数内 lazy import になっている。
4. **検証なし**: `user_config.json` に壊れた `safety_settings` を入れても、エラーが provider 呼び出し時まで遅延する。
5. **`.env` パーサが脆い**: [main.py:86-99](../main.py#L86-L99) は `line.partition("=")` で自前パース。引用符、複数行、行内コメント等を扱えない。
6. **インスタンス config の暗黙スキーマ**: `Jarvis/config.json` には `cache_ttl_hours`, `use_rag`, `use_context_cache` 等が出てくるが、これらは中央で定義されていない。誰がいつ追加したか、デフォルトは何かが追跡しづらい。
7. **`_recursive_update` の沈黙**: ユーザーが `user_config.json` でタイポしても黙ってマージされる。

## 2. ゴール

- **単一スキーマ**: pydantic-settings モデルで全設定項目を 1 箇所に定義。デフォルト値・型・バリデーションがコードで読める。
- **明示的なレイヤリング**: defaults → `user_config.json` → 環境変数 → instance config という優先順位を、コードと一致した順序で適用。
- **後方互換**: 既存の `user_config.json` / `.env` / 各インスタンスの `config.json` はそのまま動く。利用者は自分一人なので破壊的変更も可だが、不必要な手戻りは避ける。
- **テスト分離**: `Settings(ai=...)` で新規インスタンスを作って渡せる構造に。global ミューテーションをやめる。
- **段階移行**: 一気に置き換えない。**Phase 1 で互換シム** → モジュール毎に typed access へ → 最後に legacy を削除。

## 3. ライブラリ選定

**pydantic-settings v2**（pydantic v2 から分離された専用パッケージ）を採用する。

| 候補 | 採否 | 理由 |
|---|---|---|
| **pydantic-settings v2** | ✅ 採用 | JSON ファイルソース (`JsonConfigSettingsSource`)、`.env` ネイティブ、ネスト変数 `env_nested_delimiter`、検証エラーが構造化、生 pydantic とシームレス。すでに業界標準。 |
| dynaconf | ❌ | 検証が pydantic より弱く、Settings オブジェクトが dict-like で型補完が不十分。 |
| 自前 pydantic BaseModel + 自前ローダ | ❌ | env/json/ネストマージを再発明する手間が無駄。 |
| dataclasses + ad-hoc validation | ❌ | バリデーションが脆い。今の問題が小さくなるだけ。 |

依存追加: `pydantic-settings>=2.5,<3` を `requirements.txt` に追加。pydantic は既に依存に含まれている（FastAPI / Google SDK 経由）。

## 4. スキーマ設計

### 4.1 ディレクトリレイアウト

```
butly_core/settings/
├── __init__.py          # get_settings(), RootSettings の再エクスポート
├── root.py              # RootSettings — 全レイヤーを束ねるトップレベル
├── ai.py                # AIConfig / RoleConfig / GenerationConfig / SafetySetting
├── system.py            # SystemConfig / 各サブセクション
├── connections.py       # LLMConnection (user_config.json の LLM_CONNECTIONS)
├── instance.py          # InstanceConfig (per-instance config.json)
└── sources.py           # 自前 SettingsSource: 再帰マージ用
```

### 4.2 モデル例（最終形のイメージ）

```python
# butly_core/settings/ai.py
from typing import Literal, Optional
from pydantic import BaseModel, Field, model_validator


class GenerationConfig(BaseModel):
    """Provider 共通のサンプリング設定。

    値の意味はプロバイダーごとに微妙に違うので strict にせず、知らないキーも
    受け入れる (extra='allow')。将来 provider 別に分岐したくなったら
    discriminated union に切り替える。
    """
    model_config = {"extra": "allow"}

    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    max_output_tokens: Optional[int] = None
    reasoning_effort: Optional[Literal["low", "medium", "high"]] = None


class SafetySetting(BaseModel):
    category: str
    threshold: str


class RoleConfig(BaseModel):
    """AI_CONFIG の各 role エントリ。"""
    connection: Optional[str] = None
    model_name: Optional[str] = None
    generation_config: GenerationConfig = Field(default_factory=GenerationConfig)
    safety_settings: list[SafetySetting] = Field(default_factory=list)

    @model_validator(mode="after")
    def _infer_connection(self) -> "RoleConfig":
        """connection 未指定なら model_name から推定 (旧 _normalize_ai_config 相当)。"""
        if self.model_name and not self.connection:
            from butly_core.llm.model_registry import infer_connection_id
            inferred = infer_connection_id(self.model_name)
            if inferred:
                self.connection = inferred
        return self


class AIConfig(BaseModel):
    chat: RoleConfig = Field(default_factory=RoleConfig)
    summary: RoleConfig = Field(default_factory=RoleConfig)
    knowledge: RoleConfig = Field(default_factory=RoleConfig)
    embedding: RoleConfig = Field(default_factory=RoleConfig)
    gatekeeper: RoleConfig = Field(default_factory=RoleConfig)
    context_classifier: RoleConfig = Field(default_factory=RoleConfig)
```

```python
# butly_core/settings/system.py
class MemoryConfig(BaseModel):
    max_raw_tokens: int = 4096
    raw_injection_format: Literal["markdown", "plaintext", "compact"] = "plaintext"
    short_term_limit: int = 6
    generate_mid_term_summaries: bool = True
    max_digest_chars: int = 8000
    relationship_update_interval_days: int = 7
    use_summarized_mid_term: bool = True
    count_dedup_hours: int = 6


class BrainConfig(BaseModel):
    search_limit: int = 3
    keyword_hit_threshold: int = 5
    fallback_fetch_limit: int = 100
    time_decay_rate: float = 0.003
    summary_char_limit: int = 200
    readable_instances: list[str] = ["self"]
    dynamic_threshold: float = 0.6
    default_use_google_search: bool = False


class GatekeeperConfig(BaseModel):
    tier_rc_threshold: float = 0.4
    tier_cn_threshold: float = 0.3


class GlossaryConfig(BaseModel):
    scan_depth: int = 2
    scan_target: Literal["user", "assistant", "both"] = "both"
    max_entries: int = 20
    max_chars: int = 4000


class SystemConfig(BaseModel):
    agent: AgentConfig = Field(default_factory=AgentConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    brain: BrainConfig = Field(default_factory=BrainConfig)
    backup: BackupConfig = Field(default_factory=BackupConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    memory_probe: MemoryProbeConfig = Field(default_factory=MemoryProbeConfig)
    gatekeeper: GatekeeperConfig = Field(default_factory=GatekeeperConfig)
    chat: ChatConfig = Field(default_factory=ChatConfig)
    glossary: GlossaryConfig = Field(default_factory=GlossaryConfig)
```

```python
# butly_core/settings/root.py
from pathlib import Path
from typing import Type
from pydantic_settings import (
    BaseSettings, SettingsConfigDict, PydanticBaseSettingsSource,
    JsonConfigSettingsSource, EnvSettingsSource,
)


class RootSettings(BaseSettings):
    """Butly 全体の設定。

    ロード順 (優先度 低→高):
      1. BaseModel デフォルト値 (このファイル + ai.py + system.py)
      2. user_config.json (project root)
      3. .env / APIkey.env (API キー類のみ)
      4. 環境変数 (BUTLY_AI__CHAT__MODEL_NAME 等)

    instance config はランタイムで `Settings.with_instance(name)` で重ねる。
    """
    model_config = SettingsConfigDict(
        env_prefix="BUTLY_",
        env_nested_delimiter="__",
        env_file=(".env", "APIkey.env"),
        env_file_encoding="utf-8",
        json_file="user_config.json",
        extra="ignore",   # 未知のトップレベルキーは無視 (旧 _AI_CONFIG_*_example 等を許容)
    )

    ai: AIConfig = Field(default_factory=AIConfig, alias="AI_CONFIG")
    system: SystemConfig = Field(default_factory=SystemConfig, alias="SYSTEM_CONFIG")
    llm_connections: list[LLMConnection] = Field(default_factory=list, alias="LLM_CONNECTIONS")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: Type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # 自前の RecursiveJsonSettingsSource で _recursive_update 相当の deep merge を実現
        from .sources import RecursiveJsonSettingsSource
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            RecursiveJsonSettingsSource(settings_cls, Path("user_config.json")),
        )


@lru_cache(maxsize=1)
def get_settings() -> RootSettings:
    return RootSettings()
```

### 4.3 マージセマンティクスの維持

現行の `_recursive_update` は **深さ無制限の dict merge**:
```python
# 既存挙動: user_config の AI_CONFIG.chat.generation_config.temperature だけ上書きできる
{"chat": {"generation_config": {"temperature": 0.5}}}  # top_k, max_output_tokens はデフォルト維持
```

pydantic-settings の標準 JSON ソースは**フィールド単位の上書き**しかしない（dict 全体置換）。これを保つには **自前 `SettingsSource`** を書く必要がある:

```python
# butly_core/settings/sources.py
class RecursiveJsonSettingsSource(JsonConfigSettingsSource):
    """user_config.json を読み、ネストした dict を再帰マージして返す。"""
    def __call__(self) -> dict[str, Any]:
        raw = self._read_files(self.json_file_path)  # 既存実装を流用
        return raw  # pydantic 側がデフォルトとマージするが、Nested BaseModel の field は
                   # default_factory のおかげで自然に補完される
```

実際には pydantic v2 の `BaseModel.model_validate` + `BaseSettings` のソースマージは「フィールド単位」なので、**ネスト BaseModel のフィールドは default_factory で空オブジェクトが生成 → ユーザー値で部分上書き** のセマンティクスが自然に成立する。**自前ソースの実装は思っているより小さい**。実装時に挙動を一致させるテストを書いて確認する。

### 4.4 環境変数命名

```
BUTLY_AI__CHAT__MODEL_NAME=gemini-3.5-flash       # ai.chat.model_name
BUTLY_SYSTEM__MEMORY__SHORT_TERM_LIMIT=8           # system.memory.short_term_limit
BUTLY_AI__CHAT__GENERATION_CONFIG__TEMPERATURE=0.9 # ai.chat.generation_config.temperature
```

ただし、API キー類は **既存名を尊重** する:

| 既存名 | pydantic-settings での扱い |
|---|---|
| `GOOGLE_API_KEY` / `GEMINI_API_KEY` | `.env` から `os.environ` に乗せて、provider が直接読む現状維持 |
| `OPENAI_API_KEY` / `XAI_API_KEY` / `OLLAMA_BASE_URL` | 同上 |
| `TAVILY_API_KEY` / `OLLAMA_WEB_SEARCH_API_KEY` | 同上 |
| `FIRE_TV_IP` / `FIRE_TV_PORT` | RootSettings に取り込む（`BUTLY_` プレフィックスなしで読めるよう alias 設定） |

→ API キーは依然として `Connection.api_key_env` 経由で provider が取得する。pydantic-settings は `.env` 読み込みのインフラとしてのみ機能させる。

## 5. 段階移行

### Phase 0 — 棚卸し（コード変更なし、半日）

- [ ] 現在使われている設定キーを全件列挙し、`docs/config_inventory.md` にまとめる
- [ ] `_AI_CONFIG_*_example` などの dead key を特定（4 種類確認済み）
- [ ] `context_classifier` ロール（[config.py:79](../butly_core/config.py#L79) で `{}` のまま）の扱いを決める — 削除 or 正規化
- [ ] インスタンス毎の `config.json` に出現するキーを全 instance スキャン

### Phase 1 — pydantic-settings の追加と互換シム（1〜2 日）

- [ ] `pydantic-settings>=2.5,<3` を `requirements.txt` に追加
- [ ] `butly_core/settings/` パッケージを新規作成（モデル定義のみ、まだ使わない）
- [ ] 既存の `_recursive_update` / `_normalize_ai_config` / `_register_user_connections` を pydantic 側に再実装
- [ ] **互換シム**: `butly_core/config.py` を以下のように書き換え:
  ```python
  from butly_core.settings import get_settings
  _settings = get_settings()
  AI_CONFIG = _settings.ai.model_dump(by_alias=False, exclude_none=False)
  SYSTEM_CONFIG = _settings.system.model_dump(by_alias=False, exclude_none=False)
  ```
- [ ] 既存のすべてのテストが通ることを確認（765 / 766 を維持）
- [ ] `_normalize_ai_config` の動作を pydantic validator が再現することを confirm するテスト追加

**完了条件**: 既存コードに一切手を入れずに、互換シム経由で全テスト通過。

### Phase 2 — モジュール毎に typed access へ移行（漸進的、急がない）

優先順位（影響範囲が小さい順 → 大きい順）:

1. `butly_core/search/usage_tracker.py` — `os.environ` を 1 箇所しか触らない
2. `butly_core/core/fire_tv.py` — `FIRE_TV_IP` / `FIRE_TV_PORT` のみ
3. `butly_core/search/__init__.py` — 検索プロバイダー選択ロジック
4. `butly_core/core/brain.py` — `AI_CONFIG["chat"]` / `AI_CONFIG["embedding"]` の参照
5. `butly_core/core/gatekeeper/*` — `SYSTEM_CONFIG["gatekeeper"]`
6. `butly_core/chat/service.py` — `AI_CONFIG["chat"]` の参照（中規模）
7. `butly_core/llm/providers/*` — provider 内の `AI_CONFIG` 参照（最大）
8. `sleeptime.py` — 大量の `AI_CONFIG` / `SYSTEM_CONFIG` 参照
9. `routers/*` — 設定 API
10. `app.py` — 最後（UI 直結なので変更コストが見えにくい）

各モジュール移行時:
- `from butly_core.settings import get_settings` を import
- `AI_CONFIG["chat"]["model_name"]` → `get_settings().ai.chat.model_name`
- 旧 dict-style と新 typed-style が同じ値を返すゴールデンテストを追加
- レビュー & マージ

### Phase 3 — インスタンス config の型付け（1〜2 日）

- [ ] `InstanceConfig` モデルを `butly_core/settings/instance.py` に定義（既存スキーマを全反映）
- [ ] `InstanceManager.get_instance_config(name)` の返り値を `InstanceConfig` に変更（旧 dict 形式は `.model_dump()` で経由可能）
- [ ] `_load_config()` / `_load_config_migrated()` を typed 版に置換
- [ ] 既存のインスタンス config.json（[butly_core/instances/Jarvis/config.json](../butly_core/instances/Jarvis/config.json) 他）を読み込んでバリデーションエラーが出ないことを確認
- [ ] 未知のキー (`cache_ttl_hours` 等) は `extra="allow"` で受け入れ、警告ログを出す

### Phase 4 — LLM_CONNECTIONS の型付け（半日）

- [ ] `LLMConnection` pydantic モデルを定義
- [ ] `_register_user_connections` を pydantic 経由に書き換え
- [ ] `user_config.json` の `LLM_CONNECTIONS` エントリ間違い (`id` 欠落等) を起動時に検出して明示エラー化

### Phase 5 — Legacy globals 削除（半日〜1 日）

- [ ] `butly_core/config.py` の `AI_CONFIG = ... ` / `SYSTEM_CONFIG = ...` module-level 代入を削除
- [ ] 残った参照を grep で炙り出して、最終的な `get_settings()` 経由に置換
- [ ] テストの `cfg_mod.AI_CONFIG[...] = ...` を `monkeypatch.setattr(_settings, ...)` か `Settings(ai=AIConfig(...))` 方式に書き換え
- [ ] `test_embedding_role_excludes_chat_only` 等の flaky テストが安定するかを観察

### Phase 6 — `.env` ローダの cleanup（半日）

- [ ] [main.py:86-99](../main.py#L86-L99) の自前 `.env` パーサを削除
- [ ] pydantic-settings の `env_file` 機能に集約
- [ ] `frozen` 配布時の MEIPASS → LOCALAPPDATA ブートストラップロジックは残す（pydantic-settings に置き換えると複雑になるだけ）

## 6. リスクとミティゲーション

| リスク | 影響 | 対策 |
|---|---|---|
| 既存 `user_config.json` の予期せぬバリデーション失敗 | 起動不能 | Phase 1 で「現行 user_config.json をロードして dict 比較が一致するか」のテストを必須化 |
| `_recursive_update` の深さ無制限マージと pydantic-settings の挙動差 | サブ設定が消える等の sneaky bug | 自前 `RecursiveJsonSettingsSource` を最初に書き、ゴールデンテストで保護 |
| `connection` 自動推定の挙動差 | provider 解決が変わる | `_normalize_ai_config` の動作テスト (現状で動いているはず) を流用 |
| circular import の再発 | startup 失敗 | `model_validator` 内の `infer_connection_id` は遅延 import を維持 |
| テストのモンキーパッチ書き換え量 | 単発作業が積み重なる | Phase 5 でまとめて。Phase 1–4 では `AI_CONFIG` dict が動的に再構築されるので既存パッチが効くことを保証 |
| pydantic v2 vs Google `genai` の pydantic v1 共存 | (たぶん問題なし) | `pip install pydantic-settings` 後に既存テスト全件で確認 |
| `app.py` (3424 行) の参照書き換え | 大量変更 | Phase 2 では触らない。最後にまとめて、もしくは別計画 (app.py 分割) と同時に |

## 7. 完了の定義 (Definition of Done)

- [ ] `AI_CONFIG` / `SYSTEM_CONFIG` の `module-level` 代入が `butly_core/config.py` から消えている
- [ ] 全コードが `from butly_core.settings import get_settings` 経由で設定を読む
- [ ] `user_config.json` で typo すると起動時に明示的に警告される
- [ ] テストが `Settings(...)` インスタンスを直接渡せる (グローバル mutation 不要)
- [ ] `docs/config_inventory.md` で全設定キーの所在・型・デフォルトが追える
- [ ] 既存の `user_config.json` を 1 行も変えずに動作する（破壊的変更は意図的なものだけ）

## 8. 工数見積もり (合計 4〜6 日、分割可能)

| Phase | 見積 | 並列化可能か |
|---|---|---|
| 0. 棚卸し | 0.5 day | — |
| 1. シム導入 | 1〜2 days | — |
| 2. 移行 (10 モジュール) | 各 0.2〜0.5 day | 並列可（モジュール独立） |
| 3. instance config 型付け | 1 day | — |
| 4. LLM_CONNECTIONS | 0.5 day | — |
| 5. legacy 削除 | 0.5〜1 day | Phase 2 完了が前提 |
| 6. `.env` cleanup | 0.5 day | — |

## 9. やらないこと (Non-Goals)

- **設定 UI の刷新**: Streamlit の Settings タブはそのまま動かす。Phase 5 で内部実装だけ差し替える。
- **インスタンス config の正規化（古いフィールドの削除）**: `extra="allow"` で受け入れるだけ。クリーンナップは別計画。
- **YAML / TOML への移行**: JSON のまま。フォーマット変更は別計画。
- **設定ファイルのスキーマ JSON Schema 出力**: pydantic で簡単にできるが、誰も読まない。やらない。
- **`app.py` の `AI_CONFIG` 参照を全部書き換える**: app.py は別計画（分割）と同時に対応。それまでは互換シム経由でアクセス継続。

## 10. 着手前の確認事項（自分への TODO）

- [ ] Phase 1 の互換シムが本当に既存テストを 1 件も壊さないことを `pytest --co` でドライランしてから着手
- [ ] `pydantic-settings` のバージョンを pin する (`>=2.5,<3`)
- [ ] 既存の循環 import パターン (`from butly_core.config import AI_CONFIG` の関数内 import) を維持するか、root.py で循環を切れるか先に検証
- [ ] `app.py` の更新は別 PR / 別計画とする旨を明文化（混入させない）

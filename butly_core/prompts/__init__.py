"""
butly_core.prompts
------------------
control / locales 分離対応のプロンプトローダー。

control/  — 機能的プロンプト（英語固定、locale 非依存）
locales/  — 人格プロンプト（locale 別、en フォールバック）

解決優先順位:
  1. user_prompts.json（既存のユーザーオーバーライド）
  2. location に基づくファイル検索
     - control: control/{name}.txt
     - locales: locales/{locale}/{name}.txt → locales/en/{name}.txt
"""

import json
from pathlib import Path
from typing import Optional

# プロンプトディレクトリのルート
_PROMPTS_DIR = Path(__file__).resolve().parent
_CONTROL_DIR = _PROMPTS_DIR / "control"
_LOCALES_DIR = _PROMPTS_DIR / "locales"

# user_prompts.json のパス
_USER_PROMPTS_PATH = _PROMPTS_DIR.parent.parent / "user_prompts.json"

# プロンプト名 → user_prompts.json の旧キー名 マッピング
_LEGACY_KEY_MAP = {
    "sleeptime_summarize": "SLEEPTIME_SUMMARIZE_PROMPT",
    "brain_extract_keywords": "BRAIN_EXTRACT_KEYWORDS_PROMPT",
    "brain_summarize_conversation": "BRAIN_SUMMARIZE_CONVERSATION_PROMPT",
    "midterm_digest": "MIDTERM_DIGEST_PROMPT",
    "midterm_relationship": "MIDTERM_RELATIONSHIP_PROMPT",
    "recent_snapshot": "RECENT_SNAPSHOT_PROMPT",
    "web_ui_default_template": "WEB_UI_DEFAULT_TEMPLATE",
}

# Legacy key → prompt name の逆引き
_REVERSE_LEGACY_MAP = {v: k for k, v in _LEGACY_KEY_MAP.items()}

REQUIRED_SECTION_HEADER_KEYS = frozenset(
    {
        "active_nodes",
        "context_prefix_label",
        "conversation_core",
        "current_time",
        "glossary",
        "glossary_long_label",
        "glossary_short_label",
        "key_memory",
        "long_term_memory",
        "memory_usage",
        "memory_usage_note",
        "mid_term_digest",
        "mid_term_memory",
        "note_current_time",
        "note_glossary",
        "note_google_search",
        "note_mid_term_digest",
        "note_rag",
        "note_recent_snapshot",
        "note_session_digest",
        "rag_bullet",
        "rag_cards",
        "rag_date_note",
        "rag_episode_label",
        "rag_raw",
        "rag_raw_note",
        "rag_source_raw",
        "rag_source_raw_note",
        "recent_context",
        "recent_snapshot",
        "related_memory",
        "session_digest",
        "system_instruction",
        "tier_info",
        "tier_mode",
        "tier_topic",
        "web_search",
        "web_search_note",
    }
)

_REQUIRED_SECTION_HEADER_PLACEHOLDERS = {
    "tier_mode": "{tier}",
    "tier_topic": "{topic}",
}


def resolve_prompt_locale(instance_config: Optional[dict] = None) -> str:
    """Resolve prompt locale from an instance config, then the system default."""
    config = instance_config if isinstance(instance_config, dict) else {}
    agent_profile = config.get("agent_profile")
    if isinstance(agent_profile, dict):
        locale = agent_profile.get("locale")
        if isinstance(locale, str) and locale.strip():
            return locale.strip()

    legacy_agent = config.get("agent")
    if isinstance(legacy_agent, dict):
        locale = legacy_agent.get("locale")
        if isinstance(locale, str) and locale.strip():
            return locale.strip()

    try:
        from butly_core.config import SYSTEM_CONFIG

        locale = SYSTEM_CONFIG.get("agent", {}).get("locale", "ja")
    except ImportError:
        locale = "ja"
    return locale if isinstance(locale, str) and locale.strip() else "ja"


def user_prompt_overrides_enabled(instance_config: Optional[dict] = None) -> bool:
    """Return whether local ``user_prompts.json`` overrides should be applied."""
    config = instance_config if isinstance(instance_config, dict) else {}
    prompts_config = config.get("prompts")
    if not isinstance(prompts_config, dict):
        return True
    enabled = prompts_config.get("allow_user_overrides", True)
    return enabled if isinstance(enabled, bool) else True


class PromptLoader:
    """
    control/locales 分離対応のプロンプトローダー。

    control/  — 機能的プロンプト（英語固定、locale 非依存）
    locales/  — 人格プロンプト（locale 別、en フォールバック）
    """

    def __init__(
        self,
        locale: Optional[str] = None,
        *,
        allow_user_overrides: bool = True,
    ):
        if locale is None:
            locale = resolve_prompt_locale()
        self.locale = locale
        self.allow_user_overrides = allow_user_overrides
        self._registry = self._load_registry()
        self._user_prompts = self._load_user_prompts()
        self._section_headers_cache = None

    # ----------------------------------------------------------
    # Registry
    # ----------------------------------------------------------
    def _load_registry(self) -> dict:
        registry_path = _PROMPTS_DIR / "prompt_registry.yaml"
        if registry_path.exists():
            try:
                import yaml

                with open(registry_path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                return data.get("prompts", {}) if data else {}
            except ImportError:
                pass
        return {}

    def _get_location(self, name: str) -> str:
        """registry からプロンプトの location を取得。未登録なら locales。"""
        info = self._registry.get(name, {})
        return info.get("location", "locales")

    # ----------------------------------------------------------
    # User Prompts (後方互換)
    # ----------------------------------------------------------
    def _load_user_prompts(self) -> dict:
        """user_prompts.json を読み込む。"""
        if not self.allow_user_overrides:
            return {}
        if _USER_PROMPTS_PATH.exists():
            try:
                with open(_USER_PROMPTS_PATH, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"[PromptLoader] user_prompts.json error: {e}")
        return {}

    # ----------------------------------------------------------
    # Section Headers
    # ----------------------------------------------------------
    @property
    def section_headers(self) -> dict:
        if self._section_headers_cache is None:
            self._section_headers_cache = self._load_section_headers()
        return self._section_headers_cache

    def _load_section_headers(self) -> dict:
        try:
            import yaml
        except ImportError:
            return {}

        for loc in [self.locale, "en"]:
            path = _LOCALES_DIR / loc / "section_headers.yaml"
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                if not isinstance(data, dict):
                    raise ValueError(f"section headers must be a mapping: {path}")

                invalid = sorted(
                    key for key, value in data.items() if not isinstance(value, str)
                )
                if invalid:
                    raise ValueError(
                        f"section header values must be strings in {path}: "
                        + ", ".join(invalid)
                    )

                missing = sorted(REQUIRED_SECTION_HEADER_KEYS - data.keys())
                if missing:
                    raise ValueError(
                        f"missing required section headers in {path}: "
                        + ", ".join(missing)
                    )

                for key, placeholder in _REQUIRED_SECTION_HEADER_PLACEHOLDERS.items():
                    if placeholder not in data[key]:
                        raise ValueError(
                            f"section header {key!r} in {path} must contain "
                            f"{placeholder!r}"
                        )
                return data
        return {}

    def get_section_header(self, key: str) -> str:
        """セクションヘッダーまたは注釈テキストを取得する。"""
        try:
            return self.section_headers[key]
        except KeyError as exc:
            raise KeyError(
                f"unknown section header {key!r} for locale {self.locale!r}"
            ) from exc

    # ----------------------------------------------------------
    # Template Resolution
    # ----------------------------------------------------------
    def get_template(self, name: str) -> str:
        """
        テンプレート文字列を返す（.format() 未適用）。

        解決順序:
          control プロンプト:
            control/{name}.txt（user_prompts.json のオーバーライド対象外）
          locales プロンプト:
            1. user_prompts.json (後方互換オーバーライド)
            2. locales/{locale}/{name}.txt → locales/en/{name}.txt
        """
        # location を先に判定
        location = self._get_location(name)

        # control プロンプトは user_prompts.json をスキップ（英語固定・翻訳不要）
        if location == "control":
            path = _CONTROL_DIR / f"{name}.txt"
            if path.exists():
                return path.read_text(encoding="utf-8")
            raise FileNotFoundError(f"Control prompt '{name}' not found: {path}")

        # locales プロンプト: user_prompts.json を先にチェック
        legacy_key = _LEGACY_KEY_MAP.get(name)
        if legacy_key and legacy_key in self._user_prompts:
            return self._user_prompts[legacy_key]

        # locales: locale → en フォールバック
        for loc in [self.locale, "en"]:
            path = _LOCALES_DIR / loc / f"{name}.txt"
            if path.exists():
                return path.read_text(encoding="utf-8")

        raise FileNotFoundError(
            f"Locale prompt '{name}' not found "
            f"(locale={self.locale}, searched: locales/{self.locale}/, locales/en/)"
        )

    def get(self, name: str, **kwargs) -> str:
        """テンプレートを読み込み、kwargs で .format() して返す。"""
        template = self.get_template(name)
        if kwargs:
            return template.format(**kwargs)
        return template

    def reload(self):
        """設定変更時にキャッシュをクリアして再読み込み。"""
        self._user_prompts = self._load_user_prompts()
        self._registry = self._load_registry()
        self._section_headers_cache = None


# ===================================================================
# 後方互換: 旧定数名でテンプレート文字列を公開
# ===================================================================

_loader = PromptLoader()

SLEEPTIME_SUMMARIZE_PROMPT = _loader.get_template("sleeptime_summarize")
BRAIN_EXTRACT_KEYWORDS_PROMPT = _loader.get_template("brain_extract_keywords")
BRAIN_SUMMARIZE_CONVERSATION_PROMPT = _loader.get_template(
    "brain_summarize_conversation"
)
MIDTERM_DIGEST_PROMPT = _loader.get_template("midterm_digest")
MIDTERM_RELATIONSHIP_PROMPT = _loader.get_template("midterm_relationship")
RECENT_SNAPSHOT_PROMPT = _loader.get_template("recent_snapshot")
WEB_UI_DEFAULT_TEMPLATE = _loader.get_template("web_ui_default_template")

# USER_PROMPTS_PATH を公開（main.py 等で参照されている）
USER_PROMPTS_PATH = _USER_PROMPTS_PATH

# --- User Prompts Override (後方互換) ---
import sys as _sys

if _USER_PROMPTS_PATH.exists():
    try:
        with open(_USER_PROMPTS_PATH, "r", encoding="utf-8") as _f:
            _user_overrides = json.load(_f)
            _current_module = _sys.modules[__name__]
            for _key, _value in _user_overrides.items():
                if hasattr(_current_module, _key):
                    setattr(_current_module, _key, _value)
        print("[Prompts] Loaded user_prompts.json")
    except Exception as _e:
        print(f"[Prompts] Failed to load user_prompts.json: {_e}")

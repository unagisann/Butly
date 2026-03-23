"""
butly_core.prompts
------------------
Locale ベースでプロンプトテンプレートを読み込むローダー。

解決優先順位:
  1. user_prompts.json（既存のユーザーオーバーライド）
  2. butly_core/prompts/{locale}/{name}.txt
  3. butly_core/prompts/ja/{name}.txt（フォールバック）
"""

import json
from pathlib import Path
from typing import Optional

# プロンプトディレクトリのルート
_PROMPTS_DIR = Path(__file__).resolve().parent

# user_prompts.json のパス
_USER_PROMPTS_PATH = _PROMPTS_DIR.parent.parent / "user_prompts.json"

# プロンプト名 → user_prompts.json の旧キー名 マッピング
_LEGACY_KEY_MAP = {
    "housekeeper_summarize": "HOUSEKEEPER_SUMMARIZE_PROMPT",
    "brain_extract_keywords": "BRAIN_EXTRACT_KEYWORDS_PROMPT",
    "brain_summarize_conversation": "BRAIN_SUMMARIZE_CONVERSATION_PROMPT",
    "midterm_digest": "MIDTERM_DIGEST_PROMPT",
    "midterm_relationship": "MIDTERM_RELATIONSHIP_PROMPT",
    "web_ui_default_template": "WEB_UI_DEFAULT_TEMPLATE",
}


class PromptLoader:
    """
    locale ベースでプロンプトテンプレートを読み込むローダー。

    解決順序:
      1. user_prompts.json の該当キー（既存ユーザーカスタマイズ）
      2. butly_core/prompts/{current_locale}/{name}.txt
      3. butly_core/prompts/ja/{name}.txt（フォールバック）
    """

    def __init__(self, locale: str = "ja"):
        self.locale = locale
        self._user_prompts = self._load_user_prompts()

    def _load_user_prompts(self) -> dict:
        """user_prompts.json を読み込む。"""
        if _USER_PROMPTS_PATH.exists():
            try:
                with open(_USER_PROMPTS_PATH, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"[PromptLoader] user_prompts.json 読み込みエラー: {e}")
        return {}

    def get_template(self, name: str) -> str:
        """
        テンプレート文字列を返す（.format() 未適用）。

        Parameters
        ----------
        name : str
            プロンプト名（拡張子なし、snake_case）

        Returns
        -------
        str
            テンプレート文字列
        """
        # 1. user_prompts.json のオーバーライド
        legacy_key = _LEGACY_KEY_MAP.get(name)
        if legacy_key and legacy_key in self._user_prompts:
            return self._user_prompts[legacy_key]

        # 2. 指定 locale のファイル
        locale_path = _PROMPTS_DIR / self.locale / f"{name}.txt"
        if locale_path.exists():
            return locale_path.read_text(encoding="utf-8")

        # 3. フォールバック: ja
        if self.locale != "ja":
            fallback_path = _PROMPTS_DIR / "ja" / f"{name}.txt"
            if fallback_path.exists():
                return fallback_path.read_text(encoding="utf-8")

        raise FileNotFoundError(
            f"プロンプト '{name}' が見つかりません "
            f"(locale={self.locale}, 検索パス: {_PROMPTS_DIR})"
        )

    def get(self, name: str, **kwargs) -> str:
        """
        テンプレートを読み込み、kwargs で .format() して返す。

        Parameters
        ----------
        name : str
            プロンプト名（拡張子なし、snake_case）
        **kwargs
            テンプレート変数

        Returns
        -------
        str
            フォーマット済みプロンプト文字列
        """
        template = self.get_template(name)
        if kwargs:
            return template.format(**kwargs)
        return template

    def reload_user_prompts(self):
        """user_prompts.json を再読み込みする。"""
        self._user_prompts = self._load_user_prompts()


# ===================================================================
# 後方互換: 旧定数名でテンプレート文字列を公開
# ===================================================================

_loader = PromptLoader()

HOUSEKEEPER_SUMMARIZE_PROMPT = _loader.get_template("housekeeper_summarize")
BRAIN_EXTRACT_KEYWORDS_PROMPT = _loader.get_template("brain_extract_keywords")
BRAIN_SUMMARIZE_CONVERSATION_PROMPT = _loader.get_template("brain_summarize_conversation")
GATEKEEPER_CLASSIFY_PROMPT = _loader.get_template("tier_classifier")
MIDTERM_DIGEST_PROMPT = _loader.get_template("midterm_digest")
MIDTERM_RELATIONSHIP_PROMPT = _loader.get_template("midterm_relationship")
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

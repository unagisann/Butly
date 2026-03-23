"""
test_prompt_loader.py
---------------------
PromptLoader のユニットテスト。
locale解決、フォールバック、format変数展開を検証する。
"""

import pytest

from butly_core.prompts import PromptLoader


class TestPromptLoaderBasic:
    """基本的なテンプレート読み込み"""

    def test_load_ja_template(self):
        """日本語テンプレートを読み込める"""
        loader = PromptLoader(locale="ja")
        template = loader.get_template("tier_classifier")
        assert len(template) > 0
        assert "{user_input}" in template

    def test_load_all_templates(self):
        """全テンプレートが読み込める"""
        loader = PromptLoader(locale="ja")
        names = [
            "tier_classifier",
            "state_updater",
            "search_planner",
            "housekeeper_summarize",
            "brain_extract_keywords",
            "brain_summarize_conversation",
            "midterm_digest",
            "midterm_relationship",
            "web_ui_default_template",
        ]
        for name in names:
            template = loader.get_template(name)
            assert len(template) > 0, f"{name} is empty"

    def test_get_with_format(self):
        """get() でテンプレート変数を展開できる"""
        loader = PromptLoader(locale="ja")
        result = loader.get(
            "brain_extract_keywords",
            user_input="テストの入力",
        )
        assert "テストの入力" in result
        assert "{user_input}" not in result

    def test_nonexistent_template_raises(self):
        """存在しないテンプレートはFileNotFoundError"""
        loader = PromptLoader(locale="ja")
        with pytest.raises(FileNotFoundError):
            loader.get_template("nonexistent_template")


class TestPromptLoaderFallback:
    """locale フォールバックのテスト"""

    def test_unknown_locale_falls_back_to_ja(self):
        """未定義localeは ja にフォールバック"""
        loader = PromptLoader(locale="en")
        template = loader.get_template("tier_classifier")
        assert len(template) > 0

    def test_unknown_locale_unknown_template_raises(self):
        """未定義locale + 未定義テンプレートはエラー"""
        loader = PromptLoader(locale="xx")
        with pytest.raises(FileNotFoundError):
            loader.get_template("absolutely_nonexistent")


class TestBackwardCompat:
    """prompts パッケージの後方互換テスト"""

    def test_legacy_constants_available(self):
        """旧定数名がimportできる"""
        from butly_core.prompts import (
            HOUSEKEEPER_SUMMARIZE_PROMPT,
            BRAIN_EXTRACT_KEYWORDS_PROMPT,
            BRAIN_SUMMARIZE_CONVERSATION_PROMPT,
            GATEKEEPER_CLASSIFY_PROMPT,
            MIDTERM_DIGEST_PROMPT,
            MIDTERM_RELATIONSHIP_PROMPT,
            WEB_UI_DEFAULT_TEMPLATE,
        )
        assert len(HOUSEKEEPER_SUMMARIZE_PROMPT) > 0
        assert len(BRAIN_EXTRACT_KEYWORDS_PROMPT) > 0
        assert len(BRAIN_SUMMARIZE_CONVERSATION_PROMPT) > 0
        assert len(GATEKEEPER_CLASSIFY_PROMPT) > 0
        assert len(MIDTERM_DIGEST_PROMPT) > 0
        assert len(MIDTERM_RELATIONSHIP_PROMPT) > 0
        assert len(WEB_UI_DEFAULT_TEMPLATE) > 0

    def test_user_prompts_path_available(self):
        """USER_PROMPTS_PATH がimportできる"""
        from butly_core.prompts import USER_PROMPTS_PATH
        assert USER_PROMPTS_PATH is not None

    def test_module_import_style(self):
        """from butly_core import prompts スタイルのimport"""
        from butly_core import prompts
        assert hasattr(prompts, "HOUSEKEEPER_SUMMARIZE_PROMPT")
        assert hasattr(prompts, "PromptLoader")

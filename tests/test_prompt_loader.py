"""
test_prompt_loader.py
---------------------
PromptLoader のユニットテスト。
control/locales 分離解決、フォールバック、format変数展開を検証する。
"""

import pytest

from butly_core.prompts import PromptLoader


class TestPromptLoaderBasic:
    """基本的なテンプレート読み込み"""

    def test_load_control_template(self):
        """control プロンプト（機能的）を読み込める"""
        loader = PromptLoader(locale="ja")
        template = loader.get_template("context_classifier")
        assert len(template) > 0
        assert "{user_input}" in template

    def test_load_locales_template(self):
        """locales プロンプト（人格）を読み込める"""
        loader = PromptLoader(locale="ja")
        template = loader.get_template("sleeptime_summarize")
        assert len(template) > 0
        assert "{agent_name}" in template

    def test_load_all_templates(self):
        """全テンプレートが読み込める"""
        loader = PromptLoader(locale="ja")
        names = [
            "context_classifier",
            "state_updater",
            "sleeptime_summarize",
            "brain_extract_keywords",
            "brain_summarize_conversation",
            "midterm_digest",
            "midterm_relationship",
            "recent_snapshot",
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

    def test_en_locale_loads_control(self):
        """en locale で control プロンプトが読み込める"""
        loader = PromptLoader(locale="en")
        template = loader.get_template("context_classifier")
        assert len(template) > 0

    def test_en_locale_loads_locales(self):
        """en locale で locales プロンプトが読み込める"""
        loader = PromptLoader(locale="en")
        template = loader.get_template("sleeptime_summarize")
        assert len(template) > 0

    def test_unknown_locale_falls_back_to_en(self):
        """未定義localeは en にフォールバック"""
        loader = PromptLoader(locale="ko")
        template = loader.get_template("sleeptime_summarize")
        assert len(template) > 0

    def test_unknown_locale_unknown_template_raises(self):
        """未定義locale + 未定義テンプレートはエラー"""
        loader = PromptLoader(locale="xx")
        with pytest.raises(FileNotFoundError):
            loader.get_template("absolutely_nonexistent")

    def test_control_prompts_are_locale_independent(self):
        """control プロンプトは locale に依存しない（同一内容）"""
        loader_ja = PromptLoader(locale="ja")
        loader_en = PromptLoader(locale="en")
        assert loader_ja.get_template("context_classifier") == loader_en.get_template("context_classifier")


class TestSectionHeaders:
    """section_headers のテスト"""

    def test_ja_section_headers(self):
        """日本語 section_headers が読み込める"""
        loader = PromptLoader(locale="ja")
        assert "KEY MEMORY" in loader.get_section_header("key_memory")
        assert "根幹記憶" in loader.get_section_header("key_memory")

    def test_en_section_headers(self):
        """英語 section_headers が読み込める"""
        loader = PromptLoader(locale="en")
        assert "KEY MEMORY" in loader.get_section_header("key_memory")

    @pytest.mark.parametrize("locale", ["ja", "en"])
    def test_context_prefix_has_no_global_priority_note(self, locale):
        """背景ラベルに記憶レイヤー間の固定優先順位を付けない"""
        loader = PromptLoader(locale=locale)
        assert "note_context_prefix" not in loader.section_headers

    def test_section_header_fallback(self):
        """未定義キーはキー名そのものを返す"""
        loader = PromptLoader(locale="ja")
        assert loader.get_section_header("nonexistent_key") == "nonexistent_key"

    def test_tier_mode_format(self):
        """tier_mode はフォーマット可能"""
        loader = PromptLoader(locale="ja")
        tier_mode = loader.get_section_header("tier_mode")
        assert "{tier}" in tier_mode
        assert "reflex" in tier_mode.format(tier="reflex")


class TestBackwardCompat:
    """prompts パッケージの後方互換テスト"""

    def test_legacy_constants_available(self):
        """旧定数名がimportできる"""
        from butly_core.prompts import (
            SLEEPTIME_SUMMARIZE_PROMPT,
            BRAIN_EXTRACT_KEYWORDS_PROMPT,
            BRAIN_SUMMARIZE_CONVERSATION_PROMPT,
            MIDTERM_DIGEST_PROMPT,
            MIDTERM_RELATIONSHIP_PROMPT,
            RECENT_SNAPSHOT_PROMPT,
            WEB_UI_DEFAULT_TEMPLATE,
        )
        assert len(SLEEPTIME_SUMMARIZE_PROMPT) > 0
        assert len(BRAIN_EXTRACT_KEYWORDS_PROMPT) > 0
        assert len(BRAIN_SUMMARIZE_CONVERSATION_PROMPT) > 0
        assert len(MIDTERM_DIGEST_PROMPT) > 0
        assert len(MIDTERM_RELATIONSHIP_PROMPT) > 0
        assert len(RECENT_SNAPSHOT_PROMPT) > 0
        assert len(WEB_UI_DEFAULT_TEMPLATE) > 0

    def test_user_prompts_path_available(self):
        """USER_PROMPTS_PATH がimportできる"""
        from butly_core.prompts import USER_PROMPTS_PATH
        assert USER_PROMPTS_PATH is not None

    def test_module_import_style(self):
        """from butly_core import prompts スタイルのimport"""
        from butly_core import prompts
        assert hasattr(prompts, "SLEEPTIME_SUMMARIZE_PROMPT")
        assert hasattr(prompts, "PromptLoader")

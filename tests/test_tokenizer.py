"""
test_tokenizer.py
------------------
count_tokens() のユニットテスト。
"""

from butly_core.core.tokenizer import count_tokens


class TestCountTokens:
    def test_count_tokens_basic(self):
        """基本的なテキストでトークン数が正の整数"""
        result = count_tokens("Hello, world!")
        assert isinstance(result, int)
        assert result > 0

    def test_count_tokens_empty(self):
        """空文字列 → 0"""
        assert count_tokens("") == 0

    def test_count_tokens_japanese(self):
        """日本語テキストのトークン数が妥当な範囲"""
        text = "今日は天気がいいですね。散歩に行きましょう。"
        result = count_tokens(text)
        assert result > 0
        # 日本語は1文字あたり1-3トークン程度が目安
        assert result <= len(text) * 3

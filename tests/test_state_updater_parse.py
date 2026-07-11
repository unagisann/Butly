"""StateUpdater._parse_response のコードフェンス耐性テスト。

Qwen3 等のローカルLLMは JSON を ```json ... ``` で囲む。閉じフェンスが
欠落（トークン切れ・省略）したケースでもパースできることを回帰で守る。
"""

from butly_core.core.gatekeeper.state_updater import StateUpdater


def _parse(raw: str) -> dict:
    return StateUpdater()._parse_response(raw)


class TestParseResponseFences:
    def test_plain_json(self):
        assert _parse('{"topic": "pottery", "mood": "happy"}') == {
            "topic": "pottery",
            "mood": "happy",
        }

    def test_closed_json_fence(self):
        raw = '```json\n{"topic": "pottery", "mood": "calm"}\n```'
        assert _parse(raw) == {"topic": "pottery", "mood": "calm"}

    def test_unclosed_fence_still_parses(self):
        # 以前はここで JSONパースエラーになっていた（閉じ ``` が無い）
        raw = '```json\n{"topic": "pottery", "mood": "proud"}'
        assert _parse(raw) == {"topic": "pottery", "mood": "proud"}

    def test_bare_fence_without_lang(self):
        raw = '```\n{"topic": "camping", "mood": null}\n```'
        assert _parse(raw) == {"topic": "camping", "mood": None}

    def test_prose_around_json(self):
        raw = 'Here is the state:\n{"topic": "race", "mood": "excited"} done.'
        assert _parse(raw) == {"topic": "race", "mood": "excited"}

    def test_null_and_none_strings_normalized(self):
        raw = '{"topic": "None", "mood": "null"}'
        assert _parse(raw) == {"topic": None, "mood": None}

    def test_truly_broken_returns_default(self):
        # { で始まるが } が無く JSON にならない → デフォルトへフォールバック
        result = _parse('```json\n{"topic": "pottery"')
        assert result == {"topic": None, "mood": None}

    def test_empty_returns_default(self):
        assert _parse("") == {"topic": None, "mood": None}

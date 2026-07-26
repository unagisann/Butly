"""Embedding プロファイル（モデル別 prefix 規約）のテスト。

背景: nomic-embed-text は ``search_query:`` / ``search_document:`` prefix を
要求するが、以前は付与していなかった。その結果すべての埋め込みが 1 つの円錐に
潰れ、カード同士の cosine (0.756) が質問と正解カードの cosine (0.733) を上回る
＝ランキングが機能しない状態だった。
"""

from unittest.mock import MagicMock

import pytest

from butly_core.llm.embedding_profiles import (
    DOCUMENT,
    QUERY,
    EmbeddingProfile,
    apply_prefix,
    describe,
    fingerprint,
    get_profile,
    infer_profile,
    known_dims,
    list_profiles,
    resolve_profile,
)


class TestInferProfile:
    @pytest.mark.parametrize(
        "model_name,expected",
        [
            ("nomic-embed-text", "nomic"),
            ("nomic-embed-text-v1.5", "nomic"),
            ("models/nomic-embed-text", "nomic"),
            ("nomic-ai/nomic-embed-text-v1.5", "nomic"),
            ("multilingual-e5-large", "e5"),
            ("intfloat/multilingual-e5-small", "e5"),
            ("bge-m3", "bge-m3"),
            ("bge-large-en-v1.5", "bge-instruct"),
            ("qwen3-embedding-8b", "qwen3-embedding"),
            ("text-embedding-3-large", "openai-large"),
            ("text-embedding-3-small", "openai"),
            ("gemini-embedding-2", "gemini"),
            ("text-embedding-004", "gemini-004"),
        ],
    )
    def test_known_models(self, model_name, expected):
        assert infer_profile(model_name).id == expected

    @pytest.mark.parametrize("model_name", ["", None, "some-unknown-model", 123])
    def test_unknown_falls_back_to_plain(self, model_name):
        assert infer_profile(model_name).id == "plain"

    def test_longest_pattern_wins(self):
        """bge-m3 は bge-instruct の英語パターンに巻き込まれない。"""
        assert infer_profile("bge-m3").query_prefix == ""
        assert infer_profile("bge-large-en-v1.5").query_prefix != ""


class TestResolveProfile:
    def test_auto_is_same_as_infer(self):
        conf = {"model_name": "nomic-embed-text", "profile": "auto"}
        assert resolve_profile(conf).id == "nomic"

    def test_missing_profile_key_infers(self):
        assert resolve_profile({"model_name": "nomic-embed-text"}).id == "nomic"

    def test_explicit_profile_overrides_model_name(self):
        conf = {"model_name": "nomic-embed-text", "profile": "plain"}
        profile = resolve_profile(conf)
        assert profile.id == "plain"
        assert profile.query_prefix == ""

    def test_unknown_profile_id_warns_and_falls_back(self, capsys):
        conf = {"model_name": "nomic-embed-text", "profile": "does-not-exist"}
        assert resolve_profile(conf).id == "nomic"
        assert "unknown embedding profile" in capsys.readouterr().out

    def test_explicit_prefixes_win(self):
        conf = {
            "model_name": "some-new-model",
            "query_prefix": "q: ",
            "document_prefix": "d: ",
        }
        profile = resolve_profile(conf)
        assert profile.query_prefix == "q: "
        assert profile.document_prefix == "d: "

    def test_partial_prefix_override_keeps_profile_default(self):
        """query だけ差し替えても document 側はプロファイル既定が残る。"""
        conf = {"model_name": "nomic-embed-text", "query_prefix": "custom: "}
        profile = resolve_profile(conf)
        assert profile.query_prefix == "custom: "
        assert profile.document_prefix == "search_document: "

    def test_empty_string_prefix_disables_one_side(self):
        conf = {"model_name": "nomic-embed-text", "query_prefix": ""}
        assert resolve_profile(conf).query_prefix == ""

    def test_none_conf(self):
        assert resolve_profile(None).id == "plain"


class TestApplyPrefix:
    def test_query_and_document_differ(self):
        conf = {"model_name": "nomic-embed-text"}
        assert apply_prefix("hello", conf, QUERY) == "search_query: hello"
        assert apply_prefix("hello", conf, DOCUMENT) == "search_document: hello"

    def test_plain_model_is_untouched(self):
        conf = {"model_name": "text-embedding-3-small"}
        assert apply_prefix("hello", conf, QUERY) == "hello"

    def test_empty_text_untouched(self):
        assert apply_prefix("", {"model_name": "nomic-embed-text"}, QUERY) == ""

    def test_no_double_prefix(self):
        conf = {"model_name": "nomic-embed-text"}
        once = apply_prefix("hello", conf, QUERY)
        assert apply_prefix(once, conf, QUERY) == once

    def test_japanese_text(self):
        """prefix は言語非依存 — 日本語でも同じ規約が乗る。"""
        conf = {"model_name": "multilingual-e5-large"}
        assert apply_prefix("いつ引っ越した？", conf, QUERY) == "query: いつ引っ越した？"
        assert apply_prefix("2021年に引っ越した", conf, DOCUMENT) == (
            "passage: 2021年に引っ越した"
        )


class TestFingerprint:
    def test_model_and_profile(self):
        fp = fingerprint({"model_name": "nomic-embed-text"})
        assert fp == {"model_name": "nomic-embed-text", "profile": "nomic", "dim": 768}

    def test_profile_change_changes_fingerprint(self):
        base = fingerprint({"model_name": "nomic-embed-text"})
        plain = fingerprint({"model_name": "nomic-embed-text", "profile": "plain"})
        assert base["profile"] != plain["profile"]

    def test_normalizes_model_name(self):
        fp = fingerprint({"model_name": "models/nomic-embed-text"})
        assert fp["model_name"] == "nomic-embed-text"


class TestRegistryHelpers:
    def test_list_and_get(self):
        ids = {p.id for p in list_profiles()}
        assert {"plain", "nomic", "e5"} <= ids
        assert get_profile("NOMIC").id == "nomic"
        assert get_profile("nope") is None

    def test_known_dims_covers_configured_models(self):
        dims = known_dims()
        assert dims["nomic-embed"] == 768
        assert dims["gemini-embedding"] == 3072

    def test_describe_is_single_line(self):
        line = describe({"model_name": "nomic-embed-text"})
        assert "profile=nomic" in line
        assert "\n" not in line

    def test_profile_apply_is_pure(self):
        profile = EmbeddingProfile(id="t", query_prefix="Q: ")
        assert profile.apply("x", QUERY) == "Q: x"
        assert profile.apply("x", DOCUMENT) == "x"


class TestBrainAppliesQueryPrefix:
    """検索側 (Brain.get_embedding) が query prefix を通すこと。"""

    def test_query_prefix_reaches_provider(self, tmp_path, monkeypatch):
        from butly_core.core.brain import ButlyBrain

        brain = ButlyBrain(tmp_path)
        captured = []

        def fake_create(conf):
            provider = MagicMock()
            provider.embed.side_effect = lambda text, config=None: (
                captured.append(text) or [0.1]
            )
            return provider

        monkeypatch.setattr(
            "butly_core.llm.factory.ProviderFactory.create", fake_create
        )

        brain.get_embedding("いつ引っ越した？", {"model_name": "nomic-embed-text"})

        assert captured == ["search_query: いつ引っ越した？"]

    def test_plain_model_unchanged(self, tmp_path, monkeypatch):
        from butly_core.core.brain import ButlyBrain

        brain = ButlyBrain(tmp_path)
        captured = []

        def fake_create(conf):
            provider = MagicMock()
            provider.embed.side_effect = lambda text, config=None: (
                captured.append(text) or [0.1]
            )
            return provider

        monkeypatch.setattr(
            "butly_core.llm.factory.ProviderFactory.create", fake_create
        )

        brain.get_embedding("hello", {"model_name": "text-embedding-3-small"})

        assert captured == ["hello"]


class TestSleeptimeAppliesDocumentPrefix:
    """書き込み側 (Sleeptime) が document prefix を通すこと。

    ここが query prefix とズレると、保存済みベクトルと検索クエリが別空間に
    なり、RAG が無言で劣化する（例外もログも出ない種類の事故）。
    """

    def _sleeptime(self, tmp_path, monkeypatch, conf):
        from sleeptime import ButlySleeptime

        st = ButlySleeptime(
            base_dir=tmp_path,
            instances_dir=tmp_path / "butly_core" / "instances",
        )
        monkeypatch.setattr(st, "resolve_embedding_conf", lambda name=None: conf)
        return st

    def test_document_prefix_reaches_provider(self, tmp_path, monkeypatch):
        captured = []

        def fake_create(conf):
            provider = MagicMock()
            provider.embed.side_effect = lambda text, config=None: (
                captured.append(text) or [0.1]
            )
            return provider

        monkeypatch.setattr(
            "butly_core.llm.factory.ProviderFactory.create", fake_create
        )
        st = self._sleeptime(
            tmp_path, monkeypatch, {"model_name": "nomic-embed-text"}
        )

        st.generate_embedding("Title: Camping\nTags: trip\nSummary: June 2023")

        assert captured[0].startswith("search_document: Title: Camping")

    def test_write_and_read_prefixes_differ(self, tmp_path, monkeypatch):
        """同じ本文でも書き込み側と検索側で別の prefix が付く。"""
        from butly_core.core.brain import ButlyBrain

        conf = {"model_name": "nomic-embed-text"}
        captured = []

        def fake_create(c):
            provider = MagicMock()
            provider.embed.side_effect = lambda text, config=None: (
                captured.append(text) or [0.1]
            )
            return provider

        monkeypatch.setattr(
            "butly_core.llm.factory.ProviderFactory.create", fake_create
        )

        st = self._sleeptime(tmp_path, monkeypatch, conf)
        st.generate_embedding("camping in June")
        ButlyBrain(tmp_path).get_embedding("camping in June", conf)

        assert captured == [
            "search_document: camping in June",
            "search_query: camping in June",
        ]

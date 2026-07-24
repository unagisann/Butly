"""
test_raw_reference.py
---------------------
raw_reference（RAG カード → RAW 会話原文の逆引き・抜粋構築）のユニットテスト。
API キー不要 — ファイルシステムのみ使用。
"""

import json
from pathlib import Path

import pytest

from butly_core.core.gatekeeper.raw_reference import (
    collect_source_refs,
    resolve_raw_reference,
)

INST = "test_instance"


def _write_raw(
    instances_dir: Path,
    date: str,
    name: str,
    messages: list,
    timestamp: str,
    folder: str = "2_knowledgeized",
) -> None:
    if folder == "2_knowledgeized":
        dest = instances_dir / INST / "memory_archive" / "2_knowledgeized" / date
    else:
        dest = instances_dir / INST / "memory_archive" / "1_integrated"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / name).write_text(
        json.dumps({"timestamp": timestamp, "messages": messages}, ensure_ascii=False),
        encoding="utf-8",
    )


def _card(cid: str, date: str, files: list, score: float = 0.8) -> dict:
    return {
        "id": cid,
        "title": f"カード{cid}",
        "summary": "要約",
        "score": score,
        "source_date": date,
        "source_files": json.dumps(files, ensure_ascii=False),
    }


@pytest.fixture
def instances_dir(tmp_path: Path) -> Path:
    d = tmp_path / "instances"
    d.mkdir()
    return d


class TestCollectSourceRefs:
    def test_dedup_preserves_score_order(self):
        """同一チャンク由来の重複ファイルは dedup し、カードのスコア順を保つ"""
        candidates = [
            _card("a", "2023-05-08", ["s1.json", "s2.json"]),
            _card("b", "2023-05-08", ["s1.json", "s2.json"]),  # 同一チャンク
            _card("c", "2023-06-09", ["s3.json"]),
        ]
        refs = collect_source_refs(candidates, INST)
        assert refs == [
            (INST, "2023-05-08", "s1.json"),
            (INST, "2023-05-08", "s2.json"),
            (INST, "2023-06-09", "s3.json"),
        ]

    def test_source_instance_overrides_default(self):
        candidates = [
            {**_card("a", "2023-05-08", ["s1.json"]), "source_instance": "other"},
        ]
        refs = collect_source_refs(candidates, INST)
        assert refs == [("other", "2023-05-08", "s1.json")]

    def test_missing_or_broken_source_files_skipped(self):
        candidates = [
            {"id": "a", "title": "t", "summary": "s"},  # source_files 無し
            {**_card("b", "2023-05-08", []), "source_files": "{broken json"},
            {**_card("c", "2023-05-08", []), "source_files": json.dumps({"x": 1})},
            _card("d", "2023-06-09", ["ok.json"]),
        ]
        refs = collect_source_refs(candidates, INST)
        assert refs == [(INST, "2023-06-09", "ok.json")]

    def test_list_form_accepted(self):
        """DB 由来でない list 形式の source_files も受け付ける"""
        candidates = [{**_card("a", "2023-05-08", []), "source_files": ["s1.json"]}]
        refs = collect_source_refs(candidates, INST)
        assert refs == [(INST, "2023-05-08", "s1.json")]

    def test_top_k_limits_to_top_cards(self):
        """top_k=1 は最上位カードの source_files だけを採る"""
        candidates = [
            _card("a", "2023-05-08", ["s1.json"], score=0.9),
            _card("b", "2023-06-09", ["s2.json"], score=0.5),
            _card("c", "2023-07-10", ["s3.json"], score=0.4),
        ]
        assert collect_source_refs(candidates, INST, top_k=1) == [
            (INST, "2023-05-08", "s1.json"),
        ]
        assert collect_source_refs(candidates, INST, top_k=2) == [
            (INST, "2023-05-08", "s1.json"),
            (INST, "2023-06-09", "s2.json"),
        ]

    def test_top_k_zero_or_none_is_all(self):
        candidates = [
            _card("a", "2023-05-08", ["s1.json"], score=0.9),
            _card("b", "2023-06-09", ["s2.json"], score=0.5),
        ]
        expected = [
            (INST, "2023-05-08", "s1.json"),
            (INST, "2023-06-09", "s2.json"),
        ]
        assert collect_source_refs(candidates, INST, top_k=None) == expected
        assert collect_source_refs(candidates, INST, top_k=0) == expected

    def test_top_k_skips_cards_without_source_files(self):
        """source_files を持たない上位カードはスロットを消費しない（実質 raw を持つ上位 K 枚）"""
        candidates = [
            {"id": "a", "title": "t", "summary": "s", "score": 0.9},  # raw 無し
            _card("b", "2023-06-09", ["s2.json"], score=0.5),
            _card("c", "2023-07-10", ["s3.json"], score=0.4),
        ]
        assert collect_source_refs(candidates, INST, top_k=1) == [
            (INST, "2023-06-09", "s2.json"),
        ]

    def test_top_k_shared_chunk_does_not_waste_slot(self):
        """上位と同一チャンク（重複ファイル）はスロットを消費せず次カードへ進む"""
        candidates = [
            _card("a", "2023-05-08", ["s1.json"], score=0.9),
            _card("b", "2023-05-08", ["s1.json"], score=0.8),  # 同一チャンク
            _card("c", "2023-06-09", ["s2.json"], score=0.5),
        ]
        # top_k=2: カードa(s1)で1枠、bは全重複で枠を使わず、cで2枠目
        assert collect_source_refs(candidates, INST, top_k=2) == [
            (INST, "2023-05-08", "s1.json"),
            (INST, "2023-06-09", "s2.json"),
        ]


class TestResolveRawReference:
    def test_basic_rendering(self, instances_dir):
        """原文がラベル付きで描画され、ヘッダに会話日時が入る"""
        _write_raw(
            instances_dir,
            "2023-05-08",
            "s1.json",
            [
                {"role": "user", "parts": ["先週の日曜に引っ越したよ"]},
                {"role": "model", "parts": ["おめでとうございます"]},
            ],
            "2023-05-08T10:23:00.123456",
        )
        result = resolve_raw_reference(
            [_card("a", "2023-05-08", ["s1.json"])],
            instances_dir,
            INST,
            max_chars=6000,
            user_name="ゆうき",
            agent_name="バトリー",
        )
        assert result is not None
        assert "--- 2023-05-08 10:23:00 ---" in result["text"]
        assert "ゆうき: 先週の日曜に引っ越したよ" in result["text"]
        assert "バトリー: おめでとうございます" in result["text"]
        assert result["files"] == ["s1.json"]
        assert result["missing"] == []
        assert result["truncated"] is False
        assert result["chars"] == len(result["text"])

    def test_top_k_injects_only_top_card_raw(self, instances_dir):
        """top_k=1 は最上位カードの原文だけを注入する（他はサマリで渡す想定）"""
        _write_raw(
            instances_dir, "2023-05-08", "s1.json",
            [{"role": "user", "parts": ["最上位の記憶"]}], "2023-05-08T09:00:00",
        )
        _write_raw(
            instances_dir, "2023-06-09", "s2.json",
            [{"role": "user", "parts": ["二番目の記憶"]}], "2023-06-09T09:00:00",
        )
        candidates = [
            _card("a", "2023-05-08", ["s1.json"], score=0.9),
            _card("b", "2023-06-09", ["s2.json"], score=0.5),
        ]
        top1 = resolve_raw_reference(
            candidates, instances_dir, INST, max_chars=6000, top_k=1
        )
        assert top1["files"] == ["s1.json"]
        assert "最上位の記憶" in top1["text"]
        assert "二番目の記憶" not in top1["text"]
        assert top1["top_k"] == 1
        # top_k=0（全件）では両方入る
        allrows = resolve_raw_reference(
            candidates, instances_dir, INST, max_chars=6000, top_k=0
        )
        assert allrows["files"] == ["s1.json", "s2.json"]

    def test_chronological_display_order(self, instances_dir):
        """スコア順で収集しても表示は時系列順になる"""
        _write_raw(
            instances_dir, "2023-06-09", "s2.json",
            [{"role": "user", "parts": ["6月の話"]}], "2023-06-09T09:00:00",
        )
        _write_raw(
            instances_dir, "2023-05-08", "s1.json",
            [{"role": "user", "parts": ["5月の話"]}], "2023-05-08T09:00:00",
        )
        # スコア上位カードが 6 月、下位が 5 月
        result = resolve_raw_reference(
            [
                _card("a", "2023-06-09", ["s2.json"], score=0.9),
                _card("b", "2023-05-08", ["s1.json"], score=0.5),
            ],
            instances_dir,
            INST,
            max_chars=6000,
        )
        assert result["text"].find("5月の話") < result["text"].find("6月の話")

    def test_budget_greedy_skip_prefers_top_cards(self, instances_dir):
        """上限超過ファイルは greedy skip され、上位カードの原文が残る"""
        _write_raw(
            instances_dir, "2023-05-08", "big.json",
            [{"role": "user", "parts": ["あ" * 50]}], "2023-05-08T09:00:00",
        )
        _write_raw(
            instances_dir, "2023-06-09", "huge.json",
            [{"role": "user", "parts": ["い" * 5000]}], "2023-06-09T09:00:00",
        )
        result = resolve_raw_reference(
            [
                _card("a", "2023-05-08", ["big.json"], score=0.9),
                _card("b", "2023-06-09", ["huge.json"], score=0.5),
            ],
            instances_dir,
            INST,
            max_chars=200,
        )
        assert result["files"] == ["big.json"]
        assert result["truncated"] is True
        assert result["chars"] <= 200

    def test_first_file_truncated_when_nothing_fits(self, instances_dir):
        """1 件も収まらない場合は先頭ファイルを切り詰めて注入する"""
        _write_raw(
            instances_dir, "2023-05-08", "huge.json",
            [{"role": "user", "parts": ["あ" * 5000]}], "2023-05-08T09:00:00",
        )
        result = resolve_raw_reference(
            [_card("a", "2023-05-08", ["huge.json"])],
            instances_dir,
            INST,
            max_chars=100,
        )
        assert result is not None
        assert result["files"] == ["huge.json"]
        assert result["truncated"] is True
        assert "…（文字数上限で省略）" in result["text"]

    def test_zero_max_chars_is_unlimited(self, instances_dir):
        _write_raw(
            instances_dir, "2023-05-08", "big.json",
            [{"role": "user", "parts": ["あ" * 5000]}], "2023-05-08T09:00:00",
        )
        result = resolve_raw_reference(
            [_card("a", "2023-05-08", ["big.json"])],
            instances_dir,
            INST,
            max_chars=0,
        )
        assert result["truncated"] is False
        assert result["chars"] > 5000

    def test_integrated_fallback(self, instances_dir):
        """2_knowledgeized に無いファイルは 1_integrated から読む"""
        _write_raw(
            instances_dir, "", "pending.json",
            [{"role": "user", "parts": ["未処理の会話"]}],
            "2023-05-08T09:00:00",
            folder="1_integrated",
        )
        result = resolve_raw_reference(
            [_card("a", "2023-05-08", ["pending.json"])],
            instances_dir,
            INST,
            max_chars=6000,
        )
        assert result is not None
        assert "未処理の会話" in result["text"]

    def test_missing_files_reported(self, instances_dir):
        _write_raw(
            instances_dir, "2023-05-08", "s1.json",
            [{"role": "user", "parts": ["ある方"]}], "2023-05-08T09:00:00",
        )
        result = resolve_raw_reference(
            [_card("a", "2023-05-08", ["s1.json", "gone.json"])],
            instances_dir,
            INST,
            max_chars=6000,
        )
        assert result["files"] == ["s1.json"]
        assert result["missing"] == ["gone.json"]

    def test_none_when_nothing_resolvable(self, instances_dir):
        assert (
            resolve_raw_reference(
                [_card("a", "2023-05-08", ["gone.json"])],
                instances_dir,
                INST,
                max_chars=6000,
            )
            is None
        )
        assert (
            resolve_raw_reference(
                [{"id": "a", "title": "t", "summary": "s"}],
                instances_dir,
                INST,
                max_chars=6000,
            )
            is None
        )

    def test_path_traversal_components_rejected(self, instances_dir):
        """DB 由来のファイル名・日付にパス区切りが混入しても外を読まない"""
        outside = instances_dir.parent / "secret.json"
        outside.write_text(
            json.dumps(
                {"timestamp": "2023-05-08T09:00:00",
                 "messages": [{"role": "user", "parts": ["秘密"]}]}
            ),
            encoding="utf-8",
        )
        candidates = [
            _card("a", "2023-05-08", ["../../../secret.json"]),
            {**_card("b", "", ["s1.json"]), "source_date": "../.."},
        ]
        result = resolve_raw_reference(
            candidates, instances_dir, INST, max_chars=6000
        )
        assert result is None

    def test_multi_speaker_labels(self, instances_dir):
        """複数話者ログは display_name 付きラベルで描画される"""
        _write_raw(
            instances_dir,
            "2023-05-08",
            "s1.json",
            [
                {
                    "role": "user",
                    "parts": ["こんにちは"],
                    "meta": {"person_id": "p_discord_1", "display_name": "たろう"},
                },
                {"role": "user", "parts": ["やあ"]},  # meta 無し = owner
            ],
            "2023-05-08T09:00:00",
        )
        result = resolve_raw_reference(
            [_card("a", "2023-05-08", ["s1.json"])],
            instances_dir,
            INST,
            max_chars=6000,
            user_name="ゆうき",
        )
        assert "「たろう」: こんにちは" in result["text"]

    def test_english_locale_uses_plain_speaker_labels(self, instances_dir):
        """English evaluation prompts do not inherit Japanese speaker punctuation."""
        _write_raw(
            instances_dir,
            "2023-05-08",
            "s1.json",
            [
                {
                    "role": "user",
                    "parts": ["Hello"],
                    "meta": {"person_id": "p_1", "display_name": "Caroline"},
                },
                {
                    "role": "user",
                    "parts": ["Hi"],
                    "meta": {"person_id": "p_2", "display_name": "Melanie"},
                },
            ],
            "2023-05-08T09:00:00",
        )

        result = resolve_raw_reference(
            [_card("a", "2023-05-08", ["s1.json"])],
            instances_dir,
            INST,
            max_chars=6000,
            locale="en",
        )

        assert "Caroline: Hello" in result["text"]
        assert "「" not in result["text"]

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from evals.dialogue_ab import (
    DialogueABConfig,
    DialogueABError,
    _configure_policy,
    build_dialogue_scores,
    load_dialogue_dataset,
)


FIXTURE = Path(__file__).parents[1] / "data" / "ja_dialogue_ab_prompts_v1.json"


def test_loads_japanese_dialogue_fixture():
    dataset = load_dialogue_dataset(FIXTURE)

    assert dataset.dataset_id == "ja-dialogue-ab-prompts-v1"
    assert dataset.locale == "ja"
    assert len(dataset.memory_seed) == 10
    assert len(dataset.prompts) == 30
    assert {
        prompt.category for prompt in dataset.prompts
    } == {
        "memory_required",
        "memory_irrelevant",
        "memory_optional",
    }


def test_rejects_unknown_target_memory(tmp_path):
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["prompts"][0]["target_memory_ids"] = ["missing"]
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(DialogueABError, match="unknown memory seeds"):
        load_dialogue_dataset(path)


def test_build_scores_compares_policy_arms():
    dataset = load_dialogue_dataset(FIXTURE)
    prompt = dataset.prompts[0]
    results = [
        {
            "policy": "intent_gated",
            "prompt_id": prompt.prompt_id,
            "category": prompt.category,
            "response": "分かりません",
            "rag_triggered": False,
            "search_executed": True,
            "prompt_tokens": 100,
            "total_prompt_tokens": 150,
            "latency_ms": 1000,
            "target_term_recall": 0.0,
            "seed_term_mentions": [],
        },
        {
            "policy": "candidates",
            "prompt_id": prompt.prompt_id,
            "category": prompt.category,
            "response": "こむぎは三毛猫の女の子です",
            "rag_triggered": True,
            "search_executed": True,
            "prompt_tokens": 160,
            "total_prompt_tokens": 220,
            "latency_ms": 1200,
            "target_term_recall": 1.0,
            "seed_term_mentions": ["こむぎ", "三毛猫", "女の子"],
        },
    ]

    scores = build_dialogue_scores(
        dataset,
        results,
        run_id="dialogue-v1",
        knowledge_cards=10,
    )

    assert scores["run_type"] == "dialogue_ab"
    assert scores["comparison"]["prompt_tokens_mean_delta"] == 60.0
    assert scores["comparison"]["required_target_recall_delta"] == 1.0
    first = scores["prompts"][0]
    assert first["prompt_tokens_delta"] == 60.0
    assert first["response_changed"] is True


class TestPolicySearchLimits:
    def test_config_rejects_non_positive_arm_limit(self, tmp_path):
        with pytest.raises(
            DialogueABError,
            match="candidates_search_limit must be a positive",
        ):
            DialogueABConfig(
                dataset_path=tmp_path / "dataset.json",
                output_dir=tmp_path,
                run_id="run",
                profile_path=tmp_path / "profile.yaml",
                candidates_search_limit=0,
            )

    def test_policy_override_keeps_retrieval_execution_and_sets_arm_limit(self):
        class FakeInstanceManager:
            def __init__(self):
                self.config = {
                    "memory_probe": {
                        "retrieval_execution": "intent_gated",
                        "vector_search_limit": 3,
                    }
                }

            def get_instance_config(self, _instance_name):
                return self.config

            def update_instance_config(self, _instance_name, config):
                self.config = config
                return True, "ok"

        manager = FakeInstanceManager()
        runtime = SimpleNamespace(instance_manager=manager)

        _configure_policy(
            runtime,
            "ja_dialogue_ab",
            "candidates",
            search_limit=2,
        )

        assert manager.config["memory_probe"] == {
            "retrieval_execution": "intent_gated",
            "injection_policy": "candidates",
            "vector_search_limit": 2,
        }


# ===================================================================
# 既存インスタンスを種にする（--seed-instance / memory_source）
# ===================================================================

JARVIS_CANDIDATES = (
    Path(__file__).parents[1] / "data" / "ja_dialogue_ab_jarvis_candidates.json"
)


def _instance_dataset(tmp_path: Path, **overrides) -> Path:
    payload = {
        "schema_version": 1,
        "dataset_id": "ja-instance-seed",
        "locale": "ja",
        "memory_source": {"type": "instance", "name": "Jarvis"},
        "prompts": {
            "memory_required": [
                {
                    "id": "req_01",
                    "prompt": "前に決めたホスト名は？",
                    "expected_terms": ["jarvis"],
                    "source_card_id": "card_001",
                }
            ],
            "memory_irrelevant": [
                {"id": "irr_01", "prompt": "おはよう。"}
            ],
        },
    }
    payload.update(overrides)
    path = tmp_path / "instance_dataset.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class TestInstanceMemorySource:
    def test_loads_dataset_without_memory_seed(self, tmp_path):
        dataset = load_dialogue_dataset(_instance_dataset(tmp_path))

        assert dataset.seeds_from_instance is True
        assert dataset.memory_source.name == "Jarvis"
        assert dataset.memory_seed == ()
        assert len(dataset.prompts) == 2

    def test_category_dict_and_defaults(self, tmp_path):
        dataset = load_dialogue_dataset(_instance_dataset(tmp_path))
        by_id = {p.prompt_id: p for p in dataset.prompts}

        # カテゴリ辞書のキーが category になる
        assert by_id["req_01"].category == "memory_required"
        assert by_id["irr_01"].category == "memory_irrelevant"
        # expected_memory_behavior 未記載でもカテゴリ既定で埋まる
        assert by_id["irr_01"].expected_memory_behavior == "記憶を持ち出さない"
        # source_card_id は target_memory_ids として扱う（seed 検証はしない）
        assert by_id["req_01"].target_memory_ids == ("card_001",)

    def test_rejects_both_seed_and_source(self, tmp_path):
        path = _instance_dataset(
            tmp_path,
            memory_seed=[
                {
                    "id": "s1",
                    "date": "2026-01-01",
                    "user": "u",
                    "assistant": "a",
                }
            ],
        )

        with pytest.raises(DialogueABError, match="同時に指定できない"):
            load_dialogue_dataset(path)

    def test_rejects_unsupported_source_type(self, tmp_path):
        path = _instance_dataset(
            tmp_path, memory_source={"type": "s3", "name": "Jarvis"}
        )

        with pytest.raises(DialogueABError, match="unsupported memory_source.type"):
            load_dialogue_dataset(path)

    def test_reviewed_jarvis_candidates_file_loads(self):
        """yuki がレビューした実ファイルがそのまま dataset として通る。"""
        if not JARVIS_CANDIDATES.is_file():
            pytest.skip("Jarvis 候補ファイルが無い環境")

        dataset = load_dialogue_dataset(JARVIS_CANDIDATES)

        assert dataset.seeds_from_instance is True
        assert len(dataset.prompts) == 30
        required = [
            p for p in dataset.prompts if p.category == "memory_required"
        ]
        assert len(required) == 10
        assert all(p.expected_terms for p in required)


class TestInstanceSnapshot:
    """本番インスタンスは複製のみ・一切変更しない（実験の前提）。"""

    def _make_instance(self, root: Path, name: str = "Jarvis") -> Path:
        import sqlite3

        from butly_core.core.database import ButlyDatabase

        instance = root / "butly_core" / "instances" / name
        (instance / "debug_logs").mkdir(parents=True)
        (instance / "config.json").write_text(
            json.dumps({"agent_profile": {"ai_name": "Jarvis"}}),
            encoding="utf-8",
        )
        (instance / "system_instruction.txt").write_text("執事", encoding="utf-8")
        (instance / "debug_logs" / "latest.json").write_text("{}", encoding="utf-8")
        db_path = instance / "butly_memory.db"
        ButlyDatabase(db_path=str(db_path))
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO knowledge_cards (id, category, title, summary, embedding_blob)"
            " VALUES ('card_001','Tech','ホスト名','jarvis に固定', X'00000000')"
        )
        conn.commit()
        conn.close()
        return instance

    def test_copy_excludes_volatile_dirs_and_keeps_source(self, tmp_path):
        from evals.dialogue_ab import _copy_instance_snapshot

        source = self._make_instance(tmp_path)
        before = sorted(
            (p.relative_to(source).as_posix(), p.stat().st_size)
            for p in source.rglob("*")
            if p.is_file()
        )
        dest = tmp_path / "run" / "instances" / "ja_dialogue_ab"

        _copy_instance_snapshot(source, dest)

        assert (dest / "butly_memory.db").is_file()
        assert (dest / "system_instruction.txt").read_text(encoding="utf-8") == "執事"
        assert not (dest / "debug_logs").exists()
        after = sorted(
            (p.relative_to(source).as_posix(), p.stat().st_size)
            for p in source.rglob("*")
            if p.is_file()
        )
        assert after == before, "複製元が変更されている"

    def test_copy_replaces_existing_destination(self, tmp_path):
        from evals.dialogue_ab import _copy_instance_snapshot

        source = self._make_instance(tmp_path)
        dest = tmp_path / "run" / "ja_dialogue_ab"
        dest.mkdir(parents=True)
        (dest / "stale.txt").write_text("old", encoding="utf-8")

        _copy_instance_snapshot(source, dest)

        assert not (dest / "stale.txt").exists()

    def test_resolve_prefers_cli_over_dataset(self, tmp_path):
        from evals.dialogue_ab import (
            DialogueABConfig,
            _resolve_seed_instance,
            load_dialogue_dataset,
        )

        source = self._make_instance(tmp_path, name="FromCLI")
        dataset = load_dialogue_dataset(_instance_dataset(tmp_path))
        config = DialogueABConfig(
            dataset_path=tmp_path / "x.json",
            output_dir=tmp_path,
            run_id="r",
            profile_path=tmp_path / "p.yaml",
            seed_instance=source,
        )

        assert _resolve_seed_instance(dataset, config) == source.resolve()

    def test_resolve_reports_missing_instance(self, tmp_path):
        from evals.dialogue_ab import (
            DialogueABConfig,
            DialogueABError,
            _resolve_seed_instance,
            load_dialogue_dataset,
        )

        dataset = load_dialogue_dataset(
            _instance_dataset(
                tmp_path,
                memory_source={
                    "type": "instance",
                    "name": "Nope",
                    "path": str(tmp_path / "missing"),
                },
            )
        )
        config = DialogueABConfig(
            dataset_path=tmp_path / "x.json",
            output_dir=tmp_path,
            run_id="r",
            profile_path=tmp_path / "p.yaml",
        )

        with pytest.raises(DialogueABError, match="seed instance not found"):
            _resolve_seed_instance(dataset, config)

    def test_embedding_meta_absence_is_recorded_as_warning(self, tmp_path):
        from evals.dialogue_ab import _describe_stored_embeddings

        source = self._make_instance(tmp_path)

        info = _describe_stored_embeddings(source / "butly_memory.db")

        assert info["embedding_meta"] is None
        assert any("embedding_meta" in w for w in info["warnings"])
        assert info["embedding_dims"] == [1]

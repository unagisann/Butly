import asyncio
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
            "retrieval_query": "こむぎ 三毛猫 性別",
            "query_fusion": {"executed": True},
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
            "retrieval_query": "こむぎ 三毛猫 性別",
            "query_fusion": {"executed": True},
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
    assert scores["policies"]["intent_gated"]["retrieval_query_rate"] == 1.0
    assert scores["policies"]["candidates"]["dual_query_execution_rate"] == 1.0


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


def _judge_dataset(tmp_path: Path) -> Path:
    payload = {
        "schema_version": 1,
        "dataset_id": "judge-fixture",
        "locale": "ja",
        "memory_seed": [
            {
                "id": "seed_1",
                "date": "2026-01-01",
                "user": "設定ファイルをAPIKey.envと書いた。",
                "assistant": "正しくはAPIkey.envなので読み込みに失敗した。",
            }
        ],
        "prompts": [
            {
                "id": "req_01",
                "category": "memory_required",
                "prompt": "設定ファイルで失敗した原因は？",
                "target_memory_ids": ["seed_1"],
                "expected_terms": ["APIkey.env"],
                "source_fact": "APIkey.envをAPIKey.envと書いたため失敗した",
            }
        ],
    }
    path = tmp_path / "judge_dataset.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _judge_arm(grade: str, reason: str) -> dict:
    return {
        "grade": grade,
        "confidence": "high",
        "contradiction": grade == "fail",
        "memory_use": "helpful",
        "unsupported_claims": [],
        "reason": reason,
    }


class _JudgeProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def classify(self, _prompt, _config):
        self.calls += 1
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return json.dumps(value, ensure_ascii=False)

    def pop_last_token_usage(self):
        return {"prompt_tokens": 5, "completion_tokens": 2}

    def pop_last_completion_metadata(self):
        return {"finish_reason": "stop"}


def _judge_passes():
    return [
        {
            "A": _judge_arm("pass", "正しい"),
            "B": _judge_arm("fail", "逆方向"),
        },
        {
            "A": _judge_arm("fail", "逆方向"),
            "B": _judge_arm("pass", "正しい"),
        },
    ]


def _dialogue_results():
    common = {
        "prompt_id": "req_01",
        "category": "memory_required",
        "rag_triggered": True,
        "search_executed": True,
        "prompt_tokens": 10,
        "latency_ms": 20,
        "target_term_recall": 1.0,
        "seed_term_mentions": ["APIkey.env"],
    }
    return [
        {
            **common,
            "policy": "intent_gated",
            "response": "APIKey.envと誤記したためです。",
        },
        {
            **common,
            "policy": "candidates",
            "response": "APIkey.envと書いたためです。",
        },
    ]


def test_source_fact_is_preserved_for_semantic_judge(tmp_path):
    dataset = load_dialogue_dataset(_judge_dataset(tmp_path))

    assert dataset.prompts[0].source_fact == (
        "APIkey.envをAPIKey.envと書いたため失敗した"
    )


def test_required_prompt_without_reference_is_not_guessed_by_judge(tmp_path):
    from evals.dialogue_ab import _run_dialogue_judgments
    from evals.locomo.workspace import EvaluationWorkspace
    from evals.semantic_judge import JudgeConfig, SemanticJudge

    dataset_path = _judge_dataset(tmp_path)
    payload = json.loads(dataset_path.read_text(encoding="utf-8"))
    payload["prompts"][0].pop("source_fact")
    payload["prompts"][0]["target_memory_ids"] = []
    dataset_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    dataset = load_dialogue_dataset(dataset_path)
    workspace = EvaluationWorkspace.create(
        tmp_path / "runs", run_id="missing-reference"
    )
    instance_dir = workspace.instances_dir / "ja_dialogue_ab"
    instance_dir.mkdir(parents=True)
    config = JudgeConfig("judge", connection="nano")
    provider = _JudgeProvider([])

    judgments = asyncio.run(
        _run_dialogue_judgments(
            workspace=workspace,
            dataset=dataset,
            results=_dialogue_results(),
            judge_config=config,
            instance_dir=instance_dir,
            judge=SemanticJudge(config, provider=provider),
        )
    )

    assert provider.calls == 0
    assert judgments[0]["status"] == "error"
    assert "authoritative reference" in judgments[0]["error"]["message"]


def test_judgment_cache_skips_success_and_retries_error(tmp_path):
    from evals.dialogue_ab import _run_dialogue_judgments
    from evals.locomo.workspace import EvaluationWorkspace
    from evals.semantic_judge import JudgeConfig, SemanticJudge

    dataset = load_dialogue_dataset(_judge_dataset(tmp_path))
    config = JudgeConfig("judge", connection="nano")
    workspace = EvaluationWorkspace.create(
        tmp_path / "runs", run_id="cache-run"
    )
    instance_dir = workspace.instances_dir / "ja_dialogue_ab"
    instance_dir.mkdir(parents=True)

    provider = _JudgeProvider(_judge_passes())
    first = asyncio.run(
        _run_dialogue_judgments(
            workspace=workspace,
            dataset=dataset,
            results=_dialogue_results(),
            judge_config=config,
            instance_dir=instance_dir,
            judge=SemanticJudge(config, provider=provider),
        )
    )
    assert first[0]["status"] == "complete"
    assert provider.calls == 2

    cache_provider = _JudgeProvider([RuntimeError("must not run")])
    cached = asyncio.run(
        _run_dialogue_judgments(
            workspace=workspace,
            dataset=dataset,
            results=_dialogue_results(),
            judge_config=config,
            instance_dir=instance_dir,
            judge=SemanticJudge(config, provider=cache_provider),
        )
    )
    assert cached[0]["status"] == "complete"
    assert cache_provider.calls == 0

    changed = _dialogue_results()
    changed[1]["response"] = "変更してfingerprintを無効化"
    error_provider = _JudgeProvider([RuntimeError("offline")])
    errored = asyncio.run(
        _run_dialogue_judgments(
            workspace=workspace,
            dataset=dataset,
            results=changed,
            judge_config=config,
            instance_dir=instance_dir,
            judge=SemanticJudge(config, provider=error_provider),
        )
    )
    assert errored[0]["status"] == "error"

    retry_provider = _JudgeProvider(_judge_passes())
    retried = asyncio.run(
        _run_dialogue_judgments(
            workspace=workspace,
            dataset=dataset,
            results=changed,
            judge_config=config,
            instance_dir=instance_dir,
            judge=SemanticJudge(config, provider=retry_provider),
        )
    )
    assert retried[0]["status"] == "complete"
    assert retry_provider.calls == 2


def test_posthoc_judge_does_not_replay_qa(tmp_path, monkeypatch):
    import evals.dialogue_ab as dialogue
    from evals.locomo.artifacts import write_json
    from evals.locomo.workspace import EvaluationWorkspace
    from evals.semantic_judge import JudgeConfig, SemanticJudge

    dataset_path = _judge_dataset(tmp_path)
    workspace = EvaluationWorkspace.create(
        tmp_path / "runs", run_id="posthoc-run"
    )
    workspace.write_run_config(
        {
            "schema_version": 2,
            "run_type": "dialogue_ab",
            "run_id": "posthoc-run",
            "dataset_path": str(dataset_path),
        }
    )
    instance_dir = workspace.instances_dir / "ja_dialogue_ab"
    instance_dir.mkdir(parents=True)
    for result in _dialogue_results():
        path = (
            workspace.results_dir
            / "dialogue_ab"
            / result["policy"]
            / "req_01.json"
        )
        write_json(path, result)
    before = {
        path: path.read_text(encoding="utf-8")
        for path in (workspace.results_dir / "dialogue_ab").glob("*/*.json")
    }

    config = JudgeConfig("judge", connection="nano")
    provider = _JudgeProvider(_judge_passes())
    semantic = SemanticJudge(config, provider=provider)
    monkeypatch.setattr(dialogue, "SemanticJudge", lambda _config: semantic)

    scores = asyncio.run(
        dialogue.judge_dialogue_ab_run(
            workspace.run_dir,
            judge_config=config,
        )
    )

    assert provider.calls == 2
    assert scores["semantic_judge"]["status"] == "completed"
    assert scores["prompts"][0]["judgment"]["winner"] == "intent_gated"
    assert {
        path: path.read_text(encoding="utf-8") for path in before
    } == before


def test_posthoc_judge_preserves_legacy_v5_scores(tmp_path, monkeypatch):
    import evals.dialogue_ab as dialogue
    from evals.locomo.artifacts import write_json
    from evals.locomo.workspace import EvaluationWorkspace
    from evals.semantic_judge import JudgeConfig, SemanticJudge

    dataset_path = _judge_dataset(tmp_path)
    dataset = load_dialogue_dataset(dataset_path)
    workspace = EvaluationWorkspace.create(
        tmp_path / "runs", run_id="legacy-v5"
    )
    workspace.write_run_config(
        {
            "schema_version": 2,
            "run_type": "dialogue_ab",
            "run_id": "legacy-v5",
            "dataset_path": str(dataset_path),
        }
    )
    instance_dir = workspace.instances_dir / "ja_dialogue_ab"
    instance_dir.mkdir(parents=True)
    results = _dialogue_results()
    for result in results:
        write_json(
            workspace.results_dir
            / "dialogue_ab"
            / result["policy"]
            / "req_01.json",
            result,
        )

    legacy_scores = build_dialogue_scores(
        dataset,
        results,
        run_id="legacy-v5",
        knowledge_cards=87,
    )
    legacy_scores["generated_at"] = "2025-01-02T03:04:05+00:00"
    legacy_scores["future_extension"] = {
        "v5_manual_review": True,
        "opaque": [1, 2, 3],
    }
    legacy_scores["policies"]["intent_gated"]["v5_proxy"] = 0.625
    legacy_scores["comparison"]["v5_proxy_delta"] = -0.125
    legacy_scores["prompts"][0]["v5_annotation"] = "keep-me"
    legacy_scores["prompts"][0]["judgment"] = {"winner": "stale"}
    write_json(workspace.run_dir / "scores.json", legacy_scores)

    config = JudgeConfig("judge", connection="nano")
    provider = _JudgeProvider(_judge_passes())
    semantic = SemanticJudge(config, provider=provider)
    monkeypatch.setattr(dialogue, "SemanticJudge", lambda _config: semantic)

    scores = asyncio.run(
        dialogue.judge_dialogue_ab_run(
            workspace.run_dir,
            judge_config=config,
        )
    )

    assert scores["generated_at"] == "2025-01-02T03:04:05+00:00"
    assert scores["knowledge_cards_created"] == 87
    assert scores["future_extension"] == legacy_scores["future_extension"]
    assert scores["policies"] == legacy_scores["policies"]
    assert scores["comparison"] == legacy_scores["comparison"]
    assert scores["prompts"][0]["v5_annotation"] == "keep-me"
    assert scores["prompts"][0]["judgment"]["winner"] == "intent_gated"
    assert scores["semantic_judge"]["status"] == "completed"


def test_posthoc_rejects_dataset_changed_after_qa(tmp_path):
    import evals.dialogue_ab as dialogue

    dataset_path = _judge_dataset(tmp_path)
    original = load_dialogue_dataset(dataset_path)
    results = _dialogue_results()
    scores = build_dialogue_scores(
        original,
        results,
        run_id="legacy-v5",
        knowledge_cards=1,
    )
    payload = json.loads(dataset_path.read_text(encoding="utf-8"))
    payload["prompts"][0]["prompt"] = "QA後に変更された別の質問"
    dataset_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    changed = load_dialogue_dataset(dataset_path)

    with pytest.raises(DialogueABError, match="dataset changed since QA"):
        dialogue._validate_posthoc_dataset_snapshot(
            changed,
            run_payload={},
            scores=scores,
            results=results,
        )


def test_judge_model_override_reinfers_connection_from_prefix():
    import evals.dialogue_ab as dialogue
    from evals.semantic_judge import JudgeConfig

    saved = JudgeConfig("gemini-2.5-pro", connection="google-custom")
    args = SimpleNamespace(
        judge_model_name="gpt-5-mini",
        judge_connection=None,
        judge_max_output_tokens=None,
    )

    config = dialogue._judge_config_from_args(args, fallback=saved)

    assert config is not None
    assert config.model_name == "gpt-5-mini"
    assert config.connection is None


def test_custom_judge_model_override_requires_connection():
    import evals.dialogue_ab as dialogue
    from evals.semantic_judge import JudgeConfig

    saved = JudgeConfig("gemini-2.5-pro", connection="google-custom")
    args = SimpleNamespace(
        judge_model_name="gemma-custom-31b",
        judge_connection=None,
        judge_max_output_tokens=None,
    )

    with pytest.raises(
        DialogueABError,
        match="Cannot infer connection.*Specify 'connection' explicitly",
    ):
        dialogue._judge_config_from_args(args, fallback=saved)


def test_posthoc_partial_is_saved_then_raises_for_resume(tmp_path, monkeypatch):
    import evals.dialogue_ab as dialogue
    from evals.locomo.artifacts import write_json
    from evals.locomo.workspace import EvaluationWorkspace
    from evals.semantic_judge import (
        JudgeConfig,
        SemanticJudge,
        SemanticJudgeError,
    )

    dataset_path = _judge_dataset(tmp_path)
    workspace = EvaluationWorkspace.create(
        tmp_path / "runs", run_id="posthoc-error"
    )
    workspace.write_run_config(
        {
            "schema_version": 2,
            "run_type": "dialogue_ab",
            "run_id": "posthoc-error",
            "dataset_path": str(dataset_path),
        }
    )
    (workspace.instances_dir / "ja_dialogue_ab").mkdir(parents=True)
    for result in _dialogue_results():
        write_json(
            workspace.results_dir
            / "dialogue_ab"
            / result["policy"]
            / "req_01.json",
            result,
        )

    config = JudgeConfig("judge", connection="nano")
    semantic = SemanticJudge(
        config,
        provider=_JudgeProvider([RuntimeError("offline")]),
    )
    monkeypatch.setattr(dialogue, "SemanticJudge", lambda _config: semantic)

    with pytest.raises(SemanticJudgeError, match="only partially"):
        asyncio.run(
            dialogue.judge_dialogue_ab_run(
                workspace.run_dir,
                judge_config=config,
            )
        )

    scores = json.loads(
        (workspace.run_dir / "scores.json").read_text(encoding="utf-8")
    )
    checkpoint = json.loads(
        (
            workspace.checkpoints_dir / "dialogue_ab.json"
        ).read_text(encoding="utf-8")
    )
    assert scores["semantic_judge"]["status"] == "partial"
    assert scores["semantic_judge"]["error_prompt_count"] == 1
    assert scores["prompts"][0]["judgment"]["status"] == "error"
    assert checkpoint["status"] == "judge_failed"

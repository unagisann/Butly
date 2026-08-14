import json
from pathlib import Path
import sys
import time

import pytest

import evals.locomo.web_jobs as web_jobs
from evals.locomo.semantic_judge_runner import (
    calculate_question_set_fingerprint,
)
from evals.locomo.web_jobs import (
    EvaluationJobConflict,
    EvaluationJobError,
    EvaluationJobManager,
    build_dialogue_ab_command,
    build_dialogue_ab_profile_payload,
    build_job_command,
    build_judge_command,
    build_profile_payload,
    build_retrieval_replay_command,
    describe_locomo_dataset,
    validate_dialogue_ab_request,
    validate_job_request,
    validate_judge_request,
)
from evals.semantic_judge import JudgeConfig


FIXTURE = (
    Path(__file__).parent
    / "evals"
    / "fixtures"
    / "mini_locomo.json"
)


def _request(**overrides):
    payload = {
        "dataset_path": str(FIXTURE),
        "run_id": "web-test",
        "run_mode": "standard",
        "source_memory_run_id": None,
        "workflow": "full",
        "qa_mode": "independent",
        "locale": "en",
        "sample_ids": [],
        "sample_limit": 1,
        "session_limit": 3,
        "question_limit": 10,
        "time_decay_rate": 0.0,
        "context_current_time": True,
        "context_mid_term": True,
        "context_session_digest": True,
        "context_rag": True,
        "rag_source_mode": "both",
        "rag_raw_top_k": 1,
        "rag_raw_max_chars": 2500,
        "stage3_batch_size": 10,
        "stage3_bootstrap_max_cards": 2000,
        "role_models": {
            "chat": {
                "connection": "nanogpt-sub",
                "model_name": "qwen3-14b",
                "generation_config": {"temperature": 0.7},
            },
            "embedding": {
                "connection": "ollama",
                "model_name": "ollama/nomic-embed-text",
                "generation_config": {},
            },
        },
    }
    payload.update(overrides)
    return payload


def _dialogue_request(**overrides):
    payload = {
        "dataset_path": str(
            Path(__file__).parents[1]
            / "data"
            / "ja_dialogue_ab_prompts_v1.json"
        ),
        "run_id": "dialogue-v1",
        "time_decay_rate": 0.003,
        "context_current_time": True,
        "context_mid_term": True,
        "context_session_digest": True,
        "context_rag": True,
        "rag_source_mode": "both",
        "rag_raw_top_k": 1,
        "rag_raw_max_chars": 2500,
        "stage3_enabled": True,
        "stage3_batch_size": 10,
        "stage3_bootstrap_max_cards": 2000,
        "search_mode": "vector",
        "retrieval_execution": "always",
        "vector_search_limit": 3,
        "intent_gated_vector_search_limit": 3,
        "candidates_vector_search_limit": 3,
        "vector_search_threshold": 0.4,
        "deep_search_enabled": True,
        "bm25_candidates": 20,
        "vector_candidates": 20,
        "rrf_k": 60,
        "bm25_max_df_ratio": 0.5,
        "role_models": _request()["role_models"],
    }
    payload.update(overrides)
    return payload


def _write_run(
    output_dir: Path,
    run_id: str,
    *,
    overall: float,
    prediction: str,
) -> Path:
    run_dir = output_dir / run_id
    (run_dir / "checkpoints").mkdir(parents=True)
    (run_dir / "run_config.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "created_at": f"2026-01-0{1 if run_id == 'a' else 2}T00:00:00Z",
                "qa_mode": "independent",
                "locale": "en",
                "question_limit": 1,
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "checkpoints" / "checkpoint.json").write_text(
        json.dumps({"run_id": run_id, "status": "completed"}),
        encoding="utf-8",
    )
    (run_dir / "scores.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "question_count": 1,
                "official": {"overall": overall},
                "auxiliary": {
                    "exact_match_rate": float(overall == 1.0),
                    "answer_containment_rate": 1.0,
                },
                "butly": {
                    "evidence_retrieval_rate": 0.5,
                    "latency_ms_mean": 1000,
                    "prompt_tokens_total": 100,
                    "completion_tokens_total": 10,
                    "knowledge_cards_created": 2,
                    "sleeptime_failures": 0,
                },
                "questions": [
                    {
                        "question_id": "q1",
                        "sample_id": "sample",
                        "category": 2,
                        "question": "When?",
                        "expected_answer": "Monday",
                        "prediction": prediction,
                        "official_score": overall,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return run_dir


def _write_source_run(
    output_dir: Path,
    run_id: str = "source",
    *,
    embedding_meta: dict | None = None,
    with_vectors: bool = True,
) -> Path:
    """rerun-qa の再利用元になる run ディレクトリを作る。

    ``embedding_meta`` を渡すと、その素性でカードが埋め込まれた体にする。
    """
    import sqlite3
    import struct

    run_dir = output_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_config.json").write_text(
        json.dumps({"run_id": run_id}), encoding="utf-8"
    )
    inst_dir = run_dir / "workspace" / "butly_core" / "instances" / "locomo_1"
    inst_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(inst_dir / "butly_memory.db")
    conn.execute(
        "CREATE TABLE knowledge_cards (id INTEGER PRIMARY KEY, embedding_blob BLOB)"
    )
    blob = struct.pack("<768f", *([0.1] * 768)) if with_vectors else None
    conn.execute("INSERT INTO knowledge_cards (embedding_blob) VALUES (?)", (blob,))
    if embedding_meta:
        conn.execute(
            "CREATE TABLE embedding_meta ("
            "id INTEGER PRIMARY KEY CHECK (id = 1), model_name TEXT, "
            "profile TEXT, dim INTEGER, updated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO embedding_meta (id, model_name, profile, dim, updated_at) "
            "VALUES (1, ?, ?, ?, '2026-07-26T00:00:00+00:00')",
            (
                embedding_meta["model_name"],
                embedding_meta["profile"],
                embedding_meta.get("dim"),
            ),
        )
    conn.commit()
    conn.close()
    return run_dir


class TestEmbeddingCompatibilityOnReuse:
    """記憶を再利用する run で、埋め込み空間の食い違いを開始前に弾く。

    rerun-qa は元 run のカードとベクトルをそのまま使う。埋め込みモデルや
    prefix 規約が変わっていると保存済みベクトルと検索クエリが別空間になり、
    例外もログも出ないまま検索だけが壊れる（1時間かけて無意味な数字が出る）。
    """

    def _reuse_request(self, **overrides):
        base = {"run_mode": "stage3-on", "source_memory_run_id": "source"}
        base.update(overrides)
        return _request(**base)

    def test_rejects_source_written_without_prefixes(self, tmp_path):
        # 素性未記録 = prefix 導入前のベクトル。次元は一致するので
        # 次元チェックだけでは検知できない。
        _write_source_run(tmp_path)

        with pytest.raises(EvaluationJobError, match="no recorded profile"):
            validate_job_request(self._reuse_request(), output_dir=tmp_path)

    def test_rejects_source_with_different_model(self, tmp_path):
        _write_source_run(
            tmp_path,
            embedding_meta={
                "model_name": "multilingual-e5-large",
                "profile": "e5",
                "dim": 1024,
            },
        )

        with pytest.raises(EvaluationJobError, match="different model/profile"):
            validate_job_request(self._reuse_request(), output_dir=tmp_path)

    def test_accepts_matching_source(self, tmp_path):
        _write_source_run(
            tmp_path,
            embedding_meta={
                "model_name": "nomic-embed-text",
                "profile": "nomic",
                "dim": 768,
            },
        )

        normalized = validate_job_request(
            self._reuse_request(), output_dir=tmp_path
        )

        assert normalized["source_memory_run_id"] == "source"
        assert normalized["allow_embedding_mismatch"] is False

    def test_override_allows_mismatch(self, tmp_path):
        _write_source_run(tmp_path)

        normalized = validate_job_request(
            self._reuse_request(allow_embedding_mismatch=True),
            output_dir=tmp_path,
        )

        assert normalized["allow_embedding_mismatch"] is True

    def test_skips_check_when_source_has_no_workspace(self, tmp_path):
        run_dir = tmp_path / "source"
        run_dir.mkdir(parents=True)
        (run_dir / "run_config.json").write_text("{}", encoding="utf-8")

        normalized = validate_job_request(
            self._reuse_request(), output_dir=tmp_path
        )

        assert normalized["source_memory_run_id"] == "source"

    def test_fresh_run_is_never_blocked(self, tmp_path):
        """記憶を作り直す run は元ベクトルを使わないので対象外。"""
        normalized = validate_job_request(_request(), output_dir=tmp_path)

        assert normalized["source_memory_run_id"] is None


class TestEmbeddingProfileInProfilePayload:
    def test_profile_is_passed_through(self):
        request = _request()
        request["role_models"]["embedding"]["profile"] = "e5"

        profile = build_profile_payload(request)

        assert profile["embedding"]["profile"] == "e5"

    def test_explicit_prefixes_are_passed_through(self):
        request = _request()
        request["role_models"]["embedding"].update(
            {"query_prefix": "q: ", "document_prefix": "d: "}
        )

        profile = build_profile_payload(request)

        assert profile["embedding"]["query_prefix"] == "q: "
        assert profile["embedding"]["document_prefix"] == "d: "

    def test_absent_profile_stays_absent(self):
        """既定は auto — profile キー自体を書かない。"""
        profile = build_profile_payload(_request())

        assert "profile" not in profile["embedding"]

    def test_prefix_keys_ignored_on_other_roles(self):
        request = _request()
        request["role_models"]["chat"]["profile"] = "e5"

        profile = build_profile_payload(request)

        assert "profile" not in profile["chat"]


class TestSemanticJudgeWebConfig:
    def _judge_role(self, **generation_overrides):
        return {
            "connection": "nanogpt-sub",
            "model_name": "TEE/gemma4-31b",
            "generation_config": {
                "temperature": 1.0,
                "max_output_tokens": 4096,
                **generation_overrides,
            },
        }

    def test_judge_is_top_level_evaluation_profile_section(self):
        request = _request()
        request["role_models"]["judge"] = self._judge_role()

        profile = build_profile_payload(request)

        assert profile["judge"]["connection"] == "nanogpt-sub"
        assert profile["judge"]["model_name"] == "TEE/gemma4-31b"
        assert profile["judge"]["generation_config"] == {
            "temperature": 0.0,
            "max_output_tokens": 4096,
        }
        assert len(profile["judge"]["config_signature"]) == 64

    def test_validate_accepts_judge_and_forces_temperature(self, tmp_path):
        request = _request()
        request["role_models"]["judge"] = self._judge_role()

        normalized = validate_job_request(request, output_dir=tmp_path)

        judge = normalized["role_models"]["judge"]
        assert judge["generation_config"]["temperature"] == 0.0
        assert judge["generation_config"]["max_output_tokens"] == 4096

    def test_validate_rejects_bad_judge_max_tokens(self, tmp_path):
        request = _request()
        request["role_models"]["judge"] = self._judge_role(
            max_output_tokens=0
        )

        with pytest.raises(EvaluationJobError, match="max_output_tokens"):
            validate_job_request(request, output_dir=tmp_path)

    def test_validate_requires_connection_for_custom_judge_model(
        self, tmp_path
    ):
        request = _request()
        request["role_models"]["judge"] = {
            "model_name": "gemma-custom-31b",
            "generation_config": {"max_output_tokens": 2048},
        }

        with pytest.raises(EvaluationJobError, match="Cannot infer connection"):
            validate_job_request(request, output_dir=tmp_path)


class TestRerankerWebConfig:
    @staticmethod
    def _reranker_role():
        return {
            "connection": "nanogpt-sub",
            "model_name": "TEE/gemma4-31b",
            "generation_config": {
                "temperature": 1.0,
                "max_output_tokens": 4096,
            },
        }

    def test_profile_contains_optional_runtime_reranker(self):
        request = _request(
            reranker_candidate_limit=20,
            reranker_max_candidate_chars=1200,
        )
        request["role_models"]["reranker"] = self._reranker_role()

        profile = build_profile_payload(request)

        assert profile["reranker"] == {
            "enabled": True,
            "connection": "nanogpt-sub",
            "model_name": "TEE/gemma4-31b",
            "candidate_limit": 20,
            "max_candidate_chars": 1200,
            "generation_config": {
                "temperature": 0.0,
                "max_output_tokens": 4096,
            },
        }

    def test_validate_rejects_hybrid_with_reranker(self, tmp_path):
        request = _request(search_mode="hybrid")
        request["role_models"]["reranker"] = self._reranker_role()

        with pytest.raises(EvaluationJobError, match="requires search_mode=vector"):
            validate_job_request(request, output_dir=tmp_path)

    def test_validate_requires_connection_for_custom_reranker(
        self, tmp_path
    ):
        request = _request()
        request["role_models"]["reranker"] = {
            "model_name": "custom-reranker",
            "generation_config": {"max_output_tokens": 2048},
        }

        with pytest.raises(EvaluationJobError, match="Cannot infer connection"):
            validate_job_request(request, output_dir=tmp_path)

    def test_profile_contains_cross_encoder_without_llm_connection(self):
        request = _request(
            reranker_candidate_limit=20,
            reranker_max_candidate_chars=1400,
        )
        request["role_models"]["reranker"] = {
            "engine": "cross_encoder",
            "model_name": "Alibaba-NLP/gte-multilingual-reranker-base",
            "batch_size": 10,
            "score_threshold": 0.25,
            "device": "cpu",
        }

        profile = build_profile_payload(request)

        assert profile["reranker"] == {
            "enabled": True,
            "engine": "cross_encoder",
            "model_name": "Alibaba-NLP/gte-multilingual-reranker-base",
            "model_revision": "a6258e9d2b1a11aa7bccdff9efde562bbca4393d",
            "candidate_limit": 20,
            "max_candidate_chars": 1400,
            "batch_size": 10,
            "score_threshold": 0.25,
            "device": "cpu",
        }


class TestPosthocJudgeWebJob:
    def _request(self):
        return {
            "connection": "nanogpt-sub",
            "model_name": "TEE/gemma4-31b",
            "max_output_tokens": 4096,
        }

    def _dialogue_run(self, output_dir: Path, run_id: str = "dialogue-v5"):
        run_dir = output_dir / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "run_config.json").write_text(
            json.dumps({"run_id": run_id, "run_type": "dialogue_ab"}),
            encoding="utf-8",
        )
        (run_dir / "scores.json").write_text("{}", encoding="utf-8")
        return run_dir

    def test_validates_existing_locomo_run_and_builds_command(self, tmp_path):
        _write_run(tmp_path, "locomo-v1", overall=0.5, prediction="Monday")

        normalized = validate_judge_request(
            self._request(),
            run_id="locomo-v1",
            run_type="locomo",
            output_dir=tmp_path,
        )
        command = build_judge_command(
            normalized,
            python_executable="python-test",
        )

        assert command[:4] == [
            "python-test", "-m", "evals.locomo.cli", "judge"
        ]
        assert command[command.index("--judge-model-name") + 1] == (
            "TEE/gemma4-31b"
        )
        assert command[command.index("--judge-connection") + 1] == (
            "nanogpt-sub"
        )
        assert command[command.index("--judge-max-output-tokens") + 1] == (
            "4096"
        )
        assert len(normalized["judge"]["config_signature"]) == 64

    def test_builds_dialogue_posthoc_command(self, tmp_path):
        self._dialogue_run(tmp_path)
        normalized = validate_judge_request(
            self._request(),
            run_id="dialogue-v5",
            run_type="dialogue_ab",
            output_dir=tmp_path,
        )

        command = build_judge_command(normalized, python_executable="py")

        assert command[:4] == ["py", "-m", "evals.dialogue_ab", "judge"]
        assert "--judge-model-name" in command

    def test_dialogue_completion_uses_semantic_timestamp_after_legacy_merge(
        self, tmp_path
    ):
        manager = EvaluationJobManager(tmp_path / "data")
        run_dir = self._dialogue_run(manager.dialogue_output_dir)
        normalized = validate_judge_request(
            self._request(),
            run_id="dialogue-v5",
            run_type="dialogue_ab",
            output_dir=manager.dialogue_output_dir,
        )
        (run_dir / "scores.json").write_text(
            json.dumps({
                "generated_at": "2025-01-01T00:00:00+00:00",
                "semantic_judge": {
                    "status": "completed",
                    "generated_at": "2026-08-08T10:00:01+00:00",
                    "config_signature": normalized["judge"][
                        "config_signature"
                    ],
                    "model": normalized["judge"],
                },
            }),
            encoding="utf-8",
        )
        record = {
            "job_type": "dialogue_ab_judge",
            "run_dir": str(run_dir),
            "started_at": "2026-08-08T10:00:00+00:00",
            "request": normalized,
        }

        assert manager._judge_output_complete(record) is True

    def test_rejects_missing_or_unfinished_run(self, tmp_path):
        with pytest.raises(KeyError):
            validate_judge_request(
                self._request(),
                run_id="missing",
                run_type="locomo",
                output_dir=tmp_path,
            )
        run_dir = tmp_path / "unfinished"
        run_dir.mkdir()
        (run_dir / "run_config.json").write_text("{}", encoding="utf-8")
        with pytest.raises(EvaluationJobConflict, match="scores are not ready"):
            validate_judge_request(
                self._request(),
                run_id="unfinished",
                run_type="locomo",
                output_dir=tmp_path,
            )

    def test_manager_starts_posthoc_job_and_persists_safe_request(
        self, tmp_path, monkeypatch
    ):
        manager = EvaluationJobManager(
            tmp_path / "data",
            output_dir=tmp_path / "runs",
            project_root=Path(__file__).parents[1],
        )
        _write_run(
            manager.output_dir,
            "locomo-v1",
            overall=0.5,
            prediction="Monday",
        )
        monkeypatch.setattr(
            manager,
            "_launch",
            lambda record, command: manager._public_record(record),
        )

        job = manager.start_judge(
            "locomo-v1", self._request(), run_type="locomo"
        )

        assert job["job_type"] == "locomo_judge"
        assert job["request"]["judge"]["model_name"] == "TEE/gemma4-31b"
        assert "api_key" not in json.dumps(job).lower()

    def test_latest_job_for_same_run_prefers_new_judge_job(self, tmp_path):
        manager = EvaluationJobManager(tmp_path)
        manager._records = {
            "base": {
                "job_id": "base",
                "job_type": "locomo",
                "run_id": "same-run",
                "created_at": "2026-08-01T00:00:00+00:00",
            },
            "judge": {
                "job_id": "judge",
                "job_type": "locomo_judge",
                "run_id": "same-run",
                "created_at": "2026-08-02T00:00:00+00:00",
            },
        }

        latest = manager._latest_jobs_by_run({"locomo", "locomo_judge"})

        assert latest["same-run"]["job_id"] == "judge"

    def test_matching_semantic_sentinel_is_required(self, tmp_path):
        manager = EvaluationJobManager(
            tmp_path / "data",
            output_dir=tmp_path / "runs",
        )
        run_dir = _write_run(
            manager.output_dir,
            "locomo-v1",
            overall=0.5,
            prediction="Monday",
        )
        normalized = validate_judge_request(
            self._request(),
            run_id="locomo-v1",
            run_type="locomo",
            output_dir=manager.output_dir,
        )
        record = {
            "job_type": "locomo_judge",
            "run_dir": str(run_dir),
            "request": normalized,
        }

        assert manager._judge_output_complete(record) is False
        scores = json.loads(
            (run_dir / "scores.json").read_text(encoding="utf-8")
        )
        judge_config = JudgeConfig.from_mapping(normalized["judge"])
        assert judge_config is not None
        (run_dir / "semantic_scores.json").write_text(
            json.dumps({
                "status": "completed",
                "config_signature": normalized["judge"]["config_signature"],
                "judge": normalized["judge"],
                "question_set_fingerprint": (
                    calculate_question_set_fingerprint(scores, judge_config)
                ),
            }),
            encoding="utf-8",
        )
        assert manager._judge_output_complete(record) is True

        payload = json.loads(
            (run_dir / "semantic_scores.json").read_text(encoding="utf-8")
        )
        payload["config_signature"] = "old-prompt-signature"
        (run_dir / "semantic_scores.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        assert manager._judge_output_complete(record) is False

    def test_matching_old_aggregate_is_not_completion_for_new_attempt(
        self, tmp_path
    ):
        manager = EvaluationJobManager(
            tmp_path / "data",
            output_dir=tmp_path / "runs",
        )
        run_dir = _write_run(
            manager.output_dir,
            "locomo-v1",
            overall=0.5,
            prediction="Monday",
        )
        normalized = validate_judge_request(
            self._request(),
            run_id="locomo-v1",
            run_type="locomo",
            output_dir=manager.output_dir,
        )
        record = {
            "job_type": "locomo_judge",
            "run_dir": str(run_dir),
            "started_at": "2026-08-08T10:00:00+00:00",
            "request": normalized,
        }
        result = {
            "status": "completed",
            "generated_at": "2026-08-08T09:59:59+00:00",
            "config_signature": normalized["judge"]["config_signature"],
            "judge": normalized["judge"],
        }
        scores = json.loads(
            (run_dir / "scores.json").read_text(encoding="utf-8")
        )
        judge_config = JudgeConfig.from_mapping(normalized["judge"])
        assert judge_config is not None
        result["question_set_fingerprint"] = (
            calculate_question_set_fingerprint(scores, judge_config)
        )
        semantic_path = run_dir / "semantic_scores.json"
        semantic_path.write_text(json.dumps(result), encoding="utf-8")

        assert manager._judge_output_complete(record) is False

        result["generated_at"] = "2026-08-08T10:00:01+00:00"
        semantic_path.write_text(json.dumps(result), encoding="utf-8")
        assert manager._judge_output_complete(record) is True

    def test_launch_captures_attempt_time_before_fast_judge_writes(
        self, tmp_path, monkeypatch
    ):
        manager = EvaluationJobManager(
            tmp_path / "data",
            output_dir=tmp_path / "runs",
        )
        run_dir = _write_run(
            manager.output_dir,
            "locomo-v1",
            overall=0.5,
            prediction="Monday",
        )
        normalized = validate_judge_request(
            self._request(),
            run_id="locomo-v1",
            run_type="locomo",
            output_dir=manager.output_dir,
        )
        record = {
            "job_id": "fast-judge",
            "job_type": "locomo_judge",
            "run_id": "locomo-v1",
            "run_dir": str(run_dir),
            "request": normalized,
            "log_path": str(manager.jobs_dir / "fast-judge.log"),
            "status": "queued",
            "attempt": 0,
        }
        artifact_time = "2026-08-08T10:00:00+00:00"
        scores = json.loads(
            (run_dir / "scores.json").read_text(encoding="utf-8")
        )
        judge_config = JudgeConfig.from_mapping(normalized["judge"])
        assert judge_config is not None
        question_set_fingerprint = calculate_question_set_fingerprint(
            scores,
            judge_config,
        )

        class FakeProcess:
            pid = 12345

        class FakeThread:
            def __init__(self, **_kwargs):
                pass

            def start(self):
                pass

        def fast_popen(_command, **_kwargs):
            (run_dir / "semantic_scores.json").write_text(
                json.dumps({
                    "status": "completed",
                    "generated_at": artifact_time,
                    "config_signature": normalized["judge"][
                        "config_signature"
                    ],
                    "judge": normalized["judge"],
                    "question_set_fingerprint": question_set_fingerprint,
                }),
                encoding="utf-8",
            )
            return FakeProcess()

        launch_times = iter([
            artifact_time,
            "2026-08-08T10:00:01+00:00",
        ])
        monkeypatch.setattr(web_jobs, "utc_now", lambda: next(launch_times))
        monkeypatch.setattr(web_jobs.subprocess, "Popen", fast_popen)
        monkeypatch.setattr(web_jobs.threading, "Thread", FakeThread)
        monkeypatch.setattr(manager, "_process_create_time", lambda _pid: 1.0)

        manager._launch(record, ["judge"])

        assert record["started_at"] == artifact_time
        assert manager._judge_output_complete(record) is True

    def test_resume_rebuilds_posthoc_judge_command(self, tmp_path, monkeypatch):
        manager = EvaluationJobManager(
            tmp_path / "data",
            output_dir=tmp_path / "runs",
        )
        _write_run(
            manager.output_dir,
            "locomo-v1",
            overall=0.5,
            prediction="Monday",
        )
        normalized = validate_judge_request(
            self._request(),
            run_id="locomo-v1",
            run_type="locomo",
            output_dir=manager.output_dir,
        )
        record = {
            "schema_version": 1,
            "job_id": "judge-job",
            "job_type": "locomo_judge",
            "run_id": "locomo-v1",
            "run_dir": normalized["run_dir"],
            "status": "failed",
            "request": normalized,
            "stop_requested": False,
            "ended_at": "2026-08-01T00:00:00+00:00",
            "return_code": 1,
        }
        manager._records[record["job_id"]] = record
        captured = {}

        def fake_launch(item, command):
            captured["command"] = command
            return manager._public_record(item)

        monkeypatch.setattr(manager, "_launch", fake_launch)
        monkeypatch.setattr(manager, "_save_record", lambda item: None)

        resumed = manager.resume("judge-job")

        assert resumed["status"] == "queued"
        assert captured["command"][3] == "judge"
        assert "--judge-model-name" in captured["command"]

    def test_regular_job_with_judge_does_not_complete_from_scores_alone(
        self, tmp_path, monkeypatch
    ):
        manager = EvaluationJobManager(
            tmp_path / "data",
            output_dir=tmp_path / "runs",
        )
        request = _request(run_id="locomo-v1")
        request["role_models"]["judge"] = {
            "connection": "nanogpt-sub",
            "model_name": "TEE/gemma4-31b",
            "generation_config": {"max_output_tokens": 4096},
        }
        normalized = validate_job_request(
            request,
            output_dir=manager.output_dir,
        )
        run_dir = _write_run(
            manager.output_dir,
            "locomo-v1",
            overall=0.5,
            prediction="Monday",
        )
        record = {
            "job_id": "base-with-judge",
            "job_type": "locomo",
            "run_id": "locomo-v1",
            "run_dir": str(run_dir),
            "status": "running",
            "request": normalized,
            "pid": None,
            "process_created_at": None,
            "stop_requested": False,
            "ended_at": None,
        }
        manager._records[record["job_id"]] = record
        monkeypatch.setattr(manager, "_save_record", lambda item: None)

        manager._refresh_record(record)

        assert record["status"] == "interrupted"


def test_build_profile_matches_colab_stage3_controls():
    profile = build_profile_payload(
        _request(
            run_mode="stage3-on",
            context_mid_term=False,
            time_decay_rate=0.25,
        )
    )

    assert profile["chat"] == {
        "connection": "nanogpt-sub",
        "model_name": "qwen3-14b",
        "generation_config": {"temperature": 0.7},
    }
    assert profile["brain"] == {
        "time_decay_rate": 0.25,
        "use_rag": True,
        "search_mode": "vector",
    }
    assert profile["context_levels"]["levels"]["mid_term"] == "off"
    assert profile["memory"]["rag_raw_top_k"] == 1
    assert profile["memory"]["knowledge_maturation_enabled"] is True
    assert profile["sleeptime"]["update_targets"] == {
        "knowledge_maturation": True
    }


class TestSearchSettingsInProfile:
    """Web Console から hybrid の A/B を回せること（検索改修計画 §3.5）"""

    def test_vector_run_omits_bm25_params(self):
        profile = build_profile_payload(_request())

        assert profile["brain"]["search_mode"] == "vector"
        assert "bm25_candidates" not in profile["brain"]
        assert profile["memory_probe"] == {
            "retrieval_execution": "always",
            "injection_policy": "intent_gated",
            "vector_search_limit": 3,
        }

    def test_hybrid_run_writes_bm25_params(self):
        profile = build_profile_payload(
            _request(
                search_mode="hybrid",
                bm25_candidates=30,
                vector_candidates=15,
                rrf_k=40,
                bm25_max_df_ratio=0.4,
                injection_policy="retrieval_assisted",
                retrieval_execution="intent_gated",
            )
        )

        assert profile["brain"]["search_mode"] == "hybrid"
        assert profile["brain"]["bm25_candidates"] == 30
        assert profile["brain"]["vector_candidates"] == 15
        assert profile["brain"]["rrf_k"] == 40
        assert profile["brain"]["bm25_max_df_ratio"] == pytest.approx(0.4)
        assert profile["memory_probe"] == {
            "retrieval_execution": "intent_gated",
            "injection_policy": "retrieval_assisted",
            "vector_search_limit": 3,
        }

    def test_injection_top_k_reaches_memory_probe(self):
        """注入カード上限をtop3以外でもA/Bできること（top-k比較の前提）"""
        profile = build_profile_payload(_request(vector_search_limit=5))

        assert profile["memory_probe"]["vector_search_limit"] == 5

    def test_injection_top_k_rejects_non_positive(self):
        with pytest.raises(EvaluationJobError, match="vector_search_limit"):
            validate_job_request(
                _request(vector_search_limit=0),
                output_dir=Path("/tmp"),
            )

    def test_hybrid_evidence_fusion_writes_runtime_params(self):
        profile = build_profile_payload(
            _request(
                search_mode="hybrid_evidence_fusion",
                bm25_candidates=20,
                vector_candidates=20,
                evidence_fusion_base_weight=0.65,
                evidence_raw_chunk_chars=2200,
            )
        )

        assert profile["brain"]["search_mode"] == "hybrid_evidence_fusion"
        assert profile["brain"]["bm25_candidates"] == 20
        assert profile["brain"]["vector_candidates"] == 20
        assert profile["brain"]["evidence_fusion_base_weight"] == pytest.approx(
            0.65
        )
        assert profile["brain"]["evidence_raw_chunk_chars"] == 2200

    def test_dual_query_run_writes_bounded_fusion_params(self):
        profile = build_profile_payload(
            _request(
                search_mode="dual_query",
                dual_query_candidates=15,
                dual_query_pool_limit=25,
                rrf_k=40,
            )
        )

        assert profile["brain"] == {
            "time_decay_rate": 0.0,
            "use_rag": True,
            "search_mode": "dual_query",
            "dual_query_candidates": 15,
            "dual_query_pool_limit": 25,
            "rrf_k": 40,
        }
        assert "bm25_candidates" not in profile["brain"]

    def test_profile_sections_are_applicable_to_instance_config(self):
        """profile へ書いたセクションが eval 側で適用対象になっている"""
        from evals.locomo.config import PROFILE_ROLE_SECTIONS

        profile = build_profile_payload(_request(search_mode="hybrid"))
        for section in ("brain", "memory_probe"):
            assert section in profile
            assert section in PROFILE_ROLE_SECTIONS

    def test_validate_fills_defaults(self, tmp_path):
        request = _request()
        for key in (
            "search_mode",
            "retrieval_execution",
            "injection_policy",
            "bm25_candidates",
            "dual_query_candidates",
            "dual_query_pool_limit",
            "rrf_k",
            "bm25_max_df_ratio",
            "evidence_fusion_base_weight",
            "evidence_raw_chunk_chars",
        ):
            request.pop(key, None)

        normalized = validate_job_request(request, output_dir=tmp_path)

        assert normalized["search_mode"] == "vector"
        assert normalized["retrieval_execution"] == "always"
        assert normalized["injection_policy"] == "intent_gated"
        assert normalized["bm25_candidates"] == 20
        assert normalized["dual_query_candidates"] == 15
        assert normalized["dual_query_pool_limit"] == 25
        assert normalized["rrf_k"] == 60
        assert normalized["bm25_max_df_ratio"] == pytest.approx(0.5)
        assert normalized["evidence_fusion_base_weight"] == pytest.approx(0.7)
        assert normalized["evidence_raw_chunk_chars"] == 1800

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"search_mode": "bm25"}, "unsupported search_mode"),
            ({"injection_policy": "always"}, "unsupported injection_policy"),
            ({"retrieval_execution": "never"}, "unsupported retrieval_execution"),
            ({"bm25_candidates": 0}, "bm25_candidates must be a positive"),
            (
                {"dual_query_candidates": 0},
                "dual_query_candidates must be a positive",
            ),
            ({"bm25_max_df_ratio": 1.5}, "bm25_max_df_ratio must be in"),
            (
                {"evidence_fusion_base_weight": 1.1},
                "evidence_fusion_base_weight must be in",
            ),
            (
                {"evidence_raw_chunk_chars": 199},
                "evidence_raw_chunk_chars must be between",
            ),
        ],
    )
    def test_validate_rejects_bad_search_settings(
        self, tmp_path, overrides, message
    ):
        with pytest.raises(EvaluationJobError, match=message):
            validate_job_request(_request(**overrides), output_dir=tmp_path)

    def test_config_exposes_choices(self, tmp_path):
        manager = EvaluationJobManager(tmp_path)

        config = manager.config()

        assert config["search_modes"] == [
            "vector",
            "hybrid",
            "dual_query",
            "hybrid_evidence_fusion",
        ]
        assert config["retrieval_executions"] == ["always", "intent_gated"]
        assert config["injection_policies"] == [
            "intent_gated",
            "retrieval_assisted",
            "candidates",
        ]
        assert config["last_request"] is None

    def test_config_exposes_latest_job_request(self, tmp_path):
        manager = EvaluationJobManager(tmp_path)
        older = {
            "created_at": "2026-07-26T00:00:00+00:00",
            "request": _request(run_id="web-v25", question_limit=10),
        }
        latest = {
            "created_at": "2026-07-27T00:00:00+00:00",
            "request": _request(
                run_id="web-v26",
                question_limit=None,
                search_mode="vector",
                retrieval_execution="always",
            ),
        }
        manager._records = {"older": older, "latest": latest}

        previous = manager.config()["last_request"]

        assert previous["run_id"] == "web-v26"
        assert previous["question_limit"] is None
        assert previous["search_mode"] == "vector"
        assert previous["role_models"]["chat"]["connection"] == "nanogpt-sub"


class TestDialogueABWebJob:
    def test_validates_dataset_and_search_defaults(self, tmp_path):
        normalized = validate_dialogue_ab_request(
            _dialogue_request(),
            output_dir=tmp_path,
        )

        assert normalized["locale"] == "ja"
        assert normalized["search_mode"] == "vector"
        assert normalized["retrieval_execution"] == "always"
        assert normalized["vector_search_limit"] == 3
        assert normalized["intent_gated_vector_search_limit"] == 3
        assert normalized["candidates_vector_search_limit"] == 3
        assert normalized["vector_search_threshold"] == pytest.approx(0.4)
        assert normalized["deep_search_enabled"] is True
        assert normalized["time_decay_rate"] == pytest.approx(0.003)

    def test_profile_uses_japanese_and_shared_base_policy(self):
        profile = build_dialogue_ab_profile_payload(_dialogue_request())

        assert profile["locale"] == "ja"
        assert profile["brain"]["search_mode"] == "vector"
        assert profile["memory_probe"] == {
            "retrieval_execution": "always",
            "injection_policy": "intent_gated",
            "vector_search_limit": 3,
            "vector_search_threshold": 0.4,
            "deep_search_enabled": True,
        }
        assert profile["memory"]["knowledge_maturation_enabled"] is True

    def test_profile_applies_configurable_hybrid_search(self):
        profile = build_dialogue_ab_profile_payload(
            _dialogue_request(
                search_mode="hybrid",
                retrieval_execution="intent_gated",
                vector_search_limit=2,
                vector_search_threshold=0.55,
                deep_search_enabled=False,
                bm25_candidates=30,
                vector_candidates=15,
                rrf_k=40,
                bm25_max_df_ratio=0.4,
            )
        )

        assert profile["brain"]["search_mode"] == "hybrid"
        assert profile["brain"]["bm25_candidates"] == 30
        assert profile["brain"]["vector_candidates"] == 15
        assert profile["brain"]["rrf_k"] == 40
        assert profile["brain"]["bm25_max_df_ratio"] == pytest.approx(0.4)
        assert profile["memory_probe"] == {
            "retrieval_execution": "intent_gated",
            "injection_policy": "intent_gated",
            "vector_search_limit": 2,
            "vector_search_threshold": 0.55,
            "deep_search_enabled": False,
        }

    def test_profile_applies_dual_query_search(self):
        profile = build_dialogue_ab_profile_payload(
            _dialogue_request(
                search_mode="dual_query",
                dual_query_candidates=12,
                dual_query_pool_limit=22,
                rrf_k=45,
            )
        )

        assert profile["brain"]["search_mode"] == "dual_query"
        assert profile["brain"]["dual_query_candidates"] == 12
        assert profile["brain"]["dual_query_pool_limit"] == 22
        assert profile["brain"]["rrf_k"] == 45

    def test_profile_applies_hybrid_evidence_fusion(self):
        profile = build_dialogue_ab_profile_payload(
            _dialogue_request(
                search_mode="hybrid_evidence_fusion",
                evidence_fusion_base_weight=0.6,
                evidence_raw_chunk_chars=2400,
            )
        )

        assert profile["brain"]["search_mode"] == "hybrid_evidence_fusion"
        assert profile["brain"]["evidence_fusion_base_weight"] == pytest.approx(
            0.6
        )
        assert profile["brain"]["evidence_raw_chunk_chars"] == 2400

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"search_mode": "bm25"}, "unsupported search_mode"),
            (
                {"retrieval_execution": "never"},
                "unsupported retrieval_execution",
            ),
            (
                {"vector_search_limit": 0},
                "vector_search_limit must be a positive",
            ),
            (
                {"candidates_vector_search_limit": 0},
                "candidates_vector_search_limit must be a positive",
            ),
            (
                {"vector_search_threshold": 1.1},
                "vector_search_threshold must be in",
            ),
            (
                {"bm25_candidates": 0},
                "bm25_candidates must be a positive",
            ),
            (
                {"bm25_max_df_ratio": 0.0},
                "bm25_max_df_ratio must be in",
            ),
            (
                {"evidence_fusion_base_weight": -0.1},
                "evidence_fusion_base_weight must be in",
            ),
        ],
    )
    def test_rejects_bad_search_settings(
        self, tmp_path, overrides, message
    ):
        with pytest.raises(EvaluationJobError, match=message):
            validate_dialogue_ab_request(
                _dialogue_request(**overrides),
                output_dir=tmp_path,
            )

    def test_builds_dialogue_cli_command(self, tmp_path):
        profile = tmp_path / "profile.yaml"
        command = build_dialogue_ab_command(
            _dialogue_request(
                intent_gated_vector_search_limit=3,
                candidates_vector_search_limit=2,
            ),
            output_dir=tmp_path,
            profile_path=profile,
            python_executable="/python",
        )

        assert command[:5] == [
            "/python",
            "-m",
            "evals.dialogue_ab",
            "run",
            "--dataset",
        ]
        assert command[command.index("--profile") + 1] == str(profile)
        assert command[
            command.index("--intent-gated-search-limit") + 1
        ] == "3"
        assert command[
            command.index("--candidates-search-limit") + 1
        ] == "2"

    def test_config_keeps_last_requests_separate(self, tmp_path):
        manager = EvaluationJobManager(tmp_path)
        manager._records = {
            "locomo": {
                "job_type": "locomo",
                "created_at": "2026-07-27T00:00:00+00:00",
                "request": _request(run_id="locomo-v1"),
            },
            "dialogue": {
                "job_type": "dialogue_ab",
                "created_at": "2026-07-28T00:00:00+00:00",
                "request": _dialogue_request(run_id="dialogue-v1"),
            },
        }

        config = manager.config()

        assert config["last_request"]["run_id"] == "locomo-v1"
        assert (
            config["dialogue_ab"]["last_request"]["run_id"]
            == "dialogue-v1"
        )
        assert config["dialogue_ab"]["search_modes"] == [
            "vector",
            "hybrid",
            "dual_query",
            "hybrid_evidence_fusion",
        ]
        assert config["dialogue_ab"]["retrieval_executions"] == [
            "always",
            "intent_gated",
        ]

    def test_rejects_non_dialogue_dataset(self, tmp_path):
        with pytest.raises(EvaluationJobError, match="root must be an object"):
            validate_dialogue_ab_request(
                _dialogue_request(dataset_path=str(FIXTURE)),
                output_dir=tmp_path,
            )


class TestRetrievalReplayEndpointBacking:
    """QA を回さずに検索だけ比較する（検索改修計画 §8）。"""

    def _write_run(self, output_dir: Path, run_id: str = "v26") -> Path:
        import sqlite3

        from butly_core.core.database import ButlyDatabase

        run_dir = output_dir / run_id
        (run_dir / "results").mkdir(parents=True)
        instance_dir = (
            run_dir / "workspace" / "butly_core" / "instances" / "conv_1"
        )
        (instance_dir / "short_term_json").mkdir(parents=True)

        db_path = instance_dir / "butly_memory.db"
        ButlyDatabase(db_path=str(db_path))
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO knowledge_cards (id, category, title, summary, "
            "source_files) VALUES (?, ?, ?, ?, ?)",
            ("k1", "Life", "Pottery workshop", "made mugs",
             json.dumps(["session_0001.json"])),
        )
        conn.commit()
        conn.close()

        (instance_dir / "short_term_json" / "session_0001.json").write_text(
            json.dumps({
                "messages": [{
                    "role": "user",
                    "parts": ["We made mugs."],
                    "meta": {"locomo_dialog_ids": ["D1:1"]},
                }]
            }),
            encoding="utf-8",
        )
        (run_dir / "results" / "qa_results.jsonl").write_text(
            json.dumps({
                "question_id": "qa-1",
                "question": "What pottery did they make?",
                "category": 2,
                "instance_name": "conv_1",
                "evidence": ["D1:1"],
                "retrieved_card_ids": [],
            }) + "\n",
            encoding="utf-8",
        )
        (run_dir / "run_config.json").write_text(
            json.dumps({"run_id": run_id}), encoding="utf-8"
        )
        return run_dir

    def _manager(self, tmp_path: Path) -> EvaluationJobManager:
        return EvaluationJobManager(
            tmp_path / "data",
            output_dir=tmp_path / "runs",
            project_root=Path(__file__).parents[1],
        )

    def test_bm25_replay_returns_recall_and_persists(self, tmp_path):
        manager = self._manager(tmp_path)
        run_dir = self._write_run(tmp_path / "runs")

        result = manager.retrieval_replay("v26", ["bm25"])

        assert result["oracle_questions"] == 1
        assert result["bm25"]["recall_at_3"] == pytest.approx(1.0)
        saved = json.loads(
            (run_dir / "retrieval_replay.json").read_text(encoding="utf-8")
        )
        assert saved["bm25"] == result["bm25"]
        assert saved["limit"] == 20

    def test_unknown_run_raises_key_error(self, tmp_path):
        manager = self._manager(tmp_path)

        with pytest.raises(KeyError):
            manager.retrieval_replay("missing", ["bm25"])

    @pytest.mark.parametrize(
        ("run_id", "modes", "limit", "message"),
        [
            ("../escape", ["bm25"], 20, "invalid run_id"),
            ("v26", ["keyword"], 20, "modes must be a subset"),
            ("v26", [], 20, "modes must be a subset"),
            ("v26", ["bm25"], 0, "limit must be a positive integer"),
        ],
    )
    def test_rejects_bad_arguments(
        self, tmp_path, run_id, modes, limit, message
    ):
        manager = self._manager(tmp_path)
        self._write_run(tmp_path / "runs")

        with pytest.raises(EvaluationJobError, match=message):
            manager.retrieval_replay(run_id, modes, limit=limit)

    def test_run_without_workspace_reports_error(self, tmp_path):
        manager = self._manager(tmp_path)
        run_dir = tmp_path / "runs" / "bare"
        (run_dir / "results").mkdir(parents=True)
        (run_dir / "run_config.json").write_text("{}", encoding="utf-8")
        (run_dir / "results" / "qa_results.jsonl").write_text(
            "", encoding="utf-8"
        )

        with pytest.raises(EvaluationJobError):
            manager.retrieval_replay("bare", ["bm25"])

    def test_missing_profile_does_not_block_replay(self, tmp_path):
        manager = self._manager(tmp_path)
        run_dir = self._write_run(tmp_path / "runs")
        (run_dir / "run_config.json").write_text(
            json.dumps({
                "run_id": "v26",
                "profile_path": str(tmp_path / "gone.yaml"),
            }),
            encoding="utf-8",
        )

        result = manager.retrieval_replay("v26", ["bm25"])

        assert result["bm25"]["questions"] == 1

    def test_profile_sections_are_used(self, tmp_path):
        manager = self._manager(tmp_path)
        run_dir = self._write_run(tmp_path / "runs")
        profile_path = tmp_path / "profile.yaml"
        profile_path.write_text(
            "name: p\nlocale: ja\nbrain:\n  bm25_max_df_ratio: 0.9\n",
            encoding="utf-8",
        )
        (run_dir / "run_config.json").write_text(
            json.dumps({"run_id": "v26", "profile_path": str(profile_path)}),
            encoding="utf-8",
        )

        sections = manager._run_profile_sections(run_dir)
        assert sections["brain"] == {
            "bm25_max_df_ratio": 0.9
        }
        assert sections["agent_profile"] == {"locale": "ja"}

    def test_background_replay_builds_persistent_cross_encoder_job(
        self, tmp_path, monkeypatch
    ):
        manager = self._manager(tmp_path)
        run_dir = self._write_run(tmp_path / "runs")
        captured = {}

        def fake_launch(record, command):
            captured["command"] = command
            manager._save_record(record)
            return manager._public_record(record)

        monkeypatch.setattr(manager, "_launch", fake_launch)
        job = manager.start_retrieval_replay(
            {
                "run_id": "v26",
                "modes": ["vector", "reranked"],
                "limit": 20,
                "reranker": {
                    "engine": "cross_encoder",
                    "model_name": "mminilmv2",
                    "batch_size": 8,
                    "score_threshold": -1.0,
                    "device": "cpu",
                },
                "reranker_max_candidate_chars": 1200,
            }
        )

        assert job["job_type"] == "retrieval_replay"
        assert job["status"] == "queued"
        assert job["result_path"] == str(
            run_dir / "retrieval_replay.json"
        )
        command = captured["command"]
        assert command[:3] == [
            sys.executable,
            "-m",
            "evals.locomo.retrieval_replay",
        ]
        assert command[command.index("--reranker-engine") + 1] == (
            "cross_encoder"
        )
        assert command[command.index("--reranker-batch-size") + 1] == "8"
        assert command[command.index("--reranker-score-threshold") + 1] == (
            "-1.0"
        )
        assert command[command.index("--job-id") + 1] == job["job_id"]

    def test_background_evidence_replay_persists_cache_settings(
        self, tmp_path, monkeypatch
    ):
        manager = self._manager(tmp_path)
        run_dir = self._write_run(tmp_path / "runs")
        captured = {}

        def fake_launch(record, command):
            captured["command"] = command
            return manager._public_record(record)

        monkeypatch.setattr(manager, "_launch", fake_launch)
        job = manager.start_retrieval_replay(
            {
                "run_id": "v26",
                "modes": [
                    "vector",
                    "hybrid_evidence_fusion_w40",
                    "hybrid_evidence_fusion_mmr",
                ],
                "limit": 20,
                "evidence_raw_chunk_chars": 1400,
                "evidence_fusion_base_weight": 0.65,
                "evidence_mmr_lambda": 0.75,
            }
        )

        command = captured["command"]
        assert command[command.index("--evidence-raw-chunk-chars") + 1] == (
            "1400"
        )
        assert command[command.index("--evidence-cache") + 1] == str(
            run_dir / "retrieval_cache" / "evidence_embeddings.sqlite3"
        )
        assert command[
            command.index("--evidence-fusion-base-weight") + 1
        ] == "0.65"
        assert command[
            command.index("--evidence-mmr-lambda") + 1
        ] == "0.75"
        request = manager._records[job["job_id"]]["request"]
        assert request["evidence_raw_chunk_chars"] == 1400
        assert request["evidence_fusion_base_weight"] == 0.65
        assert request["evidence_mmr_lambda"] == 0.75

    def test_evidence_replay_rejects_invalid_chunk_size(self, tmp_path):
        manager = self._manager(tmp_path)
        self._write_run(tmp_path / "runs")

        with pytest.raises(
            EvaluationJobError,
            match="evidence_raw_chunk_chars must be between",
        ):
            manager.start_retrieval_replay(
                {
                    "run_id": "v26",
                    "modes": ["evidence_rerank"],
                    "evidence_raw_chunk_chars": 100,
                }
            )

    def test_evidence_fusion_rejects_invalid_weight(self, tmp_path):
        manager = self._manager(tmp_path)
        self._write_run(tmp_path / "runs")

        with pytest.raises(
            EvaluationJobError,
            match="evidence_fusion_base_weight must be between",
        ):
            manager.start_retrieval_replay(
                {
                    "run_id": "v26",
                    "modes": ["hybrid_evidence_fusion"],
                    "evidence_fusion_base_weight": 1.1,
                }
            )

    def test_evidence_mmr_rejects_invalid_lambda(self, tmp_path):
        manager = self._manager(tmp_path)
        self._write_run(tmp_path / "runs")

        with pytest.raises(
            EvaluationJobError,
            match="evidence_mmr_lambda must be between",
        ):
            manager.start_retrieval_replay(
                {
                    "run_id": "v26",
                    "modes": ["hybrid_evidence_fusion_mmr"],
                    "evidence_mmr_lambda": -0.1,
                }
            )

    def test_background_replay_completion_requires_fresh_matching_artifact(
        self, tmp_path, monkeypatch
    ):
        manager = self._manager(tmp_path)
        run_dir = self._write_run(tmp_path / "runs")
        monkeypatch.setattr(
            manager,
            "_launch",
            lambda record, _command: manager._public_record(record),
        )
        job = manager.start_retrieval_replay(
            {"run_id": "v26", "modes": ["bm25"], "limit": 20}
        )
        record = manager._records[job["job_id"]]
        record["started_at"] = "2026-08-09T10:00:00+00:00"
        result_path = run_dir / "retrieval_replay.json"
        payload = {
            "status": "completed",
            "generated_at": "2026-08-09T10:00:01+00:00",
            "job_id": job["job_id"],
            "modes": ["bm25"],
            "limit": 20,
        }
        result_path.write_text(json.dumps(payload), encoding="utf-8")

        assert manager._retrieval_replay_output_complete(record) is True
        assert manager.get_retrieval_replay_result("v26") == payload

        payload["job_id"] = "older-job"
        result_path.write_text(json.dumps(payload), encoding="utf-8")
        assert manager._retrieval_replay_output_complete(record) is False

        payload["job_id"] = job["job_id"]
        payload["generated_at"] = "2026-08-09T09:59:59+00:00"
        result_path.write_text(json.dumps(payload), encoding="utf-8")
        assert manager._retrieval_replay_output_complete(record) is False

    def test_background_replay_resume_rebuilds_same_command(
        self, tmp_path, monkeypatch
    ):
        manager = self._manager(tmp_path)
        self._write_run(tmp_path / "runs")
        captured = {}

        def fake_launch(record, command):
            captured["command"] = command
            return manager._public_record(record)

        monkeypatch.setattr(manager, "_launch", fake_launch)
        job = manager.start_retrieval_replay(
            {"run_id": "v26", "modes": ["bm25"], "limit": 10}
        )
        record = manager._records[job["job_id"]]
        record["status"] = "failed"
        record["ended_at"] = "2026-08-09T10:00:00+00:00"
        record["return_code"] = 1

        resumed = manager.resume(job["job_id"])

        assert resumed["status"] == "queued"
        command = captured["command"]
        assert command[command.index("--limit") + 1] == "10"
        assert command[command.index("--job-id") + 1] == job["job_id"]

    def test_background_bm25_job_reports_progress_and_result(self, tmp_path):
        manager = self._manager(tmp_path)
        self._write_run(tmp_path / "runs")

        job = manager.start_retrieval_replay(
            {"run_id": "v26", "modes": ["bm25"], "limit": 20}
        )
        deadline = time.monotonic() + 10
        current = manager.get(job["job_id"])
        while (
            current["status"] in web_jobs.ACTIVE_JOB_STATUSES
            and time.monotonic() < deadline
        ):
            time.sleep(0.05)
            current = manager.get(job["job_id"])

        assert current["status"] == "completed"
        assert current["progress"] == 100.0
        log = manager.read_log(job["job_id"])
        assert "[LoCoMo" in log
        assert "question=1/1" in log
        result = manager.get_retrieval_replay_result("v26")
        assert result["job_id"] == job["job_id"]
        assert result["bm25"]["recall_at_3"] == pytest.approx(1.0)


def test_build_retrieval_replay_command_uses_profile_and_llm_settings(
    tmp_path,
):
    request = {
        "run_dir": str(tmp_path / "run"),
        "modes": ["reranked"],
        "limit": 20,
        "result_path": str(tmp_path / "run" / "retrieval_replay.json"),
        "job_id": "job-1",
        "profile_path": str(tmp_path / "profile.yaml"),
        "reranker_max_candidate_chars": 1400,
        "reranker": {
            "engine": "llm",
            "connection": "nanogpt-sub",
            "model_name": "model",
            "generation_config": {"max_output_tokens": 3072},
        },
    }

    command = build_retrieval_replay_command(
        request,
        python_executable="python-test",
    )

    assert command[command.index("--profile") + 1] == request["profile_path"]
    assert command[command.index("--reranker-connection") + 1] == (
        "nanogpt-sub"
    )
    assert command[command.index("--reranker-max-output-tokens") + 1] == (
        "3072"
    )


class TestDialogueABSeedInstance:
    """既存インスタンスを種にする指定が CLI まで通ること。"""

    def _instance(self, root: Path, name: str = "SeedInst") -> Path:
        from butly_core.core.database import ButlyDatabase

        d = root / "butly_core" / "instances" / name
        d.mkdir(parents=True)
        ButlyDatabase(db_path=str(d / "butly_memory.db"))
        return d

    def test_validate_resolves_instance_path(self, tmp_path, monkeypatch):
        self._instance(tmp_path)
        monkeypatch.setattr(web_jobs, "PROJECT_ROOT", tmp_path)

        normalized = validate_dialogue_ab_request(
            _dialogue_request(seed_instance="SeedInst"),
            output_dir=tmp_path / "runs",
        )

        assert normalized["seed_instance"] == "SeedInst"
        assert normalized["seed_instance_path"].endswith("instances/SeedInst")
        assert normalized["reembed"] is False

    def test_validate_rejects_unknown_instance(self, tmp_path, monkeypatch):
        monkeypatch.setattr(web_jobs, "PROJECT_ROOT", tmp_path)

        with pytest.raises(EvaluationJobError, match="seed instance not found"):
            validate_dialogue_ab_request(
                _dialogue_request(seed_instance="Missing"),
                output_dir=tmp_path / "runs",
            )

    def test_validate_rejects_unsafe_instance_name(self, tmp_path, monkeypatch):
        monkeypatch.setattr(web_jobs, "PROJECT_ROOT", tmp_path)

        with pytest.raises(EvaluationJobError, match="invalid seed_instance"):
            validate_dialogue_ab_request(
                _dialogue_request(seed_instance="../escape"),
                output_dir=tmp_path / "runs",
            )

    def test_command_passes_seed_and_reembed(self, tmp_path):
        command = build_dialogue_ab_command(
            _dialogue_request(
                seed_instance_path=str(tmp_path / "Jarvis"), reembed=True
            ),
            output_dir=tmp_path,
            profile_path=tmp_path / "p.yaml",
            python_executable="python-test",
        )

        assert "--seed-instance" in command
        assert str(tmp_path / "Jarvis") in command
        assert "--reembed" in command

    def test_command_omits_flags_without_seed(self, tmp_path):
        command = build_dialogue_ab_command(
            _dialogue_request(),
            output_dir=tmp_path,
            profile_path=tmp_path / "p.yaml",
        )

        assert "--seed-instance" not in command
        assert "--reembed" not in command

    def test_config_lists_seed_instances(self, tmp_path):
        self._instance(tmp_path, "Alpha")
        (tmp_path / "butly_core" / "instances" / "NoDb").mkdir(parents=True)
        manager = EvaluationJobManager(
            tmp_path / "data",
            output_dir=tmp_path / "runs",
            project_root=tmp_path,
        )

        dialogue = manager.config()["dialogue_ab"]

        assert dialogue["seed_instances"] == ["Alpha"]
        assert dialogue["rag_source_modes"] == ["cards", "raw", "both"]


def test_build_fresh_command_preserves_all_scope_flags(tmp_path):
    command = build_job_command(
        _request(
            sample_limit=None,
            session_limit=None,
            question_limit=None,
        ),
        output_dir=tmp_path,
        profile_path=tmp_path / "profile.yaml",
        python_executable="python-test",
    )

    assert command[:4] == [
        "python-test",
        "-m",
        "evals.locomo.cli",
        "run",
    ]
    assert "--all-samples" in command
    assert "--all-sessions" in command
    assert "--all-questions" in command
    assert command[command.index("--workflow") + 1] == "full"


def test_exact_sample_ids_override_sample_limit_in_command(tmp_path):
    normalized = validate_job_request(
        _request(
            sample_ids=["synthetic-conv-1"],
            sample_limit=1,
            workflow="retrieval_prep",
        ),
        output_dir=tmp_path,
    )

    command = build_job_command(
        normalized,
        output_dir=tmp_path,
        profile_path=tmp_path / "profile.yaml",
        python_executable="python-test",
    )

    assert normalized["sample_limit"] is None
    assert command[command.index("--workflow") + 1] == "retrieval_prep"
    assert command[command.index("--sample-ids") + 1] == "synthetic-conv-1"
    assert "--sample-limit" not in command


def test_dataset_description_lists_exact_sample_scope():
    result = describe_locomo_dataset(FIXTURE)

    assert result["sample_count"] == 1
    assert result["locale"] == "en"
    assert result["samples"][0] == {
        "sample_id": "synthetic-conv-1",
        "session_count": 2,
        "question_count": 5,
        "speaker_a": "Maya",
        "speaker_b": "Noah",
    }


def test_job_request_keeps_prompt_and_dataset_locales_separate(tmp_path):
    normalized = validate_job_request(
        _request(locale="ja"),
        output_dir=tmp_path,
    )

    assert normalized["locale"] == "ja"
    assert normalized["dataset_locale"] == "en"


def test_build_reuse_command_enables_stage3_bootstrap(tmp_path):
    command = build_job_command(
        _request(
            run_mode="stage3-on",
            source_memory_run_id="source",
        ),
        output_dir=tmp_path,
        profile_path=tmp_path / "profile.yaml",
    )

    assert "rerun-qa" in command
    assert str(tmp_path / "source") in command
    assert "--stage3-bootstrap" in command
    assert "--sample-limit" not in command


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"run_mode": "stage3-on", "source_memory_run_id": None},
            "requires source_memory_run_id",
        ),
        (
            {
                "run_mode": "stage3-source",
                "qa_mode": "sequential",
            },
            "requires qa_mode=independent",
        ),
        (
            {"run_id": "../escape"},
            "run_id must start",
        ),
        (
            {"sample_ids": ["missing-sample"]},
            "unknown sample_ids",
        ),
        (
            {
                "workflow": "retrieval_prep",
                "role_models": {
                    **_request()["role_models"],
                    "judge": {
                        "connection": "openai",
                        "model_name": "gpt-4o-mini",
                    },
                },
            },
            "judge must be disabled",
        ),
        (
            {
                "workflow": "retrieval_prep",
                "run_mode": "stage3-on",
            },
            "cannot use Stage 3 QA-only clone modes",
        ),
    ],
)
def test_validate_rejects_unsafe_or_inconsistent_jobs(
    tmp_path,
    overrides,
    message,
):
    with pytest.raises(EvaluationJobError, match=message):
        validate_job_request(
            _request(**overrides),
            output_dir=tmp_path,
        )


def test_manager_persists_non_secret_profile_and_job_state(
    tmp_path,
    monkeypatch,
):
    manager = EvaluationJobManager(
        tmp_path / "data",
        output_dir=tmp_path / "runs",
        project_root=Path(__file__).parents[1],
    )

    def fake_launch(record, command):
        record["command"] = command
        manager._save_record(record)
        return manager._public_record(record)

    monkeypatch.setattr(manager, "_launch", fake_launch)
    job = manager.start(_request())

    assert job["status"] == "queued"
    assert "command" not in job
    profile_text = Path(job["profile_path"]).read_text(encoding="utf-8")
    assert "nanogpt-sub" in profile_text
    assert "api_key" not in profile_text.lower()
    persisted = json.loads(
        (manager.jobs_dir / f"{job['job_id']}.json").read_text(
            encoding="utf-8"
        )
    )
    assert persisted["request"]["role_models"]["chat"]["model_name"] == (
        "qwen3-14b"
    )


def test_manager_defaults_to_eval_runs_even_when_docs_temp_exists(
    tmp_path,
    monkeypatch,
):
    monkeypatch.delenv("BUTLY_EVALUATION_OUTPUT_DIR", raising=False)
    project_root = tmp_path / "project"
    (project_root / "docs" / "temp").mkdir(parents=True)
    data_dir = tmp_path / "data"

    manager = EvaluationJobManager(
        data_dir,
        project_root=project_root,
    )

    assert manager.output_dir == (data_dir / "eval_runs" / "runs").resolve()


def test_manager_rejects_second_active_job(tmp_path, monkeypatch):
    manager = EvaluationJobManager(
        tmp_path / "data",
        output_dir=tmp_path / "runs",
    )

    monkeypatch.setattr(
        manager,
        "_launch",
        lambda record, command: manager._public_record(record),
    )
    monkeypatch.setattr(manager, "_refresh_records", lambda: None)
    manager.start(_request(run_id="first"))

    with pytest.raises(EvaluationJobConflict, match="another evaluation job"):
        manager.start(_request(run_id="second"))


def test_manager_monitors_real_subprocess_to_completion(tmp_path, monkeypatch):
    manager = EvaluationJobManager(
        tmp_path / "data",
        output_dir=tmp_path / "runs",
    )
    monkeypatch.setattr(
        web_jobs,
        "build_job_command",
        lambda *args, **kwargs: [
            sys.executable,
            "-c",
            (
                "print('[LoCoMo  42.0%] [1/2] qa         | halfway', "
                "flush=True)"
            ),
        ],
    )

    job = manager.start(_request(run_id="short-process"))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = manager.get(job["job_id"])
        if job["status"] == "completed":
            break
        time.sleep(0.05)

    assert job["status"] == "completed"
    assert job["progress"] == 100.0
    assert job["return_code"] == 0
    assert "halfway" in manager.read_log(job["job_id"])


def test_manager_stops_real_subprocess(tmp_path, monkeypatch):
    manager = EvaluationJobManager(
        tmp_path / "data",
        output_dir=tmp_path / "runs",
    )
    monkeypatch.setattr(
        web_jobs,
        "build_job_command",
        lambda *args, **kwargs: [
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
        ],
    )

    job = manager.start(_request(run_id="stopped-process"))
    stopped = manager.stop(job["job_id"])
    deadline = time.monotonic() + 5
    while (
        stopped["status"] in {"queued", "running", "stopping"}
        and time.monotonic() < deadline
    ):
        time.sleep(0.05)
        stopped = manager.get(job["job_id"])

    assert stopped["status"] == "stopped"
    assert stopped["pid"] is None


def test_manager_resume_uses_existing_cli_checkpoint(tmp_path, monkeypatch):
    manager = EvaluationJobManager(
        tmp_path / "data",
        output_dir=tmp_path / "runs",
        python_executable="python-test",
    )
    run_dir = manager.output_dir / "resume-run"
    run_dir.mkdir(parents=True)
    (run_dir / "run_config.json").write_text(
        json.dumps({"run_id": "resume-run"}),
        encoding="utf-8",
    )
    record = {
        "schema_version": 1,
        "job_id": "resume-job",
        "run_id": "resume-run",
        "run_mode": "standard",
        "status": "stopped",
        "run_dir": str(run_dir),
        "log_path": str(manager.jobs_dir / "resume-job.log"),
        "attempt": 1,
        "stop_requested": True,
        "created_at": "2026-01-01T00:00:00Z",
    }
    manager._save_record(record)
    captured = {}

    def fake_launch(current, command):
        captured["command"] = command
        return manager._public_record(current)

    monkeypatch.setattr(manager, "_launch", fake_launch)
    resumed = manager.resume("resume-job")

    assert captured["command"] == [
        "python-test",
        "-m",
        "evals.locomo.cli",
        "resume",
        "--run-dir",
        str(run_dir),
    ]
    assert resumed["status"] == "queued"
    assert resumed["stop_requested"] is False


def test_manager_resume_uses_dialogue_ab_checkpoint(tmp_path, monkeypatch):
    manager = EvaluationJobManager(
        tmp_path / "data",
        output_dir=tmp_path / "runs",
        python_executable="python-test",
    )
    run_dir = manager.dialogue_output_dir / "dialogue-resume"
    run_dir.mkdir(parents=True)
    (run_dir / "run_config.json").write_text(
        json.dumps(
            {"run_id": "dialogue-resume", "run_type": "dialogue_ab"}
        ),
        encoding="utf-8",
    )
    record = {
        "schema_version": 1,
        "job_type": "dialogue_ab",
        "job_id": "dialogue-resume-job",
        "run_id": "dialogue-resume",
        "run_mode": "dialogue-ab",
        "status": "stopped",
        "run_dir": str(run_dir),
        "log_path": str(manager.jobs_dir / "dialogue-resume-job.log"),
        "attempt": 1,
        "stop_requested": True,
        "created_at": "2026-01-01T00:00:00Z",
    }
    manager._save_record(record)
    captured = {}

    def fake_launch(current, command):
        captured["command"] = command
        return manager._public_record(current)

    monkeypatch.setattr(manager, "_launch", fake_launch)
    manager.resume("dialogue-resume-job")

    assert captured["command"] == [
        "python-test",
        "-m",
        "evals.dialogue_ab",
        "resume",
        "--run-dir",
        str(run_dir),
    ]


def test_run_history_and_question_comparison(tmp_path):
    output_dir = tmp_path / "runs"
    _write_run(output_dir, "a", overall=0.5, prediction="Tuesday")
    _write_run(output_dir, "b", overall=1.0, prediction="Monday")
    manager = EvaluationJobManager(
        tmp_path / "data",
        output_dir=output_dir,
    )

    runs = manager.list_runs()
    assert [run["run_id"] for run in runs] == ["b", "a"]
    assert runs[0]["overall"] == 1.0

    comparison = manager.compare_runs(["a", "b"])
    assert comparison["baseline_run_id"] == "a"
    assert comparison["comparison_run_id"] == "b"
    assert comparison["questions"][0]["delta"] == 0.5
    assert comparison["questions"][0]["runs"]["b"]["prediction"] == "Monday"


def _write_retrieval_result(
    run_dir: Path,
    *,
    sample_id: str,
    recall_at_3: float,
    limit: int = 20,
) -> None:
    detail = {
        "sample_id": sample_id,
        "question_id": "q1",
        "question": "What happened?",
        "recall_at_1": recall_at_3,
        "recall_at_3": recall_at_3,
        "recall_at_20": 1.0,
    }
    (run_dir / "retrieval_replay.json").write_text(
        json.dumps(
            {
                "run": str(run_dir),
                "status": "completed",
                "generated_at": "2026-08-10T00:00:00+00:00",
                "limit": limit,
                "modes": ["vector"],
                "oracle_questions": 1,
                "vector": {
                    "questions": 1,
                    "recall_at_1": recall_at_3,
                    "recall_at_3": recall_at_3,
                    "recall_at_20": 1.0,
                    "hit_at_1": int(recall_at_3 > 0),
                    "hit_at_3": int(recall_at_3 > 0),
                    "hit_at_20": 1,
                    "details": [detail],
                },
            }
        ),
        encoding="utf-8",
    )


def test_saved_retrieval_results_can_be_compared_across_runs(tmp_path):
    output_dir = tmp_path / "runs"
    run_a = _write_run(output_dir, "a", overall=0.5, prediction="Tuesday")
    run_b = _write_run(output_dir, "b", overall=1.0, prediction="Monday")
    _write_retrieval_result(run_a, sample_id="sample", recall_at_3=0.0)
    _write_retrieval_result(run_b, sample_id="sample", recall_at_3=1.0)
    manager = EvaluationJobManager(tmp_path / "data", output_dir=output_dir)

    comparison = manager.compare_retrieval_runs(["a", "b"])

    assert comparison["comparable"] is True
    assert comparison["common_modes"] == ["vector"]
    target_metric = next(
        item
        for item in comparison["metrics"]
        if item["run_id"] == "b" and item["mode"] == "vector"
    )
    assert target_metric["delta_vs_baseline_at_3"] == pytest.approx(1.0)
    assert comparison["questions"][0]["delta_at_3"] == pytest.approx(1.0)
    summaries = {item["run_id"]: item for item in manager.list_runs()}
    assert summaries["a"]["has_retrieval_replay"] is True
    assert summaries["a"]["retrieval_replay_modes"] == ["vector"]


def test_retrieval_run_comparison_warns_for_different_scope(tmp_path):
    output_dir = tmp_path / "runs"
    run_a = _write_run(output_dir, "a", overall=0.5, prediction="Tuesday")
    run_b = _write_run(output_dir, "b", overall=1.0, prediction="Monday")
    _write_retrieval_result(run_a, sample_id="sample-a", recall_at_3=0.0)
    _write_retrieval_result(
        run_b,
        sample_id="sample-b",
        recall_at_3=1.0,
        limit=10,
    )
    manager = EvaluationJobManager(tmp_path / "data", output_dir=output_dir)

    comparison = manager.compare_retrieval_runs(["a", "b"])

    assert comparison["comparable"] is False
    assert comparison["question_set_match"] is False
    assert comparison["limit_match"] is False
    assert len(comparison["warnings"]) == 2


def test_retrieval_run_comparison_requires_saved_results(tmp_path):
    output_dir = tmp_path / "runs"
    run_a = _write_run(output_dir, "a", overall=0.5, prediction="Tuesday")
    _write_retrieval_result(run_a, sample_id="sample", recall_at_3=0.0)
    _write_run(output_dir, "b", overall=1.0, prediction="Monday")
    manager = EvaluationJobManager(tmp_path / "data", output_dir=output_dir)

    with pytest.raises(EvaluationJobError, match="not found for run: b"):
        manager.compare_retrieval_runs(["a", "b"])


def test_retrieval_prep_run_is_replayable_without_scores(tmp_path):
    output_dir = tmp_path / "runs"
    run_dir = output_dir / "prep-conv-30"
    (run_dir / "checkpoints").mkdir(parents=True)
    (run_dir / "results").mkdir()
    (run_dir / "run_config.json").write_text(
        json.dumps(
            {
                "run_id": "prep-conv-30",
                "created_at": "2026-08-10T00:00:00+00:00",
                "workflow": "retrieval_prep",
                "selected_sample_ids": ["conv-30"],
                "sample_ids": ["conv-30"],
                "sample_limit": None,
                "question_limit": None,
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "checkpoints" / "checkpoint.json").write_text(
        json.dumps(
            {
                "run_id": "prep-conv-30",
                "status": "completed",
            }
        ),
        encoding="utf-8",
    )
    questions = [
        {
            "sample_id": "conv-30",
            "question_id": "q1",
            "question": "What happened?",
            "instance_name": "locomo_conv_30",
            "category": 2,
            "evidence": ["D1:1"],
        }
    ]
    (run_dir / "results" / "retrieval_questions.json").write_text(
        json.dumps(
            {
                "run_id": "prep-conv-30",
                "workflow": "retrieval_prep",
                "sample_ids": ["conv-30"],
                "question_count": 1,
                "questions": questions,
            }
        ),
        encoding="utf-8",
    )
    manager = EvaluationJobManager(
        tmp_path / "data",
        output_dir=output_dir,
    )

    summary = manager.list_runs()[0]

    assert summary["status"] == "retrieval_ready"
    assert summary["has_scores"] is False
    assert summary["has_retrieval_questions"] is True
    assert summary["retrieval_question_count"] == 1
    assert summary["selected_sample_ids"] == ["conv-30"]
    assert manager._retrieval_prep_output_complete(
        {
            "run_id": "prep-conv-30",
            "run_dir": str(run_dir),
            "workflow": "retrieval_prep",
        }
    )


def test_locomo_semantic_detail_and_history_reject_stale_artifact(tmp_path):
    output_dir = tmp_path / "runs"
    run_dir = _write_run(
        output_dir,
        "semantic-run",
        overall=0.0,
        prediction="Monday",
    )
    scores_path = run_dir / "scores.json"
    scores = json.loads(scores_path.read_text(encoding="utf-8"))
    first = scores["questions"][0]
    second = {
        **first,
        "question_id": "q2",
        "prediction": "Tuesday",
        "official_score": 1.0,
    }
    scores["questions"] = [first, second]
    scores["question_count"] = 2
    scores_path.write_text(json.dumps(scores), encoding="utf-8")
    config = JudgeConfig(
        model_name="judge-model",
        connection="judge-connection",
    )
    semantic = {
        "run_id": "semantic-run",
        "status": "completed",
        "question_count": 2,
        "judged_count": 2,
        "error_count": 0,
        "coverage": 1.0,
        "judge": config.public_dict(),
        "config_signature": config.signature(),
        "question_set_fingerprint": calculate_question_set_fingerprint(
            scores,
            config,
        ),
        "summary": {
            "normalized_score_mean": 0.5,
            "pass_rate": 0.5,
            "official_disagreement": {
                "possible_false_negative_count": 1,
                "possible_false_positive_count": 1,
            },
        },
        "questions": [
            {
                "sample_id": "sample",
                "question_id": "q1",
                "status": "complete",
                "verdict": "correct",
                "normalized_score": 1.0,
                "confidence": "high",
                "contradiction": False,
                "missing_critical": False,
                "reason": "equivalent answer",
            },
            {
                "sample_id": "sample",
                "question_id": "q2",
                "status": "complete",
                "verdict": "incorrect",
                "normalized_score": 0.0,
                "confidence": "high",
                "contradiction": True,
                "missing_critical": False,
                "reason": "wrong date",
            },
        ],
    }
    (run_dir / "semantic_scores.json").write_text(
        json.dumps(semantic),
        encoding="utf-8",
    )
    manager = EvaluationJobManager(
        tmp_path / "data",
        output_dir=output_dir,
    )

    summary = manager.list_runs()[0]
    detail = manager.get_run_result("semantic-run")

    assert summary["semantic_status"] == "completed"
    assert summary["semantic_review_count"] == 2
    assert detail["review_required_count"] == 2
    assert [
        item["official_disagreement"] for item in detail["questions"]
    ] == ["possible_false_negative", "possible_false_positive"]

    scores["questions"][0]["prediction"] = "changed after judging"
    scores_path.write_text(json.dumps(scores), encoding="utf-8")

    stale_summary = manager.list_runs()[0]
    stale_detail = manager.get_run_result("semantic-run")

    assert stale_summary["semantic_status"] == "stale"
    assert stale_summary["semantic_score_mean"] is None
    assert stale_summary["semantic_review_count"] is None
    assert stale_detail["semantic_judge"]["status"] == "stale"
    assert stale_detail["semantic_judge"]["rejudge_required"] is True
    assert stale_detail["review_required_count"] is None
    assert all(
        item["semantic_verdict"] is None
        for item in stale_detail["questions"]
    )
    assert all(
        item["semantic_status"] == "stale"
        for item in stale_detail["questions"]
    )


def test_comparison_keeps_same_question_id_from_different_samples(tmp_path):
    output_dir = tmp_path / "runs"
    for run_id, overall in (("a", 0.0), ("b", 1.0)):
        run_dir = _write_run(
            output_dir,
            run_id,
            overall=overall,
            prediction="answer",
        )
        scores_path = run_dir / "scores.json"
        scores = json.loads(scores_path.read_text(encoding="utf-8"))
        first = dict(scores["questions"][0])
        first["sample_id"] = "sample-a"
        second = {
            **first,
            "sample_id": "sample-b",
            "prediction": "another answer",
        }
        scores["questions"] = [first, second]
        scores["question_count"] = 2
        scores_path.write_text(json.dumps(scores), encoding="utf-8")
    manager = EvaluationJobManager(
        tmp_path / "data",
        output_dir=output_dir,
    )

    comparison = manager.compare_runs(["a", "b"])

    assert len(comparison["questions"]) == 2
    assert {
        (item["sample_id"], item["question_id"])
        for item in comparison["questions"]
    } == {("sample-a", "q1"), ("sample-b", "q1")}


def test_dialogue_ab_history_summary(tmp_path):
    manager = EvaluationJobManager(
        tmp_path / "data",
        output_dir=tmp_path / "runs",
    )
    run_dir = manager.dialogue_output_dir / "dialogue-v1"
    (run_dir / "checkpoints").mkdir(parents=True)
    (run_dir / "run_config.json").write_text(
        json.dumps(
            {
                "run_id": "dialogue-v1",
                "run_type": "dialogue_ab",
                "dataset_id": "ja-dialogue-ab-prompts-v1",
                "prompt_count": 30,
                "created_at": "2026-07-28T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "scores.json").write_text(
        json.dumps(
            {
                "run_id": "dialogue-v1",
                "prompt_count": 30,
                "knowledge_cards_created": 10,
                "policies": {
                    "intent_gated": {
                        "rag_trigger_rate": 0.4,
                        "prompt_tokens_mean": 1000,
                    },
                    "candidates": {
                        "rag_trigger_rate": 1.0,
                        "prompt_tokens_mean": 1400,
                    },
                },
                "comparison": {"prompt_tokens_mean_delta": 400},
            }
        ),
        encoding="utf-8",
    )

    runs = manager.list_dialogue_ab_runs()
    result = manager.get_dialogue_ab_result("dialogue-v1")

    assert runs[0]["candidates_rag_trigger_rate"] == 1.0
    assert runs[0]["prompt_tokens_mean_delta"] == 400
    assert result["knowledge_cards_created"] == 10


class TestGatekeeperTokenWarning:
    """Reasoning モデル + 小さい出力上限の組み合わせを事前に警告する。

    v25 の実測: Qwen3-14B / max_output_tokens=512 で thinking が上限を食い切り、
    classifier fallback 77.9% (empty_response=135) → RAG 発火 33% まで低下した。
    """

    @pytest.mark.parametrize(
        "model_name",
        [
            "qwen/qwen3-14b",
            "Qwen3-32B",
            "deepseek-r1",
            "qwq-32b",
            "gpt-oss-120b",
            "glm-4.6-thinking",
            "grok-4.20-0309-reasoning",
        ],
    )
    def test_warns_for_reasoning_models_below_recommendation(self, model_name):
        message = web_jobs.gatekeeper_token_warning(model_name, 512)

        assert message is not None
        assert "2048" in message

    def test_quiet_at_or_above_recommendation(self):
        assert web_jobs.gatekeeper_token_warning("qwen/qwen3-14b", 2048) is None
        assert web_jobs.gatekeeper_token_warning("qwen/qwen3-14b", 4096) is None

    @pytest.mark.parametrize(
        "model_name",
        [
            "gemini-3.1-flash-lite",
            "gpt-4.1-mini",
            "grok-4.20-0309-non-reasoning",
            "nomic-embed-text",
            "",
            None,
        ],
    )
    def test_quiet_for_non_reasoning_models(self, model_name):
        assert web_jobs.gatekeeper_token_warning(model_name, 512) is None

    def test_ignores_unparsable_limit(self):
        assert web_jobs.gatekeeper_token_warning("qwen3-14b", None) is None
        assert web_jobs.gatekeeper_token_warning("qwen3-14b", "abc") is None


class TestRunSummaryExposesRetrievalDiagnostics:
    """evidence の分母を読み違えないよう、発火率と分類器の状態を履歴に出す。"""

    def test_history_row_includes_rag_and_classifier_rates(self, tmp_path):
        manager = EvaluationJobManager(
            tmp_path / "data",
            output_dir=tmp_path / "runs",
            project_root=Path(__file__).parents[1],
        )
        run_dir = tmp_path / "runs" / "v25"
        run_dir.mkdir(parents=True)
        (run_dir / "run_config.json").write_text(
            json.dumps({"run_id": "v25"}), encoding="utf-8"
        )
        (run_dir / "scores.json").write_text(
            json.dumps(
                {
                    "run_id": "v25",
                    "question_count": 199,
                    "official": {"overall": 0.404},
                    "auxiliary": {},
                    "butly": {
                        "evidence_retrieval_rate": 0.219,
                        "rag_trigger_rate": 0.332,
                        "classifier_fallback_rate": 0.779,
                    },
                }
            ),
            encoding="utf-8",
        )

        rows = manager.list_runs()

        row = next(r for r in rows if r["run_id"] == "v25")
        assert row["rag_trigger_rate"] == 0.332
        assert row["classifier_fallback_rate"] == 0.779
        assert row["evidence_retrieval_rate"] == 0.219
        # 旧 run（検索指標なし）は None のまま落ちない
        assert row["search_execution_rate"] is None
        assert row["retrieval_recall_at_3"] is None

    def test_history_row_includes_retrieval_metrics(self, tmp_path):
        manager = EvaluationJobManager(
            tmp_path / "data",
            output_dir=tmp_path / "runs",
            project_root=Path(__file__).parents[1],
        )
        run_dir = tmp_path / "runs" / "v27"
        run_dir.mkdir(parents=True)
        (run_dir / "run_config.json").write_text(
            json.dumps({"run_id": "v27"}), encoding="utf-8"
        )
        (run_dir / "scores.json").write_text(
            json.dumps(
                {
                    "run_id": "v27",
                    "question_count": 199,
                    "official": {"overall": 0.41},
                    "auxiliary": {},
                    "butly": {
                        "rag_trigger_rate": 0.9,
                        "search_execution_rate": 1.0,
                        "retrieval_recall_at_3": 0.61,
                        "bm25_rescue_rate": 0.18,
                    },
                }
            ),
            encoding="utf-8",
        )

        row = next(r for r in manager.list_runs() if r["run_id"] == "v27")

        assert row["search_execution_rate"] == 1.0
        assert row["retrieval_recall_at_3"] == 0.61
        assert row["bm25_rescue_rate"] == 0.18

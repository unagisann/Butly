import json
from pathlib import Path
import sys
import time

import pytest

import evals.locomo.web_jobs as web_jobs
from evals.locomo.web_jobs import (
    EvaluationJobConflict,
    EvaluationJobError,
    EvaluationJobManager,
    build_job_command,
    build_profile_payload,
    validate_job_request,
)


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
        "qa_mode": "independent",
        "locale": "en",
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


def _write_run(
    output_dir: Path,
    run_id: str,
    *,
    overall: float,
    prediction: str,
) -> None:
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
    }
    assert profile["context_levels"]["levels"]["mid_term"] == "off"
    assert profile["memory"]["rag_raw_top_k"] == 1
    assert profile["memory"]["knowledge_maturation_enabled"] is True
    assert profile["sleeptime"]["update_targets"] == {
        "knowledge_maturation": True
    }


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

from fastapi import HTTPException
from pydantic import ValidationError
import pytest

from evals.locomo.web_jobs import EvaluationJobConflict, EvaluationJobError
from routers import evaluations


class FakeManager:
    output_dir = "/tmp/evaluations"
    dialogue_output_dir = "/tmp/dialogue-ab"

    def config(self):
        return {"output_dir": self.output_dir, "run_modes": ["standard"]}

    def dataset_samples(self, dataset_path):
        if dataset_path == "missing":
            raise EvaluationJobError("dataset not found")
        return {
            "dataset_path": dataset_path,
            "sample_count": 2,
            "samples": [
                {"sample_id": "conv-26"},
                {"sample_id": "conv-30"},
            ],
        }

    def start(self, request):
        return {
            "job_id": "job-1",
            "run_id": request["run_id"],
            "status": "running",
        }

    def start_dialogue_ab(self, request):
        return {
            "job_id": "job-dialogue",
            "run_id": request["run_id"],
            "job_type": "dialogue_ab",
            "status": "running",
        }

    def start_judge(self, run_id, request, *, run_type):
        return {
            "job_id": f"job-{run_type}-judge",
            "run_id": run_id,
            "job_type": f"{run_type}_judge",
            "request": request,
            "status": "running",
        }

    def list_jobs(self):
        return [{"job_id": "job-1", "status": "running"}]

    def get(self, job_id):
        if job_id != "job-1":
            raise KeyError(job_id)
        return {"job_id": job_id, "status": "running"}

    def stop(self, job_id):
        return {"job_id": job_id, "status": "stopping"}

    def resume(self, job_id):
        raise EvaluationJobConflict("not resumable")

    def read_log(self, job_id, *, tail_lines):
        return f"{job_id}:{tail_lines}"

    def list_runs(self):
        return [{"run_id": "run-a", "overall": 0.5}]

    def list_dialogue_ab_runs(self):
        return [{"run_id": "dialogue-a", "has_scores": True}]

    def get_dialogue_ab_result(self, run_id):
        if run_id == "missing":
            raise KeyError(run_id)
        return {"run_id": run_id, "run_type": "dialogue_ab"}

    def get_run_result(self, run_id):
        if run_id == "missing":
            raise KeyError(run_id)
        return {
            "run_id": run_id,
            "semantic_judge": {"status": "completed"},
            "questions": [{"question_id": "q1"}],
        }

    def compare_runs(self, run_ids):
        return {"run_ids": run_ids}

    def compare_retrieval_runs(self, run_ids):
        return {"retrieval_run_ids": run_ids}

    def retrieval_replay(
        self,
        run_id,
        modes,
        *,
        limit,
        evidence_raw_chunk_chars,
        evidence_fusion_base_weight,
        evidence_mmr_lambda,
    ):
        if run_id == "missing":
            raise KeyError(run_id)
        return {
            "run": run_id,
            "modes": modes,
            "limit": limit,
            "evidence_raw_chunk_chars": evidence_raw_chunk_chars,
            "evidence_fusion_base_weight": evidence_fusion_base_weight,
            "evidence_mmr_lambda": evidence_mmr_lambda,
        }

    def start_retrieval_replay(self, request):
        if request["run_id"] == "missing":
            raise KeyError(request["run_id"])
        return {
            "job_id": "job-retrieval",
            "job_type": "retrieval_replay",
            "run_id": request["run_id"],
            "status": "running",
        }

    def get_retrieval_replay_result(self, run_id):
        if run_id == "missing":
            raise KeyError(run_id)
        return {"run_id": run_id, "status": "completed"}


def _start_request():
    return evaluations.EvaluationStartRequest(
        dataset_path="/tmp/locomo.json",
        run_id="web-run",
        role_models={
            "chat": {
                "connection": "google",
                "model_name": "gemini-3.5-flash",
            }
        },
    )


def _dialogue_start_request():
    return evaluations.DialogueABStartRequest(
        dataset_path="/tmp/dialogue.json",
        run_id="dialogue-run",
        role_models={
            "chat": {
                "connection": "google",
                "model_name": "gemini-3.5-flash",
            }
        },
    )


def test_start_request_accepts_evaluation_only_judge_role():
    request = evaluations.EvaluationStartRequest(
        dataset_path="/tmp/locomo.json",
        run_id="judge-run",
        role_models={
            "judge": {
                "connection": "nanogpt-sub",
                "model_name": "TEE/gemma4-31b",
                "generation_config": {
                    "temperature": 0.0,
                    "max_output_tokens": 4096,
                },
            }
        },
    )

    assert request.role_models["judge"].model_name == "TEE/gemma4-31b"


def test_start_request_accepts_exact_samples_and_retrieval_prep():
    request = evaluations.EvaluationStartRequest(
        dataset_path="/tmp/locomo.json",
        run_id="prep-conv-30",
        workflow="retrieval_prep",
        sample_ids=["conv-30"],
        sample_limit=None,
    )

    assert request.workflow == "retrieval_prep"
    assert request.sample_ids == ["conv-30"]


def test_evaluation_endpoints_delegate_to_manager(monkeypatch):
    manager = FakeManager()
    monkeypatch.setattr(evaluations, "_get_manager", lambda: manager)

    assert evaluations.get_evaluation_config()["run_modes"] == ["standard"]
    samples = evaluations.get_evaluation_dataset_samples("/tmp/locomo.json")
    assert [item["sample_id"] for item in samples["samples"]] == [
        "conv-26",
        "conv-30",
    ]
    assert evaluations.start_evaluation_job(_start_request())["job_id"] == "job-1"
    assert (
        evaluations.start_dialogue_ab_job(_dialogue_start_request())[
            "job_type"
        ]
        == "dialogue_ab"
    )
    assert evaluations.list_evaluation_jobs()["jobs"][0]["status"] == "running"
    assert evaluations.get_evaluation_job("job-1")["status"] == "running"
    assert evaluations.stop_evaluation_job("job-1")["status"] == "stopping"
    assert evaluations.get_evaluation_job_log("job-1", 25)["text"] == "job-1:25"
    assert evaluations.list_evaluation_runs()["runs"][0]["run_id"] == "run-a"
    assert (
        evaluations.list_dialogue_ab_runs()["runs"][0]["run_id"]
        == "dialogue-a"
    )
    assert (
        evaluations.get_dialogue_ab_result("dialogue-a")["run_type"]
        == "dialogue_ab"
    )
    assert evaluations.get_evaluation_run_result("run-a")["questions"] == [
        {"question_id": "q1"}
    ]
    judge_request = evaluations.SemanticJudgeRequest(
        connection="nanogpt-sub",
        model_name="TEE/gemma4-31b",
        max_output_tokens=4096,
    )
    assert evaluations.judge_evaluation_run(
        "run-a", judge_request
    )["job_type"] == "locomo_judge"
    assert evaluations.judge_dialogue_ab_run(
        "dialogue-a", judge_request
    )["job_type"] == "dialogue_ab_judge"
    compared = evaluations.compare_evaluation_runs(
        evaluations.RunCompareRequest(run_ids=["run-a", "run-b"])
    )
    assert compared["run_ids"] == ["run-a", "run-b"]
    retrieval_compared = evaluations.compare_retrieval_replay_runs(
        evaluations.RunCompareRequest(run_ids=["run-a", "run-b"])
    )
    assert retrieval_compared["retrieval_run_ids"] == ["run-a", "run-b"]
    replayed = evaluations.replay_run_retrieval(
        evaluations.RetrievalReplayRequest(
            run_id="run-a", modes=["bm25", "hybrid"], limit=5
        )
    )
    assert replayed == {
        "run": "run-a",
        "modes": ["bm25", "hybrid"],
        "limit": 5,
        "evidence_raw_chunk_chars": 1800,
        "evidence_fusion_base_weight": 0.7,
        "evidence_mmr_lambda": 0.8,
    }
    replay_request = evaluations.RetrievalReplayRequest(
        run_id="run-a",
        modes=[
            "vector",
            "dual_query",
            "reranked",
            "evidence_rerank",
            "hybrid_evidence_rerank",
            "hybrid_evidence_fusion",
            "hybrid_evidence_fusion_w40",
            "hybrid_evidence_fusion_w50",
            "hybrid_evidence_fusion_w60",
            "hybrid_evidence_fusion_mmr",
        ],
    )
    replay_job = evaluations.start_retrieval_replay_job(replay_request)
    assert replay_job["job_type"] == "retrieval_replay"
    assert (
        evaluations.get_retrieval_replay_result("run-a")["status"]
        == "completed"
    )
    routes = {route.path: route for route in evaluations.router.routes}
    assert "/evaluations/datasets/samples" in routes
    assert routes[
        "/evaluations/runs/retrieval-replay/jobs"
    ].status_code == 202
    assert "/evaluations/runs/retrieval-replay/compare" in routes


def test_evaluation_endpoints_translate_conflicts_and_missing_jobs(monkeypatch):
    manager = FakeManager()
    monkeypatch.setattr(evaluations, "_get_manager", lambda: manager)

    try:
        evaluations.resume_evaluation_job("job-1")
    except HTTPException as exc:
        assert exc.status_code == 409
    else:
        raise AssertionError("resume conflict was not translated")

    try:
        evaluations.get_evaluation_job("missing")
    except HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("missing job was not translated")

    try:
        evaluations.get_evaluation_run_result("missing")
    except HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("missing run was not translated")

    with pytest.raises(HTTPException) as raised:
        evaluations.start_retrieval_replay_job(
            evaluations.RetrievalReplayRequest(run_id="missing")
        )
    assert raised.value.status_code == 404

    with pytest.raises(HTTPException) as raised:
        evaluations.get_retrieval_replay_result("missing")
    assert raised.value.status_code == 404

    with pytest.raises(HTTPException) as raised:
        evaluations.get_evaluation_dataset_samples("missing")
    assert raised.value.status_code == 400

    try:
        evaluations.replay_run_retrieval(
            evaluations.RetrievalReplayRequest(run_id="missing")
        )
    except HTTPException as exc:
        assert exc.status_code == 404
        assert "評価run" in exc.detail
    else:
        raise AssertionError("missing run was not translated")


def test_semantic_judge_routes_and_validation(monkeypatch):
    manager = FakeManager()
    monkeypatch.setattr(evaluations, "_get_manager", lambda: manager)
    payload = {
        "connection": "nanogpt-sub",
        "model_name": "TEE/gemma4-31b",
        "max_output_tokens": 4096,
    }

    judge_request = evaluations.SemanticJudgeRequest(**payload)
    locomo = evaluations.judge_evaluation_run("run-a", judge_request)
    dialogue = evaluations.judge_dialogue_ab_run(
        "dialogue-a", judge_request
    )
    routes = {route.path: route for route in evaluations.router.routes}

    assert locomo["job_type"] == "locomo_judge"
    assert locomo["request"] == payload
    assert dialogue["job_type"] == "dialogue_ab_judge"
    assert routes["/evaluations/runs/{run_id}/judge"].status_code == 202
    assert routes["/evaluations/runs/{run_id}"].methods == {"GET"}
    assert routes[
        "/evaluations/dialogue-ab/runs/{run_id}/judge"
    ].status_code == 202
    with pytest.raises(ValidationError):
        evaluations.SemanticJudgeRequest(
            **{**payload, "max_output_tokens": 0}
        )


def test_semantic_judge_endpoints_translate_404_409_and_400(monkeypatch):
    manager = FakeManager()

    def fail_start(run_id, request, *, run_type):
        del request, run_type
        if run_id == "missing":
            raise KeyError(run_id)
        if run_id == "busy":
            raise EvaluationJobConflict("judge already running")
        raise EvaluationJobError("invalid judge request")

    manager.start_judge = fail_start
    monkeypatch.setattr(evaluations, "_get_manager", lambda: manager)
    request = evaluations.SemanticJudgeRequest(model_name="judge-model")

    for run_id, expected_status in (
        ("missing", 404),
        ("busy", 409),
        ("invalid", 400),
    ):
        with pytest.raises(HTTPException) as raised:
            evaluations.judge_evaluation_run(run_id, request)
        assert raised.value.status_code == expected_status

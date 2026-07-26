from fastapi import HTTPException

from evals.locomo.web_jobs import EvaluationJobConflict
from routers import evaluations


class FakeManager:
    output_dir = "/tmp/evaluations"

    def config(self):
        return {"output_dir": self.output_dir, "run_modes": ["standard"]}

    def start(self, request):
        return {
            "job_id": "job-1",
            "run_id": request["run_id"],
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

    def compare_runs(self, run_ids):
        return {"run_ids": run_ids}


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


def test_evaluation_endpoints_delegate_to_manager(monkeypatch):
    manager = FakeManager()
    monkeypatch.setattr(evaluations, "_get_manager", lambda: manager)

    assert evaluations.get_evaluation_config()["run_modes"] == ["standard"]
    assert evaluations.start_evaluation_job(_start_request())["job_id"] == "job-1"
    assert evaluations.list_evaluation_jobs()["jobs"][0]["status"] == "running"
    assert evaluations.get_evaluation_job("job-1")["status"] == "running"
    assert evaluations.stop_evaluation_job("job-1")["status"] == "stopping"
    assert evaluations.get_evaluation_job_log("job-1", 25)["text"] == "job-1:25"
    assert evaluations.list_evaluation_runs()["runs"][0]["run_id"] == "run-a"
    compared = evaluations.compare_evaluation_runs(
        evaluations.RunCompareRequest(run_ids=["run-a", "run-b"])
    )
    assert compared["run_ids"] == ["run-a", "run-b"]


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

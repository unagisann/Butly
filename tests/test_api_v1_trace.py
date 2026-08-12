"""
test_api_v1_trace.py
────────────────────
`GET /api/v1/instances/{name}/trace`（issue #51）の contract テスト。

確認するのは 3 点:
- developer mode でだけ開く（chat debug と同じ gate）
- TraceNode の metadata を返さない（原文クエリ・検索候補は UI へ出さない）
- 保存済み trace.json から Mermaid を生成し、status を塗り分ける
"""

import json

import pytest
from fastapi.testclient import TestClient

from butly_api.app import create_app
from butly_api.context import ApiContext


TRACE = {
    "schema_version": 1,
    "trace_id": "turn_7",
    "instance": "Alpha",
    "turn_id": 7,
    "source": "web",
    "created_at": "2026-08-12T23:28:28",
    "nodes": [
        {
            "id": "user_message",
            "label": "User Message",
            "type": "input",
            "status": "active",
            "summary": "こんばんは",
            "metadata": {"source": "web"},
        },
        {
            "id": "gatekeeper",
            "label": "Gatekeeper",
            "type": "decision",
            "status": "active",
            "summary": "tier=reflex, need=null",
            "metadata": {"original_query": "秘密にしたい原文クエリ"},
        },
        {
            "id": "rag",
            "label": "RAG Search",
            "type": "retrieval",
            "status": "skipped",
            "summary": "need=null",
            "metadata": {},
        },
        {
            "id": "llm_call",
            "label": "LLM Provider Call",
            "type": "llm",
            "status": "error",
            "summary": "provider error",
            "metadata": {},
        },
    ],
    "edges": [
        {"source": "user_message", "target": "gatekeeper", "status": "active"},
        {"source": "gatekeeper", "target": "rag", "status": "skipped"},
        {"source": "rag", "target": "llm_call", "status": "error"},
    ],
}


def _write_instance(tmp_path, name="Alpha", trace=TRACE):
    instances_dir = tmp_path / "butly_core" / "instances"
    instance_dir = instances_dir / name
    instance_dir.mkdir(parents=True)
    if trace is not None:
        traces_dir = instance_dir / "traces"
        traces_dir.mkdir()
        (traces_dir / "latest.json").write_text(
            json.dumps(trace, ensure_ascii=False), encoding="utf-8"
        )
    return instances_dir


def _client(instances_dir, *, developer_mode=True):
    app = create_app()
    app.state.api_context = ApiContext(
        instances_dir=instances_dir,
        developer_mode=developer_mode,
    )
    return TestClient(app)


class TestTraceAccess:
    def test_requires_developer_mode(self, tmp_path):
        instances_dir = _write_instance(tmp_path)
        client = _client(instances_dir, developer_mode=False)

        response = client.get("/api/v1/instances/Alpha/trace")

        assert response.status_code == 403
        assert response.json()["code"] == "debug_not_available"

    def test_unknown_instance_is_404(self, tmp_path):
        instances_dir = _write_instance(tmp_path)
        client = _client(instances_dir)

        response = client.get("/api/v1/instances/Missing/trace")

        assert response.status_code == 404
        assert response.json()["code"] == "instance_not_found"

    def test_path_traversal_is_refused(self, tmp_path):
        instances_dir = _write_instance(tmp_path)
        client = _client(instances_dir)

        response = client.get("/api/v1/instances/..%2F..%2Fetc/trace")

        assert response.status_code == 404

    def test_instance_without_trace_is_404(self, tmp_path):
        instances_dir = _write_instance(tmp_path, name="Beta", trace=None)
        client = _client(instances_dir)

        response = client.get("/api/v1/instances/Beta/trace")

        assert response.status_code == 404
        assert response.json()["code"] == "trace_not_found"

    def test_unreadable_trace_is_404_without_details(self, tmp_path):
        instances_dir = _write_instance(tmp_path, name="Gamma", trace=None)
        traces_dir = instances_dir / "Gamma" / "traces"
        traces_dir.mkdir()
        (traces_dir / "latest.json").write_text("{not json", encoding="utf-8")
        client = _client(instances_dir)

        response = client.get("/api/v1/instances/Gamma/trace")

        assert response.status_code == 404
        assert response.json()["code"] == "trace_not_found"


class TestTracePayload:
    @pytest.fixture
    def payload(self, tmp_path):
        client = _client(_write_instance(tmp_path))
        response = client.get("/api/v1/instances/Alpha/trace")
        assert response.status_code == 200
        return response.json()

    def test_reports_the_stored_identity(self, payload):
        assert payload["trace_id"] == "turn_7"
        assert payload["turn_id"] == 7
        assert payload["source"] == "web"
        assert payload["created_at"] == "2026-08-12T23:28:28"

    def test_counts_nodes_by_status(self, payload):
        assert payload["node_counts"] == {"active": 2, "skipped": 1, "error": 1}

    def test_renders_a_mermaid_flowchart_with_status_classes(self, payload):
        mermaid = payload["mermaid"]
        assert mermaid.startswith("flowchart TD")
        assert 'gatekeeper["Gatekeeper<br/>tier=reflex, need=null"]' in mermaid
        # skipped / error は線種でも区別する
        assert "gatekeeper -. skipped .-> rag" in mermaid
        assert "rag == error ==> llm_call" in mermaid
        assert "classDef skipped" in mermaid
        assert "classDef error" in mermaid

    def test_never_exposes_node_metadata(self, payload):
        """metadata には原文クエリなど UI へ出さない情報が入る。"""
        assert "秘密にしたい原文クエリ" not in json.dumps(payload, ensure_ascii=False)
        assert "metadata" not in payload
        assert set(payload) == {
            "trace_id",
            "turn_id",
            "source",
            "created_at",
            "mermaid",
            "node_counts",
        }

    def test_trims_long_summaries_so_the_graph_stays_readable(self, tmp_path):
        trace = json.loads(json.dumps(TRACE))
        trace["nodes"][0]["summary"] = "あ" * 500
        client = _client(_write_instance(tmp_path, name="Delta", trace=trace))

        payload = client.get("/api/v1/instances/Delta/trace").json()

        assert "…" in payload["mermaid"]
        assert "あ" * 200 not in payload["mermaid"]

    def test_direction_switches_the_flowchart_orientation(self, tmp_path):
        """縦長のグラフは横長の画面に収まらない。向きは表示側の選択肢にする。"""
        client = _client(_write_instance(tmp_path, name="Epsilon"))

        vertical = client.get("/api/v1/instances/Epsilon/trace").json()
        horizontal = client.get(
            "/api/v1/instances/Epsilon/trace", params={"direction": "LR"}
        ).json()

        assert vertical["mermaid"].startswith("flowchart TD")
        assert horizontal["mermaid"].startswith("flowchart LR")

    def test_unknown_direction_is_rejected(self, tmp_path):
        client = _client(_write_instance(tmp_path, name="Zeta"))

        response = client.get(
            "/api/v1/instances/Zeta/trace", params={"direction": "diagonal"}
        )

        assert response.status_code == 422

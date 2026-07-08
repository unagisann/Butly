"""
test_sse_fixture.py
-------------------
SSE parser contract fixture（openapi/sse_fixtures/）の snapshot テスト。

- fixture が generator（scripts/generate_sse_fixture.py）の現出力と一致する
  （schema / serializer 変更時は再生成を強制し、frontend parser との
  contract ずれを commit 時点で検出する）
- fixture 自体が SSE contract の不変条件を満たす
"""

import json
import sys
from pathlib import Path

import pytest

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "scripts"))

from generate_sse_fixture import (  # noqa: E402
    FIXTURES,
    FIXTURES_DIR,
    render,
)

REGENERATE_HINT = (
    "fixture が現行実装と一致しません。"
    "`venv/bin/python scripts/generate_sse_fixture.py` で再生成してください。"
)


def parse_sse(body: str):
    events = []
    for frame in body.split("\n\n"):
        if not frame.strip():
            continue
        event_name = None
        data_lines = []
        for line in frame.split("\n"):
            if line.startswith("event: "):
                event_name = line[len("event: "):]
            elif line.startswith("data: "):
                data_lines.append(line[len("data: "):])
        events.append(
            {"event": event_name, "payload": json.loads("\n".join(data_lines))}
        )
    return events


@pytest.mark.parametrize("filename", sorted(FIXTURES))
def test_fixture_matches_generator(filename):
    path = FIXTURES_DIR / filename
    assert path.exists(), f"{path} がありません。{REGENERATE_HINT}"
    assert path.read_text(encoding="utf-8") == render(FIXTURES[filename]()), (
        REGENERATE_HINT
    )


class TestSuccessFixtureInvariants:
    @pytest.fixture
    def events(self):
        return parse_sse(
            (FIXTURES_DIR / "chat_stream_success.sse").read_text(encoding="utf-8")
        )

    def test_event_order(self, events):
        names = [e["event"] for e in events]
        assert names[0] == "metadata"
        assert names[-1] == "done"
        assert all(n == "chunk" for n in names[1:-1])

    def test_request_id_consistent(self, events):
        ids = {e["payload"]["request_id"] for e in events}
        assert len(ids) == 1

    def test_sequence_monotonic(self, events):
        seqs = [
            e["payload"]["sequence"] for e in events if e["event"] == "chunk"
        ]
        assert seqs == list(range(len(seqs)))

    def test_done_full_text_matches_chunks(self, events):
        joined = "".join(
            e["payload"]["data"]["text"] for e in events if e["event"] == "chunk"
        )
        assert events[-1]["payload"]["data"]["full_text"] == joined

    def test_multiline_text_stays_single_data_line(self):
        """改行を含む chunk も JSON escape され 1 data 行に収まる"""
        raw = (FIXTURES_DIR / "chat_stream_success.sse").read_text(encoding="utf-8")
        for frame in raw.split("\n\n"):
            if not frame.strip():
                continue
            data_lines = [l for l in frame.split("\n") if l.startswith("data: ")]
            assert len(data_lines) == 1


class TestErrorFixtureInvariants:
    @pytest.fixture
    def events(self):
        return parse_sse(
            (FIXTURES_DIR / "chat_stream_error.sse").read_text(encoding="utf-8")
        )

    def test_error_terminates_without_done(self, events):
        names = [e["event"] for e in events]
        assert names[-1] == "error"
        assert "done" not in names

    def test_error_payload_shape(self, events):
        error = events[-1]["payload"]
        assert error["data"]["code"] == "generation_failed"
        assert isinstance(error["recoverable"], bool)

"""ButlySleeptime の隔離パス注入に対する回帰テスト。"""

import json
from pathlib import Path
from unittest.mock import MagicMock

from sleeptime import ButlySleeptime


def _write_turn(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "timestamp": "2023-05-08T13:56:00",
                "messages": [
                    {
                        "role": "user",
                        "parts": ["I started learning pottery."],
                        "meta": {
                            "person_id": "p_eval_caroline",
                            "display_name": "Caroline",
                            "lane": "direct",
                            "source": "eval",
                        },
                    },
                    {"role": "model", "parts": ["That sounds exciting."]},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _build_isolated_instance(instances_dir: Path) -> Path:
    instance_dir = instances_dir / "locomo_sample_0"
    (instance_dir / "short_term_json").mkdir(parents=True)
    (instance_dir / "memory_archive" / "1_integrated").mkdir(parents=True)
    (instance_dir / "config.json").write_text(
        json.dumps(
            {
                "agent_profile": {"ai_name": "Melanie"},
                "user_profile": {"preferred_call": "Caroline"},
                "sleeptime": {
                    "update_targets": {
                        "mid_term_digest": False,
                        "recent_headlines": False,
                        "recent_snapshot": False,
                        "key_memory": False,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    _write_turn(
        instance_dir / "short_term_json" / "session_20230508_135600_000000.json"
    )
    return instance_dir


def test_injected_paths_isolate_stage1_and_person_registry(tmp_path):
    data_dir = tmp_path / "workspace"
    instances_dir = tmp_path / "isolated_instances"
    instance_dir = _build_isolated_instance(instances_dir)
    sleeptime = ButlySleeptime(
        base_dir=data_dir,
        instances_dir=instances_dir,
    )

    sleeptime.stage_1_cleanup(instance_dir.name)

    integrated = instance_dir / "memory_archive" / "1_integrated"
    assert not list((instance_dir / "short_term_json").glob("*.json"))
    assert len(list(integrated.glob("session_*.json"))) == 1
    assert (data_dir / "persons.json").exists()


def test_injected_paths_are_used_by_stage2_and_backup(tmp_path):
    data_dir = tmp_path / "workspace"
    instances_dir = tmp_path / "isolated_instances"
    instance_dir = _build_isolated_instance(instances_dir)
    source = instance_dir / "short_term_json" / "session_20230508_135600_000000.json"
    source.unlink()
    sleeptime = ButlySleeptime(
        base_dir=data_dir,
        instances_dir=instances_dir,
    )

    sleeptime.stage_2_knowledgeize(instance_dir.name, instance_dir.name)

    database = instance_dir / "butly_memory.db"
    assert database.exists()
    sleeptime.backup_database(instance_dir.name)
    backups = data_dir / "butly_core" / "db_backups"
    assert list(backups.glob(f"{instance_dir.name}_butly_memory_*.db"))


def test_run_enumerates_only_injected_instances(tmp_path, monkeypatch):
    data_dir = tmp_path / "workspace"
    instances_dir = tmp_path / "isolated_instances"
    expected = _build_isolated_instance(instances_dir)
    sleeptime = ButlySleeptime(
        base_dir=data_dir,
        instances_dir=instances_dir,
    )
    processed = []
    monkeypatch.setattr(sleeptime, "process_instance", processed.append)

    sleeptime.run()

    assert processed == [expected]


def _instance_with_roles(instances_dir: Path, roles: dict) -> Path:
    """user 定義 connection を持つロール設定つきインスタンスを作る。"""
    instance_dir = instances_dir / "locomo_conn"
    (instance_dir / "memory_archive" / "1_integrated").mkdir(parents=True)
    config = {
        "agent_profile": {"ai_name": "Melanie"},
        "user_profile": {"preferred_call": "Caroline"},
    }
    config.update(roles)
    (instance_dir / "config.json").write_text(
        json.dumps(config, ensure_ascii=False), encoding="utf-8"
    )
    return instance_dir


def test_daily_digest_resolves_provider_with_connection(tmp_path, monkeypatch):
    """summary の provider 取得が model_name 文字列でなく connection 込み
    dict で解決される（user 定義 connection の推定失敗を防ぐ回帰）。"""
    instances_dir = tmp_path / "instances"
    instance_dir = _instance_with_roles(
        instances_dir,
        {"summary": {"connection": "colab_local", "model_name": "qwen3-14b"}},
    )
    sleeptime = ButlySleeptime(
        base_dir=tmp_path / "workspace", instances_dir=instances_dir
    )

    created = []

    def fake_create(model):
        created.append(model)
        provider = MagicMock()
        provider.classify.return_value = "digest body"
        return provider

    monkeypatch.setattr(
        "butly_core.llm.factory.ProviderFactory.create", fake_create
    )

    raw_text = "[2023-05-08 13:56] Caroline: I started pottery today.\n" * 30
    sleeptime._generate_daily_digest(instance_dir, raw_text)

    assert created, "provider was never created"
    assert all(isinstance(model, dict) for model in created), created
    assert created[0]["connection"] == "colab_local"
    assert created[0]["model_name"] == "qwen3-14b"


def test_recent_headlines_keeps_connection_in_provider_and_classify(
    tmp_path, monkeypatch
):
    """recent_headlines は provider 取得と classify の両方で connection を保つ
    （classify へ渡す conf を connection 抜きで組み直していたバグの回帰）。"""
    instances_dir = tmp_path / "instances"
    instance_dir = _instance_with_roles(
        instances_dir,
        {"summary": {"connection": "colab_local", "model_name": "qwen3-14b"}},
    )
    (instance_dir / "mid_term_digest.txt").write_text(
        "[2023-05-08] Pottery\n- Caroline started pottery and planned a mug.\n" * 5,
        encoding="utf-8",
    )
    sleeptime = ButlySleeptime(
        base_dir=tmp_path / "workspace", instances_dir=instances_dir
    )

    created = []
    classify_confs = []

    def fake_create(model):
        created.append(model)
        provider = MagicMock()

        def classify(prompt, conf):
            classify_confs.append(conf)
            return json.dumps({"headlines": []})

        provider.classify.side_effect = classify
        return provider

    monkeypatch.setattr(
        "butly_core.llm.factory.ProviderFactory.create", fake_create
    )

    sleeptime._generate_recent_headlines(instance_dir)

    assert created and isinstance(created[0], dict)
    assert created[0]["connection"] == "colab_local"
    assert classify_confs and classify_confs[0].get("connection") == "colab_local"


# ===================================================================
# ask_gemini_to_summarize の status 返却テスト (v10 の無音失敗対策)
# ===================================================================

def _fake_factory(monkeypatch, text=None, exc=None):
    from butly_core.llm import factory

    provider = MagicMock()
    if exc is not None:
        provider.classify.side_effect = exc
    else:
        provider.classify.return_value = text
    monkeypatch.setattr(factory.ProviderFactory, "create", lambda conf: provider)
    return provider


def _sleeptime_with_instance(tmp_path):
    instances_dir = tmp_path / "instances"
    instance_dir = _instance_with_roles(instances_dir, {})
    sleeptime = ButlySleeptime(
        base_dir=tmp_path / "workspace", instances_dir=instances_dir
    )
    return sleeptime, instance_dir


def test_knowledgeize_ok_with_unclosed_fence(tmp_path, monkeypatch):
    sleeptime, instance_dir = _sleeptime_with_instance(tmp_path)
    _fake_factory(monkeypatch, text='```json\n[{"title": "t1"}, {"title": "t2"}]')

    cards, status = sleeptime.ask_gemini_to_summarize("text", instance_dir.name)

    assert status == "ok"
    assert [c["title"] for c in cards] == ["t1", "t2"]


def test_knowledgeize_object_wrapped_array(tmp_path, monkeypatch):
    sleeptime, instance_dir = _sleeptime_with_instance(tmp_path)
    _fake_factory(monkeypatch, text='{"cards": [{"title": "t"}]}')

    cards, status = sleeptime.ask_gemini_to_summarize("text", instance_dir.name)

    assert status == "ok"
    assert cards == [{"title": "t"}]


def test_knowledgeize_empty_array_is_no_cards(tmp_path, monkeypatch):
    sleeptime, instance_dir = _sleeptime_with_instance(tmp_path)
    _fake_factory(monkeypatch, text="[]")

    cards, status = sleeptime.ask_gemini_to_summarize("text", instance_dir.name)

    assert status == "no_cards"
    assert cards == []


def test_knowledgeize_empty_response_status(tmp_path, monkeypatch):
    sleeptime, instance_dir = _sleeptime_with_instance(tmp_path)
    _fake_factory(monkeypatch, text="")

    cards, status = sleeptime.ask_gemini_to_summarize("text", instance_dir.name)

    assert status == "empty_response"
    assert cards is None


def test_knowledgeize_parse_error_status(tmp_path, monkeypatch):
    sleeptime, instance_dir = _sleeptime_with_instance(tmp_path)
    _fake_factory(monkeypatch, text="thinking out loud, no json at all")

    cards, status = sleeptime.ask_gemini_to_summarize("text", instance_dir.name)

    assert status == "parse_error"
    assert cards is None


def test_knowledgeize_non_list_json_is_parse_error(tmp_path, monkeypatch):
    sleeptime, instance_dir = _sleeptime_with_instance(tmp_path)
    _fake_factory(monkeypatch, text='{"title": "not an array"}')

    cards, status = sleeptime.ask_gemini_to_summarize("text", instance_dir.name)

    assert status == "parse_error"
    assert cards is None


def test_knowledgeize_provider_error_status(tmp_path, monkeypatch):
    sleeptime, instance_dir = _sleeptime_with_instance(tmp_path)
    _fake_factory(monkeypatch, exc=RuntimeError("boom"))

    cards, status = sleeptime.ask_gemini_to_summarize("text", instance_dir.name)

    assert status == "provider_error"
    assert cards is None


# ===================================================================
# stage_2_knowledgeize のチャンク統計と RAW 保持テスト
# ===================================================================

def _instance_with_integrated_turn(tmp_path):
    instances_dir = tmp_path / "instances"
    instance_dir = _build_isolated_instance(instances_dir)
    short_term = instance_dir / "short_term_json"
    integrated = instance_dir / "memory_archive" / "1_integrated"
    for f in short_term.glob("*.json"):
        f.rename(integrated / f.name)
    sleeptime = ButlySleeptime(
        base_dir=tmp_path / "workspace", instances_dir=instances_dir
    )
    return sleeptime, instance_dir, integrated


def test_stage2_failed_chunk_keeps_raw_and_reports(tmp_path, monkeypatch):
    sleeptime, instance_dir, integrated = _instance_with_integrated_turn(tmp_path)
    monkeypatch.setattr("sleeptime.time.sleep", lambda s: None)
    monkeypatch.setattr(
        sleeptime, "ask_gemini_to_summarize", lambda text, db: (None, "parse_error")
    )

    stats = sleeptime.stage_2_knowledgeize(instance_dir.name, instance_dir.name)

    assert stats["chunks"] == 1
    assert stats["failed_chunks"] == 1
    assert stats["failures"] == [
        {"date": "2023-05-08", "chunk": 1, "reason": "parse_error"}
    ]
    # RAW は移動されず次回再試行できる
    assert len(list(integrated.glob("session_*.json"))) == 1
    knowledgeized = instance_dir / "memory_archive" / "2_knowledgeized"
    assert not list(knowledgeized.rglob("session_*.json"))


def test_stage2_no_cards_archives_raw(tmp_path, monkeypatch):
    sleeptime, instance_dir, integrated = _instance_with_integrated_turn(tmp_path)
    monkeypatch.setattr("sleeptime.time.sleep", lambda s: None)
    monkeypatch.setattr(
        sleeptime, "ask_gemini_to_summarize", lambda text, db: ([], "no_cards")
    )

    stats = sleeptime.stage_2_knowledgeize(instance_dir.name, instance_dir.name)

    assert stats == {
        "chunks": 1, "failed_chunks": 0, "cards_created": 0,
        "insert_failures": 0, "failures": [],
        "source_files_card": 0, "source_files_chunk": 0,
    }
    # 正当な抽出なしは再処理ループを防ぐため処理済みとして移動
    assert not list(integrated.glob("session_*.json"))


def test_stage2_ok_counts_cards(tmp_path, monkeypatch):
    sleeptime, instance_dir, integrated = _instance_with_integrated_turn(tmp_path)
    monkeypatch.setattr("sleeptime.time.sleep", lambda s: None)
    monkeypatch.setattr(sleeptime, "generate_embedding", lambda text, instance_name=None: None)
    card = {
        "category": "Life", "title": "Pottery", "tags": "hobby",
        "ai_importance": 3, "humanity_importance": 3,
        "summary": "Caroline started pottery.", "episode": "She enjoyed it.",
    }
    monkeypatch.setattr(
        sleeptime, "ask_gemini_to_summarize", lambda text, db: ([card], "ok")
    )

    stats = sleeptime.stage_2_knowledgeize(instance_dir.name, instance_dir.name)

    assert stats["chunks"] == 1
    assert stats["failed_chunks"] == 0
    assert stats["cards_created"] == 1
    assert not list(integrated.glob("session_*.json"))

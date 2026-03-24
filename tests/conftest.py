"""
conftest.py
-----------
pytest 共有フィクスチャ。
- テスト用一時インスタンスの作成・削除
- モック用の記憶データ生成
- API キーの有無に応じたスキップ制御
"""

import json
import os
import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# プロジェクトルートを sys.path に追加
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ===================================================================
# マーカー登録
# ===================================================================

def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: 実際の Gemini API を叩く統合テスト（API キー必須）",
    )


# ===================================================================
# API キー関連
# ===================================================================

@pytest.fixture(scope="session")
def has_api_key() -> bool:
    """API キーが利用可能かどうかを返す"""
    from dotenv import load_dotenv
    for env_file in [PROJECT_ROOT / ".env", PROJECT_ROOT / "APIkey.env"]:
        if env_file.exists():
            load_dotenv(dotenv_path=env_file, override=True)
    return bool(os.getenv("GOOGLE_API_KEY"))


def skip_without_api_key(has_api_key):
    """API キーがない場合にテストをスキップするヘルパー"""
    if not has_api_key:
        pytest.skip("GOOGLE_API_KEY が設定されていないためスキップ")


# ===================================================================
# テスト用一時インスタンス
# ===================================================================

TEST_INSTANCE_NAME = "test_instance"
TEST_INSTANCE_FOLDER = TEST_INSTANCE_NAME


@pytest.fixture
def base_dir(tmp_path: Path) -> Path:
    """
    テスト用のプロジェクトルートを tmp_path に構築する。
    butly_core/instances/ 配下に最低限の構造を作成。
    """
    instances_dir = tmp_path / "butly_core" / "instances"
    instances_dir.mkdir(parents=True)

    return tmp_path


@pytest.fixture
def test_instance_dir(base_dir: Path) -> Path:
    """
    テスト用インスタンスのディレクトリを構築して返す。
    テスト終了後に自動で削除される（tmp_path の仕組み）。
    """
    instances_dir = base_dir / "butly_core" / "instances"
    inst_dir = instances_dir / TEST_INSTANCE_FOLDER

    inst_dir.mkdir(parents=True, exist_ok=True)
    (inst_dir / "short_term_json").mkdir(exist_ok=True)
    (inst_dir / "floating_summaries").mkdir(exist_ok=True)

    archive_root = inst_dir / "memory_archive"
    (archive_root / "1_integrated").mkdir(parents=True, exist_ok=True)
    (archive_root / "2_knowledgeized").mkdir(parents=True, exist_ok=True)
    (archive_root / "3_log").mkdir(parents=True, exist_ok=True)

    # 基本ファイル
    (inst_dir / "system_instruction.txt").write_text(
        "あなたはテスト用の執事AIです。礼儀正しく、簡潔に応答してください。",
        encoding="utf-8",
    )
    (inst_dir / "Key_Memory.txt").write_text(
        "テストユーザーはプログラマーです。好きな言語はPythonです。",
        encoding="utf-8",
    )
    (inst_dir / "mid_term.txt").write_text("", encoding="utf-8")
    (inst_dir / "mid_term_digest.txt").write_text("", encoding="utf-8")
    (inst_dir / "mid_term_relationship.txt").write_text("", encoding="utf-8")
    (inst_dir / "floating_summary.txt").write_text("", encoding="utf-8")
    (inst_dir / "session_state.json").write_text(
        json.dumps({
            "topic": "",
            "mood": "neutral",
            "goals": [],
            "unresolved": [],
            "turn_count": 0,
            "last_tier": "mid",
        }),
        encoding="utf-8",
    )

    return inst_dir


@pytest.fixture
def memory_manager(base_dir: Path, test_instance_dir: Path):
    """テスト用 ButlyMemory インスタンスを返す"""
    from butly_core.core.memory import ButlyMemory
    return ButlyMemory(base_dir, instance_name=TEST_INSTANCE_FOLDER)


# ===================================================================
# 記憶データ生成ヘルパー
# ===================================================================

@pytest.fixture
def sample_short_term_data() -> list:
    """短期記憶のサンプルデータ"""
    return [
        {"role": "user", "parts": ["今日の天気は？"]},
        {"role": "model", "parts": ["本日は晴れの予報です。"]},
        {"role": "user", "parts": ["ありがとう"]},
        {"role": "model", "parts": ["どういたしまして。"]},
    ]


@pytest.fixture
def sample_history() -> list:
    """Gatekeeper / Brain に渡す形式の会話履歴"""
    return [
        {"role": "user", "parts": ["プロジェクトの進捗を教えて"]},
        {"role": "model", "parts": ["Phase 3 まで完了しています。"]},
        {"role": "user", "parts": ["次は何をすべき？"]},
        {"role": "model", "parts": ["Phase 4 の要約注入切替に取り組むのが良いでしょう。"]},
    ]


@pytest.fixture
def sample_session_state() -> dict:
    """Gatekeeper に渡すセッション状態のサンプル"""
    return {
        "topic": "プロジェクト進捗",
        "mood": "focused",
        "goals": ["Phase 4 完了"],
        "unresolved": ["要約品質の検証"],
        "turn_count": 5,
        "last_tier": "mid",
    }


# ===================================================================
# モック Brain
# ===================================================================

@pytest.fixture
def mock_brain():
    """API を叩かない Brain モック"""
    brain = MagicMock()
    brain.extract_keywords.return_value = {"keywords": ["テスト", "プロジェクト"]}
    brain.search_knowledge.return_value = [
        {
            "id": "test_001",
            "title": "テストプロジェクト",
            "summary": "テスト用のナレッジカード",
            "episode": "テスト中に生成されたカード",
            "score": 0.85,
        }
    ]
    return brain

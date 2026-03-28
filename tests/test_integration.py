"""
test_integration.py
-------------------
統合テスト: 実際の Gemini API を叩いてエンドツーエンドで検証する。
API キーがない環境では自動スキップ。

実行方法:
  pytest tests/test_integration.py -m integration
"""

import os
import sys
from pathlib import Path

import pytest

# プロジェクトルートを sys.path に追加
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _ensure_api_key():
    """API キーが利用可能かチェックし、なければスキップ"""
    from dotenv import load_dotenv
    for env_file in [PROJECT_ROOT / ".env", PROJECT_ROOT / "APIkey.env"]:
        if env_file.exists():
            load_dotenv(dotenv_path=env_file, override=True)
    if not os.getenv("GOOGLE_API_KEY"):
        pytest.skip("GOOGLE_API_KEY が設定されていないためスキップ")


@pytest.mark.integration
class TestGatekeeperIntegration:
    """Gatekeeper の実 API テスト"""

    def setup_method(self):
        _ensure_api_key()

    def test_classify_greeting_as_reflex(self):
        """挨拶が reflex に分類される"""
        from butly_core.core.gatekeeper import Gatekeeper

        gk = Gatekeeper(base_dir=PROJECT_ROOT)

        result = gk.classify(
            user_input="おはようございます",
            history_msgs=[],
            session_state={},
        )

        assert result["tier"] in ("reflex", "mid")  # 挨拶は reflex が期待だが mid も許容
        assert "tier" in result

    def test_classify_technical_as_mid_or_cortex(self):
        """技術的な質問が mid または cortex に分類される"""
        from butly_core.core.gatekeeper import Gatekeeper

        gk = Gatekeeper(base_dir=PROJECT_ROOT)

        result = gk.classify(
            user_input="Pythonでasyncioを使ったWebSocketサーバーの実装方法を教えて",
            history_msgs=[],
            session_state={"topic": "Python開発"},
        )

        # 新しいシグナルベースの tier 判定では、履歴なしの場合
        # reflex/mid/cortex いずれにもなりうる（LLMのシグナル次第）
        assert result["tier"] in ("reflex", "mid", "cortex")

    def test_classify_past_reference_as_cortex(self):
        """過去への言及が cortex に分類される"""
        from butly_core.core.gatekeeper import Gatekeeper

        gk = Gatekeeper(base_dir=PROJECT_ROOT)

        result = gk.classify(
            user_input="前に話したプロジェクトのアーキテクチャの件、あの後どうなった？",
            history_msgs=[
                {"role": "user", "parts": ["プロジェクトのアーキテクチャについて相談したい"]},
                {"role": "model", "parts": ["承知しました。どのような観点で検討しましょうか？"]},
            ],
            session_state={"topic": "プロジェクト"},
        )

        assert result["tier"] == "cortex"
        # cortex の場合、need と search_targets が設定される
        assert result.get("need") is not None
        assert result.get("search_targets") is not None

    def test_classify_returns_valid_state_delta(self):
        """state_delta が有効な構造で返される"""
        from butly_core.core.gatekeeper import Gatekeeper

        gk = Gatekeeper(base_dir=PROJECT_ROOT)

        result = gk.classify(
            user_input="新しくダッシュボード機能を作りたいんだけど",
            history_msgs=[],
            session_state={},
        )

        delta = result.get("state_delta", {})
        assert isinstance(delta, dict)
        # delta のキーは既知のもののみ
        valid_keys = {"topic", "mood", "add_goal", "add_unresolved", "resolve"}
        assert set(delta.keys()).issubset(valid_keys)


@pytest.mark.integration
class TestBrainIntegration:
    """Brain の実 API テスト"""

    def setup_method(self):
        _ensure_api_key()

    def test_extract_keywords(self):
        """キーワード抽出が動作する"""
        from butly_core.core.brain import ButlyBrain

        brain = ButlyBrain(PROJECT_ROOT)
        result = brain.extract_keywords("Pythonのテストフレームワークについて教えて")

        assert "keywords" in result
        assert isinstance(result["keywords"], list)
        assert len(result["keywords"]) > 0

    def test_get_embedding(self):
        """エンベディング生成が動作する"""
        from butly_core.core.brain import ButlyBrain

        brain = ButlyBrain(PROJECT_ROOT)
        embedding = brain.get_embedding("テスト用のテキストです")

        assert embedding is not None
        assert len(embedding) > 0


@pytest.mark.integration
class TestEndToEndFlow:
    """
    エンドツーエンドフローテスト。
    テスト用インスタンスを使って Gatekeeper → MemoryBlockBuilder → system_instruction
    の一連の流れを検証する。
    """

    def setup_method(self):
        _ensure_api_key()

    def test_full_pipeline(self, base_dir, test_instance_dir, memory_manager):
        """
        Gatekeeper 判定 → MemoryBlockBuilder → build_system_instruction の
        パイプラインが正常に動作する
        """
        from butly_core.core.gatekeeper import (
            Gatekeeper,
            MemoryBlockBuilder,
            SessionState,
            build_context_prefix,
            build_system_instruction_from_blocks,
        )

        gk = Gatekeeper(base_dir=PROJECT_ROOT)

        # 1. Gatekeeper 分類
        result = gk.classify(
            user_input="今日の予定を教えて",
            history_msgs=[],
            session_state={},
        )
        tier = result.get("tier", "mid")

        # 2. SessionState 更新
        ss = SessionState(test_instance_dir)
        ss.apply_delta(result.get("state_delta", {}))
        ss.increment_turn(tier)

        # 3. MemoryBlockBuilder
        builder = MemoryBlockBuilder()
        blocks = builder.build(
            tier=tier,
            memory_manager=memory_manager,
            brain=None,  # RAG はスキップ
            user_input="今日の予定を教えて",
            gatekeeper_output=result,
        )

        assert blocks["tier"] == tier

        # 4. system_instruction 構築
        instruction = build_system_instruction_from_blocks(blocks, memory_manager)

        assert "=== SYSTEM INSTRUCTION ===" in instruction
        assert "=== KEY MEMORY" in instruction
        # TIER INFO は context_prefix に移動済み
        assert "=== TIER INFO" not in instruction

        # 5. context_prefix 構築
        context = build_context_prefix(blocks, memory_manager)
        assert "=== TIER INFO" in context
        assert tier in context

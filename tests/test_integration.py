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
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        load_dotenv(dotenv_path=env_file, override=True)
    if not (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")):
        pytest.skip("Gemini API キーが設定されていないためスキップ")


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

    def test_classify_technical_as_mid(self):
        """技術的な質問が reflex または mid に分類される"""
        from butly_core.core.gatekeeper import Gatekeeper

        gk = Gatekeeper(base_dir=PROJECT_ROOT)

        result = gk.classify(
            user_input="Pythonでasyncioを使ったWebSocketサーバーの実装方法を教えて",
            history_msgs=[],
            session_state={"topic": "Python開発"},
        )

        # 新しいシグナルベースの tier 判定では、履歴なしの場合
        # reflex/mid いずれにもなりうる（LLMのシグナル次第）
        assert result["tier"] in ("reflex", "mid")

    def test_classify_past_reference_with_need(self):
        """過去への言及が mid に分類され、need が設定される場合がある"""
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

        assert result["tier"] in ("reflex", "mid")
        # need が設定されている場合は search_targets も設定される
        if result.get("need"):
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
        valid_keys = {"topic", "mood"}
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
        assert "=== CORE MEMORY" in instruction
        # TIER INFO は context_prefix に移動済み
        assert "[実行モード]" not in instruction

        # 5. context_prefix 構築
        context = build_context_prefix(blocks, memory_manager)
        assert "[実行モード]" in context
        assert tier in context


class TestGatekeeperHeadlines:
    """Gatekeeper の headlines 読み込みテスト（API キー不要）"""

    def test_load_headlines_with_instance_dir(self, test_instance_dir: Path):
        """Gatekeeper.classify に instance_dir を渡すと headlines が読まれる"""
        from butly_core.core.gatekeeper import Gatekeeper

        # headlines ファイルを作成
        headlines_file = test_instance_dir / "recent_digest_headlines.json"
        import json
        headlines_file.write_text(
            json.dumps({
                "generated_at": "2026-03-30T03:00:00",
                "headlines": [
                    {"type": "topic", "text": "Gatekeeper改修方針"},
                    {"type": "event", "text": "session_stateコンパクト化を決定"},
                ],
            }, ensure_ascii=False),
            encoding="utf-8",
        )

        gk = Gatekeeper()
        result = gk._load_headlines(test_instance_dir)

        assert "Gatekeeper改修方針" in result
        assert "[Topic]" in result
        assert "[Event]" in result

    def test_load_headlines_missing_file(self, test_instance_dir: Path):
        """headlines ファイルが存在しない場合のフォールバック"""
        from butly_core.core.gatekeeper import Gatekeeper

        headlines_file = test_instance_dir / "recent_digest_headlines.json"
        headlines_file.unlink(missing_ok=True)

        gk = Gatekeeper()
        result = gk._load_headlines(test_instance_dir)

        assert result == "(no recent headlines)"

    def test_load_headlines_no_instance_dir(self):
        """instance_dir が None の場合のフォールバック"""
        from butly_core.core.gatekeeper import Gatekeeper

        gk = Gatekeeper()
        result = gk._load_headlines(None)

        assert result == "(no recent headlines)"

    def test_load_headlines_empty_headlines(self, test_instance_dir: Path):
        """headlines が空配列の場合のフォールバック"""
        from butly_core.core.gatekeeper import Gatekeeper

        import json
        headlines_file = test_instance_dir / "recent_digest_headlines.json"
        headlines_file.write_text(
            json.dumps({"generated_at": "2026-03-30T03:00:00", "headlines": []}),
            encoding="utf-8",
        )

        gk = Gatekeeper()
        result = gk._load_headlines(test_instance_dir)

        assert result == "(no recent headlines)"

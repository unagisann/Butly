"""
gatekeeper.py
-------------
Butly の「前頭葉（ゲートキーパー）」モジュール。

役割:
  1. Ollama gemma3:1b へユーザー発言を送信し、
     tier（reflex / mid / cortex）を判定する。
  2. 判定結果に応じて brain.py の system_instruction へ
     注入する記憶ブロックを構築する。

tier ごとの記憶注入ルール:
  reflex  → short_term（history） + floating_summary
  mid     → 上記 + mid_term.txt
  cortex  → 上記 + RAG（butly_memory.db 検索結果）
"""

import copy
import json
import urllib.request
import urllib.error
import os
from pathlib import Path
from typing import Callable, Optional

from google import genai
from google.genai import types

try:
    from butly_core.config import AI_CONFIG, SYSTEM_CONFIG
    from butly_core.prompts import GATEKEEPER_CLASSIFY_PROMPT
except ImportError:
    import sys
    sys.path.append(str(Path(__file__).resolve().parent.parent.parent))
    from butly_core.config import AI_CONFIG, SYSTEM_CONFIG
    from butly_core.prompts import GATEKEEPER_CLASSIFY_PROMPT

# ===================================================================
# 定数
# ===================================================================

# Ollama エンドポイント（ローカル固定・フォールバック用）
OLLAMA_ENDPOINT = "http://localhost:11434/api/generate"

# 使用モデル
GATEKEEPER_MODEL = "gemma3:4b"

# next topic が空のときのデフォルト tier
DEFAULT_TIER_WHEN_NO_TOPIC = "mid"

# 有効な tier 値
VALID_TIERS = {"reflex", "mid", "cortex"}

# ゲートキーパー用プロンプトテンプレート
GATEKEEPER_PROMPT_TEMPLATE = """\
あなたはAIアシスタントの「前頭葉（ゲートキーパー）」です。
最新のユーザー発言を、以下の3つの階層のいずれかに厳格に分類してください。

【現在の話題（Context）】
{current_topic}

【直近の会話履歴（Recent History）】
{history_3_lines}

【判定対象のユーザー発言】
「{user_input}」

---
【階層定義と判定基準】
1. reflex（脊髄反射）:
   - 挨拶、相槌（「うん」「わかった」「了解」）、短い感情表現。
   - 知識や過去の記憶を必要とせず、即座にリズムを合わせるべき内容。

2. mid（中脳・感情系）:
   - 現在の話題に関する具体的な質問、手順の確認、日常的な対話。
   - セッション内の文脈は必要だが、長期記憶（RAG）の検索までは不要な内容。

3. cortex（大脳皮質）:
   - 専門的な技術相談、過去の出来事への言及（「あの時の」「前話した」）、深い考察を求める問い。
   - 長期記憶（DB）や外部知識（Google検索）の参照が不可欠な内容。

---
【出力ルール】
- 判定結果のみを[reflex / mid / cortex]のいずれか1単語で出力せよ。
- 理由や解説は一切不要。

判定結果:\
"""


# ===================================================================
# SessionState: セッション状態の管理
# ===================================================================

class SessionState:
    """
    セッション全体の内部状態を管理する。
    JSONファイルベースで永続化。
    """
    
    DEFAULT_STATE = {
        "topic": "",
        "mood": "neutral",
        "goals": [],
        "unresolved": [],
        "turn_count": 0,
        "last_tier": "mid",
    }
    
    def __init__(self, instance_dir: Path):
        self.state_file = instance_dir / "session_state.json"
        self.state = self._load()
    
    def _load(self) -> dict:
        """ファイルから状態を読み込む。なければデフォルトを返す。"""
        if self.state_file.exists():
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    # 欠損キーをデフォルトで補完
                    for k, v in self.DEFAULT_STATE.items():
                        if k not in data:
                            data[k] = copy.deepcopy(v)
                    return data
            except Exception as e:
                print(f"[SessionState] 状態ファイルの読み込みエラー: {e}")
        return copy.deepcopy(self.DEFAULT_STATE)
    
    def _save(self):
        """現在の状態をファイルに書き出す。"""
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[SessionState] 状態ファイルの保存エラー: {e}")
    
    def apply_delta(self, delta: dict):
        """
        Gatekeeperのstate_deltaを適用する。
        """
        if not delta:
            return

        # topic: nullでなければ上書き
        if delta.get("topic") is not None:
            self.state["topic"] = delta["topic"]
            
        # mood: nullでなければ上書き
        if delta.get("mood") is not None:
            self.state["mood"] = delta["mood"]
            
        # add_goal: nullでなければgoalsに追加（重複チェック）
        add_goal = delta.get("add_goal")
        if add_goal and add_goal not in self.state["goals"]:
            self.state["goals"].append(add_goal)
            
        # add_unresolved: nullでなければunresolvedに追加（重複チェック）
        add_unresolved = delta.get("add_unresolved")
        if add_unresolved and add_unresolved not in self.state["unresolved"]:
            self.state["unresolved"].append(add_unresolved)
            
        # resolve: nullでなければunresolvedから部分一致で除去
        resolve = delta.get("resolve")
        if resolve:
            # resolve文字列が部分的にでも含まれる要素を除外
            self.state["unresolved"] = [
                item for item in self.state["unresolved"]
                if resolve.lower() not in item.lower()
            ]

        # 上限管理
        if len(self.state["goals"]) > 5:
            self.state["goals"] = self.state["goals"][-5:]
        if len(self.state["unresolved"]) > 8:
            self.state["unresolved"] = self.state["unresolved"][-8:]
            
        self._save()
        
    def increment_turn(self, tier: str):
         self.state["turn_count"] += 1
         self.state["last_tier"] = tier
         self._save()
    
    def to_prompt_text(self) -> str:
        """Gatekeeperのプロンプトに注入するテキスト形式に変換する。"""
        lines = [
            f"Topic: {self.state['topic'] or '(未設定)'}",
            f"Mood: {self.state['mood']}",
            f"Goals: {', '.join(self.state['goals']) if self.state['goals'] else '(なし)'}",
            f"Unresolved: {', '.join(self.state['unresolved']) if self.state['unresolved'] else '(なし)'}",
            f"Turn: {self.state['turn_count']}",
        ]
        return "\n".join(lines)
    
    def to_dict(self) -> dict:
        return self.state.copy()
    
    def reset(self):
        """セッションリセット。"""
        self.state = copy.deepcopy(self.DEFAULT_STATE)
        self._save()


# ===================================================================
# Gatekeeper: tier 判定クラス
# ===================================================================

class Gatekeeper:
    """
    Gemini API (2.5 Flash-Lite) を使って階層・不足前提・状態を判定するクラス。
    """

    def __init__(self, base_dir: Path = None):
        self.base_dir = base_dir or Path(__file__).resolve().parent.parent.parent
        self.client = self._configure_api(self.base_dir)
        try:
            self.model_name = AI_CONFIG["gatekeeper"]["model_name"]
            self.gatekeeper_config = AI_CONFIG["gatekeeper"]
        except KeyError:
            # Fallback
            self.model_name = "gemini-3.1-flash-lite-preview"
            self.gatekeeper_config = {
                "generation_config": {"temperature": 0.0, "max_output_tokens": 512},
                "safety_settings": []
            }

    def _configure_api(self, base_dir: Path):
        from dotenv import load_dotenv
        env_files = ["APIkey.env", ".env"]
        api_key = None
        for f in env_files:
            env_path = base_dir / f
            if env_path.exists():
                load_dotenv(dotenv_path=env_path, override=True)
                api_key = os.getenv("GOOGLE_API_KEY")
                if api_key: break
        
        if not api_key:
            api_key = os.getenv("GOOGLE_API_KEY")
        
        if not api_key:
            print("[Gatekeeper] Warning: API Keyが見つかりません。")
            # fallback for now, maybe it'll use ollama
            return None
            
        return genai.Client(api_key=api_key)

    # ------------------------------------------------------------------
    # 公開メソッド
    # ------------------------------------------------------------------

    def classify_tier_only(self, user_input: str, history_msgs: list, current_topic: str = "") -> str:
        """後方互換用: tier文字列のみを返す"""
        # SessionStateはない状態でコールされるため、ダミー辞書を渡す
        result = self.classify(user_input, history_msgs, session_state={}, current_topic=current_topic)
        return result.get("tier", "mid")

    def classify(
        self,
        user_input: str,
        history_msgs: list,
        session_state: dict,
        current_topic: str = "",
    ) -> dict:
        """
        ユーザー発言を tier に分類する。
        Returns:
            dict: 構造化JSON（tier, topic, need, search_targets, state_delta）
        """
        if not self.client:
             print("[Gatekeeper] APIクライアント未構成 → デフォルト: mid")
             return self._get_default_output()

        start_time = os.times().elapsed if hasattr(os, 'times') else 0 # simple tracking
        import time
        t0 = time.time()

        history_text = self._format_history(history_msgs, max_turns=3)
        
        # Format Session State
        state_text = ""
        if isinstance(session_state, dict):
            # SessionState obj.to_dict() is passed
            topic = session_state.get('topic', current_topic)
            lines = [
                f"Topic: {topic or '(未設定)'}",
                f"Mood: {session_state.get('mood', 'neutral')}",
                f"Goals: {', '.join(session_state.get('goals', [])) if session_state.get('goals') else '(なし)'}",
                f"Unresolved: {', '.join(session_state.get('unresolved', [])) if session_state.get('unresolved') else '(なし)'}",
                f"Turn: {session_state.get('turn_count', 0)}",
            ]
            state_text = "\n".join(lines)
        else:
             state_text = "(未設定)"

        prompt = GATEKEEPER_CLASSIFY_PROMPT.format(
            agent_name=SYSTEM_CONFIG["agent"]["agent_name"],
            session_state=state_text,
            history_text=history_text,
            user_input=user_input,
        )

        try:
             response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=self.gatekeeper_config["generation_config"].get("temperature", 0.0),
                    max_output_tokens=self.gatekeeper_config["generation_config"].get("max_output_tokens", 512),
                    safety_settings=self.gatekeeper_config.get("safety_settings")
                )
             )
             raw_text = response.text if response.text else ""
             result_dict = self._parse_response(raw_text)
        except Exception as e:
             print(f"[Gatekeeper] API呼び出しエラー: {e}")
             result_dict = self._get_default_output()
             
        t1 = time.time()
        tier_val = result_dict.get('tier', 'mid')
        need_str = result_dict.get('need')
        targets_str = result_dict.get('search_targets')
        
        print(f"[Gatekeeper] user='{user_input[:30]}' → tier={tier_val} ({int((t1-t0)*1000)}ms)")
        if tier_val == "cortex" and need_str:
            print(f"[Gatekeeper] need='{need_str}', search_targets={targets_str}")
            
        return result_dict

    # ------------------------------------------------------------------
    # 内部メソッド
    # ------------------------------------------------------------------

    def _get_default_output(self) -> dict:
        return {
            "tier": "mid",
            "topic": "",
            "need": None,
            "search_targets": None,
            "state_delta": {}
        }

    def _parse_response(self, raw_text: str) -> dict:
        """
        Gemini応答からJSONを抽出しパースする。
        失敗時はデフォルトのmid tierを返す。
        """
        default = self._get_default_output()
        if not raw_text:
            return default
            
        try:
            import re
            # Improved regex to handle newlines
            match = re.search(r"```json(.*?)```", raw_text, re.DOTALL)
            if match:
                json_str = match.group(1).strip()
            else:
                 json_str = raw_text.strip()
                 # Try to strip prefix if any
                 if json_str.startswith("{") and "}" in json_str:
                     json_str = json_str[json_str.find("{"):json_str.rfind("}")+1]

            data = json.loads(json_str)
            
            # Validate Tier
            tier = data.get("tier")
            if tier not in ["reflex", "mid", "cortex"]:
                data["tier"] = "mid"
                
            # Merge with default to ensure keys exist
            for k, v in default.items():
                if k not in data:
                    data[k] = copy.deepcopy(v)
                    
            if data["tier"] != "cortex":
                data["need"] = None
                data["search_targets"] = None
                
            return data
            
        except Exception as e:
            print(f"[Gatekeeper] JSONパースエラー: {e}\\nRaw: '{raw_text}'")
            return default

    def _format_history(self, history_msgs: list, max_turns: int = 3) -> str:
        """
        history_msgs の末尾 max_turns 件を人間が読める文字列へ変換する。
        各メッセージの role と parts[0] を使用する。
        """
        if not history_msgs:
            return "（履歴なし）"

        recent = history_msgs[-(max_turns * 2):]
        lines = []
        for msg in recent:
            role = msg.get("role", "unknown")
            parts = msg.get("parts", [""])
            text = parts[0] if parts else ""
            if isinstance(text, str) and len(text) > 80:
                text = text[:80] + "…"
            label = "ユーザー" if role == "user" else SYSTEM_CONFIG["agent"]["agent_name"]
            lines.append(f"{label}: {text}")

        return "\n".join(lines) if lines else "（履歴なし）"


# ===================================================================
# MemoryBlockBuilder: tier → 記憶ブロック dict を構築するクラス
# ===================================================================

class MemoryBlockBuilder:
    """
    tier に応じて brain.py に渡す記憶ブロック辞書を構築する。

    返却する辞書のキー:
        "short_term"  : 最近の会話履歴（list[dict]）
        "floating"    : 浮動要約テキスト（str）
        "mid_term"    : 中期記憶テキスト（str、mid 以上）
        "rag_context" : RAG 検索結果テキスト（str、cortex のみ）
        "tier"        : 使用した tier（str、デバッグ用）
    """

    def build(
        self,
        tier: str,
        memory_manager,
        brain=None,
        user_input: str = "",
        instance_name: str = "00_master",
        override_config: dict = None,
        gatekeeper_output: dict = None,
    ) -> dict:
        """
        Parameters
        ----------
        tier : str
            "reflex" | "mid" | "cortex"
        memory_manager : ButlyMemory
            記憶管理オブジェクト。
        brain : ButlyBrain | None
            cortex 時の RAG 検索に使用。None の場合 RAG はスキップ。
        user_input : str
            cortex 用 RAG 検索のクエリ文字列。
        instance_name : str
            RAG 検索時のインスタンス名。
        override_config : dict | None
            brain.search_knowledge へ渡すオーバーライド設定。
        gatekeeper_output : dict | None
            Gatekeeperの出力する構造化辞書

        Returns
        -------
        dict
            記憶ブロック辞書。
        """
        # --- 全 tier 共通 ---
        short_term_msgs, _ = memory_manager.load_recent_sessions(limit=6)
        floating = memory_manager.get_floating_summary()

        blocks = {
            "tier": tier,
            "short_term": short_term_msgs,
            "floating": floating,
            "mid_term": "",
            "rag_context": "",
            "need": gatekeeper_output.get("need") if gatekeeper_output else None,
            "search_targets": gatekeeper_output.get("search_targets") if gatekeeper_output else None,
        }

        if tier == "reflex":
            # short_term + floating のみ
            print("[Gatekeeper] MemoryBlock: reflex（short_term + floating）")
            return blocks

        # --- mid 以上: mid_term を追加 ---
        from butly_core.config import SYSTEM_CONFIG
        use_summary = SYSTEM_CONFIG.get("memory", {}).get("use_summarized_mid_term", False)
        
        if use_summary:
            # 要約モード: digest + relationship を使用
            digest = memory_manager.get_mid_term_digest()
            relationship = memory_manager.get_mid_term_relationship()
            
            if digest or relationship:
                blocks["mid_term_digest"] = digest
                blocks["mid_term_relationship"] = relationship
                blocks["mid_term"] = ""  # RAWは空にする
                blocks["mid_term_mode"] = "summary"
                total_chars = len(digest) + len(relationship)
                print(f"[Gatekeeper] MemoryBlock: {tier}（+ digest {len(digest)}文字 + relationship {len(relationship)}文字 = {total_chars}文字）")
            else:
                # 要約ファイルが存在しない → RAWにフォールバック
                mid_term = memory_manager.get_mid_term_text_content()
                blocks["mid_term"] = mid_term
                blocks["mid_term_mode"] = "raw_fallback"
                print(f"[Gatekeeper] MemoryBlock: {tier}（要約なし → RAWフォールバック {len(mid_term)}文字）")
        else:
            # RAWモード: 従来通り
            mid_term = memory_manager.get_mid_term_text_content()
            blocks["mid_term"] = mid_term
            blocks["mid_term_mode"] = "raw"
            print(f"[Gatekeeper] MemoryBlock: {tier}（+ mid_term RAW {len(mid_term)}文字）")

        if tier == "mid":
            return blocks

        # --- cortex のみ: RAG 検索を実施 ---
        if brain and user_input:
            try:
                keyword_data = brain.extract_keywords(user_input, override_config)
                keywords = keyword_data.get("keywords", [])
                from butly_core.config import SYSTEM_CONFIG
                limit = SYSTEM_CONFIG["brain"]["search_limit"]
                knowledge_list = brain.search_knowledge(
                    keywords,
                    user_input,
                    instance_name=instance_name,
                    limit=limit,
                    override_config=override_config,
                )
                if knowledge_list:
                    rag_lines = ["【過去の記憶（RAG）】"]
                    for k in knowledge_list:
                        rag_lines.append(f"・{k['title']}: {k['summary']}")
                        if k.get("episode"):
                            rag_lines.append(f"  (補足: {k['episode']})")
                    blocks["rag_context"] = "\n".join(rag_lines)
                    print(f"[Gatekeeper] MemoryBlock: cortex（RAG hits={len(knowledge_list)}）")
            except Exception as e:
                print(f"[Gatekeeper] RAG 検索エラー: {e}")
        else:
            print("[Gatekeeper] MemoryBlock: cortex（brain/user_input 未指定のため RAG スキップ）")

        return blocks


# ===================================================================
# MemoryBlocksToSystemInstruction: ブロック → system_instruction 変換
# ===================================================================

def build_system_instruction_from_blocks(
    blocks: dict,
    memory_manager,
    use_google_search: bool = False,
) -> str:
    """
    MemoryBlockBuilder.build() の戻り値から system_instruction 文字列を生成する。

    brain.py の start_chat / chat_with_interactions と同じ構造を踏襲しつつ、
    tier に応じた記憶ブロックのみを含める。

    Parameters
    ----------
    blocks : dict
        MemoryBlockBuilder.build() の戻り値。
    memory_manager : ButlyMemory
        system_instruction.txt / Key_Memory.txt の読み取りに使用。
    use_google_search : bool
        Google 検索使用フラグ（注意書き用）。

    Returns
    -------
    str
        Gemini に渡す system_instruction 文字列。
    """
    from butly_core.core.chronos import ButlyChronos

    tier = blocks.get("tier", "mid")
    sections = []

    # 1. SYSTEM INSTRUCTION（性格設定）— 全 tier 共通
    sys_inst = memory_manager.get_system_instruction()
    sections.append(f"=== SYSTEM INSTRUCTION ===\n{sys_inst}")

    # 2. KEY MEMORY（根幹記憶）— 全 tier 共通
    key_mem = memory_manager.get_key_memory()
    if key_mem:
        sections.append(f"=== KEY MEMORY (根幹記憶) ===\n{key_mem}")

    # 3. MID-TERM MEMORY（中期記憶）— mid 以上
    if tier in ("mid", "cortex"):
        mid_term_mode = blocks.get("mid_term_mode", "raw")
        
        if mid_term_mode == "summary":
            # 要約モード: digest + relationship を個別セクションで注入
            digest = blocks.get("mid_term_digest", "")
            relationship = blocks.get("mid_term_relationship", "")
            
            if digest:
                sections.append(
                    f"=== MID-TERM DIGEST (中期記憶・事実ダイジェスト) ===\n"
                    f"※以下はAIの主観的な記憶です。直近の会話と矛盾する場合は、直近の会話を優先してください。\n"
                    f"{digest}"
                )
            if relationship:
                sections.append(
                    f"=== RELATIONSHIP SNAPSHOT (関係性スナップショット) ===\n"
                    f"※以下は現在の関係性のステータスです。Key Memoryの根幹を補完する参考情報として扱ってください。\n"
                    f"{relationship}"
                )
        else:
            # RAWモード（raw / raw_fallback）: 従来通り
            mid_term = blocks.get("mid_term", "")
            if mid_term:
                sections.append(f"=== MID-TERM MEMORY (中期記憶) ===\n{mid_term}")

    # 4. CURRENT TIME（現在時刻）— 全 tier 共通
    current_time = ButlyChronos().get_system_note()
    sections.append(
        f"=== CURRENT TIME (現在時刻) ===\n{current_time}\n"
        "※上記の時刻情報は文脈理解のための参考情報です。随時日付や時間を読み上げる必要はありません。あくまで自然な対話を最優先してください。"
    )

    # 5. RAG コンテキスト — cortex のみ
    rag_context = blocks.get("rag_context", "")
    if tier == "cortex" and rag_context:
        sections.append(f"=== LONG-TERM MEMORY (RAG) ===\n※以下は過去の記録からの参考情報です。直近の会話と矛盾する場合は、直近の会話を優先してください。\n{rag_context}")

    # 6. FLOATING SUMMARY（未整理記憶）— 全 tier 共通
    floating = blocks.get("floating", "")
    if floating:
        sections.append(f"=== FLOATING SUMMARY (未整理記憶) ===\n※以下は直近の会話の要約です。直前の3ターンと併用して最も新しい文脈として優先的に参照してください。\n{floating.strip()}")

    # 7. tier 情報を注記（デバッグ用にコメントアウト可能）
    sections.append(f"=== TIER INFO ===\n現在の思考モード: {tier}")

    return "\n\n".join(sections)


# ===================================================================
# 動作確認（スタンドアロン実行）
# ===================================================================

if __name__ == "__main__":
    import shutil
    print("=== Gatekeeper V2 テスト開始 ===")

    # テスト用インスタンス・ディレクトリ
    base_dir = Path(__file__).resolve().parent.parent.parent
    test_instance_dir = base_dir / "butly_core" / "instances" / "test_gk_instance"
    if test_instance_dir.exists():
        shutil.rmtree(test_instance_dir)
    test_instance_dir.mkdir(parents=True, exist_ok=True)

    print("\\n[Test 1] SessionState の挙動テスト")
    state = SessionState(test_instance_dir)
    state.reset()
    state.apply_delta({
        "topic": "テスト開始",
        "mood": "focused",
        "add_goal": "Gatekeeper改修",
        "add_unresolved": "プロンプト最適化",
        "resolve": None,
    })
    
    assert state.state["topic"] == "テスト開始", "topic update failed"
    assert "Gatekeeper改修" in state.state["goals"], "goal add failed"
    assert "プロンプト最適化" in state.state["unresolved"], "unresolved add failed"
    print(" - Delta Apply (Add): OK")

    state.apply_delta({
        "resolve": "プロンプト",
        "topic": None,
        "mood": None,
        "add_goal": None,
        "add_unresolved": None
    })
    assert "プロンプト最適化" not in state.state["unresolved"], "resolve failed"
    print(" - Delta Apply (Resolve): OK")
    
    state.increment_turn("mid")
    assert state.state["turn_count"] == 1
    assert state.state["last_tier"] == "mid"
    print(" - Turn Increment: OK")


    print("\\n[Test 2] Gatekeeper classify テスト")
    gk = Gatekeeper(base_dir=base_dir)
    
    if not gk.client:
        print("APIキーが設定されていないため、LLMテストをスキップします。")
    else:
        test_cases = [
            ("おはよう", {}, "reflex"),
            ("うん、わかった", {}, "reflex"),
            ("今日のタスクを教えて", {"topic": "日常"}, "mid"),
            ("さっきの手順をもう一度確認させて", {"topic": "API設計"}, "mid"),
            ("前に話したプロジェクト計画を振り返りたい", {}, "cortex"),
            ("あの時のコードのバグって結局どうなったの", {}, "cortex"),
        ]
        
        for user_input, st_inject, expected in test_cases:
            print(f"\\n--- Input: {user_input} ---")
            
            # 状態を準備
            state.reset()
            if st_inject:
                state.apply_delta(st_inject)
            
            # Classify!
            result = gk.classify(
                user_input=user_input,
                history_msgs=[],
                session_state=state.to_dict(),
            )
            
            tier = result.get('tier')
            needs = result.get('need')
            targets = result.get('search_targets')
            delta = result.get('state_delta', {})
            
            assert tier in ["reflex", "mid", "cortex"], f"Invalid tier: {tier}"
            print(f"Tier: {tier} (Expected ~ {expected})")
            print(f"Delta: {delta}")
            
            if tier == "cortex":
                assert needs is not None, "cortex has NO needs"
                assert targets is not None, "cortex has NO targets"
                print(f"Needs: {needs}")
                print(f"Targets: {targets}")
            
            # Apply delta context
            state.apply_delta(delta)
            
        print("\\n=== すべてのテストが完了しました ===")
        
    if test_instance_dir.exists():
        shutil.rmtree(test_instance_dir)

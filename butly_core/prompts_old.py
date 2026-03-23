
"""
Butly Project Prompts Configuration
------------------------------------
後方互換ラッパー。実体は butly_core/prompts/ パッケージの PromptLoader。
旧定数名（HOUSEKEEPER_SUMMARIZE_PROMPT 等）でのアクセスを維持する。
"""

from pathlib import Path

from butly_core.prompts import PromptLoader

_loader = PromptLoader()

# ==========================================
# 後方互換: 旧定数名でテンプレート文字列を公開
# ==========================================
HOUSEKEEPER_SUMMARIZE_PROMPT = _loader.get_template("housekeeper_summarize")
BRAIN_EXTRACT_KEYWORDS_PROMPT = _loader.get_template("brain_extract_keywords")
BRAIN_SUMMARIZE_CONVERSATION_PROMPT = _loader.get_template("brain_summarize_conversation")
GATEKEEPER_CLASSIFY_PROMPT = _loader.get_template("tier_classifier")
MIDTERM_DIGEST_PROMPT = _loader.get_template("midterm_digest")
MIDTERM_RELATIONSHIP_PROMPT = _loader.get_template("midterm_relationship")
WEB_UI_DEFAULT_TEMPLATE = _loader.get_template("web_ui_default_template")

# --- User Prompts Override (後方互換) ---
import json
import sys

USER_PROMPTS_PATH = Path(__file__).parent.parent / "user_prompts.json"
if USER_PROMPTS_PATH.exists():
    try:
        with open(USER_PROMPTS_PATH, "r", encoding="utf-8") as f:
            user_prompts = json.load(f)
            current_module = sys.modules[__name__]
            for key, value in user_prompts.items():
                if hasattr(current_module, key):
                    setattr(current_module, key, value)
        print("[Prompts] Loaded user_prompts.json")
    except Exception as e:
        print(f"[Prompts] Failed to load user_prompts.json: {e}")

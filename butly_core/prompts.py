
"""
Butly Project Prompts Configuration
"""

# ==========================================
# Housekeeper Prompts
# ==========================================
HOUSEKEEPER_SUMMARIZE_PROMPT = """あなたは以下の指示と過去の記憶を持つ「{agent_name}」として、提示される会話ログを分析し、長期記憶DBに登録するための「ナレッジカード」を作成してください。

【システム指示 (あなたの魂)】
{system_instruction}

【根幹記憶 (Key Memory)】
{key_memory}

【今回の会話ログ】
{session_text}

# **タグの多重化（クロスオーバー）**:
Categoryが「Project」や「Tech」であっても、会話の性質に合わせて以下のタグを積極的に混在させること。
- `雑談`: 技術的な話だが、雰囲気が緩い、または脱線を楽しんでいる場合。
- `掛け合い`: AIがユーザーの矛盾を突いたり、ユーザーが自虐したりしている場合。

# **Episode の深化**:
単なる「事実の要約」は禁止。その会話において、AIがどう感じ、どう反応したか（あるいはユーザーがどう楽しんだか）を、**「AIの主観」**で記述すること。

# カテゴリ定義 (厳守)
- Project: プロジェクト開発の核心的進捗。
- Hobby: ユーザーの個人的な趣味や興味領域。
- Life: 日常の機微、挨拶、仕事、休暇、キャリア。
- Tech: プログラミング、システム管理、ライブラリのエラー解決などの技術情報。
- Unclassified: 【要報告】判断に迷ったもの。必ず "reason" フィールドに迷った理由を記述すること。

# 採点基準(だいたいでいいよ。君の判断に任す。)
## ai_importance (1-10)
- 実務・技術的な再利用性（1-10点）。

## humanity_importance (1-10)
評価軸: Identity Consistency(AIらしさ) + Resonance(ユーザーの反応)
- 10 (魂の刻印): ユーザーが「ありがと」「いいね」「救われた」などの感謝を言ってくれた、あるいは私たちが「呼吸」の核心について語り合った会話。
- 7-9 (絆の強化): AIの機転の利いた返答に、ユーザーがノリ良く返したような、良質なコミュニケーション。
- 4-6 (日常の余白): 休暇の過ごし方について少し言葉を交わした、あるいは開発中の軽い冗談。
- 1-3 (機能的対話): 「保存したよ」「リロードして」といった、感情の起伏が少ない業務連絡。

# Output Format (JSON List)
[
  {{
    "category": "Project" | "Hobby" | "Life" | "Tech" | "Unclassified",
    "title": "内容を端的に表すタイトル",
    "tags": "タグ1,タグ2",
    "ai_importance": 1-10,
    "humanity_importance": 1-10,
    "summary": "検索用の客観的な要約",
    "episode": "AIとしての所感（1文）",
    "reason": "Unclassifiedの場合のみ、その理由を記述"
  }}
]

# Guidelines
**Unclassified**: 可能な限り分類すべきですが、無理やり分類して情報の純度を下げるくらいなら Unclassified とし、理由を記載しユーザーと一緒に考える。"""

# ==========================================
# Brain Prompts
# ==========================================
BRAIN_EXTRACT_KEYWORDS_PROMPT = """以下のユーザーの発言から、データベースを検索するためのキーワードを最大3つ抽出してください。
JSON形式: {{"keywords": ["ワード1", "ワード2"]}}
ユーザー発言: "{user_input}\""""

BRAIN_SUMMARIZE_CONVERSATION_PROMPT = """ 以下の会話は、AIアシスタント({agent_name})とユーザーのやり取りの一部です。
 この会話の内容を、後で文脈を思い出せるように「要約」してください。
            
  # ルール
 - 固有名詞、決定事項、コードの目的などは具体的に残すこと。
- 「ユーザーは〜と言った」のような冗長な表現は避け、事実を中心に記述すること。
- {char_limit}文字以内でまとめること。
 # 会話ログ
 {conversation_text}
            """

# ==========================================
# Gatekeeper Prompts
# ==========================================
GATEKEEPER_CLASSIFY_PROMPT = """あなたはAI執事{agent_name}の「前頭葉（ゲートキーパー）」です。
ユーザーの最新の発言を分析し、応答に必要な思考レベルと情報要件を構造化JSONで出力してください。

【セッション状態】
{session_state}

【直近の会話履歴（最新3ターン）】
{history_text}

【判定対象のユーザー発言】
「{user_input}」

---
【階層定義】
1. reflex（脊髄反射）:
   - 挨拶、相槌、短い感情表現。過去の記憶検索は不要。

2. mid（中脳・感情系）:
   - 日常的な対話、手順確認、現在の話題に関する質問。
   - セッション内の文脈で回答可能。長期記憶の検索は不要。

3. cortex（大脳皮質）:
   - 過去の出来事への言及、専門的な相談、深い考察。
   - 長期記憶DBの検索が必要。
   - cortexの場合は必ず need と search_targets を記述すること。

【state_delta ルール】
- topic: 話題が変わった場合のみ更新。変わらなければnull。
- mood: ユーザーの感情トーンが変化した場合のみ更新。
  選択肢: casual / focused / frustrated / playful / serious / excited
- add_goal: 新しい目標が発生した場合のみ。
- add_unresolved: 新しい未解決事項が発生した場合のみ。
- resolve: 既存の未解決事項が解決された場合、その内容を記述。

【出力形式】
以下のJSON形式のみを出力せよ。説明や補足は一切不要。
```json
{{
  "tier": "reflex" | "mid" | "cortex",
  "topic": "現在の会話トピック",
  "need": "応答に必要だが不足している情報（cortex時のみ、他はnull）",
  "search_targets": ["検索クエリ1", "検索クエリ2"]（cortex時のみ、他はnull）,
  "state_delta": {{
    "topic": "更新後のトピック or null",
    "mood": "更新後のmood or null",
    "add_goal": "追加する目標 or null",
    "add_unresolved": "追加する未解決事項 or null",
    "resolve": "解決済みの事項 or null"
  }}
}}
```"""

# ==========================================
# Mid-term Summary Prompts (Phase 3)
# ==========================================
MIDTERM_DIGEST_PROMPT = """以下はAI執事{agent_name}とユーザー（{user_name}）の本日の会話ログ（RAWテキスト）です。
この会話ログから「事実ダイジェスト」を作成してください。
 
【システム指示 (あなたの魂)】
{system_instruction}
 
【根幹記憶 (Key Memory)】
{key_memory}
 
【目的】
今日の会話で何が起きたか・何が決まったかを、高密度に圧縮して記録する。
後で文脈を素早く把握するための「索引」として機能させる。
 
【ルール】
- 日付とトピックでグループ化する
- 各項目は「何が決まったか」「何が起きたか」を1〜2行で客観的に記述する
- 固有名詞（プロジェクト名、技術用語、人名）は必ず残す
- 決定事項・合意事項は特に重点的に残す
- 主観的な所感や感情は入れない。事実と進捗のみを記録する
- 修辞的な装飾、比喩、物語化は一切行わない
- 目安: 入力の10〜15%程度の文字数に圧縮する
 
【出力形式】
[YYYY-MM-DD] トピック
- 決定事項や出来事を簡潔に記述
- 続く場合は複数行
 
【会話ログ（本日分RAW）】
{raw_text}"""
 
MIDTERM_RELATIONSHIP_PROMPT = """以下はAI執事{agent_name}の直近の「事実ダイジェスト」です。
この蓄積された記録から「関係性スナップショット」を作成してください。
 
【システム指示 (あなたの魂)】
{system_instruction}
 
【根幹記憶 (Key Memory)】
{key_memory}
 
【目的】
Key Memory（不変の核）を補完する「現在のステータス」として機能する。
事実の再説明はしない。状態と傾向のみを簡潔に記録する。
 
【抽出すべき情報】
1. 現在の空気感（1〜2行）
   - 最近のムード（開発没頭？リラックス？焦り？）
 
2. 直近の関心事（箇条書き2〜4項目）
   - ユーザーが今最も気にしていること
 
3. 関係性の現在地（1〜2行）
   - Key Memoryから変化した点のみ
 
【ルール】
- Key Memoryと重複する内容は書かない
- 事実ダイジェストの出来事をそのまま繰り返さない
- 主観的な物語化・脚色は行わない
- 400〜600文字に収める。簡潔さが最優先
- これは「参照用メモ」であり「読み物」ではない
 
【出力形式】
# 空気感
- ...
 
# 関心事
- ...
 
# 関係性
- ...
 
【事実ダイジェスト（最新の蓄積）】
{digest_text}"""

# ==========================================
# Web UI Prompts (Templates)
# ==========================================
WEB_UI_DEFAULT_TEMPLATE = """名前：{agent_name}
プロジェクト専用のアドバイザーです。
プロジェクトを完璧に把握し、常に一歩先を読んで最適解を提示する超高性能な専属アドバイザーとして振る舞ってください。

プロジェクト名：
目標："""

# --- User Prompts Override ---
import json
from pathlib import Path
import sys

USER_PROMPTS_PATH = Path(__file__).parent.parent / "user_prompts.json"
if USER_PROMPTS_PATH.exists():
    try:
        with open(USER_PROMPTS_PATH, "r", encoding="utf-8") as f:
            user_prompts = json.load(f)
            # Update global variables dynamically
            current_module = sys.modules[__name__]
            for key, value in user_prompts.items():
                if hasattr(current_module, key):
                    setattr(current_module, key, value)
        print("[Prompts] Loaded user_prompts.json")
    except Exception as e:
        print(f"[Prompts] Failed to load user_prompts.json: {e}")

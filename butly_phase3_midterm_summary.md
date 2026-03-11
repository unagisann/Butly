# Butly Phase 3: Mid-term 二層要約生成パイプライン構築
## Housekeeper Stage1 拡張 — エピソード付きダイジェスト（日次）+ 関係性スナップショット（週次）

## 概要

mid_term.txtの既存パイプライン（RAW会話ログの蓄積・アーカイブ）は一切変更せず、
Housekeeper Stage1の処理末尾に二層要約の生成ステップを追加する。

**このフェーズの目標**:
1. 事実ダイジェスト: 日次で当日分RAWからエピソード感情付きの要約を差分追記
2. 関係性スナップショット: 前回更新から7日以上経過した場合のみ再生成
3. Brain（LLM）への注入切替は行わない（Phase 4で対応）

**設計思想**:
- 事実ダイジェストは「データベースのレコード」ではなく「ジャービスの記憶」として扱われるよう、エピソード（感情・所感）を含める。これによりLLMが柔軟に扱える余地を持たせる
- 関係性スナップショットはKey Memoryが「不変の核」、スナップショットが「緩やかに変化するステータス」という役割分担。毎日書き換えると不安定になるため週次とする
- 構造化データが「確定した事実」として固定される危険を避けるため、主観的ニュアンスを意図的に残す

**既存パイプラインで変更しないもの**:
- mid_term.txt の蓄積・アーカイブ処理すべて
- short_term_json → 1_integrated → 2_knowledgeized のフロー
- Stage 2（ナレッジカード生成）、Stage 3（DBバックアップ）

---

## 対象ファイル

| ファイル | 変更種別 | 説明 |
|---------|---------|------|
| `housekeeper.py` | **追記** | `stage_1_cleanup()`末尾に要約生成ステップ追加 |
| `butly_core/prompts.py` | **追記** | 要約用プロンプトテンプレート2つ追加 |
| `butly_core/config.py` | **修正+追記** | summaryモデル更新、要約設定追加 |

**変更しないファイル**: main.py, gatekeeper.py, brain.py, memory.py

---

## 生成するファイル

```
butly_core/instances/{instance_name}/
├── mid_term.txt                    ← 既存（RAW、変更なし）
├── mid_term_digest.txt             ← ★NEW: エピソード付き事実ダイジェスト（日次差分追記）
├── mid_term_relationship.txt       ← ★NEW: 関係性スナップショット（週次上書き）
└── memory_archive/
    └── 3_log/
        ├── archive_long_term.txt   ← 既存
        └── archive_digest.txt      ← ★NEW: digestから溢れた古い要約のアーカイブ
```

---

## モデル設定の更新

### config.py の AI_CONFIG 更新

```python
"summary": {
    "model_name": "gemini-3.1-flash-lite-preview",  # ★更新
    ...
}
```

### config.py の SYSTEM_CONFIG に追加

```python
"memory": {
    "max_mid_term_chars": 30000,
    "short_term_limit": 6,
    "generate_mid_term_summaries": True,   # ★NEW: 二層要約の生成を有効化
    "max_digest_chars": 8000,              # ★NEW: digest上限（超過分はアーカイブ）
    "relationship_update_interval_days": 7, # ★NEW: 関係性スナップショットの更新間隔（日数）
}
```

---

## 変更1: housekeeper.py の拡張

### 1.1 stage_1_cleanup() の末尾に追加

`stage_1_cleanup()` の末尾（「4. Short Term JSON の空フォルダ削除」の後）に追加:

```python
        # --- 6. ★NEW: 二層要約の日次生成 ---
        if new_text.strip():
            self._generate_daily_digest(instance_path, new_text)
            self._update_relationship_if_due(instance_path)
```

### 1.2 事実ダイジェスト（日次差分追記）

```python
    def _generate_daily_digest(self, instance_path: Path, new_text: str):
        """
        当日分のRAWテキストからエピソード付き事実ダイジェストを生成し、
        mid_term_digest.txt に差分追記する。
        上限超過時は古い部分を archive_digest.txt へアーカイブ。
        
        入力は常に当日分のRAW（new_text）のみ。要約の要約は絶対にしない。
        """
        from butly_core.config import SYSTEM_CONFIG, AI_CONFIG
        
        if not SYSTEM_CONFIG.get("memory", {}).get("generate_mid_term_summaries", True):
            print("[Housekeeper] Mid-term summary generation is disabled in config.")
            return
        
        if len(new_text.strip()) < 200:
            print(f"[Housekeeper] Daily digest: new_text too short ({len(new_text)} chars), skipping.")
            return
        
        print(f"[Housekeeper] Daily digest: Generating from {len(new_text)} chars of today's raw text...")
        
        from google import genai
        from google.genai import types as genai_types
        from butly_core.prompts import MIDTERM_DIGEST_PROMPT
        
        try:
            client = self._get_genai_client()
            if not client:
                print("[Housekeeper] Daily digest: API client not available, skipping.")
                return
            
            summary_conf = AI_CONFIG.get("summary", {})
            model_name = summary_conf.get("model_name", "gemini-3.1-flash-lite-preview")
            safety = summary_conf.get("safety_settings")
            temp = summary_conf.get("generation_config", {}).get("temperature", 0.3)
            
            digest_file = instance_path / "mid_term_digest.txt"
            archive_digest_file = instance_path / "memory_archive" / "3_log" / "archive_digest.txt"
            archive_digest_file.parent.mkdir(parents=True, exist_ok=True)
            
            digest_prompt = MIDTERM_DIGEST_PROMPT.format(raw_text=new_text)
            digest_response = client.models.generate_content(
                model=model_name,
                contents=digest_prompt,
                config=genai_types.GenerateContentConfig(
                    temperature=temp,
                    max_output_tokens=2048,
                    safety_settings=safety,
                ),
            )
            digest_new = digest_response.text.strip() if digest_response.text else ""
            
            if digest_new:
                # 既存digestに追記
                current_digest = digest_file.read_text(encoding="utf-8") if digest_file.exists() else ""
                combined_digest = current_digest + "\n" + digest_new if current_digest else digest_new
                
                # 上限チェック & アーカイブ（mid_term.txtと同じパターン）
                max_digest_chars = SYSTEM_CONFIG.get("memory", {}).get("max_digest_chars", 8000)
                
                if len(combined_digest) > max_digest_chars:
                    min_overflow = len(combined_digest) - max_digest_chars
                    cut_point = combined_digest.find('\n', min_overflow)
                    if cut_point == -1:
                        cut_point = min_overflow
                    else:
                        cut_point += 1
                    overflow_text = combined_digest[:cut_point]
                    kept_text = combined_digest[cut_point:]
                    
                    with open(archive_digest_file, "a", encoding="utf-8") as f:
                        f.write(overflow_text)
                    print(f"[Housekeeper] Digest archived: {len(overflow_text)} chars to archive_digest.txt")
                    digest_file.write_text(kept_text, encoding="utf-8")
                else:
                    digest_file.write_text(combined_digest, encoding="utf-8")
                
                print(f"[Housekeeper] Digest updated: +{len(digest_new)} chars")
            
        except Exception as e:
            print(f"[Housekeeper] Daily digest generation error: {e}")
```

### 1.3 関係性スナップショット（条件付き週次更新）

```python
    def _update_relationship_if_due(self, instance_path: Path):
        """
        関係性スナップショットを条件付きで更新する。
        前回の更新から relationship_update_interval_days（デフォルト7日）以上
        経過している場合のみ再生成する。
        
        入力は mid_term_digest.txt（蓄積された事実ダイジェスト）を使用する。
        日々の断片ではなく、最近の全体像から関係性パターンを抽出するため。
        
        関係性は緩やかに変化するもの。毎日書き換えると不安定になるため、
        週次程度の頻度で更新する。
        """
        from butly_core.config import SYSTEM_CONFIG, AI_CONFIG
        from datetime import datetime
        import os
        
        if not SYSTEM_CONFIG.get("memory", {}).get("generate_mid_term_summaries", True):
            return
        
        rel_file = instance_path / "mid_term_relationship.txt"
        digest_file = instance_path / "mid_term_digest.txt"
        interval_days = SYSTEM_CONFIG.get("memory", {}).get("relationship_update_interval_days", 7)
        
        # 前回更新日の確認
        should_update = False
        if not rel_file.exists():
            should_update = True
            print("[Housekeeper] Relationship: File not found, creating initial snapshot.")
        else:
            last_modified = datetime.fromtimestamp(os.path.getmtime(rel_file))
            days_since = (datetime.now() - last_modified).days
            if days_since >= interval_days:
                should_update = True
                print(f"[Housekeeper] Relationship: {days_since} days since last update (interval: {interval_days}), updating.")
            else:
                print(f"[Housekeeper] Relationship: {days_since} days since last update (interval: {interval_days}), skipping.")
        
        if not should_update:
            return
        
        # 入力: 蓄積された事実ダイジェスト
        if not digest_file.exists():
            print("[Housekeeper] Relationship: No digest file yet, skipping.")
            return
        
        digest_text = digest_file.read_text(encoding="utf-8").strip()
        if len(digest_text) < 200:
            print("[Housekeeper] Relationship: digest too short, skipping.")
            return
        
        from google import genai
        from google.genai import types as genai_types
        from butly_core.prompts import MIDTERM_RELATIONSHIP_PROMPT
        
        try:
            client = self._get_genai_client()
            if not client:
                print("[Housekeeper] Relationship: API client not available, skipping.")
                return
            
            summary_conf = AI_CONFIG.get("summary", {})
            model_name = summary_conf.get("model_name", "gemini-3.1-flash-lite-preview")
            safety = summary_conf.get("safety_settings")
            temp = summary_conf.get("generation_config", {}).get("temperature", 0.3)
            
            rel_prompt = MIDTERM_RELATIONSHIP_PROMPT.format(digest_text=digest_text)
            rel_response = client.models.generate_content(
                model=model_name,
                contents=rel_prompt,
                config=genai_types.GenerateContentConfig(
                    temperature=temp,
                    max_output_tokens=1024,
                    safety_settings=safety,
                ),
            )
            rel_text = rel_response.text.strip() if rel_response.text else ""
            
            if rel_text:
                rel_file.write_text(rel_text, encoding="utf-8")
                print(f"[Housekeeper] Relationship snapshot updated: {len(rel_text)} chars.")
            
        except Exception as e:
            print(f"[Housekeeper] Relationship generation error: {e}")
```

### 1.4 genaiクライアント取得メソッド

```python
    def _get_genai_client(self):
        """Gemini APIクライアントを取得する。"""
        import os
        from dotenv import load_dotenv
        from google import genai
        
        api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
        
        if not api_key:
            env_files = [BASE_DIR / "APIkey.env", BASE_DIR / ".env"]
            for env_file in env_files:
                if env_file.exists():
                    load_dotenv(dotenv_path=env_file, override=True)
                    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
                    if api_key:
                        break
        
        if not api_key:
            return None
        
        return genai.Client(api_key=api_key)
```

### 1.5 importの追加

housekeeper.pyの先頭のimportセクションに以下を追加する（必要に応じて）:

```python
from google import genai
from google.genai import types as genai_types
```

---

## 変更2: prompts.py に要約プロンプトを追加

### 2.1 事実ダイジェスト用プロンプト（エピソード付き）

```python
MIDTERM_DIGEST_PROMPT = """以下はAI執事ジャービスとユーザー（主人）の本日の会話ログ（RAWテキスト）です。
この会話ログから「エピソード付き事実ダイジェスト」を作成してください。

【目的】
今日の会話で何が起きたか・何が決まったかを圧縮して記録する。
ただし無味乾燥な事実の羅列ではなく、ジャービスの「記憶」として
温かみと主観的なニュアンスを持った形で記録する。

【ルール】
- 日付とトピックでグループ化する
- 各項目は「何が決まったか」「何が起きたか」を記述する
- 固有名詞（プロジェクト名、技術用語、人名）は必ず残す
- 決定事項・合意事項は特に重点的に残す
- 各項目にジャービスとしての所感（1文）を添える
  例: 「主人の直感がまた正しかった」「私の皮肉が少々きつすぎたかもしれない」
- 事実を歪めてはならないが、「確定した事実」ではなく「記憶」として扱えるよう
  主観的なニュアンスを残す
- 目安: 入力の10〜15%程度の文字数に圧縮する

【出力形式】
[YYYY-MM-DD] トピック
- 出来事や決定事項の記述
  → 所感: ジャービスの一言

[YYYY-MM-DD] トピック
- ...
  → 所感: ...

【会話ログ（本日分RAW）】
{raw_text}"""
```

### 2.2 関係性スナップショット用プロンプト

```python
MIDTERM_RELATIONSHIP_PROMPT = """以下はAI執事ジャービスの直近の「事実ダイジェスト」（エピソード付きの記憶要約）です。
この蓄積された記憶から「関係性スナップショット」を作成してください。

【目的】
Key Memory（不変の核）を補完する「現在のステータス」として機能する。
関係性の根幹はKey Memoryが保持しているため、ここでは環境や状況の
移り変わりに応じた「今の温度感」を記録する。

【抽出すべき情報】
1. 現在のトーンと空気感
   - 最近の会話で支配的なムード（開発に没頭？リラックス？焦り？）
   - 今のやりとりの距離感

2. 直近の関心事・優先事項
   - ユーザーが今最も気にしていること
   - 進行中のプロジェクトや課題の状態

3. 関係性の現在地
   - Key Memoryから変化した点があれば
   - 最近の印象的なやりとり

【ルール】
- Key Memoryとの重複は避ける（根幹の関係性はあちらに任せる）
- 事実ダイジェストの事実をそのまま繰り返さない（パターンと傾向を抽出する）
- 800〜1500文字程度に収める
- 「ステータス」として簡潔に。長い説明は不要

【出力形式】
# 現在のトーンと空気感
- ...

# 直近の関心事・優先事項
- ...

# 関係性の現在地
- ...

【事実ダイジェスト（最新の蓄積）】
{digest_text}"""
```

---

## テスト計画

### 3.1 手動テスト

1. **Housekeeper実行**: `python housekeeper.py`
2. **digestファイル生成確認**: `mid_term_digest.txt` が生成され、エピソード（所感）が含まれていること
3. **digest差分追記確認**: 2回目の実行（間に会話を挟む）でdigestに追記されること
4. **digestアーカイブ確認**: `max_digest_chars`（8000）超過時に `archive_digest.txt` へ退避されること
5. **relationship初回生成**: `mid_term_relationship.txt` が生成されること
6. **relationshipスキップ確認**: 7日未満で再実行 → 「skipping」ログが出ること
7. **relationship更新確認**: ファイルの更新日を7日以上前に変更して再実行 → 再生成されること
8. **mid_term.txt不変確認**: Phase 3の変更でmid_term.txtが一切変わっていないこと
9. **new_textが空の場合**: 要約生成がスキップされること
10. **設定無効化**: `generate_mid_term_summaries: false` → 要約が生成されないこと
11. **既存パイプライン不変**: Stage2, Stage3が正常動作すること

### 3.2 品質確認（目視）

- digest: 事実が圧縮されつつ、所感（エピソード）が温かみを持っているか
- digest: 「確定した事実」ではなく「記憶」として読めるか
- relationship: Key Memoryと重複していないか
- relationship: 「ステータス」として簡潔か

---

## 実装上の注意事項

1. **入力は常に当日分のRAW（new_text）のみ**: mid_term.txt全文やdigest自体を入力にしない。要約の要約は絶対にしない。

2. **関係性スナップショットの更新判定**: `os.path.getmtime()` でファイルの最終更新日を取得し、`relationship_update_interval_days`（デフォルト7日）と比較する。ファイルが存在しない場合は常に生成する。

3. **関係性スナップショットの入力**: mid_term_digest.txt（蓄積された事実ダイジェスト）を使用する。日々の断片ではなく「最近の全体像」からパターンを抽出するため。digestファイルが存在しない場合はスキップする。将来Stage3が実装された際には、さらに広い範囲の情報から生成する形に拡張可能。

4. **エラー耐性**: digestとrelationshipの生成は独立しており、一方が失敗しても他方には影響しない。いずれの失敗も既存パイプラインには影響を与えない。

5. **housekeeper.pyにgenaiのimport**: housekeeper.pyの先頭にgoogle.genaiのimportがない場合は追加すること。

---

## 完了の定義

- [ ] `stage_1_cleanup()` 末尾で `_generate_daily_digest()` と `_update_relationship_if_due()` が呼ばれる
- [ ] 入力は当日分のRAW（`new_text`）のみ
- [ ] `mid_term_digest.txt` がエピソード付きで差分追記される
- [ ] digest上限超過時に `archive_digest.txt` へアーカイブされる
- [ ] `mid_term_relationship.txt` が7日間隔で条件付き更新される（初回は即時生成）
- [ ] `mid_term.txt` が一切変更されていない
- [ ] `prompts.py` にプロンプト2つが追加されている
- [ ] `config.py` にモデル更新 + 要約設定 + 更新間隔が追加されている
- [ ] 要約生成エラー時に既存パイプラインが影響を受けない
- [ ] Web UI / スタンドアロン両方で正常に動作する

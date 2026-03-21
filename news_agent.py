"""
news_agent.py
-------------
エンタメ・スポーツ・一般ニュースを Gemini + Google検索グラウンディングで収集・要約する。
毎朝 cron から呼ばれ、結果を news_cache.json に保存する。

実行方法:
    python news_agent.py           # 通常実行
    python news_agent.py --dry-run # キャッシュの中身を確認するだけ
"""

import os
import json
import re
import argparse
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
from google import genai
from google.genai import types

# ===== 設定 =====
BASE_DIR = Path(__file__).resolve().parent
CACHE_PATH = BASE_DIR / "news_cache.json"
AGENT_NAME = "ジャービス"

# ===== ソース定義 =====
SOURCES = [
    {
        "section": "🎮 エンタメ・カルチャー",
        "items": [
            {
                "name": "GameWith",
                "query": "GameWith ゲームニュース 最新 今日 人気 3件",
            },
            {
                "name": "Famitsu",
                "query": "ファミ通 ゲームニュース 最新 今日 人気 3件",
            },
            {
                "name": "Game*Spark",
                "query": "Game*Spark ゲームニュース 最新 今日 人気 3件",
            },
            {
                "name": "X バズ・お得情報",
                "query": "Twitter X 日本 今日 バズ コンビニ 外食 お得 キャンペーン 割引 話題 3件",
            },
        ],
    },
    {
        "section": "⚽ スポーツ",
        "items": [
            {
                "name": "LoL Esports",
                "query": "League of Legends esports LCK LEC LCS 最新試合結果 ニュース 今日 3件",
            },
            {
                "name": "スポーツニュース（日本）",
                "query": "日本 スポーツニュース 急上昇 話題 今日 3件 サッカー 野球 バスケ",
            },
        ],
    },
    {
        "section": "🌏 一般ニュース",
        "items": [
            {
                "name": "NHK",
                "query": "NHK ニュース 今日 注目 トピック 3件",
            },
            {
                "name": "宮崎のニュース",
                "query": "宮崎県 宮崎市 ニュース 今日 最新 話題 地域 3件",
            },
        ],
    },
]

# ===== プロンプト =====

SOURCE_PROMPT = """\
以下のソースから、本日最も注目されているトピックを**3件**リサーチしてください。

ソース: {source_name}
検索指示: {query}

# 出力形式（JSONのみ、他の文章は不要）
[
  {{
    "title": "タイトル（30文字以内）",
    "summary": "要約（60〜80文字、日本語）"
  }}
]

# ルール
- 必ず3件出力する
- summary は「何が起きたか・なぜ面白いか」を簡潔に
- 今日または直近数日の情報に限定する
- JSONのみ出力。コードブロック（```）は不要
"""

COMMENT_PROMPT = """\
あなたは AI 執事「{agent_name}」です。
以下は本日のニュースまとめです。

{news_summary}

このニュースを読んだ {agent_name} として、短いコメントを**1〜2文**で書いてください。

# 観点
- 全体の中で特に気になったトピックへの軽いひとこと
- 宮崎在住・ゲーム好きのユーザー（主人）の視点に寄り添う
- {agent_name} らしい知的で親しみやすいトーン。軽めでOK

# ルール
- コメント本文のみ出力（タイトル・見出し不要）
- 150文字以内
"""


# ===== パース =====

def _parse_items_from_text(text: str) -> list[dict]:
    """
    Gemini の応答テキストから items を抽出する。
    2段階フォールバックで確実にパースする。
    """
    text = text.strip()

    # フォールバック 1: json.loads（コードブロック除去込み）
    try:
        candidate = re.sub(r"```(?:json)?", "", text).replace("```", "").strip()
        m = re.search(r"\[.*\]", candidate, re.DOTALL)
        if m:
            candidate = m.group(0)
        items = json.loads(candidate)
        result = []
        for item in items[:3]:
            if isinstance(item, dict) and "title" in item and "summary" in item:
                result.append({
                    "title": str(item["title"])[:60],
                    "summary": str(item["summary"])[:200],
                })
        if result:
            return result
    except Exception:
        pass

    # フォールバック 2: 正規表現で title/summary を1件ずつ拾う
    result = []
    block_pattern = re.compile(
        r'"title"\s*:\s*"(?P<title>.*?)"\s*,\s*"summary"\s*:\s*"(?P<summary>.*?)"'
        r'|"summary"\s*:\s*"(?P<summary2>.*?)"\s*,\s*"title"\s*:\s*"(?P<title2>.*?)"',
        re.DOTALL,
    )
    for m in block_pattern.finditer(text):
        title = (m.group("title") or m.group("title2") or "").replace("\\n", " ").strip()
        summary = (m.group("summary") or m.group("summary2") or "").replace("\\n", " ").strip()
        if title and summary:
            result.append({"title": title[:60], "summary": summary[:200]})
        if len(result) >= 3:
            break

    return result


# ===== 処理 =====

def load_api_key() -> str:
    for fname in [".env", "APIkey.env"]:
        p = BASE_DIR / fname
        if p.exists():
            load_dotenv(p, override=True)
    key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not key:
        raise ValueError("GOOGLE_API_KEY が見つかりません")
    return key


def fetch_source_items(client, model_name: str, source_name: str, query: str) -> list[dict]:
    """1ソースのトピック3件を Gemini + Google検索で取得する"""
    prompt = SOURCE_PROMPT.format(source_name=source_name, query=query)
    try:
        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[{"google_search": {}}],
                temperature=0.3,
                max_output_tokens=2048,  # 日本語3件分を確実に出力できる量
            ),
        )
        text = response.text.strip() if response.text else "[]"
        items = _parse_items_from_text(text)

        if items:
            return items

        print(f"[News] Parse failed for {source_name}. Raw snippet: {text[:200]}")
        return [{"title": "取得エラー", "summary": f"{source_name} の情報を取得できませんでした。"}]

    except Exception as e:
        print(f"[News] Error fetching {source_name}: {e}")
        return [{"title": "取得エラー", "summary": f"{source_name} の情報を取得できませんでした。"}]


def generate_comment(client, model_name: str, sections: list[dict]) -> str:
    """全ニュースを読んだ Jarvis からのコメントを生成する"""
    lines = []
    for section in sections:
        lines.append(section["section"])
        for source in section["sources"]:
            lines.append(f"  [{source['name']}]")
            for item in source["items"]:
                if item["title"] != "取得エラー":
                    lines.append(f"  - {item['title']}: {item['summary']}")
    news_summary = "\n".join(lines)

    prompt = COMMENT_PROMPT.format(agent_name=AGENT_NAME, news_summary=news_summary)
    try:
        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.8,
                max_output_tokens=256,
            ),
        )
        return response.text.strip() if response.text else ""
    except Exception as e:
        print(f"[News] Comment generation error: {e}")
        return ""


def run(dry_run: bool = False):
    if dry_run:
        if CACHE_PATH.exists():
            data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print("[News] キャッシュが存在しません。先に通常実行してください。")
        return

    print(f"[News] 開始: {datetime.now().isoformat()}")

    api_key = load_api_key()
    client = genai.Client(api_key=api_key)

    SEARCH_MODEL = "gemini-2.5-flash"
    COMMENT_MODEL = "gemini-3.1-flash-lite-preview"

    sections = []
    for section_def in SOURCES:
        section_data = {"section": section_def["section"], "sources": []}
        for source_def in section_def["items"]:
            print(f"[News] 取得中: {source_def['name']} ...")
            items = fetch_source_items(
                client, SEARCH_MODEL, source_def["name"], source_def["query"]
            )
            section_data["sources"].append({"name": source_def["name"], "items": items})
        sections.append(section_data)

    print("[News] Jarvis コメント生成中...")
    comment = generate_comment(client, COMMENT_MODEL, sections)

    cache = {
        "updated_at": datetime.now().isoformat(),
        "sections": sections,
        "jarvis_comment": comment,
    }
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[News] 完了。保存先: {CACHE_PATH}")
    print(f"[News] Jarvis コメント: {comment[:80]}...")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="キャッシュの中身を表示するだけ")
    args = parser.parse_args()
    run(dry_run=args.dry_run)

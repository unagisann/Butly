"""
json_extract.py
---------------
LLM 応答テキストから JSON 文字列を取り出す共通ヘルパー。
ContextClassifier / StateUpdater / Stage 3 (knowledge_maturation) で共用する。
"""

import re


def extract_json_str(raw_text: str) -> str:
    """LLM 応答から JSON オブジェクト部分の文字列を抽出する（パースは呼び出し側の責務）。

    コードフェンスは閉じ ``` が無いケース（トークン切れ・省略）も許容。
    開きフェンス内または地の文いずれでも、最初の { 〜 最後の } を抽出する。
    JSON らしき部分が見つからなければ strip 済みの原文を返す。
    """
    return _extract_span(raw_text, "{", "}")


def extract_json_array(raw_text: str) -> str:
    """LLM 応答から JSON 配列部分の文字列を抽出する（パースは呼び出し側の責務）。

    フェンス耐性は extract_json_str と同じ。オブジェクトに包まれた配列
    （{"cards": [...]} 等）も最初の [ 〜 最後の ] で配列側を拾う。
    """
    return _extract_span(raw_text, "[", "]")


def _extract_span(raw_text: str, open_char: str, close_char: str) -> str:
    if not raw_text:
        return ""
    match = re.search(r"```(?:json)?\s*(.*?)```", raw_text, re.DOTALL)
    json_str = match.group(1).strip() if match else raw_text.strip()
    if open_char in json_str and close_char in json_str:
        json_str = json_str[
            json_str.find(open_char) : json_str.rfind(close_char) + 1
        ]
    return json_str

"""
reply_profiles.py
-----------------
外部入口ごとの返答スタイル・文字数制約（reply profile）。

reply profile は 2 つの役割を持つ:
  1. 生成前: ``to_metadata()`` が ``ChatRequest.metadata`` に載せる dict を返す。
     ChatService はそのうち ``style_hint`` を full_prompt に注入する（記憶本文は不変）。
  2. 送信前: ``soft_char_limit`` / ``hard_char_limit`` を message_splitter に渡して
     プラットフォームの上限に収める。

discord.py 等の SDK には依存しない（純粋データ）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


@dataclass(frozen=True)
class ReplyProfile:
    """返信スタイル・文字数制約の定義。

    Attributes
    ----------
    name : str
        プロファイル識別子（metadata["reply_profile"] に入る）。
    style_hint : str
        生成時にモデルへ渡す文体ヒント。会話記憶本文には混ぜない。
    soft_char_limit : int
        モデルへの目安上限（ヒント止まり。強制はしない）。
    hard_char_limit : int
        送信前分割の上限。これを超えたら複数メッセージに分割する。
    """

    name: str
    style_hint: str
    soft_char_limit: int
    hard_char_limit: int

    def to_metadata(self) -> Dict[str, Any]:
        """ChatRequest.metadata に載せる dict を返す。

        ChatService が現状参照するのは ``style_hint`` のみ。残りは将来拡張・
        デバッグ用の付帯情報として保持する。
        """
        return {
            "reply_profile": self.name,
            "style_hint": self.style_hint,
            "soft_char_limit": self.soft_char_limit,
            "hard_char_limit": self.hard_char_limit,
        }


# Discord 用プロファイル。
# - Discord の hard limit は 2000 文字。余白を見て 1900 を上限にする。
# - soft limit は 1400 程度（モデルへの目安）。
DISCORD_PROFILE = ReplyProfile(
    name="discord",
    style_hint=(
        "Discord向けに、通常よりやや簡潔に、数段落以内で返してください。"
        "可能なら1400文字以内、長くても1900文字を超えないようにしてください。"
    ),
    soft_char_limit=1400,
    hard_char_limit=1900,
)

# LINE 用プロファイル。
# - LINE の text 上限は 5000 UTF-16 code units。初期版は安全余白を取り 4500。
# - reply は最大 5 メッセージ。超過分は adapter が push に回す。
LINE_PROFILE = ReplyProfile(
    name="line",
    style_hint=(
        "LINE向けに、読みやすく簡潔に返してください。"
        "可能なら1600文字以内、長くても4500文字を超えないようにしてください。"
    ),
    soft_char_limit=1600,
    hard_char_limit=4500,
)


# name -> ReplyProfile のレジストリ。
_PROFILES: Dict[str, ReplyProfile] = {
    DISCORD_PROFILE.name: DISCORD_PROFILE,
    LINE_PROFILE.name: LINE_PROFILE,
}


def get_profile(name: str) -> ReplyProfile:
    """名前から ReplyProfile を取得する。未知の場合は KeyError。"""
    return _PROFILES[name]

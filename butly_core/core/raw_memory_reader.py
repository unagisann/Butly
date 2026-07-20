"""
raw_memory_reader.py
---------------------
RawMemoryReader: 2_knowledgeized/ の JSON を時系列で収集し、
末尾からトークン数上限までファイル単位で収集。指定形式に変換して
raw_memory_cache.txt に書き出す。チャット時はキャッシュを1ファイル読むだけ。
"""

import json
import re
from pathlib import Path

from butly_core.core.tokenizer import count_tokens
from butly_core.core.turn_meta import has_multiple_speakers, speaker_label, user_label

# ファイル名からタイムスタンプを抽出する正規表現
_TS_PATTERN_8 = re.compile(
    r"session_(\d{8}_\d{6}(?:_\d{6})?)"
)  # session_YYYYMMDD_HHMMSS[_ffffff]
_TS_PATTERN_6D = re.compile(
    r"session_(\d{6}_\d{6}(?:_\d{6})?)"
)  # session_YYMMDD_HHMMSS[_ffffff]
_TS_PATTERN_TIME = re.compile(r"session_(\d{6})\.json$")  # session_HHMMSS.json
_DIR_DATE_PATTERN = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")  # YYYY-MM-DD

CACHE_FILENAME = "raw_memory_cache.txt"


def _extract_sort_key(filepath: Path) -> str:
    """ファイルパスから YYYYMMDD_HHMMSS 形式のソートキーを生成。

    3 つのファイル名形式に対応:
      - session_YYYYMMDD_HHMMSS[_ffffff].json  → そのまま
      - session_YYMMDD_HHMMSS[_ffffff].json    → 20 を前置
      - session_HHMMSS.json           → 親ディレクトリ名から日付を補完
    """
    m = _TS_PATTERN_8.search(filepath.name)
    if m:
        return m.group(1)
    m = _TS_PATTERN_6D.search(filepath.name)
    if m:
        return "20" + m.group(1)
    # 時刻のみの場合 → 親ディレクトリ名 (YYYY-MM-DD) から日付を取得
    m_time = _TS_PATTERN_TIME.search(filepath.name)
    if m_time:
        m_dir = _DIR_DATE_PATTERN.search(filepath.parent.name)
        if m_dir:
            return f"{m_dir.group(1)}{m_dir.group(2)}{m_dir.group(3)}_{m_time.group(1)}"
    return filepath.name


def _format_timestamp(raw_ts: str) -> str:
    """ISO タイムスタンプを 'YYYY-MM-DD HH:MM:SS' に正規化。"""
    return raw_ts.split(".")[0].replace("T", " ")


def collect_raw_sessions(
    instance_path: Path,
    max_tokens: int = 4096,
) -> list[dict]:
    """
    2_knowledgeized/ から JSON を時系列順に収集し、
    末尾からトークン数上限まで返す。

    Returns:
        [{"timestamp": str, "messages": list, "filename": str}, ...]
        時系列順（古い→新しい）
    """
    archive_root = instance_path / "memory_archive"
    knowledgeized_dir = archive_root / "2_knowledgeized"

    all_files: list[Path] = []

    # 2_knowledgeized/ 配下の全日付フォルダを走査
    if knowledgeized_dir.exists():
        for date_dir in knowledgeized_dir.iterdir():
            if date_dir.is_dir() and not date_dir.name.startswith("processed_"):
                all_files.extend(date_dir.glob("*.json"))

    if not all_files:
        return []

    # ファイル名のタイムスタンプでソート
    all_files.sort(key=_extract_sort_key)

    # 末尾（最新）から逆順にファイルを読み、累計トークン数が max_tokens を超える直前まで収集
    collected: list[dict] = []
    total_tokens = 0

    for filepath in reversed(all_files):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        messages = data.get("messages", [])
        if not messages:
            continue

        # メッセージのテキストを結合してトークン数を計算
        text_parts = []
        for msg in messages:
            parts = msg.get("parts", [])
            for part in parts:
                if isinstance(part, str):
                    text_parts.append(part)
                elif isinstance(part, dict):
                    text_parts.append(part.get("text", ""))
        session_text = "\n".join(text_parts)
        session_tokens = count_tokens(session_text)

        if total_tokens + session_tokens > max_tokens and collected:
            break  # 上限超過 → ここまでで打ち切り

        total_tokens += session_tokens
        collected.append(
            {
                "timestamp": _format_timestamp(data.get("timestamp", "")),
                "messages": messages,
                "filename": filepath.name,
            }
        )

        if total_tokens >= max_tokens:
            break  # ちょうど上限到達でも終了

    # 時系列順（古い→新しい）に戻す
    collected.reverse()
    return collected


def format_sessions(
    sessions: list[dict],
    format: str = "plaintext",
    agent_name: str = "Agent",
    user_name: str = "User",
    locale: str = "ja",
) -> str:
    """収集済みセッションを指定形式のテキストに変換。

    Args:
        sessions: collect_raw_sessions() の戻り値
        format: "markdown" | "plaintext" | "compact"
        agent_name: エージェント表示名
        user_name: ユーザー表示名
        locale: 話者ラベルの locale

    Returns:
        変換済みテキスト
    """
    if not sessions:
        return ""

    # キャッシュ全体で複数話者かを判定し、複数話者のときだけ user 発言を
    # meta の 「display_name」 でラベリングする (1:1 は従来どおり)。
    multi_speaker = has_multiple_speakers(
        [m for s in sessions for m in s.get("messages", [])]
    )

    def _user_role(msg: dict) -> str:
        if locale != "ja":
            return speaker_label(msg, user_name)
        return user_label(msg, user_name, multi_speaker=multi_speaker)

    lines: list[str] = []

    for session in sessions:
        ts = session.get("timestamp", "")
        messages = session.get("messages", [])

        if format == "markdown":
            if ts:
                lines.append(f"### {ts}")
                lines.append("")
            for msg in messages:
                role = _user_role(msg) if msg.get("role") == "user" else agent_name
                content = _extract_content(msg)
                lines.append(f"**{role}**: {content}")
                lines.append("")

        elif format == "compact":
            for msg in messages:
                if msg.get("role") == "user":
                    prefix = _user_role(msg) if multi_speaker else "U"
                else:
                    prefix = "A"
                content = _extract_content(msg)
                lines.append(f"{prefix}: {content}")

        else:  # plaintext (default)
            for msg in messages:
                role = _user_role(msg) if msg.get("role") == "user" else agent_name
                content = _extract_content(msg)
                lines.append(f"[{ts}] {role}: {content}")

    return "\n".join(lines)


def _extract_content(msg: dict) -> str:
    """メッセージから content テキストを抽出。"""
    parts = msg.get("parts", [])
    if not parts:
        return ""
    first = parts[0]
    if isinstance(first, str):
        return first
    if isinstance(first, dict):
        return first.get("text", "")
    return ""


def build_raw_memory_cache(
    instance_path: Path,
    max_tokens: int = 4096,
    injection_format: str = "plaintext",
    agent_name: str = "Agent",
    user_name: str = "User",
    locale: str = "ja",
) -> dict:
    """2_knowledgeized/ の JSON からキャッシュファイルを生成する。

    Sleeptime 実行時や手動再生成時に呼ばれる。
    毎回全体を再構築する（インクリメンタル追記ではない）。

    Args:
        instance_path: インスタンスディレクトリのパス
        max_tokens: RAW 読み込みトークン上限
        injection_format: "markdown" | "plaintext" | "compact"
        agent_name: エージェント表示名
        user_name: ユーザー表示名
        locale: 話者ラベルの locale

    Returns:
        {"sessions": int, "tokens": int, "format": str, "path": str}
    """
    sessions = collect_raw_sessions(instance_path, max_tokens=max_tokens)
    text = format_sessions(
        sessions,
        format=injection_format,
        agent_name=agent_name,
        user_name=user_name,
        locale=locale,
    )

    cache_path = instance_path / CACHE_FILENAME
    cache_path.write_text(text, encoding="utf-8")

    tokens = count_tokens(text) if text else 0
    print(
        f"[RawMemoryReader] Cache written: {len(sessions)} sessions, {tokens} tokens, format={injection_format}"
    )

    return {
        "sessions": len(sessions),
        "tokens": tokens,
        "format": injection_format,
        "path": str(cache_path),
    }

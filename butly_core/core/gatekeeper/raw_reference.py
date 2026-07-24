"""
raw_reference.py
----------------
RAG 候補カードの source_files から、カード生成時に使った当時の RAW 会話ログ
（memory_archive 配下の JSON）を逆引きし、プロンプト注入用の抜粋テキストを
構築する（parent-document retrieval）。

カードは非可逆圧縮であり抽出漏れが起こり得るため、
「カード = 検索インデックス、事実の根拠 = 原文」という役割分担で原文を併記する。
注入の有無は memory.rag_source_mode（"cards" | "raw" | "both"）で制御し、
RAW ファイルの読み込みは raw を要求するモードのときだけ行う（遅延解決）。

RAW ファイルの所在（sleeptime Stage 2 の移動規則と対応）:
  instances/<inst>/memory_archive/2_knowledgeized/<source_date>/<fname>  … 処理済み
  instances/<inst>/memory_archive/1_integrated/<fname>                   … 未処理
"""

import json
from pathlib import Path

from butly_core.core import turn_meta


def collect_source_refs(
    candidates: list, default_instance: str, top_k: int | None = None
) -> list:
    """候補カード（スコア順）から (instance, source_date, file_name) を集める。

    順序はカードのスコア順を保つ（文字数上限で切るとき上位カードの原文が残る）。
    同一 instance の同名ファイルは dedup する（同一チャンク由来のカードは
    source_files が丸ごと重複するため）。source_files は DB 上 JSON 文字列
    （list[str]）。欠落・破損はスキップする。

    top_k を指定すると、RAW を展開するカードを「新規ファイルを供給した上位
    top_k 枚」に絞る（source_files を持たない旧カードや、上位と同一チャンクの
    重複カードはスロットを消費しない＝スコア順で実質的に raw を持つ上位 top_k
    枚を採る）。None / 0 以下は全件。カード=検索インデックス／根拠=原文の構成で、
    最上位の根拠だけに原文を絞りサマリの希釈を避ける用途に使う。
    """
    refs = []
    seen = set()
    cards_used = 0
    for c in candidates:
        raw = c.get("source_files")
        if not raw:
            continue
        if isinstance(raw, str):
            try:
                names = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue
        elif isinstance(raw, list):
            names = raw
        else:
            continue
        if not isinstance(names, list):
            continue
        if top_k is not None and top_k > 0 and cards_used >= top_k:
            break
        inst = c.get("source_instance") or default_instance
        date = c.get("source_date") or ""
        added_any = False
        for name in names:
            if not isinstance(name, str) or not name:
                continue
            key = (inst, name)
            if key in seen:
                continue
            seen.add(key)
            refs.append((inst, date, name))
            added_any = True
        if added_any:
            cards_used += 1
    return refs


def _is_safe_component(value: str) -> bool:
    """パス部品として安全か（DB 由来の値をそのまま結合するため区切り文字を拒否）。"""
    return bool(value) and not (
        "/" in value or "\\" in value or ".." in value or value.startswith("~")
    )


def _find_raw_file(instances_dir: Path, inst: str, date: str, name: str):
    if not _is_safe_component(name) or not _is_safe_component(inst):
        return None
    archive = instances_dir / inst / "memory_archive"
    if date and _is_safe_component(date):
        path = archive / "2_knowledgeized" / date / name
        if path.exists():
            return path
    path = archive / "1_integrated" / name
    if path.exists():
        return path
    return None


def _render_file(
    data: dict,
    user_name: str,
    agent_name: str,
    multi_speaker: bool,
    locale: str,
) -> str:
    ts = str(data.get("timestamp", "Unknown")).replace("T", " ").split(".")[0]
    lines = [f"--- {ts} ---"]
    for msg in data.get("messages", []):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "user":
            if locale == "ja":
                label = turn_meta.user_label(
                    msg, user_name, multi_speaker=multi_speaker
                )
            else:
                label = turn_meta.speaker_label(msg, user_name)
        else:
            label = agent_name
        text = turn_meta.message_text(msg)
        if text:
            lines.append(f"{label}: {text}")
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


def resolve_raw_reference(
    candidates: list,
    instances_dir,
    default_instance: str,
    max_chars: int,
    user_name: str = "User",
    agent_name: str = "AI",
    locale: str = "ja",
    top_k: int | None = None,
):
    """候補カード群に対応する RAW 会話原文の抜粋を構築する。

    Parameters
    ----------
    candidates : list
        RAG 候補カード（source_files / source_date / source_instance を含み得る）。
    instances_dir : Path | str
        instances ルートディレクトリ（ButlyBrain.instances_dir）。
    default_instance : str
        source_instance が無い候補に使うインスタンス名。
    max_chars : int
        抜粋合計の文字数上限。0 以下は無制限。超過ファイルは greedy skip
        （glossary の上限適用と同じ規則）。1 件も入らない場合のみ先頭を切り詰める。
    user_name / agent_name : str
        整形時の話者ラベル。複数話者ログは turn_meta の帰属規則に従う。
    top_k : int | None
        RAW を展開する上位カード数。None / 0 以下は全件（collect_source_refs 参照）。

    Returns
    -------
    dict | None
        {"text": str, "files": list[str], "missing": list[str],
         "truncated": bool, "chars": int, "top_k": int|None}
        参照可能な RAW が 1 件も無ければ None。
    """
    instances_dir = Path(instances_dir)
    refs = collect_source_refs(candidates, default_instance, top_k=top_k)
    if not refs:
        return None

    loaded = []  # [(date, name, data)] スコア順
    missing = []
    for inst, date, name in refs:
        path = _find_raw_file(instances_dir, inst, date, name)
        if path is None:
            missing.append(name)
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError, UnicodeDecodeError):
            missing.append(name)
            continue
        if isinstance(data, dict):
            loaded.append((date, name, data))
        else:
            missing.append(name)
    if not loaded:
        return None

    all_msgs = [m for _, _, d in loaded for m in d.get("messages", [])]
    multi_speaker = turn_meta.has_multiple_speakers(all_msgs)

    included = []  # [(date, name, text)]
    total = 0
    truncated = False
    for date, name, data in loaded:
        text = _render_file(
            data,
            user_name,
            agent_name,
            multi_speaker,
            locale,
        )
        if not text:
            continue
        if max_chars > 0 and total + len(text) > max_chars:
            if not included:
                suffix = (
                    "\n…（文字数上限で省略）"
                    if locale == "ja"
                    else "\n… (truncated at the character limit)"
                )
                text = text[:max_chars] + suffix
                included.append((date, name, text))
                total += len(text)
            truncated = True
            continue
        included.append((date, name, text))
        total += len(text)

    if not included:
        return None

    # 表示は時系列順（source_date → ファイル名。ファイル名はタイムスタンプ由来）
    included.sort(key=lambda item: (item[0], item[1]))
    body = "\n\n".join(text for _, _, text in included)
    return {
        "text": body,
        "files": [name for _, name, _ in included],
        "missing": missing,
        "truncated": truncated,
        "chars": len(body),
        "top_k": top_k,
    }

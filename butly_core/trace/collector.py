"""
collector.py
------------
1 ターン中の LLM 呼び出しを ContextVar 経由で収集する軽量コレクター (issue #51)。

ChatService がターン開始時に ``start_collection()`` を呼び、ターン終了時に
try/finally で ``reset_collection(token)`` する。補助 LLM の呼び出し元
(ContextClassifier / StateUpdater / Brain 等) は ``record_llm_call()`` を
1 行呼ぶだけでよい。収集が開始されていない文脈 (sleeptime、単体テスト等)
では record は no-op になり、副作用を持たない。

並列実行との相性:
    ContextVar には可変 list を入れる。``run_in_threadpool`` (anyio) や
    ``asyncio.create_task`` は context を **コピー** するが、コピーされるのは
    ContextVar → 値 のマッピングであり list オブジェクト自体は共有される。
    そのため並列タスク / worker thread からの append も同じ list に届く
    (list.append は GIL 下で atomic)。
"""

import time
from contextvars import ContextVar, Token
from typing import Any, Dict, List, Optional

_llm_calls: ContextVar[Optional[List[Dict[str, Any]]]] = ContextVar(
    "butly_trace_llm_calls", default=None
)


def start_collection() -> Token:
    """収集を開始し、reset 用の Token を返す。

    呼び出し側は必ず try/finally で ``reset_collection(token)`` すること。
    ``execute_stream`` のような async generator では、generator 全体を包む
    位置 (最初の処理の前 / finally) に置く。
    """
    return _llm_calls.set([])


def reset_collection(token: Token) -> None:
    """収集を終了する。

    async generator が別 context で close された場合など、Token が現在の
    context と一致しないことがある。収集はターン単位の使い捨てなので、
    その場合はログだけ残して無視する。
    """
    try:
        _llm_calls.reset(token)
    except ValueError as e:
        print(f"[TraceCollector] reset をスキップ (context 不一致、無害): {e}")


def is_collecting() -> bool:
    """現在の context で収集中かを返す。"""
    return _llm_calls.get() is not None


def get_collected() -> List[Dict[str, Any]]:
    """収集済みの LLM 呼び出し記録のコピーを返す (未開始なら空リスト)。"""
    calls = _llm_calls.get()
    return list(calls) if calls is not None else []


def usage_metadata(provider) -> Optional[Dict[str, Any]]:
    """provider の直近 API usage を record_llm_call の metadata 形式で返す。

    呼び出し直後に使う（BaseProvider の 1 スロット方式に対応）。
    usage が取れないときは None（metadata 自体を付けない）。
    """
    pop = getattr(provider, "pop_last_token_usage", None)
    usage = pop() if callable(pop) else None
    # モック等の非 dict 汚染に耐える（実 provider は dict か None を返す）
    if not isinstance(usage, dict) or not usage:
        return None
    return {"token_usage": usage}


def aggregate_token_usage(
    calls: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """記録済み呼び出し全体の token usage を合算する。

    Returns
    -------
    dict | None
        {"prompt_tokens", "completion_tokens", "calls",
         "by_purpose": {purpose: {...}}}。usage が 1 件も無ければ None。
    """
    total_prompt = 0
    total_completion = 0
    total_calls = 0
    by_purpose: Dict[str, Dict[str, int]] = {}
    for call in calls:
        usage = (call.get("metadata") or {}).get("token_usage")
        if not usage:
            continue
        prompt = usage.get("prompt_tokens") or 0
        completion = usage.get("completion_tokens") or 0
        total_prompt += prompt
        total_completion += completion
        total_calls += 1
        bucket = by_purpose.setdefault(
            call.get("purpose") or "unknown",
            {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0},
        )
        bucket["prompt_tokens"] += prompt
        bucket["completion_tokens"] += completion
        bucket["calls"] += 1
    if total_calls == 0:
        return None
    return {
        "prompt_tokens": total_prompt,
        "completion_tokens": total_completion,
        "calls": total_calls,
        "by_purpose": by_purpose,
    }


def record_llm_call(
    *,
    purpose: str,
    model: str = "",
    connection_id: str = "",
    duration_ms: Optional[int] = None,
    prompt_chars: Optional[int] = None,
    error: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """LLM 呼び出し 1 件を記録する。収集未開始なら no-op。

    Parameters
    ----------
    purpose : str
        呼び出しの目的。trace のノード分類・表示フィルタのキーになる。
        既知の値: "context_classifier" / "state_updater" / "embedding" /
        "keyword_extract" / "chat_generate"。
    """
    calls = _llm_calls.get()
    if calls is None:
        return
    entry: Dict[str, Any] = {
        "purpose": purpose,
        "model": model,
        "connection_id": connection_id,
        "duration_ms": duration_ms,
        "prompt_chars": prompt_chars,
        "error": error,
        "recorded_at": time.time(),
    }
    if metadata:
        entry["metadata"] = metadata
    calls.append(entry)

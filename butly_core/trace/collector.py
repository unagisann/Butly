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

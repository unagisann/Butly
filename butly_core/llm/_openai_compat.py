"""
_openai_compat.py
-----------------
OpenAI 互換プロバイダー（OpenAI / Ollama / xAI / 将来の Groq 等）で
共通利用するヘルパー関数群。継承ではなく import して使う設計。

Phase 1 (v3.1): 共通化 + reasoning_effort 対応
Phase 5 (#43): async_chat_completion_stream 追加（SSE 用 stream 共通実装）
"""

import asyncio
import logging
import os
import threading
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional

from dotenv import load_dotenv

from butly_core.chat.types import Attachment, ChatResponse

logger = logging.getLogger(__name__)

# =====================================================================
# 環境変数ロード (M4 対応: Provider 間共通)
# =====================================================================


def _find_env_path() -> Optional[str]:
    """リポジトリルートの .env を探す。

    読み込み先の統一方針:
      1. main.py の DATA_DIR/.env（routers/settings.py が書き込む先）
      2. repo root の .env

    実行時の DATA_DIR は main.py で決まり、dev 環境ではリポジトリルートと同じ。
    Provider ファイルからの相対パスでルートを推定し、同じ場所を探す。
    """
    # _openai_compat.py は butly_core/llm/ にあるので parents[2] = repo root
    repo_root = Path(__file__).resolve().parents[2]
    env_path = repo_root / ".env"
    return str(env_path) if env_path.exists() else None


def load_env_file():
    """API キー用の .env をロードする。

    各 Provider の _get_client() から呼び出す。
    既に os.environ に値がある場合は上書きしない (load_dotenv のデフォルト挙動)。
    """
    path = _find_env_path()
    if path:
        load_dotenv(path)


# =====================================================================
# Reasoning モデル判定 (B1 対応)
# =====================================================================


def is_reasoning_model(model_name: str) -> bool:
    """OpenAI の o1/o3/o4 系 (reasoning モデル) かを判定する。

    このモデル群は:
      - temperature パラメータを受け付けない (固定値 1)
      - max_tokens ではなく max_completion_tokens を使う
      - reasoning_effort パラメータを受け付ける ("low" / "medium" / "high")

    gpt-4o / gpt-4o-mini は "gpt-" 始まりなので誤判定しない。
    grok-4 等も "grok-" 始まりなので誤判定しない。
    """
    return model_name.startswith(("o1", "o3", "o4"))


# =====================================================================
# context 解決
# =====================================================================


def resolve_position(context: Dict[str, Any]) -> str:
    """system_instruction の配置位置を解決する。

    優先順位:
      1. context["context_levels"]["system_instruction_position"]
      2. context["context_order"]["system_instruction_position"]  (死に枝: 将来削除検討)
      3. "top" (デフォルト)
    """
    context_levels = context.get("context_levels")
    if context_levels:
        return context_levels.get("system_instruction_position", "top")
    context_order = context.get("context_order")
    return (context_order or {}).get("system_instruction_position", "top")


def resolve_system_instruction(context: Dict[str, Any]) -> str:
    """context から system_instruction を構築する。

    既存の openai.py / ollama.py の _build_system_instruction() と同じ呼び出しを行う。
    override_config は引数に取らない (現状どちらの Provider でも未使用)。
    """
    from butly_core.core.gatekeeper import build_system_instruction_from_blocks

    return build_system_instruction_from_blocks(
        blocks=context.get("memory_blocks"),
        memory_manager=context.get("memory_manager"),
        use_google_search=False,
        context_order=context.get("context_order"),
        context_levels=context.get("context_levels"),
    )


def resolve_context_prefix(context: Dict[str, Any]) -> str:
    """context から context_prefix を構築する。"""
    from butly_core.core.gatekeeper import build_context_prefix

    return build_context_prefix(
        blocks=context.get("memory_blocks"),
        memory_manager=context.get("memory_manager"),
        use_google_search=False,
        context_order=context.get("context_order"),
        context_levels=context.get("context_levels"),
    )


# =====================================================================
# メッセージ構築
# =====================================================================


def build_user_content(text: str, attachments: List[Attachment]):
    """テキスト + 画像 → OpenAI 形式の content に変換する。

    attachments がなければ str を返す。
    あれば list[dict] (multimodal) を返す。
    """
    if not attachments:
        return text

    content = [{"type": "text", "text": text}]
    for att in attachments:
        data_url = f"data:{att.mime_type};base64,{att.data_base64}"
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": data_url},
            }
        )
    return content


def convert_history(history: list) -> list:
    """Butly の history 形式を OpenAI messages 形式に変換する。

    Gemini 形式の ``role: "model"`` を OpenAI 互換の ``"assistant"`` に変換する。
    """
    _ROLE_MAP = {"model": "assistant"}
    result = []
    for h in history:
        role = h.get("role", "user")
        role = _ROLE_MAP.get(role, role)
        parts = h.get("parts", [])
        content = parts[0] if parts else ""
        result.append({"role": role, "content": str(content)})
    return result


def build_messages(
    system_instruction: str,
    context_prefix: str,
    history: list,
    user_content,
    position: str = "top",
) -> list:
    """OpenAI 互換の messages 配列を構築する。

    position="top": sys_inst → prefix → 履歴 → ユーザー入力
    position="bottom": prefix → 履歴 → sys_inst → ユーザー入力
    """
    converted = convert_history(history)

    if position == "bottom":
        messages = []
        if context_prefix:
            messages.append({"role": "system", "content": context_prefix})
        messages.extend(converted)
        messages.append({"role": "system", "content": system_instruction})
        messages.append({"role": "user", "content": user_content})
    else:
        messages = [{"role": "system", "content": system_instruction}]
        if context_prefix:
            messages.append({"role": "user", "content": context_prefix})
        messages.extend(converted)
        messages.append({"role": "user", "content": user_content})

    return messages


# =====================================================================
# デバッグ
# =====================================================================


def _debug_content(c, max_len: int = 500) -> str:
    """content が str / list(multimodal) のどちらでも安全に truncate する。"""
    if isinstance(c, str):
        return (c[:max_len] + "...") if len(c) > max_len else c
    if isinstance(c, list):
        texts = [
            p.get("text", "")
            for p in c
            if isinstance(p, dict) and p.get("type") == "text"
        ]
        joined = " ".join(texts)
        return (joined[:max_len] + "...") if len(joined) > max_len else joined
    return str(c)[:max_len]


def build_debug_messages(messages: list, max_len: int = 500) -> list:
    """debug_info 用に messages を truncate する。"""
    return [
        {"role": m["role"], "content": _debug_content(m.get("content", ""), max_len)}
        for m in messages
    ]


def _full_text_content(c) -> str:
    """content が str / list(multimodal) のどちらでも全文テキストを返す（画像 part は除外）。"""
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(
            p.get("text", "")
            for p in c
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return str(c)


def build_full_messages(messages: list) -> list:
    """debug_info 用の全文 messages（テキストのみ）。

    prompt_full の表示とトークン概算のフォールバックに使う。画像 part は
    base64 が巨大になるため含めない。
    """
    return [
        {"role": m["role"], "content": _full_text_content(m.get("content", ""))}
        for m in messages
    ]


def extract_token_usage(resp) -> Optional[dict]:
    """API レスポンスの usage を dict 化する（取れなければ None）。

    OpenAI 互換 API (llama.cpp サーバー含む) は非ストリーム応答に
    usage.prompt_tokens / completion_tokens を返す。ストリーミングは
    stream_options={"include_usage": True} の指定が必要なため未対応。
    """
    usage = getattr(resp, "usage", None)
    if usage is None:
        return None

    def _as_int(name: str):
        # 実数値のみ受け付ける（MagicMock 等は int() 可能なため型で判定する）
        value = getattr(usage, name, None)
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        return None

    prompt = _as_int("prompt_tokens")
    completion = _as_int("completion_tokens")
    if prompt is None and completion is None:
        return None
    out = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "source": "api",
    }
    total = _as_int("total_tokens")
    if total is not None:
        out["total_tokens"] = total
    return out


# =====================================================================
# 設定マージ
# =====================================================================


def merge_chat_config(base_conf: dict, override_config: Optional[dict]) -> dict:
    """AI_CONFIG["chat"] と override_config["chat"] をマージする。"""
    if override_config and "chat" in override_config:
        return {**base_conf, **override_config["chat"]}
    return base_conf


# =====================================================================
# API 呼び出しパラメータ構築 (B1 対応: reasoning / 通常の 2 系統分岐)
# =====================================================================


def build_chat_completion_kwargs(
    chat_conf: dict,
    messages: list,
    model_name: Optional[str] = None,
) -> dict:
    """chat.completions.create() の kwargs を構築する。

    reasoning モデル (o1/o3/o4 系) の場合:
      - temperature / top_p / max_tokens を送らない
      - max_completion_tokens を送る
      - reasoning_effort を送る (デフォルト "medium")

    通常モデルの場合:
      - temperature / max_tokens を従来通り送る
    """
    _gen = chat_conf.get("generation_config", {})
    _model = model_name or chat_conf.get("model_name", "")

    kwargs = {"messages": messages}

    if is_reasoning_model(_model):
        # reasoning モデル: temperature 禁止、max_completion_tokens 使用
        max_tokens = _gen.get("max_output_tokens") or 8192
        kwargs["max_completion_tokens"] = max_tokens
        kwargs["reasoning_effort"] = chat_conf.get("reasoning_effort", "medium")
    else:
        # 通常モデル
        kwargs["temperature"] = _gen.get("temperature", 0.7)
        max_tokens = _gen.get("max_output_tokens")
        if max_tokens:
            kwargs["max_tokens"] = max_tokens

    # Judge等の限定された呼び出しだけが設定するOpenAI互換の構造化出力。
    # 通常チャットはresponse_formatを持たないため従来挙動のまま。
    response_format = chat_conf.get("response_format")
    if response_format is not None:
        if not isinstance(response_format, dict):
            raise ValueError("response_format must be a mapping")
        kwargs["response_format"] = response_format

    return kwargs


# =====================================================================
# ChatResponse 組み立て
# =====================================================================


def build_chat_response(
    response_text: str,
    rag_results: list,
    web_sources: Optional[list] = None,
) -> ChatResponse:
    """ChatResponse を組み立てる。refs には rag_results、sources には web_sources。"""
    result = ChatResponse(
        text=response_text or "",
        refs=[dict(k) for k in rag_results] if rag_results else [],
    )
    if web_sources:
        result.sources = web_sources
    return result


# =====================================================================
# ストリーミング (OpenAI 互換 SDK 共通)
# =====================================================================

# 最初の 1 回 + 即時リトライ 1 回。上流が空応答を返す事象が実際に起きており、
# 同じ入力の手動再送で成功している（transient）。
MAX_STREAM_ATTEMPTS = 2


def is_retryable_stream_error(error: Exception) -> bool:
    """chunk が 1 つも来ないまま**即座に**失敗した種類か。

    再試行してよいのは「待ち時間をほとんど増やさず、成功する見込みがある」もの
    だけに限る。

    - 対象: 上流の空応答、接続断、5xx
    - 対象外: **timeout**（再試行すると待ちが倍になる。体感を最も損なう）
    - 対象外: **429 / 認証 / 不正リクエスト**（間を置かない再試行が無意味か有害）

    呼び出し側で「まだ 1 文字も送っていない」ことを確認しているので、
    二重生成にはならない。
    """
    try:
        import openai
    except ImportError:  # openai 未導入の互換プロバイダー経路
        return False

    # timeout は APIConnectionError の派生なので、先に弾く
    if isinstance(error, openai.APITimeoutError):
        return False
    if isinstance(error, openai.APIConnectionError):
        return True
    if isinstance(error, openai.InternalServerError):
        return True
    if isinstance(error, openai.APIStatusError):
        # 429 / 4xx はここで落とす（status を持つ失敗は上流の意思表示）
        return False
    # status を持たない素の APIError。上流が本文なしでストリームを閉じた場合が該当。
    return isinstance(error, openai.APIError)


async def async_chat_completion_stream(
    client,
    model: str,
    messages: list,
    chat_conf: dict,
    debug_data: Optional[dict] = None,
    web_sources: Optional[list] = None,
    log_tag: str = "OpenAICompat",
    request_kwargs: Optional[dict[str, Any]] = None,
    parameter_error_handler: Optional[
        Callable[[Exception, dict[str, Any]], Optional[dict[str, Any]]]
    ] = None,
    on_success: Optional[Callable[[], None]] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    OpenAI 互換 SDK (`client.chat.completions.create(stream=True)`) を使い、
    Gemini と同じ event 形式 (`chunk` / `done` / `error`) で yield する。

    Producer スレッドで同期イテレータを走らせ、asyncio.Queue 経由で
    main loop に橋渡しする。

    Parameters
    ----------
    client : openai.OpenAI 互換クライアント
    model : str
    messages : list[dict]
        OpenAI 形式の messages
    chat_conf : dict
        kwargs 構築用 config (build_chat_completion_kwargs に渡す)
    debug_data : dict, optional
        done event の debug フィールドに含めるデータ
    web_sources : list, optional
        done event の sources フィールドに含める web 検索結果
    log_tag : str
        ログ出力用のプロバイダ名タグ
    """
    kwargs = (
        dict(request_kwargs)
        if request_kwargs is not None
        else build_chat_completion_kwargs(chat_conf, messages, model_name=model)
    )
    kwargs["model"] = model
    kwargs["stream"] = True

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    stop_event = threading.Event()
    stream_holder: Dict[str, Any] = {}

    def _enqueue(event: Dict[str, Any]) -> None:
        if stop_event.is_set() or loop.is_closed():
            return
        loop.call_soon_threadsafe(queue.put_nowait, event)

    def _producer():
        full_text = ""
        attempt = 0
        while True:
            attempt += 1
            stream = None
            try:
                print(f"[{log_tag}] Streaming start")
                stream = client.chat.completions.create(**kwargs)
                stream_holder["stream"] = stream
                for chunk in stream:
                    if stop_event.is_set():
                        break
                    if not getattr(chunk, "choices", None):
                        continue
                    choice = chunk.choices[0]
                    delta = getattr(choice, "delta", None)
                    if delta is None:
                        continue
                    text_chunk = getattr(delta, "content", None) or ""
                    if text_chunk:
                        full_text += text_chunk
                        _enqueue({"type": "chunk", "text": text_chunk})
                if stop_event.is_set():
                    return
                print(f"[{log_tag}] Streaming done")
                if on_success is not None:
                    try:
                        on_success()
                    except Exception:
                        # 応答自体は成功しているため、キャッシュ保存失敗で
                        # ユーザーへの出力を失敗扱いにしない。
                        logger.warning(
                            "provider capability observation could not be saved",
                            exc_info=True,
                        )
                _enqueue(
                    {
                        "type": "done",
                        "full_text": full_text,
                        "sources": list(web_sources or []),
                        "debug": debug_data or {},
                    }
                )
                return
            except Exception as e:
                if stop_event.is_set():
                    return
                if (
                    not full_text
                    and attempt < MAX_STREAM_ATTEMPTS
                    and parameter_error_handler is not None
                ):
                    try:
                        corrected_kwargs = parameter_error_handler(
                            e,
                            dict(kwargs),
                        )
                    except Exception as handler_error:
                        logger.exception(
                            "provider parameter correction failed"
                        )
                        _enqueue(
                            {
                                "type": "error",
                                "message": str(handler_error),
                            }
                        )
                        return
                    if corrected_kwargs is not None:
                        kwargs.clear()
                        kwargs.update(corrected_kwargs)
                        kwargs["model"] = model
                        kwargs["stream"] = True
                        logger.info(
                            "provider stream unsupported-parameter correction: "
                            "attempt=%d error=%s",
                            attempt,
                            type(e).__name__,
                        )
                        continue
                if (
                    not full_text
                    and attempt < MAX_STREAM_ATTEMPTS
                    and is_retryable_stream_error(e)
                ):
                    # まだ 1 文字も送っていないので、二重生成にはならない。
                    print(
                        f"[{log_tag}] Stream failed before any output "
                        f"({type(e).__name__}); retrying once"
                    )
                    logger.info(
                        "provider stream retry: attempt=%d error=%s",
                        attempt,
                        type(e).__name__,
                    )
                    continue
                import traceback

                traceback.print_exc()
                _enqueue(
                    {
                        "type": "error",
                        "message": str(e),
                    }
                )
                return
            finally:
                stream_holder.pop("stream", None)
                close = getattr(stream, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        logger.debug("provider stream close failed", exc_info=True)

    producer_future = loop.run_in_executor(None, _producer)

    def _producer_finished(future) -> None:
        if future.cancelled():
            return
        error = future.exception()
        if error is None or stop_event.is_set() or loop.is_closed():
            return
        logger.error(
            "provider stream producer terminated unexpectedly",
            exc_info=(type(error), error, error.__traceback__),
        )
        queue.put_nowait({"type": "error", "message": str(error)})

    producer_future.add_done_callback(_producer_finished)

    try:
        while True:
            event = await queue.get()
            yield event
            if event["type"] in ("done", "error"):
                break
    finally:
        stop_event.set()
        stream = stream_holder.get("stream")
        close = getattr(stream, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                logger.debug("provider stream cancellation failed", exc_info=True)
        if producer_future.done():
            try:
                await producer_future
            except Exception:
                logger.debug("provider stream producer failed", exc_info=True)
        else:
            producer_future.cancel()

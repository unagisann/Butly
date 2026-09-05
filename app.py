import streamlit as st
import time
import base64
from datetime import datetime
from pathlib import Path
import asyncio
import os
import re

# 自作モジュールのインポート
from butly_core.core.memory import ButlyMemory
from butly_core.core.brain import ButlyBrain
from butly_core.core.chronos import ButlyChronos
from butly_core.core.instance_manager import InstanceManager
from butly_core.llm.selection import (
    ModelChoice,
    ensure_current_in_candidates,
    find_current_index,
    normalize_candidates,
    set_model_choice,
)

# --- 基本設定 ---
BASE_DIR = Path(__file__).resolve().parent
INSTANCES_DIR = BASE_DIR / "butly_core" / "instances"

# --- UI設定 ---
st.set_page_config(
    page_title="Butly Web Console",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# --- マテリアル3風のカスタムCSS ---
st.markdown(
    """
<style>
    /* 全体のフォントを Noto Sans JP 風に */
    html, body, [class*="css"]  {
        font-family: 'Noto Sans JP', sans-serif;
    }

    /* Streamlitのデフォルトヘッダー/フッターを隠す */
    header {visibility: hidden;}
    footer {visibility: hidden;}

    /* ヘッダーエリアのスタイリング */
    .app-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: 1rem;
        background-color: var(--background-color);
        border-bottom: 1px solid var(--border-color);
        margin-top: -3rem; /* デフォルトの余白を詰める */
        margin-bottom: 1rem;
    }
    
    .app-title {
        font-size: 1.5rem;
        font-weight: 600;
        margin: 0;
    }

    /* 各種ボタン類のスタイリング (Material 3 風) */
    .stButton > button {
        border-radius: 20px !important;
        font-weight: 500 !important;
        transition: all 0.2s ease !important;
    }

    /* カード風のコンテナ */
    .instance-card {
        background-color: var(--secondary-background-color);
        border-radius: 16px;
        padding: 1.5rem;
        margin-bottom: 1rem;
        border: 1px solid var(--border-color);
        display: flex;
        justify-content: space-between;
        align-items: center;
        cursor: pointer;
        transition: background-color 0.2s;
    }
    .instance-card:hover {
        background-color: rgba(128, 128, 128, 0.1);
    }
    
    .instance-name {
        font-size: 1.2rem;
        font-weight: 600;
    }

    /* チャットバブルのスタイリング */
    .chat-bubble-user {
        background-color: #d3e3fd; /* Primary Container */
        color: #041e49; /* On Primary Container */
        border-radius: 16px 16px 0px 16px;
        padding: 12px 16px;
        margin-bottom: 8px;
        max-width: 80%;
        float: right;
        clear: both;
    }
    .chat-bubble-ai {
        background-color: #e1e2e8; /* Surface Variant */
        color: #1a1b20; /* On Surface Variant */
        border-radius: 16px 16px 16px 0px;
        padding: 12px 16px;
        margin-bottom: 8px;
        max-width: 80%;
        float: left;
        clear: both;
    }
    
    /* ダークモード対応の調整 */
    @media (prefers-color-scheme: dark) {
        .chat-bubble-user {
            background-color: #004a77;
            color: #c2e7ff;
        }
        .chat-bubble-ai {
            background-color: #44474e;
            color: #c4c6d0;
        }
    }

    /* 添付画像サムネイル */
    .chat-bubble-user img.attachment-thumb {
        max-width: 240px;
        max-height: 180px;
        border-radius: 8px;
        margin-top: 6px;
        display: block;
    }

    /* 汎用クラス */
    .clearfix::after {
        content: "";
        clear: both;
        display: table;
    }
    
    .debug-box { background-color: #262730; border-radius: 5px; padding: 10px; border: 1px solid #444; margin-top: 10px;}
    .rag-ref { font-size: 0.9em; color: #aaa; border-left: 2px solid #00ff00; padding-left: 10px; margin-bottom: 5px;}
</style>
""",
    unsafe_allow_html=True,
)

# --- 共通ヘルパー関数 ---

# Phase 3: ハードコード preset を撤廃し、backend の /settings/model_candidates が
# 唯一の真実 (model_registry + 動的 /models + 保存中モデルの和集合) を返す。
# backend 到達不能時は model_registry を直接 import して fallback。
_CONNECTION_ICONS = {
    "google": "🟦",
    "openai": "🟩",
    "xai": "🟥",
    "ollama": "🟧",
    "nanogpt": "💎",
    "nanogpt-sub": "💎",
}
_MODEL_CANDIDATE_CACHE_TTL_SECONDS = 600
_UI_READ_CACHE_TTL_SECONDS = 30


@st.cache_data(ttl=_UI_READ_CACHE_TTL_SECONDS, show_spinner=False)
def _cached_api_json(api_url: str, path: str) -> dict:
    """Return a short-lived copy of read-only Web Console API data."""
    import requests

    response = requests.get(f"{api_url}{path}", timeout=10)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f"API response must be an object: {path}")
    return payload


@st.cache_data(ttl=_MODEL_CANDIDATE_CACHE_TTL_SECONDS, show_spinner=False)
def _get_role_candidates(
    api_url: str,
    role: str,
    connection_id: str | None = None,
) -> list:
    """role に対するモデル候補を backend から取得する。

    Returns
    -------
    list[dict]
        各要素 = {"connection_id", "model_name", "label", "deprecated", "preview",
                "replacement", "is_builtin_connection", ...}
    """
    try:
        import requests as _req

        params = {"role": role}
        if connection_id:
            params["connection_id"] = connection_id
        resp = _req.get(
            f"{api_url}/settings/model_candidates",
            params=params,
            timeout=8,
        )
        if resp.ok:
            return resp.json().get("candidates", [])
    except Exception:
        pass
    # フォールバック: model_registry から直接 (backend 到達不能時)
    try:
        from butly_core.llm.model_registry import get_presets_for_role

        return [
            {
                "connection_id": p.connection_id,
                "model_name": p.model_name,
                "label": p.label,
                "deprecated": p.deprecated,
                "preview": p.preview,
                "replacement": p.replacement,
                "is_builtin_connection": True,
                "source": "preset",
            }
            for p in get_presets_for_role(role)
        ]
    except Exception:
        return []


def _get_selector_candidates(
    api_url: str,
    role: str,
    current_connection: str | None,
    key_prefix: str,
) -> list:
    """Load dynamic models only for the currently selected Connection."""
    selected_connection = (
        st.session_state.get(f"{key_prefix}_connection")
        or current_connection
    )
    return _get_role_candidates(api_url, role, selected_connection)


@st.cache_data(ttl=300, show_spinner=False)
def _get_connections(api_url: str) -> list:
    """Connection metadata for provider-first model selection."""
    try:
        import requests as _req

        resp = _req.get(f"{api_url}/settings/connections", timeout=8)
        if resp.ok:
            return resp.json().get("connections", [])
    except Exception:
        pass

    try:
        from butly_core.llm.connections import (
            is_builtin_connection,
            list_connections,
        )

        return [
            {
                "id": conn.id,
                "label": conn.display_label,
                "api_key_env": conn.api_key_env,
                "api_key_set": bool(conn.resolve_api_key())
                if conn.api_key_env
                else None,
                "embeddings_supported": conn.embeddings_supported,
                "is_builtin": is_builtin_connection(conn.id),
            }
            for conn in list_connections()
        ]
    except Exception:
        return []


@st.cache_data(ttl=300, show_spinner=False)
def _get_connection_templates(api_url: str) -> list:
    try:
        import requests as _req

        resp = _req.get(
            f"{api_url}/settings/connection_templates",
            timeout=8,
        )
        if resp.ok:
            return resp.json().get("templates", [])
    except Exception:
        pass
    return []


def _candidate_label(c: dict) -> str:
    """候補 dict を表示用ラベル文字列に整形する。"""
    icon = _CONNECTION_ICONS.get(c.get("connection_id"), "🔌")
    base = f"{icon} {c.get('connection_id', '?')} / {c.get('model_name', '?')}"
    if c.get("deprecated"):
        base += "  ⚠ deprecated"
    elif c.get("preview"):
        base += "  (preview)"
    return base


def _connection_label(connection: dict) -> str:
    connection_id = connection.get("id", "?")
    icon = _CONNECTION_ICONS.get(connection_id, "🔌")
    label = connection.get("label") or connection_id
    key_status = ""
    if connection.get("api_key_env"):
        key_status = "  ✅" if connection.get("api_key_set") else "  🔑未設定"
    return f"{icon} {label} ({connection_id}){key_status}"


def get_provider_label(model_name: str) -> str:
    """モデル名からプロバイダーラベルを返す (legacy fallback 表示用)。"""
    if not model_name:
        return "❓ 不明"
    if model_name.startswith("gemini") or model_name.startswith("models/gemini"):
        return "🟦 Gemini"
    elif model_name.startswith(("gpt-", "o1", "o3", "o4", "text-embedding")):
        return "🟩 OpenAI"
    elif model_name.startswith(("grok-", "xai/")):
        return "🟥 xAI"
    elif model_name.startswith("ollama/"):
        return "🟧 Ollama"
    else:
        return "❓ 不明"


def _model_selector(
    label: str,
    current_value: str,
    current_connection: str | None,
    candidates: list,
    connections: list,
    key_prefix: str,
    *,
    embeddings_only: bool = False,
) -> ModelChoice:
    """Select a Connection first, then a model within that Connection."""
    from butly_core.llm.model_registry import infer_connection_id

    normalized = normalize_candidates(candidates)
    for candidate in normalized:
        if not candidate.get("connection_id"):
            candidate["connection_id"] = infer_connection_id(
                candidate["model_name"]
            )

    resolved_connection = current_connection or infer_connection_id(current_value)
    if not resolved_connection and current_value:
        matching_connections = {
            candidate.get("connection_id")
            for candidate in normalized
            if candidate.get("model_name") == current_value
            and candidate.get("connection_id")
        }
        if len(matching_connections) == 1:
            resolved_connection = matching_connections.pop()

    current = ModelChoice(resolved_connection, current_value or "")
    normalized = ensure_current_in_candidates(normalized, current)

    connection_map = {
        item.get("id"): dict(item)
        for item in connections
        if item.get("id")
    }
    for candidate in normalized:
        connection_id = candidate.get("connection_id")
        if connection_id and connection_id not in connection_map:
            connection_map[connection_id] = {
                "id": connection_id,
                "label": connection_id,
                "embeddings_supported": True,
            }
    if resolved_connection and resolved_connection not in connection_map:
        connection_map[resolved_connection] = {
            "id": resolved_connection,
            "label": resolved_connection,
            "embeddings_supported": True,
        }

    provider_ids = []
    for connection_id, metadata in connection_map.items():
        if (
            embeddings_only
            and not metadata.get("embeddings_supported", True)
            and connection_id != resolved_connection
        ):
            continue
        provider_ids.append(connection_id)

    if not provider_ids:
        fallback_model = st.text_input(
            f"{label} — モデル",
            value=current_value or "",
            key=f"{key_prefix}_model_only",
        )
        return ModelChoice(resolved_connection, fallback_model.strip())

    provider_index = (
        provider_ids.index(resolved_connection)
        if resolved_connection in provider_ids
        else 0
    )
    selected_connection = st.selectbox(
        f"{label} — プロバイダー / Connection",
        options=provider_ids,
        index=provider_index,
        key=f"{key_prefix}_connection",
        format_func=lambda connection_id: _connection_label(
            connection_map[connection_id]
        ),
    )

    selected_candidates = [
        candidate
        for candidate in normalized
        if candidate.get("connection_id") == selected_connection
    ]
    if selected_candidates:
        selected_current = ModelChoice(
            selected_connection,
            current_value if selected_connection == resolved_connection else "",
        )
        model_index = find_current_index(selected_candidates, selected_current)
        selected_index = st.selectbox(
            f"{label} — モデル",
            options=list(range(len(selected_candidates))),
            index=model_index,
            key=f"{key_prefix}_model_{selected_connection}",
            format_func=lambda index: _candidate_label(
                selected_candidates[index]
            ),
        )
        selected_candidate = selected_candidates[selected_index]
        selected_model = selected_candidate.get("model_name", "")
        if selected_candidate.get("deprecated"):
            replacement = selected_candidate.get("replacement")
            suffix = f" 代替: {replacement}" if replacement else ""
            st.warning(f"⚠ {selected_model} は deprecated。{suffix}")
    else:
        selected_model = ""
        st.caption(
            "モデル一覧を取得できません。APIキー設定後に再読込するか、"
            "モデルIDを直接入力してください。"
        )

    custom = st.text_input(
        "モデルIDを直接入力（任意）",
        value="",
        placeholder="例: Qwen/Qwen3-14B",
        key=f"{key_prefix}_custom_{selected_connection}",
    ).strip()
    return ModelChoice(selected_connection, custom or selected_model)


def _api_error_detail(response) -> str:
    try:
        detail = response.json().get("detail")
        if isinstance(detail, dict):
            message = detail.get("message") or str(detail)
            references = detail.get("references") or []
            if references:
                message += f"（参照: {', '.join(references)}）"
            return message
        if detail:
            return str(detail)
    except Exception:
        pass
    return response.text


def _render_model_catalog_refresh(api_url: str, *, key: str) -> None:
    """Refresh provider-discovered model IDs only on explicit request."""
    import requests

    if not st.button(
        "🔄 モデル一覧を更新",
        key=key,
        help=(
            "通常は10分間のキャッシュを利用します。プロバイダー側で"
            "モデルが追加・削除されたときだけ更新してください。"
        ),
    ):
        return
    try:
        response = requests.post(
            f"{api_url}/settings/model_catalog/refresh",
            json={},
            timeout=10,
        )
        if not response.ok:
            st.error(_api_error_detail(response))
            return
        _get_role_candidates.clear()
        st.success("モデル一覧キャッシュを更新しました。")
        st.rerun()
    except Exception as exc:
        st.error(f"モデル一覧の更新エラー: {exc}")


def _render_connection_manager(api_url: str) -> list:
    """Render generic Connection, secret, test, and template management."""
    import requests

    connections = _get_connections(api_url)
    st.subheader("🔌 Connection / APIキー管理")
    st.caption(
        "接続先とAPIキーをConnection単位で管理します。キー本体は保存後に"
        "再表示されません。"
    )

    with st.expander("既存 Connection", expanded=True):
        if not connections:
            st.warning("Connection情報を取得できませんでした。")

        env_users: dict[str, list[str]] = {}
        for connection in connections:
            env_name = connection.get("api_key_env")
            if env_name:
                env_users.setdefault(env_name, []).append(connection.get("id"))

        for connection in connections:
            connection_id = connection.get("id")
            if not connection_id:
                continue
            with st.container(border=True):
                header_cols = st.columns([5, 2, 2])
                with header_cols[0]:
                    badge = (
                        "🔒 built-in"
                        if connection.get("is_builtin")
                        else "✏️ user"
                    )
                    st.markdown(
                        f"**{_connection_label(connection)}** · {badge}"
                    )
                    if connection.get("base_url"):
                        st.caption(
                            f"`{connection['base_url']}` · "
                            f"`{connection.get('protocol')}`"
                        )
                    if connection.get("notes"):
                        st.caption(connection["notes"])
                with header_cols[1]:
                    st.caption(
                        "Embedding: "
                        + (
                            "対応"
                            if connection.get("embeddings_supported")
                            else "非対応"
                        )
                    )
                with header_cols[2]:
                    if st.button(
                        "📡 疎通テスト",
                        key=f"test_conn_{connection_id}",
                        width="stretch",
                    ):
                        try:
                            response = requests.post(
                                f"{api_url}/settings/test_connection",
                                json={"connection_id": connection_id},
                                timeout=15,
                            )
                            result = response.json() if response.ok else {}
                            if result.get("status") == "ok":
                                st.success(
                                    f"OK: {len(result.get('models') or [])}モデル"
                                )
                            else:
                                st.error(
                                    result.get("message")
                                    or _api_error_detail(response)
                                )
                        except Exception as exc:
                            st.error(f"接続エラー: {exc}")

                api_key_env = connection.get("api_key_env")
                if api_key_env:
                    if len(env_users.get(api_key_env, [])) > 1:
                        shared = ", ".join(env_users[api_key_env])
                        st.caption(
                            f"🔗 `{api_key_env}` は {shared} で共有されます。"
                        )
                    key_widget = f"connection_secret_{connection_id}"
                    with st.form(f"connection_secret_form_{connection_id}"):
                        key_cols = st.columns([6, 1, 1])
                        with key_cols[0]:
                            api_key = st.text_input(
                                (
                                    f"{api_key_env} "
                                    + (
                                        "（設定済み）"
                                        if connection.get("api_key_set")
                                        else "（未設定）"
                                    )
                                ),
                                type="password",
                                value="",
                                key=key_widget,
                                placeholder="新しいAPIキーを入力",
                            )
                        with key_cols[1]:
                            save_api_key = st.form_submit_button(
                                "🔑 保存",
                                width="stretch",
                            )
                        with key_cols[2]:
                            clear_api_key = st.form_submit_button(
                                "解除",
                                disabled=not connection.get("api_key_set"),
                                width="stretch",
                            )
                    if save_api_key:
                        if not api_key:
                            st.warning("APIキーを入力してください。")
                        else:
                            try:
                                response = requests.post(
                                    (
                                        f"{api_url}/settings/connections/"
                                        f"{connection_id}/api_key"
                                    ),
                                    json={"api_key": api_key},
                                    timeout=5,
                                )
                                if response.ok:
                                    st.session_state.pop(key_widget, None)
                                    st.cache_data.clear()
                                    st.success("APIキーを保存しました。")
                                    st.rerun()
                                else:
                                    st.error(_api_error_detail(response))
                            except Exception as exc:
                                st.error(f"保存エラー: {exc}")
                    if clear_api_key:
                        try:
                            response = requests.delete(
                                (
                                    f"{api_url}/settings/connections/"
                                    f"{connection_id}/api_key"
                                ),
                                timeout=5,
                            )
                            if response.ok:
                                st.cache_data.clear()
                                st.success("APIキーを解除しました。")
                                st.rerun()
                            else:
                                st.error(_api_error_detail(response))
                        except Exception as exc:
                            st.error(f"解除エラー: {exc}")
                else:
                    st.caption("🔓 APIキー不要")

                if not connection.get("is_builtin"):
                    with st.form(f"delete_connection_form_{connection_id}"):
                        delete_cols = st.columns([5, 2, 1])
                        with delete_cols[1]:
                            force_delete = st.checkbox(
                                "参照中でも強制",
                                key=f"force_delete_{connection_id}",
                                help=(
                                    "参照中のモデル設定が壊れる可能性があります。"
                                    "通常は先にモデル割り当てを変更してください。"
                                ),
                            )
                        with delete_cols[2]:
                            delete_connection = st.form_submit_button(
                                "🗑️ 削除",
                                width="stretch",
                            )
                    if delete_connection:
                        try:
                            response = requests.delete(
                                (
                                    f"{api_url}/settings/connections/"
                                    f"{connection_id}"
                                ),
                                params={"force": force_delete},
                                timeout=5,
                            )
                            if response.ok:
                                st.cache_data.clear()
                                st.success(f"{connection_id} を削除しました。")
                                st.rerun()
                            else:
                                st.error(_api_error_detail(response))
                        except Exception as exc:
                            st.error(f"削除エラー: {exc}")

    with st.expander("➕ Connectionを追加"):
        templates = _get_connection_templates(api_url)
        template_map = {
            template["id"]: template
            for template in templates
            if template.get("id")
        }
        template_ids = ["__custom__", *template_map]
        selected_template_id = st.selectbox(
            "プロバイダーテンプレート",
            options=template_ids,
            format_func=lambda template_id: (
                "カスタム（OpenAI互換）"
                if template_id == "__custom__"
                else template_map[template_id].get("label", template_id)
            ),
            key="new_connection_template",
        )
        template = template_map.get(selected_template_id, {})
        widget_suffix = selected_template_id.replace("-", "_")

        if template.get("notes"):
            st.info(template["notes"])

        with st.form(f"add_connection_form_{widget_suffix}"):
            form_cols = st.columns(2)
            with form_cols[0]:
                new_id = st.text_input(
                    "Connection ID",
                    value=template.get("id", ""),
                    key=f"new_conn_id_{widget_suffix}",
                    help="小文字英数字で開始し、英数字・_・-を使用できます。",
                )
                new_label = st.text_input(
                    "表示名",
                    value=template.get("label", ""),
                    key=f"new_conn_label_{widget_suffix}",
                )
            with form_cols[1]:
                new_base_url = st.text_input(
                    "Base URL",
                    value=template.get("base_url", ""),
                    key=f"new_conn_base_url_{widget_suffix}",
                    placeholder="https://example.com/v1",
                )
                new_api_key_env = st.text_input(
                    "APIキー環境変数名",
                    value=template.get("api_key_env", ""),
                    key=f"new_conn_api_env_{widget_suffix}",
                    placeholder="EXAMPLE_API_KEY",
                )
            new_embeddings = st.checkbox(
                "Embeddings対応",
                value=bool(template.get("embeddings_supported", False)),
                key=f"new_conn_embeddings_{widget_suffix}",
            )
            add_connection = st.form_submit_button("💾 Connectionを追加")

        if add_connection:
            payload = {
                "id": new_id.strip(),
                "protocol": template.get("protocol", "openai_compat"),
                "base_url": new_base_url.strip() or None,
                "api_key_env": new_api_key_env.strip() or None,
                "label": new_label.strip() or None,
                "embeddings_supported": bool(new_embeddings),
                "extra_headers": template.get("extra_headers", {}),
            }
            if not payload["id"] or not payload["base_url"]:
                st.error("Connection IDとBase URLは必須です。")
            else:
                try:
                    response = requests.post(
                        f"{api_url}/settings/connections",
                        json=payload,
                        timeout=5,
                    )
                    if response.ok:
                        st.cache_data.clear()
                        st.success(
                            "Connectionを追加しました。"
                            "一覧からAPIキーを保存してください。"
                        )
                        st.rerun()
                    else:
                        st.error(_api_error_detail(response))
                except Exception as exc:
                    st.error(f"追加エラー: {exc}")

    return connections


# --- マネージャー初期化 ---
instance_manager = InstanceManager(BASE_DIR)


# --- システム初期化 ---
@st.cache_resource
def initialize_system(base_dir, instance_name):
    print(f"[System] Initializing instance: {instance_name}")
    memory = ButlyMemory(base_dir, instance_name=instance_name)
    brain = ButlyBrain(base_dir)
    chronos = ButlyChronos()
    return memory, brain, chronos


# --- 初期化・ディレクトリ作成 ---
if not INSTANCES_DIR.exists():
    INSTANCES_DIR.mkdir(parents=True, exist_ok=True)

def normalize_api_url(api_url: str) -> str:
    return api_url.strip().rstrip("/") or "http://127.0.0.1:8000"


# デフォルトAPI接続先（session_state 初期化前のモジュールレベルでも使う）
DEFAULT_API_URL = normalize_api_url(
    os.environ.get("BUTLY_API_URL", "http://127.0.0.1:8000")
)


def fetch_instance_names(api_url: str) -> list:
    """新 API（GET /api/v1/instances）から instance 名一覧を取得する。

    INSTANCES_DIR 直読みの置換（frontend_migration_plan.ja.md §3.3）。
    backend 到達不能時は例外を投げる。API fallback で直読みに戻さず、
    呼び出し側で明示的な error state にする（同計画 §7-12）。
    """
    import requests as _req_inst

    api_url = normalize_api_url(api_url)
    resp = _req_inst.get(f"{api_url}/api/v1/instances", timeout=5)
    resp.raise_for_status()
    items = resp.json().get("items", [])
    return sorted(
        item["name"]
        for item in items
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    )


# --- セッションステートの初期化 ---
if "current_page" not in st.session_state:
    st.session_state.current_page = "home"
# API接続先（DEFAULT_API_URL はモジュール先頭で定義済み）
if "api_base_url" not in st.session_state:
    st.session_state.api_base_url = DEFAULT_API_URL
if "api_connection_error" not in st.session_state:
    st.session_state.api_connection_error = None

available_instances = []

if "current_instance" not in st.session_state:
    st.session_state.current_instance = None
if "messages" not in st.session_state:
    st.session_state.messages = []
if "is_holiday" not in st.session_state:
    st.session_state.is_holiday = False
if "debug_mode" not in st.session_state:
    st.session_state.debug_mode = True
if "use_google_search" not in st.session_state:
    # インスタンス設定のdefault_use_google_searchを初期値に使用
    st.session_state.use_google_search = False
if "use_web_search" not in st.session_state:
    st.session_state.use_web_search = False
if "input_key_counter" not in st.session_state:
    st.session_state.input_key_counter = 0  # チャット入力欄クリア用
if "pending_attachments" not in st.session_state:
    st.session_state.pending_attachments = []
# テーマカラー (Butlyアプリ対応)
if "theme_color" not in st.session_state:
    st.session_state.theme_color = "teal"
if "sleeptime_instance" not in st.session_state:
    st.session_state.sleeptime_instance = None
if "db_browser_instance" not in st.session_state:
    st.session_state.db_browser_instance = None
if "card_edit_id" not in st.session_state:
    st.session_state.card_edit_id = None


# 画面遷移ヘルパー
def navigate_to(page, instance=None):
    st.session_state.current_page = page
    if instance:
        if st.session_state.current_instance != instance:
            st.session_state.current_instance = instance
            st.session_state.messages = []
            if "last_interaction_time" in st.session_state:
                del st.session_state.last_interaction_time
            st.cache_resource.clear()
    st.rerun()


# 性格テンプレート読み込みヘルパー
def load_personality_templates(base_dir, locale="ja"):
    """locales/{locale}/templates/ から性格テンプレートを読み込む。"""
    template_dir = (
        base_dir / "butly_core" / "prompts" / "locales" / locale / "templates"
    )
    templates = {}
    if template_dir.exists():
        for f in sorted(template_dir.glob("system_instruction_*.txt")):
            name = f.stem.replace("system_instruction_", "")
            templates[name] = f.read_text(encoding="utf-8")
    return templates


# Key Memory ビルダー
def is_gemini_provider(model_name: str) -> bool:
    """model_name がGeminiプロバイダーかどうかを判定する。"""
    if not model_name:
        return True  # デフォルトはGemini
    name = model_name.lower()
    return name.startswith("gemini") or name.startswith("models/gemini")


def get_active_chat_model(api_url: str, instance_name: str) -> str:
    """現在のインスタンスの chat model_name を取得する。"""
    # 1. インスタンス固有config
    try:
        inst_cfg = _cached_api_json(api_url, f"/instances/{instance_name}/config")
        inst_model = inst_cfg.get("chat", {}).get("model_name", "")
        if inst_model:
            return inst_model
    except Exception:
        pass
    # 2. グローバルconfig
    try:
        global_cfg = _cached_api_json(api_url, "/config")
        return (
            global_cfg.get("AI_CONFIG", {})
            .get("chat", {})
            .get("model_name", "gemini-3.5-flash")
        )
    except Exception:
        pass
    return "gemini-3.5-flash"  # 最終フォールバック


# ==========================================
# 🏠 ホーム画面 (Home Screen)
# ==========================================
def render_home_screen():
    # ヘッダー
    col1, col2, col3, col4, col5, col6 = st.columns([5, 1, 1, 1, 1, 1])
    with col1:
        st.markdown('<h1 class="app-title">Butly</h1>', unsafe_allow_html=True)
    with col2:
        if st.button("🗄️", help="データベースブラウザ"):
            st.session_state.db_browser_instance = st.session_state.current_instance
            navigate_to("database_browser")
    with col3:
        if st.button("📊", help="LoCoMo評価"):
            navigate_to("evaluations")
    with col4:
        if st.button("🔗", help="LINE連携"):
            navigate_to("pairing")
    with col5:
        if st.button("⚙️", help="設定"):
            navigate_to("settings")
    with col6:
        if st.button("🚪", help="終了 (セッションクリア)"):
            st.session_state.messages = []
            st.cache_resource.clear()
            st.success("セッションをクリアしました")

    st.divider()

    # インスタンス一覧
    st.subheader("Your AI Instances")
    api_error = st.session_state.get("api_connection_error")
    if api_error:
        st.error(
            f"Butly API（{st.session_state.api_base_url}）に接続できません: "
            f"{api_error}\n\n"
            "FastAPI サーバー（main.py）が起動しているか、設定の API 接続先を確認してください。"
        )
    elif not available_instances:
        st.write("インスタンスがありません。")
    else:
        for name in available_instances:
            # st.button()を使った簡易なカード風リスト
            if st.button(
                f"🤖 {name}", key=f"btn_inst_{name}", width="stretch"
            ):
                navigate_to("chat", instance=name)

    st.divider()

    # locale 取得（テンプレート選択に使用）
    try:
        _home_cfg = _cached_api_json(
            st.session_state.api_base_url,
            "/config",
        )
    except Exception:
        _home_cfg = {"SYSTEM_CONFIG": {}}
    _home_locale = (
        _home_cfg.get("SYSTEM_CONFIG", {}).get("agent", {}).get("locale", "ja")
    )

    # 新規インスタンス作成 (FABの代わり)
    with st.expander("➕ 新しいインスタンスを作成"):
        new_proj_name = st.text_input(
            (
                "インスタンス名（半角英数字・_）"
                if _home_locale != "en"
                else "Instance Name (alphanumeric & _)"
            ),
            placeholder="e.g. new_agent",
        )

        # --- テンプレート選択UI ---
        _templates = load_personality_templates(BASE_DIR, _home_locale)
        _template_labels_ja = {
            "butly": "Butly（知的協働パートナー）",
            "creator": "Creator（創造的パートナー）",
            "analyst": "Analyst（分析パートナー）",
            "friendly": "Friendly（カジュアルパートナー）",
            "caring": "Caring（寄り添い型パートナー）",
            "custom": "カスタム（自由入力）",
        }
        _template_labels_en = {
            "butly": "Butly (Intellectual Partner)",
            "creator": "Creator (Creative Partner)",
            "analyst": "Analyst (Analytical Partner)",
            "friendly": "Friendly (Casual Partner)",
            "caring": "Caring (Supportive Partner)",
            "custom": "Custom (Free Input)",
        }
        _labels = _template_labels_en if _home_locale == "en" else _template_labels_ja
        _options = list(_templates.keys()) + ["custom"]

        if "prev_template_choice" not in st.session_state:
            st.session_state.prev_template_choice = (
                _options[0] if _options else "custom"
            )

        _selected = st.selectbox(
            "性格テンプレート" if _home_locale != "en" else "Personality Template",
            options=_options,
            format_func=lambda x: _labels.get(x, x),
        )

        _default_text = "" if _selected == "custom" else _templates.get(_selected, "")

        if _selected != st.session_state.prev_template_choice:
            st.session_state.prev_template_choice = _selected
            st.session_state.create_instance_template = _default_text
            st.rerun()

        new_template = st.text_area(
            "性格設定" if _home_locale != "en" else "Personality Settings",
            value=_default_text,
            height=200,
            key="create_instance_template",
        )
        # --- テンプレート選択UI（ここまで） ---

        st.divider()

        # --- Key Memory 初期設定 ---
        st.markdown(
            "**🧠 初期設定**" if _home_locale == "ja" else "**🧠 Initial Setup**"
        )

        ai_name_input = st.text_input(
            "AIの名前" if _home_locale == "ja" else "AI Name",
            placeholder=(
                "ジャービス、ルナ、アトラス..."
                if _home_locale == "ja"
                else "Jarvis, Luna, Atlas..."
            ),
            help=(
                "AIの呼び名を決めてください。"
                if _home_locale == "ja"
                else "Choose a name for your AI."
            ),
        )

        _col_name, _col_nick = st.columns(2)
        with _col_name:
            user_name_input = st.text_input(
                "あなたの名前" if _home_locale == "ja" else "Your Name",
                placeholder="太郎" if _home_locale == "ja" else "John",
                help=(
                    "AIがあなたを認識するための名前です。"
                    if _home_locale == "ja"
                    else "The name your AI will use to recognize you."
                ),
            )
        with _col_nick:
            nickname_input = st.text_input(
                "呼ばれたい名前" if _home_locale == "ja" else "Preferred Name",
                placeholder=(
                    "たろ、マスター、〇〇さん"
                    if _home_locale == "ja"
                    else "Johnny, Boss, Mr. Smith"
                ),
                help=(
                    "AIがあなたを呼ぶときの名前です。空欄なら「あなたの名前」を使います。"
                    if _home_locale == "ja"
                    else "How your AI will address you. Leave blank to use your name."
                ),
            )

        with st.expander(
            "🎁 追加設定（任意）"
            if _home_locale == "ja"
            else "🎁 Additional Settings (Optional)"
        ):
            _gender_opts = (
                ["", "男性", "女性", "その他"]
                if _home_locale == "ja"
                else ["", "Male", "Female", "Other"]
            )
            gender_input = st.selectbox(
                "性別" if _home_locale == "ja" else "Gender",
                options=_gender_opts,
                index=0,
            )
            birthday_input = st.date_input(
                "生年月日" if _home_locale == "ja" else "Date of Birth",
                value=None,
                min_value=datetime(datetime.today().year - 90, 1, 1),
                max_value=datetime(datetime.today().year + 5, 12, 31),
                format="YYYY/MM/DD",
            )

        _birthday_str = birthday_input.strftime("%Y/%m/%d") if birthday_input else ""
        # --- Key Memory 初期設定（ここまで） ---

        if st.button("作成" if _home_locale == "ja" else "Create", type="primary"):
            if not new_proj_name:
                st.error(
                    "インスタンス名を入力してください。"
                    if _home_locale == "ja"
                    else "Please enter an instance name."
                )
            elif not ai_name_input:
                st.error(
                    "AIの名前を入力してください。"
                    if _home_locale == "ja"
                    else "Please enter an AI name."
                )
            else:
                import requests

                api_url = st.session_state.api_base_url
                try:
                    _agent_profile = {
                        "ai_name": ai_name_input,
                        "ai_gender": "",
                        "locale": _home_locale,
                    }
                    _user_profile = {
                        "user_name": user_name_input,
                        "preferred_call": (
                            nickname_input if nickname_input else user_name_input
                        ),
                        "gender": gender_input if gender_input else "",
                        "birthday": _birthday_str,
                        "location": "",
                    }
                    res = requests.post(
                        f"{api_url}/instances",
                        json={
                            "name": new_proj_name,
                            "template": new_template,
                            "key_memory": "",
                            "agent_profile": _agent_profile,
                            "user_profile": _user_profile,
                        },
                        timeout=5,
                    )
                    if res.ok:
                        st.rerun()
                    else:
                        st.error(
                            f"エラー: {res.text}"
                            if _home_locale == "ja"
                            else f"Error: {res.text}"
                        )
                except Exception as e:
                    st.error(f"エラー: {e}" if _home_locale == "ja" else f"Error: {e}")


# ==========================================
# ⚙️ 設定画面 (3セクション構成) - Butly Client準拠
# ==========================================
@st.fragment
def render_settings_screen():
    col1, col2 = st.columns([1, 8])
    with col1:
        if st.button("＜ 戻る"):
            navigate_to("home")
    with col2:
        st.markdown('<h1 class="app-title">⚙️ 設定</h1>', unsafe_allow_html=True)

    sections = ["🏠 基本設定", "📝 プロンプト", "🤖 LLMプロバイダー"]
    section = st.segmented_control(
        "設定セクション",
        options=sections,
        default=sections[0],
        key="settings_console_section",
        label_visibility="collapsed",
        width="stretch",
    )

    # ========================
    # セクション1: 基本設定
    # ========================
    if section == sections[0]:
        st.subheader("🔗 API接続先 (サーバーアドレス)")
        st.caption("ラズパイなどのバックエンドサーバのアドレスを入力してください。")
        with st.form("settings_api_url_form"):
            new_url = st.text_input(
                "サーバアドレス (URL)",
                value=st.session_state.api_base_url,
                placeholder="http://127.0.0.1:8000",
            )
            save_url = st.form_submit_button("💾 接続先を保存")
        if save_url:
            normalized_url = normalize_api_url(new_url)
            st.session_state.api_base_url = normalized_url
            st.session_state.api_connection_error = None
            st.success(f"接続先を {normalized_url} に変更しました。")
            st.rerun()

        st.divider()
        st.subheader("🏖️ Holiday Mode (休暇設定)")
        if "streaming_enabled" not in st.session_state:
            st.session_state.streaming_enabled = True
        with st.form("settings_runtime_toggles_form"):
            is_holiday = st.toggle(
                "🏖️ 休暇モードを有効にする",
                value=st.session_state.is_holiday,
            )
            st.caption("有効にすると、AIは今日が休日であると認識して応答します。")

            st.divider()
            st.subheader("🔧 System Toggles")
            debug_mode = st.toggle(
                "🐛 Debug Mode",
                value=st.session_state.debug_mode,
            )
            streaming_enabled = st.toggle(
                "⚡ Streaming (応答を逐次表示)",
                value=st.session_state.streaming_enabled,
                help="OFF にすると従来の一括返却モードになります。",
            )
            save_runtime_toggles = st.form_submit_button("💾 動作設定を適用")
        if save_runtime_toggles:
            st.session_state.is_holiday = is_holiday
            st.session_state.debug_mode = debug_mode
            st.session_state.streaming_enabled = streaming_enabled
            st.success("動作設定を適用しました。")

        if st.button("🗑️ Clear Cache"):
            st.cache_resource.clear()
            st.cache_data.clear()
            st.success("Cache cleared!")

        st.divider()
        st.subheader("🌐 言語設定")
        import requests as _req_lang

        _lang_api_url = st.session_state.api_base_url
        try:
            _lang_cfg = _cached_api_json(_lang_api_url, "/config")
        except Exception:
            _lang_cfg = {"SYSTEM_CONFIG": {}}
        _lang_agent_s = _lang_cfg.get("SYSTEM_CONFIG", {}).get("agent", {})
        _locale_options = {"ja": "日本語", "en": "English"}
        _locale_keys = list(_locale_options.keys())
        _current_locale = _lang_agent_s.get("locale", "ja")
        _locale_index = (
            _locale_keys.index(_current_locale)
            if _current_locale in _locale_keys
            else 0
        )
        with st.form("settings_locale_form"):
            _selected_locale = st.selectbox(
                "Language / 言語",
                options=_locale_keys,
                index=_locale_index,
                format_func=lambda x: _locale_options.get(x, x),
                key="tab1_locale",
            )
            save_locale = st.form_submit_button("💾 言語設定を保存")
        if save_locale:
            _lang_cfg.setdefault("SYSTEM_CONFIG", {}).setdefault("agent", {})[
                "locale"
            ] = _selected_locale
            try:
                _r = _req_lang.post(
                    f"{_lang_api_url}/config", json=_lang_cfg, timeout=5
                )
                if _r.ok:
                    _cached_api_json.clear()
                    st.success("言語設定を保存しました。")
                else:
                    st.error(f"保存エラー: {_r.text}")
            except Exception as _e:
                st.error(f"サーバー接続エラー: {_e}")

    # ========================
    # セクション2: プロンプト編集
    # ========================
    elif section == sections[1]:
        st.subheader("📝 グローバルプロンプト編集")
        st.caption(
            "各インスタンス共通のタイムコンテキストなどのグローバルプロンプトを編集できます。"
        )
        import requests

        api_url = st.session_state.api_base_url
        try:
            raw_prompts = _cached_api_json(api_url, "/prompts")
            for key, val in raw_prompts.items():
                with st.expander(f"📌 {key}"):
                    with st.form(f"settings_prompt_form_{key}"):
                        new_val = st.text_area(
                            key, value=val, height=200, key=f"prompt_{key}"
                        )
                        save_prompt = st.form_submit_button("💾 保存")
                    if save_prompt:
                        update_resp = requests.post(
                            f"{api_url}/prompts", json={key: new_val}, timeout=5
                        )
                        if update_resp.ok:
                            _cached_api_json.clear()
                            st.success(f"{key} を保存しました。")
                        else:
                            st.error(f"保存エラー: {update_resp.text}")
        except Exception as e:
            st.error(f"サーバー接続エラー: {e}")

    # ========================
    # セクション3: LLMプロバイダー設定
    # ========================
    else:
        import requests

        api_url = st.session_state.api_base_url

        # --- セクション1: Connection / LLM APIキー管理 ---
        _provider_connections = _render_connection_manager(api_url)

        st.divider()

        # LLM Connectionではない検索サービス用キーは独立して管理する。
        with st.expander("🔎 Ollama Web Search APIキー"):
            try:
                api_key_status = _cached_api_json(
                    api_url,
                    "/settings/api_key_status",
                )
                search_key_set = bool(api_key_status.get("ollama_web_search"))
            except Exception:
                search_key_set = False
            st.caption("✅ 設定済み" if search_key_set else "❌ 未設定")
            search_key_widget = "provider_ollama_ws_key"
            with st.form("ollama_web_search_key_form"):
                ollama_ws_key = st.text_input(
                    "Ollama WebSearch API Key",
                    type="password",
                    value="",
                    key=search_key_widget,
                )
                save_ollama_ws_key = st.form_submit_button("💾 保存")
            if save_ollama_ws_key:
                if not ollama_ws_key:
                    st.warning("キーを入力してください。")
                else:
                    try:
                        response = requests.post(
                            f"{api_url}/settings/api_key",
                            json={
                                "api_key": ollama_ws_key,
                                "key_type": "ollama_web_search",
                            },
                            timeout=5,
                        )
                        if response.ok:
                            st.session_state.pop(search_key_widget, None)
                            _cached_api_json.clear()
                            st.success("APIキーを保存しました。")
                            st.rerun()
                        else:
                            st.error(_api_error_detail(response))
                    except Exception as exc:
                        st.error(f"保存エラー: {exc}")

        st.divider()

        # --- セクション2: Ollama接続設定 ---
        st.subheader("🖥️ Ollama (ローカルLLM)")

        # 現在の設定を取得
        try:
            provider_cfg = _cached_api_json(api_url, "/config")
        except Exception:
            provider_cfg = {"AI_CONFIG": {}, "SYSTEM_CONFIG": {}}

        # 保存済みの接続先を初期値にする（別PCの Ollama を指したまま維持する）
        try:
            saved_ollama = _cached_api_json(api_url, "/settings/ollama_url")
        except Exception:
            saved_ollama = {}
        current_ollama_url = saved_ollama.get("url") or "http://localhost:11434"

        with st.form("ollama_connection_form"):
            ollama_url = st.text_input(
                "接続先URL",
                value=current_ollama_url,
                placeholder="http://localhost:11434",
                key="ollama_url",
                help=(
                    "別PCのOllamaを使う場合は http://<ホスト>:11434 を指定して保存します。"
                    "保存先は DATA_DIR/.env の OLLAMA_BASE_URL で、再起動は不要です。"
                ),
            )
            ollama_actions = st.columns(2)
            with ollama_actions[0]:
                save_ollama_url = st.form_submit_button("💾 接続先を保存")
            with ollama_actions[1]:
                test_ollama = st.form_submit_button("🔍 接続テスト")
        if saved_ollama.get("source") == "default":
            st.caption("未保存（既定の localhost を使用中）")

        if save_ollama_url:
            try:
                resp = requests.post(
                    f"{api_url}/settings/ollama_url",
                    json={"url": ollama_url},
                    timeout=10,
                )
                if resp.ok:
                    _cached_api_json.clear()
                    st.success("接続先を保存しました。")
                    st.rerun()
                else:
                    st.error(_api_error_detail(resp))
            except Exception as e:
                st.error(f"保存エラー: {e}")
        if test_ollama:
            try:
                resp = requests.post(
                    f"{api_url}/settings/ollama_test",
                    json={"url": ollama_url},
                    timeout=10,
                )
                if resp.ok:
                    result = resp.json()
                    if result.get("status") == "ok":
                        models = ", ".join(result.get("models", [])) or "(なし)"
                        st.success(f"✅ Ollama 接続OK (利用可能モデル: {models})")
                    else:
                        st.error(f"❌ 接続失敗: {result.get('message', '')}")
                else:
                    st.error(f"サーバーエラー: {resp.text}")
            except Exception as e:
                st.error(f"接続エラー: {e}")

        st.divider()

        # --- セクション3: 各ロールのモデル選択 ---
        st.subheader("🧠 モデル割り当て")
        st.caption(
            "💡 候補は backend (`/settings/model_candidates`) から取得します。"
            "Connection を追加すると Groq / Together などのモデルも自動で並びます。"
        )
        _render_model_catalog_refresh(
            api_url,
            key="provider_model_catalog_refresh",
        )

        ROLE_LABELS = {
            "chat": "Chat (メイン応答)",
            "summary": "Summary (要約)",
            "knowledge": "Knowledge (知識抽出)",
            "gatekeeper": "Gatekeeper (Tier判定)",
            "embedding": "Embedding (ベクトル化)",
        }

        provider_ai_cfg = provider_cfg.get("AI_CONFIG", {})
        model_selections = {}

        for role in [
            "chat",
            "summary",
            "knowledge",
            "gatekeeper",
            "embedding",
        ]:
            current_role = provider_ai_cfg.get(role, {})
            current_model = current_role.get("model_name", "")
            selector_key = f"provider_model_{role}"
            _candidates = _get_selector_candidates(
                api_url,
                role,
                current_role.get("connection"),
                selector_key,
            )
            model_selections[role] = _model_selector(
                label=ROLE_LABELS[role],
                current_value=current_model,
                current_connection=current_role.get("connection"),
                candidates=_candidates,
                connections=_provider_connections,
                key_prefix=selector_key,
                embeddings_only=(role == "embedding"),
            )

        st.divider()
        st.subheader("🌡️ Temperature 設定")
        st.caption(
            "バックグラウンドロールの生成パラメータを設定します（Chat の Temperature はインスタンス設定から変更できます）。"
        )
        with st.form("provider_model_settings_form"):
            _TEMP_ROLES = {
                "summary": "Summary (要約)",
                "gatekeeper": "Gatekeeper (Tier判定)",
                "knowledge": "Knowledge (知識蒸留)",
            }
            _TEMP_DEFAULTS = {
                "summary": 0.3,
                "gatekeeper": 0.0,
                "knowledge": 0.7,
            }
            for _t_role, _t_label in _TEMP_ROLES.items():
                _mc = provider_ai_cfg.get(_t_role, {})
                _cur_temp = float(
                    _mc.get("generation_config", {}).get(
                        "temperature", _TEMP_DEFAULTS[_t_role]
                    )
                )
                _new_temp = st.slider(
                    _t_label,
                    0.0,
                    2.0,
                    value=_cur_temp,
                    step=0.1,
                    key=f"provider_temp_{_t_role}",
                )
                provider_ai_cfg.setdefault(_t_role, {}).setdefault(
                    "generation_config",
                    {},
                )["temperature"] = _new_temp
            save_provider_models = st.form_submit_button(
                "💾 モデル設定を保存",
                type="primary",
            )

        if save_provider_models:
            # 空白ガード
            empty_roles = [
                role
                for role, choice in model_selections.items()
                if not choice.model_name.strip()
            ]
            if empty_roles:
                st.error(
                    f"モデル名が未入力のロールがあります: {', '.join(empty_roles)}"
                )
            else:
                # provider_cfg のAI_CONFIGを更新
                for role, choice in model_selections.items():
                    set_model_choice(
                        provider_ai_cfg.setdefault(role, {}),
                        ModelChoice(
                            choice.connection_id,
                            choice.model_name.strip(),
                        ),
                    )
                provider_cfg["AI_CONFIG"] = provider_ai_cfg
                try:
                    save_resp = requests.post(
                        f"{api_url}/config", json=provider_cfg, timeout=5
                    )
                    if save_resp.ok:
                        _cached_api_json.clear()
                        st.success("モデル設定を保存しました。")
                    else:
                        st.error(f"保存エラー: {save_resp.text}")
                except Exception as e:
                    st.error(f"サーバー接続エラー: {e}")

        st.divider()

        # --- セクション4: Embedding再生成 ---
        st.subheader("🔄 Embeddingの再生成")
        st.caption(
            "Embeddingモデルを変更した場合、既存の記憶データベースのベクトルを再生成する必要があります。"
        )

        with st.form("reindex_embeddings_form"):
            reindex_target = st.selectbox(
                "対象インスタンス",
                ["__all__"] + available_instances,
                format_func=lambda x: "全インスタンス" if x == "__all__" else x,
                key="reindex_target",
            )
            run_reindex = st.form_submit_button("🔄 再生成を実行")
        if run_reindex:
            try:
                resp = requests.post(
                    f"{api_url}/settings/reindex_embeddings",
                    json={"instance_name": reindex_target},
                    timeout=10,
                )
                if resp.ok:
                    st.success(
                        f"Embedding再生成を開始しました。(対象: {reindex_target})"
                    )
                else:
                    st.error(f"エラー: {resp.text}")
            except Exception as e:
                st.error(f"サーバー接続エラー: {e}")


# ==========================================
# 🔗 LINE pairing 管理画面
# ==========================================
def render_pairing_screen():
    import requests

    api_url = st.session_state.api_base_url
    col1, col2 = st.columns([1, 8])
    with col1:
        if st.button("＜ 戻る", key="pairing_back"):
            navigate_to("home")
    with col2:
        st.markdown('<h1 class="app-title">🔗 LINE連携</h1>', unsafe_allow_html=True)

    st.caption(
        "LINEに表示された6桁コードを確認し、接続する Butly instance を選んで承認します。"
    )
    try:
        response = requests.get(f"{api_url}/pairing/pending", timeout=5)
        if not response.ok:
            st.error(f"pairing 一覧の取得に失敗しました: {response.text}")
            return
        payload = response.json()
    except Exception as exc:
        st.error(f"FastAPIサーバーに接続できません: {exc}")
        return

    pending = payload.get("pending", [])
    instances = payload.get("instances", [])
    default_instance = payload.get("default_instance")
    if not pending:
        st.info("承認待ちの LINE アカウントはありません。")
        return
    if not instances:
        st.warning("承認先にできる Butly instance がありません。")
        return

    default_index = (
        instances.index(default_instance) if default_instance in instances else 0
    )

    def post_pairing_action(action, body):
        try:
            result = requests.post(
                f"{api_url}/pairing/{action}", json=body, timeout=5
            )
        except Exception as exc:
            st.error(f"操作に失敗しました: {exc}")
            return False
        if not result.ok:
            st.error(f"操作に失敗しました: {result.text}")
            return False
        return True

    for record in pending:
        code = record.get("code", "")
        user_id = record.get("external_user_id", "")
        masked_user = f"…{user_id[-8:]}" if len(user_id) > 8 else user_id
        with st.container(border=True):
            st.subheader(f"連携コード: {code}")
            st.caption(
                f"source: {record.get('source', '?')} / user: {masked_user} / "
                f"expires: {record.get('expires_at', '?')}"
            )
            selected = st.selectbox(
                "接続先 instance",
                instances,
                index=default_index,
                key=f"pairing_instance_{code}",
            )
            approve_col, reject_col = st.columns(2)
            with approve_col:
                if st.button("承認", type="primary", key=f"pairing_approve_{code}"):
                    if post_pairing_action(
                        "approve", {"code": code, "instance_name": selected}
                    ):
                        st.success("LINEアカウントを連携しました。")
                        st.rerun()
            with reject_col:
                if st.button("却下", key=f"pairing_reject_{code}"):
                    if post_pairing_action("reject", {"code": code}):
                        st.success("pairing request を却下しました。")
                        st.rerun()


# ==========================================
# 📊 LoCoMo評価画面
# ==========================================
_EVALUATION_ACTIVE_STATUSES = {"queued", "running", "stopping"}
_EVALUATION_RESUMABLE_STATUSES = {"stopped", "failed", "interrupted"}


def _evaluation_option_index(options: list, value) -> int:
    """Return a safe Streamlit selectbox index for a persisted value."""
    try:
        return options.index(value)
    except ValueError:
        return 0


def _evaluation_next_run_id(previous_run_id: str, existing_run_ids: list) -> str:
    """Suggest a unique run id while preserving a trailing ``_vNN`` series."""
    existing = {str(run_id) for run_id in existing_run_ids if run_id}
    match = re.fullmatch(r"(.+_v)(\d+)", previous_run_id or "")
    if match:
        prefix, digits = match.groups()
        number = int(digits) + 1
        while True:
            candidate = f"{prefix}{number:0{len(digits)}d}"
            if candidate not in existing:
                return candidate
            number += 1

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = f"web_eval_{timestamp}"
    suffix = 2
    while candidate in existing:
        candidate = f"web_eval_{timestamp}_{suffix}"
        suffix += 1
    return candidate


@st.cache_data(ttl=10, show_spinner=False)
def _evaluation_get(api_url: str, path: str) -> dict:
    import requests

    response = requests.get(f"{api_url}{path}", timeout=10)
    if not response.ok:
        raise RuntimeError(_api_error_detail(response))
    return response.json()


@st.cache_data(ttl=60, show_spinner=False)
def _evaluation_dataset_samples(api_url: str, dataset_path: str) -> dict:
    """Fetch validated LoCoMo sample metadata for the exact-ID selector."""
    import requests

    response = requests.get(
        f"{api_url}/evaluations/datasets/samples",
        params={"dataset_path": dataset_path},
        timeout=15,
    )
    if not response.ok:
        raise RuntimeError(_api_error_detail(response))
    return response.json()


def _evaluation_role_models(
    api_url: str,
    provider_config: dict,
    connections: list,
    *,
    key_prefix: str = "evaluation_model",
) -> dict[str, ModelChoice]:
    role_labels = {
        "chat": "Chat（評価対象の応答）",
        "gatekeeper": "Gatekeeper（検索判定）",
        "summary": "Summary（要約）",
        "knowledge": "Knowledge（カード生成）",
        "embedding": "Embedding（検索ベクトル）",
    }
    choices = {}
    for role, label in role_labels.items():
        current = provider_config.get(role, {})
        selector_key = f"{key_prefix}_{role}"
        choices[role] = _model_selector(
            label=label,
            current_value=current.get("model_name", ""),
            current_connection=current.get("connection"),
            candidates=_get_selector_candidates(
                api_url,
                role,
                current.get("connection"),
                selector_key,
            ),
            connections=connections,
            key_prefix=selector_key,
            embeddings_only=(role == "embedding"),
        )
    return choices


def _evaluation_judge_model(
    api_url: str,
    current: dict,
    connections: list,
    *,
    key_prefix: str,
) -> ModelChoice:
    """Render an evaluation-only judge selector using chat-capable models."""
    current = current if isinstance(current, dict) else {}
    return _model_selector(
        label="Semantic Judge（回答の意味判定）",
        current_value=str(current.get("model_name") or ""),
        current_connection=current.get("connection"),
        candidates=_get_selector_candidates(
            api_url,
            "chat",
            current.get("connection"),
            key_prefix,
        ),
        connections=connections,
        key_prefix=key_prefix,
    )


def _evaluation_reranker_model(
    api_url: str,
    current: dict,
    connections: list,
    *,
    key_prefix: str,
) -> ModelChoice:
    """Render the retrieval reranker selector using chat-capable models."""
    current = current if isinstance(current, dict) else {}
    return _model_selector(
        label="Memory Reranker（候補20件→注入3件）",
        current_value=str(current.get("model_name") or ""),
        current_connection=current.get("connection"),
        candidates=_get_selector_candidates(
            api_url,
            "chat",
            current.get("connection"),
            key_prefix,
        ),
        connections=connections,
        key_prefix=key_prefix,
    )


def _evaluation_cross_encoder_model(
    current_model: str,
    *,
    key: str,
) -> str:
    """Render the reviewed local multilingual reranker presets."""
    from butly_core.core.reranker import CROSS_ENCODER_MODELS

    options = [spec.model_name for spec in CROSS_ENCODER_MODELS]
    labels = {spec.model_name: spec.label for spec in CROSS_ENCODER_MODELS}
    selected = current_model if current_model in options else options[0]
    return st.selectbox(
        "Cross-Encoder model",
        options=options,
        index=_evaluation_option_index(options, selected),
        format_func=lambda value: labels[value],
        key=key,
    )


def _evaluation_embedding_profile(
    model_name: str,
    current_profile: str | None = None,
    *,
    widget_key: str = "evaluation_embedding_profile",
) -> str | None:
    """Embedding の prefix 規約セレクタ。戻り値 None は auto（モデル名から推定）。

    prefix を付け忘れると埋め込みが 1 つの円錐に潰れて検索が機能しなくなる。
    通常は auto で足りるが、名前から判別できないモデル用に手動指定を残す。
    """
    from butly_core.llm.embedding_profiles import describe, list_profiles

    options = ["auto"] + [p.id for p in list_profiles()]
    selected_profile = current_profile or "auto"
    if selected_profile not in options:
        options.append(selected_profile)
    selected = st.selectbox(
        "Embedding prefix 規約",
        options,
        index=_evaluation_option_index(options, selected_profile),
        key=widget_key,
        help=(
            "auto はモデル名から推定します（nomic → search_query:/"
            "search_document:、E5 → query:/passage: など）。"
            "推定できないモデルだけ手動で選んでください。"
        ),
    )
    conf = {"model_name": model_name}
    if selected != "auto":
        conf["profile"] = selected
    st.caption(f"適用される規約: {describe(conf)}")
    return None if selected == "auto" else selected


@st.fragment
def _render_evaluation_start_form(
    api_url: str,
    evaluation_config: dict,
    runs: list,
) -> None:
    import requests

    st.subheader("新しい評価")
    st.caption(
        "既存のLoCoMo CLIとcheckpointを利用します。APIキーは通常の"
        "Connection設定を共有し、評価runには保存しません。"
    )

    dataset_candidates = evaluation_config.get("dataset_candidates") or []
    run_ids = [
        run.get("run_id")
        for run in runs
        if run.get("run_id")
    ]
    previous_request = evaluation_config.get("last_request")
    if not isinstance(previous_request, dict):
        previous_request = {}
    previous_run_id = str(previous_request.get("run_id") or "")

    pending_run_id = st.session_state.pop(
        "evaluation_pending_run_id",
        None,
    )
    if pending_run_id:
        st.session_state.evaluation_run_id = pending_run_id
        st.session_state.evaluation_default_run_id = pending_run_id
        # このチェックは検索結果を壊し得る危険な承知操作なので、runごとに確認する。
        st.session_state.pop(
            "evaluation_allow_embedding_mismatch",
            None,
        )

    if "evaluation_dataset_path" not in st.session_state:
        st.session_state.evaluation_dataset_path = (
            str(previous_request.get("dataset_path") or "")
            or (dataset_candidates[0] if dataset_candidates else "")
        )
    if "evaluation_default_run_id" not in st.session_state:
        st.session_state.evaluation_default_run_id = _evaluation_next_run_id(
            previous_run_id,
            run_ids,
        )
    if "evaluation_run_id" not in st.session_state:
        st.session_state.evaluation_run_id = (
            st.session_state.evaluation_default_run_id
        )

    if previous_run_id:
        st.caption(
            f"前回の評価 `{previous_run_id}` の設定を引き継いでいます。"
            "RUN_IDは次の候補へ更新し、埋め込み不一致の承知チェックだけは"
            "安全のため毎回OFFに戻します。"
        )

    if dataset_candidates:
        selected_dataset = st.selectbox(
            "検出済みデータセット",
            options=dataset_candidates,
            key="evaluation_dataset_candidate",
        )
        if st.button(
            "このパスを使用",
            key="evaluation_use_dataset_candidate",
        ):
            st.session_state.evaluation_dataset_path = selected_dataset
            try:
                detected_locale = _evaluation_dataset_samples(
                    api_url,
                    selected_dataset,
                ).get("locale")
            except (requests.RequestException, RuntimeError, ValueError) as exc:
                st.warning(f"データセット言語を判定できません: {exc}")
                detected_locale = None
            if detected_locale in {"en", "ja"}:
                st.session_state.evaluation_locale = detected_locale
            st.rerun()
    dataset_path = st.text_input(
        "LoCoMo dataset path",
        key="evaluation_dataset_path",
        help="Backendから読み取れるJSONファイルの絶対パスを指定します。",
    )

    workflow_options = evaluation_config.get("workflows") or [
        "full",
        "retrieval_prep",
    ]
    workflow = st.selectbox(
        "実行内容",
        options=workflow_options,
        index=_evaluation_option_index(
            workflow_options,
            previous_request.get("workflow", "full"),
        ),
        format_func=lambda value: (
            "通常評価（Replay → Sleeptime → QA → 採点）"
            if value == "full"
            else "検索比較用（Replay → Sleeptime、QA生成なし）"
        ),
        key="evaluation_workflow",
    )
    retrieval_prep = workflow == "retrieval_prep"
    workflow_state_key = "evaluation_previous_workflow"
    if st.session_state.get(workflow_state_key) != workflow:
        for widget_key in (
            "evaluation_run_mode",
            "evaluation_qa_mode",
            "evaluation_source_run",
            "evaluation_judge_enabled",
            "evaluation_all_question",
        ):
            st.session_state.pop(widget_key, None)
        st.session_state[workflow_state_key] = workflow
    if retrieval_prep:
        st.info(
            "会話から記憶カードを作り、LoCoMoの質問一覧を固定したところで"
            "終了します。回答生成・公式採点・Semantic Judgeは実行せず、"
            "完了後に下の「検索だけ比較」から各検索方式を試せます。"
        )

    top_cols = st.columns(2)
    with top_cols[0]:
        run_id = st.text_input(
            "RUN_ID",
            key="evaluation_run_id",
        )
        run_mode_options = evaluation_config.get("run_modes") or [
            "standard",
            "stage3-full",
            "stage3-source",
            "stage3-off",
            "stage3-on",
        ]
        if retrieval_prep:
            run_mode_options = [
                value
                for value in run_mode_options
                if value not in {"stage3-off", "stage3-on"}
            ]
        run_mode = st.selectbox(
            "RUN_MODE",
            options=run_mode_options,
            index=_evaluation_option_index(
                run_mode_options,
                previous_request.get("run_mode", "standard"),
            ),
            key="evaluation_run_mode",
        )
    with top_cols[1]:
        formal_stage3 = run_mode in {
            "stage3-source",
            "stage3-off",
            "stage3-on",
        }
        if (
            (formal_stage3 or retrieval_prep)
            and st.session_state.get("evaluation_qa_mode")
            not in {None, "independent"}
        ):
            st.session_state.pop("evaluation_qa_mode", None)
        qa_mode_options = (
            ["independent"]
            if formal_stage3 or retrieval_prep
            else ["independent", "sequential"]
        )
        qa_mode = st.selectbox(
            "QA_MODE",
            options=qa_mode_options,
            index=_evaluation_option_index(
                qa_mode_options,
                previous_request.get("qa_mode", "independent"),
            ),
            key="evaluation_qa_mode",
        )
        locale_options = ["en", "ja"]
        locale = st.selectbox(
            "LOCALE",
            options=locale_options,
            index=_evaluation_option_index(
                locale_options,
                previous_request.get("locale", "en"),
            ),
            key="evaluation_locale",
        )

    source_required = (
        not retrieval_prep and run_mode in {"stage3-off", "stage3-on"}
    )
    source_allowed = (
        not retrieval_prep
        and run_mode not in {"stage3-full", "stage3-source"}
    )
    source_memory_run_id = ""
    if source_allowed:
        source_options = ["", *run_ids]
        previous_source = str(
            previous_request.get("source_memory_run_id") or ""
        )
        source_memory_run_id = st.selectbox(
            "SOURCE_MEMORY_RUN_ID",
            options=source_options,
            index=_evaluation_option_index(
                source_options,
                previous_source,
            ),
            format_func=lambda value: value or "（新規にReplay/Sleeptimeを実行）",
            key="evaluation_source_run",
            help=(
                "指定すると元runのカードを複製し、QAだけを実行します。"
            ),
        )
        if source_required and not source_memory_run_id:
            st.warning(f"{run_mode} ではSOURCE_MEMORY_RUN_IDが必須です。")
    allow_embedding_mismatch = False
    if source_memory_run_id:
        allow_embedding_mismatch = st.checkbox(
            "埋め込み不一致を承知で実行する",
            value=False,
            key="evaluation_allow_embedding_mismatch",
            help=(
                "元runのカードは、そのとき使った埋め込みモデルと prefix 規約で"
                "ベクトル化されています。現在の設定と食い違うと、保存済み"
                "ベクトルと検索クエリが別空間になり検索が無言で壊れます。"
                "既定では開始前に弾きます。"
            ),
        )
    elif retrieval_prep:
        st.caption("検索比較用runは新規にReplay/Sleeptimeを実行します。")
    elif run_mode == "stage3-full":
        st.info("Stage3ノード作成とQAを1回のrunで実行します。")
    else:
        st.info("Stage3 OFFの正本カードを作成するsource runです。")

    st.markdown("#### 評価範囲")
    dataset_samples = []
    if dataset_path.strip():
        try:
            dataset_samples = _evaluation_dataset_samples(
                api_url,
                dataset_path.strip(),
            ).get("samples") or []
        except Exception as exc:
            st.warning(f"sample ID一覧を取得できません: {exc}")
    sample_options = [
        str(item.get("sample_id"))
        for item in dataset_samples
        if item.get("sample_id")
    ]
    sample_metadata = {
        str(item.get("sample_id")): item
        for item in dataset_samples
        if item.get("sample_id")
    }
    sample_path_key = "evaluation_sample_ids_dataset_path"
    if st.session_state.get(sample_path_key) != dataset_path.strip():
        st.session_state.pop("evaluation_sample_ids", None)
        st.session_state[sample_path_key] = dataset_path.strip()
    if not sample_options:
        st.session_state.pop("evaluation_sample_ids", None)
    previous_sample_ids = previous_request.get("sample_ids") or []
    sample_defaults = [
        str(sample_id)
        for sample_id in previous_sample_ids
        if str(sample_id) in sample_options
    ]
    selected_sample_ids = st.multiselect(
        "Sample IDs（複数選択可）",
        options=sample_options,
        default=sample_defaults,
        format_func=lambda sample_id: (
            f"{sample_id} — "
            f"{sample_metadata[sample_id].get('session_count', 0)} sessions / "
            f"{sample_metadata[sample_id].get('question_count', 0)} questions"
        ),
        key="evaluation_sample_ids",
        disabled=bool(source_memory_run_id),
        help=(
            "選択したIDだけを実行します。未選択なら従来どおり"
            "Samples limit / ALL_SAMPLESを使います。"
        ),
    )
    if selected_sample_ids:
        st.caption(
            f"選択中: {', '.join(selected_sample_ids)}。"
            "Samples limitよりこちらを優先します。"
        )
    scope_values = {}
    scope_cols = st.columns(3)
    scope_defaults = {
        "sample": (False, 1),
        "session": (False, 3),
        "question": (retrieval_prep, 10),
    }
    scope_labels = {
        "sample": "Samples",
        "session": "Sessions",
        "question": "Questions",
    }
    for column, (dimension, defaults) in zip(
        scope_cols,
        scope_defaults.items(),
    ):
        previous_limit = previous_request.get(f"{dimension}_limit")
        if f"{dimension}_limit" in previous_request:
            use_all_default = previous_limit is None
            limit_default = (
                defaults[1]
                if previous_limit is None
                else int(previous_limit)
            )
        else:
            use_all_default, limit_default = defaults
        exact_samples_selected = (
            dimension == "sample" and bool(selected_sample_ids)
        )
        with column:
            use_all = st.checkbox(
                f"ALL_{scope_labels[dimension].upper()}",
                value=use_all_default,
                key=f"evaluation_all_{dimension}",
                disabled=(
                    exact_samples_selected
                    or (
                        bool(source_memory_run_id)
                        and dimension in {"sample", "session"}
                    )
                ),
            )
            limit = st.number_input(
                f"{scope_labels[dimension]} limit",
                min_value=1,
                value=limit_default,
                step=1,
                key=f"evaluation_{dimension}_limit",
                disabled=use_all
                or exact_samples_selected
                or (
                    bool(source_memory_run_id)
                    and dimension in {"sample", "session"}
                ),
            )
            scope_values[dimension] = (
                None if use_all or exact_samples_selected else int(limit)
            )

    with st.expander("RAG・コンテキスト設定", expanded=True):
        context_cols = st.columns(4)
        with context_cols[0]:
            context_current_time = st.checkbox(
                "Current Time",
                value=bool(
                    previous_request.get("context_current_time", True)
                ),
                key="evaluation_context_current_time",
            )
        with context_cols[1]:
            context_mid_term = st.checkbox(
                "Mid-term",
                value=bool(
                    previous_request.get("context_mid_term", True)
                ),
                key="evaluation_context_mid_term",
            )
        with context_cols[2]:
            context_session_digest = st.checkbox(
                "Session Digest",
                value=bool(
                    previous_request.get("context_session_digest", True)
                ),
                key="evaluation_context_session_digest",
            )
        with context_cols[3]:
            context_rag = st.checkbox(
                "RAG",
                value=bool(previous_request.get("context_rag", True)),
                key="evaluation_context_rag",
            )
        rag_cols = st.columns(5)
        with rag_cols[0]:
            rag_source_options = ["both", "cards", "raw"]
            rag_source_mode = st.selectbox(
                "RAG source",
                options=rag_source_options,
                index=_evaluation_option_index(
                    rag_source_options,
                    previous_request.get("rag_source_mode", "both"),
                ),
                key="evaluation_rag_source_mode",
            )
        with rag_cols[1]:
            rag_raw_top_k = st.number_input(
                "RAW top-k",
                min_value=0,
                value=int(previous_request.get("rag_raw_top_k", 1)),
                step=1,
                key="evaluation_rag_raw_top_k",
            )
        with rag_cols[2]:
            rag_raw_max_chars = st.number_input(
                "RAW max chars",
                min_value=0,
                value=int(
                    previous_request.get("rag_raw_max_chars", 2500)
                ),
                step=100,
                key="evaluation_rag_raw_max_chars",
            )
        with rag_cols[3]:
            rag_raw_neighbor_radius = st.number_input(
                "RAW neighbor ±N",
                min_value=0,
                max_value=10,
                value=int(
                    previous_request.get("rag_raw_neighbor_radius", 0)
                ),
                step=1,
                help=(
                    "0で無効。1なら正確なsource_filesを優先した後、"
                    "同一source_date内の前後1ファイルを追加します。"
                ),
                key="evaluation_rag_raw_neighbor_radius",
            )
        with rag_cols[4]:
            time_decay_rate = st.number_input(
                "Time decay rate",
                min_value=0.0,
                value=float(
                    previous_request.get("time_decay_rate", 0.0)
                ),
                step=0.01,
                key="evaluation_time_decay_rate",
            )

    previous_search_models = previous_request.get("role_models") or {}
    previous_reranker = (
        previous_search_models.get("reranker") or {}
        if isinstance(previous_search_models, dict)
        else {}
    )
    reranker_enabled = False
    reranker_candidate_limit = int(
        previous_request.get("reranker_candidate_limit", 20)
    )
    reranker_max_candidate_chars = int(
        previous_request.get("reranker_max_candidate_chars", 1600)
    )
    previous_reranker_engine = str(
        previous_reranker.get("engine")
        or ("llm" if previous_reranker else "cross_encoder")
    )
    reranker_engine = previous_reranker_engine
    reranker_batch_size = int(previous_reranker.get("batch_size") or 20)
    reranker_device = str(previous_reranker.get("device") or "auto")
    reranker_threshold_enabled = (
        previous_reranker.get("score_threshold") is not None
    )
    reranker_score_threshold = float(
        previous_reranker.get("score_threshold") or 0.0
    )
    with st.expander("検索設定（Dual Query / Hybrid / Reranker）", expanded=False):
        st.caption(
            "search_mode=hybrid で BM25(FTS5/trigram) とベクトルを RRF 融合する。"
            "hybrid_evidence_fusion はその上位候補をEpisode/RAWで再評価し、"
            "hybrid順位を主軸に融合する。"
            "dual_query は元発話とGatekeeper検索文を各15件検索してRRF融合する。"
            "検索の実行と注入判定は別設定。"
        )
        search_cols = st.columns(4)
        with search_cols[0]:
            search_mode_options = (
                evaluation_config.get("search_modes")
                or [
                    "vector",
                    "hybrid",
                    "dual_query",
                    "hybrid_evidence_fusion",
                ]
            )
            search_mode = st.selectbox(
                "Search mode",
                options=search_mode_options,
                index=_evaluation_option_index(
                    search_mode_options,
                    previous_request.get("search_mode", "vector"),
                ),
                format_func=lambda value: _QA_SEARCH_MODE_LABELS.get(
                    value, value
                ),
                key="evaluation_search_mode",
            )
        with search_cols[1]:
            retrieval_execution_options = (
                evaluation_config.get("retrieval_executions")
                or ["always", "intent_gated"]
            )
            retrieval_execution = st.selectbox(
                "Retrieval execution",
                options=retrieval_execution_options,
                index=_evaluation_option_index(
                    retrieval_execution_options,
                    previous_request.get(
                        "retrieval_execution",
                        "always",
                    ),
                ),
                key="evaluation_retrieval_execution",
                help="always=全質問で検索 / intent_gated=分類器が past_fact 等の時だけ",
            )
        with search_cols[2]:
            injection_policy_options = (
                evaluation_config.get("injection_policies")
                or ["intent_gated", "retrieval_assisted", "candidates"]
            )
            injection_policy = st.selectbox(
                "Injection policy",
                options=injection_policy_options,
                index=_evaluation_option_index(
                    injection_policy_options,
                    previous_request.get(
                        "injection_policy",
                        "intent_gated",
                    ),
                ),
                key="evaluation_injection_policy",
                help=(
                    "retrieval_assisted は分類器 null でもベクトルと BM25 の"
                    "両方が支持した候補を注入する（hybrid 専用）。"
                    "candidates は候補があれば注入する"
                ),
            )
        with search_cols[3]:
            vector_search_limit = st.number_input(
                "注入カード上限 (top-k)",
                min_value=1,
                value=int(previous_request.get("vector_search_limit", 3)),
                step=1,
                key="evaluation_vector_search_limit",
                help=(
                    "候補から実際に回答へ渡すカード枚数。既定3。"
                    "候補数(BM25/Vector candidates)とは別で、"
                    "Recall@k の k はこの値に対応する"
                ),
            )
        bm25_candidates = int(
            previous_request.get("bm25_candidates", 20)
        )
        vector_candidates = int(
            previous_request.get("vector_candidates", 20)
        )
        dual_query_candidates = int(
            previous_request.get("dual_query_candidates", 15)
        )
        dual_query_pool_limit = int(
            previous_request.get("dual_query_pool_limit", 25)
        )
        rrf_k = int(previous_request.get("rrf_k", 60))
        bm25_max_df_ratio = float(
            previous_request.get("bm25_max_df_ratio", 0.5)
        )
        evidence_fusion_base_weight = float(
            previous_request.get("evidence_fusion_base_weight", 0.7)
        )
        evidence_raw_chunk_chars = int(
            previous_request.get("evidence_raw_chunk_chars", 1800)
        )
        if search_mode in {"hybrid", "hybrid_evidence_fusion"}:
            hybrid_cols = st.columns(4)
            with hybrid_cols[0]:
                bm25_candidates = st.number_input(
                    "BM25 candidates",
                    min_value=1,
                    value=bm25_candidates,
                    step=1,
                    key="evaluation_bm25_candidates",
                )
            with hybrid_cols[1]:
                vector_candidates = st.number_input(
                    "Vector candidates",
                    min_value=1,
                    value=vector_candidates,
                    step=1,
                    key="evaluation_vector_candidates",
                )
            with hybrid_cols[2]:
                rrf_k = st.number_input(
                    "RRF k",
                    min_value=1,
                    value=rrf_k,
                    step=1,
                    key="evaluation_rrf_k",
                )
            with hybrid_cols[3]:
                bm25_max_df_ratio = st.number_input(
                    "BM25 max df ratio",
                    min_value=0.05,
                    max_value=1.0,
                    value=bm25_max_df_ratio,
                    step=0.05,
                    key="evaluation_bm25_max_df_ratio",
                )
            if search_mode == "hybrid_evidence_fusion":
                fusion_cols = st.columns(2)
                with fusion_cols[0]:
                    evidence_fusion_base_weight = st.number_input(
                        "Fusion hybrid weight",
                        min_value=0.0,
                        max_value=1.0,
                        value=evidence_fusion_base_weight,
                        step=0.05,
                        key="evaluation_evidence_fusion_base_weight",
                        help="0.70ならhybrid順位70%、Evidence順位30%です。",
                    )
                with fusion_cols[1]:
                    evidence_raw_chunk_chars = st.number_input(
                        "Evidence RAW chunk chars",
                        min_value=200,
                        max_value=10000,
                        value=evidence_raw_chunk_chars,
                        step=100,
                        key="evaluation_evidence_raw_chunk_chars",
                    )
                st.caption(
                    f"QA設定: hybrid weight="
                    f"{float(evidence_fusion_base_weight):.2f}。"
                    f"BM25/Vectorから統合した最大"
                    f"{max(int(bm25_candidates), int(vector_candidates))}件を"
                    f"Episode/RAW Evidenceで順位付けし、"
                    f"最終top{int(vector_search_limit)}だけを注入します。"
                    "初回結果はrun内へvector/hashのみ保存し、以後の質問で再利用します。"
                    "質問と候補Episode/RAWは選択中のEmbedding Connectionへ送信されます。"
                    "API keyは通常の認証だけに使い、cacheや評価artifactへ保存しません。"
                    "処理失敗時は通常のhybrid順位へ戻ります。"
                )
        elif search_mode == "dual_query":
            dual_cols = st.columns(3)
            with dual_cols[0]:
                dual_query_candidates = st.number_input(
                    "Candidates / query",
                    min_value=1,
                    value=dual_query_candidates,
                    step=1,
                    key="evaluation_dual_query_candidates",
                    help="元発話とGatekeeper検索文からそれぞれ取得する件数",
                )
            with dual_cols[1]:
                dual_query_pool_limit = st.number_input(
                    "Deduped pool limit",
                    min_value=1,
                    value=dual_query_pool_limit,
                    step=1,
                    key="evaluation_dual_query_pool_limit",
                    help="RRF融合・重複排除後に診断へ残す最大件数",
                )
            with dual_cols[2]:
                rrf_k = st.number_input(
                    "RRF k",
                    min_value=1,
                    value=rrf_k,
                    step=1,
                    key="evaluation_dual_query_rrf_k",
                )
            st.caption(
                "追加の生成LLMは呼ばず、通常のGatekeeper出力に検索文を含めます。"
                "検索文が無い場合は元発話だけの順位へフォールバックします。"
            )
        if search_mode == "vector":
            st.markdown("##### Memory Reranker（任意）")
            reranker_enabled = st.checkbox(
                "Vector上位候補をリランクしてから注入する",
                value=bool(previous_reranker),
                key="evaluation_reranker_enabled",
                help=(
                    "vector top-Nを別モデルで順位付けし、通常どおり上位3件を"
                    "注入します。Cross-Encoderは生成LLMを呼びません。"
                ),
            )
            if reranker_enabled:
                reranker_engine_options = ["cross_encoder", "llm"]
                reranker_engine = st.selectbox(
                    "Reranker engine",
                    options=reranker_engine_options,
                    index=_evaluation_option_index(
                        reranker_engine_options, previous_reranker_engine
                    ),
                    format_func=lambda value: (
                        "Cross-Encoder（推奨）"
                        if value == "cross_encoder"
                        else "生成LLM（旧方式・比較用）"
                    ),
                    key="evaluation_reranker_engine",
                )
                reranker_cols = st.columns(2)
                with reranker_cols[0]:
                    reranker_candidate_limit = st.number_input(
                        "Reranker candidate pool",
                        min_value=3,
                        max_value=100,
                        value=reranker_candidate_limit,
                        step=1,
                        key="evaluation_reranker_candidate_limit",
                    )
                with reranker_cols[1]:
                    reranker_max_candidate_chars = st.number_input(
                        "Max chars / candidate",
                        min_value=100,
                        max_value=10000,
                        value=reranker_max_candidate_chars,
                        step=100,
                        key="evaluation_reranker_max_candidate_chars",
                    )
                if reranker_engine == "cross_encoder":
                    cross_cols = st.columns(2)
                    with cross_cols[0]:
                        reranker_batch_size = st.number_input(
                            "Cross-Encoder batch size",
                            min_value=1,
                            max_value=100,
                            value=reranker_batch_size,
                            step=1,
                            key="evaluation_reranker_batch_size",
                        )
                    with cross_cols[1]:
                        device_options = ["auto", "cpu", "cuda", "mps"]
                        reranker_device = st.selectbox(
                            "Cross-Encoder device",
                            options=device_options,
                            index=_evaluation_option_index(
                                device_options, reranker_device
                            ),
                            key="evaluation_reranker_device",
                        )
                    reranker_threshold_enabled = st.checkbox(
                        "関連度しきい値を使い、該当なしなら0枚にする",
                        value=reranker_threshold_enabled,
                        key="evaluation_reranker_threshold_enabled",
                        help=(
                            "モデルのraw scoreは確率とは限りません。"
                            "offline評価で校正するまではOFFを推奨します。"
                        ),
                    )
                    if reranker_threshold_enabled:
                        reranker_score_threshold = st.number_input(
                            "Cross-Encoder score threshold",
                            value=reranker_score_threshold,
                            step=0.1,
                            key="evaluation_reranker_score_threshold",
                        )
                st.caption(
                    "推奨初期値: vector top20 → reranker → top3。"
                    "失敗時は元のvector順位へ自動フォールバックします。"
                )
        else:
            st.info(
                "Rerankerはvector検索専用です。Hybrid / Dual Queryとは"
                "同時に使えません。"
            )

    stage3_batch_size = int(
        previous_request.get("stage3_batch_size", 10)
    )
    stage3_bootstrap_max_cards = int(
        previous_request.get("stage3_bootstrap_max_cards", 2000)
    )
    if run_mode != "standard":
        with st.expander("Stage3設定"):
            stage3_cols = st.columns(2)
            with stage3_cols[0]:
                stage3_batch_size = st.number_input(
                    "Batch size",
                    min_value=1,
                    value=stage3_batch_size,
                    step=1,
                    key="evaluation_stage3_batch_size",
                )
            with stage3_cols[1]:
                stage3_bootstrap_max_cards = st.number_input(
                    "Bootstrap max cards",
                    min_value=1,
                    value=stage3_bootstrap_max_cards,
                    step=10,
                    key="evaluation_stage3_bootstrap_max_cards",
                )

    try:
        provider_config = _cached_api_json(api_url, "/config").get(
            "AI_CONFIG",
            {},
        )
    except Exception:
        provider_config = {}
    provider_config = {
        role: dict(config)
        for role, config in provider_config.items()
        if isinstance(config, dict)
    }
    previous_role_models = previous_request.get("role_models") or {}
    if isinstance(previous_role_models, dict):
        for role, config in previous_role_models.items():
            if isinstance(config, dict):
                provider_config[role] = dict(config)
    connections = _get_connections(api_url)

    with st.expander("モデル割り当て", expanded=True):
        role_choices = _evaluation_role_models(
            api_url,
            provider_config,
            connections,
        )
        embedding_profile = _evaluation_embedding_profile(
            role_choices["embedding"].model_name,
            provider_config.get("embedding", {}).get("profile"),
        )
        st.markdown("##### Temperature / output")
        generation_values = {}
        generation_cols = st.columns(4)
        temperatures = {
            "chat": 0.7,
            "gatekeeper": 0.0,
            "summary": 0.3,
            "knowledge": 0.2,
        }
        for column, (role, fallback) in zip(
            generation_cols,
            temperatures.items(),
        ):
            current_generation = (
                provider_config.get(role, {}).get("generation_config", {})
            )
            with column:
                temperature = st.number_input(
                    f"{role} temperature",
                    min_value=0.0,
                    max_value=2.0,
                    value=float(
                        current_generation.get("temperature", fallback)
                    ),
                    step=0.1,
                    key=f"evaluation_temperature_{role}",
                )
                generation_values[role] = {
                    "temperature": float(temperature)
                }
        gatekeeper_max_tokens = st.number_input(
            "Gatekeeper max output tokens",
            min_value=1,
            value=int(
                provider_config.get("gatekeeper", {})
                .get("generation_config", {})
                .get("max_output_tokens", 2048)
            ),
            step=128,
            key="evaluation_gatekeeper_max_tokens",
            help="Reasoningモデルでは2048程度を推奨します。",
        )
        generation_values["gatekeeper"]["max_output_tokens"] = int(
            gatekeeper_max_tokens
        )
        from evals.locomo.web_jobs import gatekeeper_token_warning

        _token_warning = gatekeeper_token_warning(
            role_choices["gatekeeper"].model_name, gatekeeper_max_tokens
        )
        if _token_warning:
            st.warning(_token_warning)

        reranker_choice = None
        reranker_cross_encoder_model = None
        reranker_max_output_tokens = 2048
        if reranker_enabled:
            st.markdown("##### Memory Reranker")
            current_reranker = previous_role_models.get("reranker") or {}
            if reranker_engine == "cross_encoder":
                reranker_cross_encoder_model = (
                    _evaluation_cross_encoder_model(
                        str(current_reranker.get("model_name") or ""),
                        key="evaluation_reranker_cross_encoder_model",
                    )
                )
                st.caption(
                    "質問と候補カードをローカルで一括採点します。"
                    "生成LLM・外部Connection・JSON出力は使いません。"
                )
            else:
                reranker_choice = _evaluation_reranker_model(
                    api_url,
                    current_reranker,
                    connections,
                    key_prefix="evaluation_reranker_model",
                )
                reranker_max_output_tokens = st.number_input(
                    "Reranker max output tokens",
                    min_value=1,
                    value=int(
                        (current_reranker.get("generation_config") or {}).get(
                            "max_output_tokens", 2048
                        )
                    ),
                    step=256,
                    key="evaluation_reranker_max_output_tokens",
                )
                st.caption(
                    "temperatureは0に固定。選択したConnectionへ現在の質問と"
                    "候補カードのtitle/summary/episodeを上限付きで送信します。"
                    "DB全体やAPIキーをprompt・成果物へ含めません。"
                )

        st.markdown("##### Semantic Judge（任意）")
        judge_enabled = st.checkbox(
            "AIで回答の意味的な正しさも判定する",
            value=(
                "judge" in previous_role_models and not retrieval_prep
            ),
            key="evaluation_judge_enabled",
            disabled=retrieval_prep,
            help=(
                "LoCoMo公式スコアは保持し、別のsemantic指標として"
                "追加します。問題数分の追加LLM呼び出しが発生します。"
            ),
        )
        if retrieval_prep:
            judge_enabled = False
        judge_choice = None
        judge_max_output_tokens = 2048
        if judge_enabled:
            current_judge = previous_role_models.get("judge") or {}
            judge_choice = _evaluation_judge_model(
                api_url,
                current_judge,
                connections,
                key_prefix="evaluation_judge_model",
            )
            judge_max_output_tokens = st.number_input(
                "Judge max output tokens",
                min_value=1,
                value=int(
                    (current_judge.get("generation_config") or {}).get(
                        "max_output_tokens", 2048
                    )
                ),
                step=256,
                key="evaluation_judge_max_output_tokens",
            )
            st.caption(
                "temperatureは0に固定。評価対象のChatとは別系統の"
                "モデルを推奨します。"
            )
            st.caption(
                "JudgeのConnectionへ各問のquestion・reference answer・"
                "predictionを送信します。記憶DB全体は送信しません。APIキーは"
                "判定promptや成果物に含めず、Connection認証にだけ使います。"
            )

    can_start = bool(
        dataset_path.strip()
        and run_id.strip()
        and (not source_required or source_memory_run_id)
    )
    if st.button(
        (
            "▶ 検索比較用の記憶を作成"
            if retrieval_prep
            else "▶ 評価を開始"
        ),
        type="primary",
        width="stretch",
        disabled=not can_start,
        key="evaluation_start",
    ):
        role_models = {}
        empty_roles = []
        for role, choice in role_choices.items():
            if not choice.model_name.strip():
                empty_roles.append(role)
                continue
            role_models[role] = {
                "connection": choice.connection_id,
                "model_name": choice.model_name.strip(),
                "generation_config": generation_values.get(role, {}),
            }
            if role == "embedding" and embedding_profile:
                role_models[role]["profile"] = embedding_profile
        if reranker_enabled:
            if reranker_engine == "cross_encoder":
                if not reranker_cross_encoder_model:
                    empty_roles.append("reranker")
                else:
                    role_models["reranker"] = {
                        "engine": "cross_encoder",
                        "model_name": reranker_cross_encoder_model,
                        "batch_size": int(reranker_batch_size),
                        "device": reranker_device,
                        "score_threshold": (
                            float(reranker_score_threshold)
                            if reranker_threshold_enabled
                            else None
                        ),
                    }
            elif (
                reranker_choice is None
                or not reranker_choice.model_name.strip()
            ):
                empty_roles.append("reranker")
            else:
                role_models["reranker"] = {
                    "engine": "llm",
                    "connection": reranker_choice.connection_id,
                    "model_name": reranker_choice.model_name.strip(),
                    "generation_config": {
                        "temperature": 0.0,
                        "max_output_tokens": int(
                            reranker_max_output_tokens
                        ),
                    },
                }
        if judge_enabled:
            if judge_choice is None or not judge_choice.model_name.strip():
                empty_roles.append("judge")
            else:
                role_models["judge"] = {
                    "connection": judge_choice.connection_id,
                    "model_name": judge_choice.model_name.strip(),
                    "generation_config": {
                        "temperature": 0.0,
                        "max_output_tokens": int(judge_max_output_tokens),
                    },
                }
        if empty_roles:
            st.error(
                "モデルが未設定のロールがあります: "
                + ", ".join(empty_roles)
            )
            return
        payload = {
            "dataset_path": dataset_path.strip(),
            "run_id": run_id.strip(),
            "run_mode": run_mode,
            "workflow": workflow,
            "source_memory_run_id": source_memory_run_id or None,
            "qa_mode": qa_mode,
            "locale": locale,
            "sample_ids": (
                [] if source_memory_run_id else list(selected_sample_ids)
            ),
            "sample_limit": scope_values["sample"],
            "session_limit": scope_values["session"],
            "question_limit": scope_values["question"],
            "time_decay_rate": float(time_decay_rate),
            "context_current_time": context_current_time,
            "context_mid_term": context_mid_term,
            "context_session_digest": context_session_digest,
            "context_rag": context_rag,
            "rag_source_mode": rag_source_mode,
            "rag_raw_top_k": int(rag_raw_top_k),
            "rag_raw_max_chars": int(rag_raw_max_chars),
            "rag_raw_neighbor_radius": int(rag_raw_neighbor_radius),
            "search_mode": search_mode,
            "retrieval_execution": retrieval_execution,
            "injection_policy": injection_policy,
            "vector_search_limit": int(vector_search_limit),
            "bm25_candidates": int(bm25_candidates),
            "vector_candidates": int(vector_candidates),
            "dual_query_candidates": int(dual_query_candidates),
            "dual_query_pool_limit": int(dual_query_pool_limit),
            "reranker_candidate_limit": int(reranker_candidate_limit),
            "reranker_max_candidate_chars": int(
                reranker_max_candidate_chars
            ),
            "rrf_k": int(rrf_k),
            "bm25_max_df_ratio": float(bm25_max_df_ratio),
            "evidence_fusion_base_weight": float(
                evidence_fusion_base_weight
            ),
            "evidence_raw_chunk_chars": int(evidence_raw_chunk_chars),
            "stage3_batch_size": int(stage3_batch_size),
            "stage3_bootstrap_max_cards": int(
                stage3_bootstrap_max_cards
            ),
            "role_models": role_models,
            "allow_embedding_mismatch": allow_embedding_mismatch,
        }
        try:
            response = requests.post(
                f"{api_url}/evaluations/jobs",
                json=payload,
                timeout=10,
            )
            if response.ok:
                job = response.json()
                _evaluation_get.clear()
                st.session_state.evaluation_selected_job = job["job_id"]
                st.session_state.evaluation_pending_run_id = (
                    _evaluation_next_run_id(
                        str(job["run_id"]),
                        [*run_ids, str(job["run_id"])],
                    )
                )
                st.success(
                    (
                        "検索比較用の記憶作成を開始しました: "
                        if retrieval_prep
                        else "評価を開始しました: "
                    )
                    + f"{job['run_id']} "
                    f"(job {job['job_id'][:8]})。次の新規評価では"
                    "この設定を引き継ぎます。"
                )
            else:
                st.error(_api_error_detail(response))
        except Exception as exc:
            st.error(f"開始エラー: {exc}")


def _render_evaluation_jobs_content(api_url: str) -> bool | None:
    """Render evaluation jobs and report whether polling should continue."""
    import requests

    st.subheader("評価ジョブ")
    st.caption(
        "実行中はこのジョブ表示だけを2秒ごとに更新します。完了後は停止し、"
        "評価フォームやモデル候補は再読み込みしません。"
    )
    try:
        jobs = _evaluation_get(
            api_url,
            "/evaluations/jobs",
        ).get("jobs", [])
    except Exception as exc:
        st.error(f"ジョブ状態の取得エラー: {exc}")
        return None
    if not jobs:
        st.info("Web Consoleから開始した評価ジョブはまだありません。")
        return False

    job_rows = [
        {
            "type": job.get("job_type", "locomo"),
            "run_id": job.get("run_id"),
            "status": job.get("status"),
            "progress": job.get("progress"),
            "phase": job.get("phase"),
            "message": job.get("message"),
            "attempt": job.get("attempt"),
            "started_at": job.get("started_at"),
        }
        for job in jobs
    ]
    st.dataframe(job_rows, width="stretch", hide_index=True)

    job_ids = [job["job_id"] for job in jobs]
    current_job = st.session_state.get("evaluation_selected_job")
    selected_index = (
        job_ids.index(current_job) if current_job in job_ids else 0
    )
    selected_job_id = st.selectbox(
        "詳細を表示するジョブ",
        options=job_ids,
        index=selected_index,
        format_func=lambda job_id: next(
            (
                f"{job['run_id']} · {job['status']} · {job_id[:8]}"
                for job in jobs
                if job["job_id"] == job_id
            ),
            job_id,
        ),
        key="evaluation_selected_job",
    )
    selected = next(
        job for job in jobs if job["job_id"] == selected_job_id
    )
    progress = float(selected.get("progress") or 0.0)
    st.progress(min(max(progress / 100.0, 0.0), 1.0))
    st.caption(
        f"{progress:.1f}% · {selected.get('phase')} · "
        f"{selected.get('message')}"
    )

    action_cols = st.columns(2)
    with action_cols[0]:
        if st.button(
            "■ 停止",
            disabled=selected.get("status")
            not in _EVALUATION_ACTIVE_STATUSES,
            key=f"evaluation_stop_{selected_job_id}",
            width="stretch",
        ):
            response = requests.post(
                f"{api_url}/evaluations/jobs/{selected_job_id}/stop",
                timeout=10,
            )
            if response.ok:
                st.success("停止要求を送信しました。")
            else:
                st.error(_api_error_detail(response))
    with action_cols[1]:
        if st.button(
            "▶ 再開",
            disabled=selected.get("status")
            not in _EVALUATION_RESUMABLE_STATUSES,
            key=f"evaluation_resume_{selected_job_id}",
            width="stretch",
        ):
            response = requests.post(
                f"{api_url}/evaluations/jobs/{selected_job_id}/resume",
                timeout=10,
            )
            if response.ok:
                st.session_state.evaluation_jobs_polling = True
                if selected.get("job_type") == "retrieval_replay":
                    st.success("検索比較を再実行しました。")
                else:
                    st.success("checkpointから再開しました。")
                st.rerun()
            else:
                st.error(_api_error_detail(response))

    with st.expander("実行ログ", expanded=True):
        try:
            log_payload = _evaluation_get(
                api_url,
                f"/evaluations/jobs/{selected_job_id}/log?tail_lines=300",
            )
            st.code(log_payload.get("text") or "ログはまだありません。")
        except Exception as exc:
            st.error(f"ログ取得エラー: {exc}")

    return any(
        str(job.get("status") or "") in _EVALUATION_ACTIVE_STATUSES
        for job in jobs
    )


def _render_evaluation_jobs(api_url: str) -> None:
    """Poll jobs in a fragment only while at least one job is active."""
    polling_key = "evaluation_jobs_polling"
    polling = bool(st.session_state.get(polling_key, True))

    @st.fragment(run_every=2 if polling else None)
    def _jobs_fragment() -> None:
        active = _render_evaluation_jobs_content(api_url)
        if active is not None and active != polling:
            st.session_state[polling_key] = active
            # Rebuild the fragment with/without its automatic timer. A normal
            # rerun is valid both during the initial app render and a fragment
            # rerun; scope="fragment" is not.
            st.rerun()

    _jobs_fragment()


_RETRIEVAL_EVIDENCE_MODES = frozenset(
    {
        "evidence_rerank",
        "hybrid_evidence_rerank",
        "hybrid_evidence_fusion",
        "hybrid_evidence_fusion_w40",
        "hybrid_evidence_fusion_w50",
        "hybrid_evidence_fusion_w60",
        "hybrid_evidence_fusion_mmr",
    }
)
_RETRIEVAL_FUSION_MODES = frozenset(
    {
        "hybrid_evidence_fusion",
        "hybrid_evidence_fusion_w40",
        "hybrid_evidence_fusion_w50",
        "hybrid_evidence_fusion_w60",
        "hybrid_evidence_fusion_mmr",
    }
)
_RETRIEVAL_MODE_LABELS = {
    "hybrid_evidence_fusion_w40": "Fusion（Hybrid 0.40）",
    "hybrid_evidence_fusion_w50": "Fusion（Hybrid 0.50）",
    "hybrid_evidence_fusion_w60": "Fusion（Hybrid 0.60）",
    "hybrid_evidence_fusion_mmr": "Fusion + MMR top3",
}
_QA_SEARCH_MODE_LABELS = {
    "hybrid_evidence_fusion": (
        "hybrid_evidence_fusion（Fusion比率を調整可能）"
    ),
}


def _format_retrieval_reranker_scores(scores) -> str:
    """Format problem-level reranker scores for the compact UI table."""
    formatted = []
    for item in scores or []:
        if not isinstance(item, dict):
            continue
        card_id = item.get("id") or item.get("card_id") or "?"
        score = item.get("score")
        if score is None:
            score = item.get("fusion_score")
        if score is None:
            score = item.get("mmr_score")
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            formatted.append(f"{card_id}:{score:.4f}")
        else:
            formatted.append(f"{card_id}:{score}")
    return " / ".join(formatted)


def _render_retrieval_replay_result(result: dict) -> None:
    """Render aggregate and problem-level offline retrieval results."""
    st.markdown("#### 検索比較の結果")
    st.caption(
        f"{result.get('run')} / oracleカードがある問: "
        f"{result.get('oracle_questions')} / "
        f"保存日時: {result.get('generated_at') or '—'}"
    )
    rows = []
    detail_modes = []
    result_modes = result.get("modes")
    mode_order = (
        [str(mode) for mode in result_modes]
        if isinstance(result_modes, list)
        else [
            "bm25",
            "vector",
            "hybrid",
            "dual_query",
            "reranked",
            "evidence_rerank",
            "hybrid_evidence_rerank",
            "hybrid_evidence_fusion",
            "hybrid_evidence_fusion_w40",
            "hybrid_evidence_fusion_w50",
            "hybrid_evidence_fusion_w60",
            "hybrid_evidence_fusion_mmr",
        ]
    )
    for mode in mode_order:
        stats = result.get(mode)
        if not isinstance(stats, dict):
            continue
        if isinstance(stats.get("details"), list):
            detail_modes.append(mode)
        row = {
            "mode": mode,
            "questions": stats.get("questions"),
            "recall@1": stats.get("recall_at_1"),
            "recall@3": stats.get("recall_at_3"),
            "recall@5": stats.get("recall_at_5"),
            "recall@20": stats.get("recall_at_20"),
            "hit@3": stats.get("hit_at_3"),
            "hit@5": stats.get("hit_at_5"),
            "hit@20": stats.get("hit_at_20"),
        }
        reranker = stats.get("reranker") or {}
        if reranker:
            row.update(
                {
                    "reranker": reranker.get("model_name"),
                    "engine": reranker.get("engine"),
                    "reranker完了率": reranker.get("completion_rate"),
                    "fallback率": reranker.get("fallback_rate"),
                    "rescue@3": reranker.get("rescued_at_3"),
                    "harm@3": reranker.get("harmed_at_3"),
                    "selected recall@3": reranker.get(
                        "selected_recall_at_3"
                    ),
                    "reranker mean ms": reranker.get("latency_ms_mean"),
                    "reranker p95 ms": reranker.get("latency_ms_p95"),
                }
            )
        query_fusion = stats.get("query_fusion") or {}
        if query_fusion:
            row.update(
                {
                    "query model": query_fusion.get(
                        "gatekeeper_model_name"
                    ),
                    "query生成率": query_fusion.get("query_available_rate"),
                    "original recall@3": query_fusion.get(
                        "original_recall_at_3"
                    ),
                    "rewrite recall@3": query_fusion.get(
                        "retrieval_query_recall_at_3"
                    ),
                    "rescue@3": query_fusion.get("rescued_at_3"),
                    "harm@3": query_fusion.get("harmed_at_3"),
                }
            )
        evidence = stats.get("evidence_reranker") or {}
        if evidence:
            cache = evidence.get("cache") or {}
            embedding = cache.get("embedding") or {}
            row.update(
                {
                    "evidence embedding": embedding.get("model_name"),
                    "evidence完了率": evidence.get("completion_rate"),
                    "fallback率": evidence.get("fallback_rate"),
                    "rescue@3": evidence.get("rescued_at_3"),
                    "harm@3": evidence.get("harmed_at_3"),
                    "selected recall@3": evidence.get(
                        "selected_recall_at_3"
                    ),
                    "evidence mean ms": evidence.get("latency_ms_mean"),
                    "cache hits": cache.get("hits"),
                    "cache writes": cache.get("writes"),
                    "fusion base weight": (
                        (evidence.get("fusion") or {}).get("base_weight")
                    ),
                    "MMR relevance weight": (
                        (evidence.get("mmr") or {}).get(
                            "relevance_weight"
                        )
                    ),
                    "MMR top3入替": (
                        (evidence.get("mmr") or {}).get(
                            "selected_set_changed"
                        )
                    ),
                    "MMR rescue": (
                        (evidence.get("mmr") or {}).get(
                            "rescued_at_3_vs_fusion"
                        )
                    ),
                    "MMR harm": (
                        (evidence.get("mmr") or {}).get(
                            "harmed_at_3_vs_fusion"
                        )
                    ),
                    "MMR similarity before": (
                        (evidence.get("mmr") or {}).get(
                            "pairwise_similarity_before_mean"
                        )
                    ),
                    "MMR similarity after": (
                        (evidence.get("mmr") or {}).get(
                            "pairwise_similarity_after_mean"
                        )
                    ),
                }
            )
        rows.append(row)
    st.dataframe(rows, width="stretch", hide_index=True)
    st.caption(
        "recall は「上位k候補が evidence ターンを覆った割合」の平均、"
        "hit は1件でも覆えた問数です。結果は run 直下の "
        "retrieval_replay.json にも保存されます。"
    )

    if not detail_modes:
        return
    detail_key = "evaluation_replay_detail_mode"
    if st.session_state.get(detail_key) not in detail_modes:
        st.session_state[detail_key] = detail_modes[-1]
    selected_mode = st.selectbox(
        "問題別結果を表示するモード",
        options=detail_modes,
        key=detail_key,
    )
    stats = result.get(selected_mode) or {}
    details = stats.get("details") or []
    filter_options = ["all"]
    if selected_mode in {"reranked", "dual_query"} | set(
        _RETRIEVAL_EVIDENCE_MODES
    ):
        filter_options.extend(
            ["rescue", "harm", "fallback", "error", "unchanged"]
        )
    filter_key = "evaluation_replay_detail_filter"
    if st.session_state.get(filter_key) not in filter_options:
        st.session_state[filter_key] = "all"
    selected_filter = st.selectbox(
        "問題の絞り込み",
        options=filter_options,
        format_func=lambda value: {
            "all": "すべて",
            "rescue": "rescue（改善）",
            "harm": "harm（悪化）",
            "fallback": "fallback",
            "error": "error",
            "unchanged": "変化なし",
        }.get(value, value),
        key=filter_key,
    )
    detail_rows = []
    for item in details:
        if not isinstance(item, dict):
            continue
        delta = item.get("reranker_delta_at_3")
        if selected_mode in _RETRIEVAL_EVIDENCE_MODES:
            delta = item.get("evidence_delta_at_3")
        if selected_mode == "dual_query":
            fused_recall = item.get("recall_at_3")
            original_recall = item.get("original_recall_at_3")
            if isinstance(fused_recall, (int, float)) and isinstance(
                original_recall, (int, float)
            ):
                delta = fused_recall - original_recall
        query_fusion = item.get("query_fusion") or {}
        if item.get("error"):
            change = "error"
        elif item.get("reranker_fallback") or item.get(
            "evidence_rerank_fallback"
        ) or (
            selected_mode == "dual_query"
            and query_fusion.get("status") == "fallback"
        ):
            change = "fallback"
        elif isinstance(delta, (int, float)) and delta > 0:
            change = "rescue"
        elif isinstance(delta, (int, float)) and delta < 0:
            change = "harm"
        else:
            change = "unchanged"
        if selected_filter != "all" and change != selected_filter:
            continue
        candidate_ids = item.get("candidate_ids") or []
        vector_ids = item.get("vector_candidate_ids") or []
        base_ids = item.get("base_candidate_ids") or vector_ids
        selected_ids = item.get("selected_candidate_ids") or []
        original_ids = item.get("original_candidate_ids") or []
        rewrite_ids = item.get("retrieval_query_candidate_ids") or []
        detail_rows.append(
            {
                "sample_id": item.get("sample_id"),
                "question_id": item.get("question_id"),
                "category": item.get("category"),
                "question": item.get("question"),
                "recall@1": item.get("recall_at_1"),
                "recall@3": item.get("recall_at_3"),
                "recall@5": item.get("recall_at_5"),
                "recall@20": item.get("recall_at_20"),
                "base mode": item.get("base_search_mode"),
                "base recall@3": (
                    item.get("base_recall_at_3")
                    if item.get("base_recall_at_3") is not None
                    else item.get("vector_recall_at_3")
                ),
                "vector recall@3": item.get("vector_recall_at_3"),
                "hybrid recall@3": item.get("hybrid_recall_at_3"),
                "original recall@3": item.get("original_recall_at_3"),
                "rewrite recall@3": item.get(
                    "retrieval_query_recall_at_3"
                ),
                "selected recall@3": item.get("selected_recall_at_3"),
                "MMR base recall@3": (
                    (item.get("evidence_mmr") or {}).get(
                        "base_recall_at_3"
                    )
                ),
                "MMR delta@3": (
                    (item.get("evidence_mmr") or {}).get("delta_at_3")
                ),
                "MMR set changed": (
                    (item.get("evidence_mmr") or {}).get(
                        "selected_set_changed"
                    )
                ),
                "delta@3": delta,
                "change": change,
                "reranker status": item.get("reranker_status"),
                "evidence status": item.get("evidence_rerank_status"),
                "retrieval query": item.get("retrieval_query"),
                "query source": item.get("retrieval_query_source"),
                "query status": item.get("retrieval_query_status"),
                "fusion status": query_fusion.get("status"),
                "latency ms": (
                    item.get("evidence_rerank_latency_ms")
                    if selected_mode in _RETRIEVAL_EVIDENCE_MODES
                    else item.get("reranker_latency_ms")
                ),
                "base top3": ", ".join(
                    str(value) for value in base_ids[:3]
                ),
                "vector top3": ", ".join(
                    str(value) for value in vector_ids[:3]
                ),
                "selected top3": ", ".join(
                    str(value) for value in selected_ids[:3]
                ),
                "original top3": ", ".join(
                    str(value) for value in original_ids[:3]
                ),
                "rewrite top3": ", ".join(
                    str(value) for value in rewrite_ids[:3]
                ),
                "vector IDs": ", ".join(
                    str(value) for value in vector_ids
                ),
                "base IDs": ", ".join(
                    str(value) for value in base_ids
                ),
                "candidate IDs": ", ".join(
                    str(value) for value in candidate_ids
                ),
                "reranker scores": _format_retrieval_reranker_scores(
                    item.get("reranker_scores")
                ),
                "MMR scores": _format_retrieval_reranker_scores(
                    (item.get("evidence_mmr") or {}).get("scores")
                ),
                "evidence scores": _format_retrieval_reranker_scores(
                    item.get("evidence_scores")
                ),
                "fusion scores": _format_retrieval_reranker_scores(
                    item.get("evidence_fusion_scores")
                ),
                "selected evidence": " / ".join(
                    (
                        f"{match.get('card_id')} "
                        f"[{match.get('evidence_type')}] "
                        f"{match.get('source_file') or 'Episode'}: "
                        f"{match.get('preview') or ''}"
                    )
                    for match in (item.get("selected_evidence") or [])
                    if isinstance(match, dict)
                ),
                "evidence": ", ".join(
                    str(value) for value in (item.get("evidence") or [])
                ),
                "error": item.get("error"),
            }
        )
    if detail_rows:
        st.dataframe(detail_rows, width="stretch", hide_index=True)
    else:
        st.info("選択した条件に該当する問題はありません。")


def _render_retrieval_replay_status_content(
    api_url: str,
    target_run: str,
) -> bool | None:
    """Render one replay job and report whether polling should continue."""
    import requests

    try:
        jobs = _evaluation_get(api_url, "/evaluations/jobs").get("jobs", [])
    except Exception as exc:
        st.error(f"検索比較の進捗取得エラー: {exc}")
        return None
    matching_jobs = [
        job
        for job in jobs
        if job.get("job_type") == "retrieval_replay"
        and str(job.get("run_id")) == target_run
    ]
    matching_jobs.sort(
        key=lambda item: str(item.get("created_at") or ""),
        reverse=True,
    )
    job = matching_jobs[0] if matching_jobs else None
    result = None
    if job is not None:
        job_id = str(job.get("job_id"))
        status = str(job.get("status") or "unknown")
        progress = float(job.get("progress") or 0.0)
        st.markdown("#### 実行状況")
        st.progress(min(max(progress / 100.0, 0.0), 1.0))
        st.caption(
            f"{progress:.1f}% · {job.get('phase')} · "
            f"{job.get('message')} · job {job_id[:8]}"
        )
        action_cols = st.columns(2)
        with action_cols[0]:
            if st.button(
                "■ 検索比較を停止",
                disabled=status not in _EVALUATION_ACTIVE_STATUSES,
                key=f"evaluation_replay_stop_{job_id}",
                width="stretch",
            ):
                response = requests.post(
                    f"{api_url}/evaluations/jobs/{job_id}/stop",
                    timeout=10,
                )
                if response.ok:
                    st.success("停止要求を送信しました。")
                else:
                    st.error(_api_error_detail(response))
        with action_cols[1]:
            if st.button(
                "▶ 検索比較を再実行",
                disabled=status not in _EVALUATION_RESUMABLE_STATUSES,
                key=f"evaluation_replay_resume_{job_id}",
                width="stretch",
            ):
                response = requests.post(
                    f"{api_url}/evaluations/jobs/{job_id}/resume",
                    timeout=10,
                )
                if response.ok:
                    st.session_state.evaluation_replay_result = None
                    st.session_state.evaluation_replay_result_job_id = None
                    st.session_state.evaluation_replay_polling = True
                    st.success("同じ条件で検索比較を再実行しました。")
                    st.rerun()
                else:
                    st.error(_api_error_detail(response))
        try:
            log_payload = _evaluation_get(
                api_url,
                f"/evaluations/jobs/{job_id}/log?tail_lines=12",
            )
            st.caption("直近の実行ログ")
            st.code(log_payload.get("text") or "ログはまだありません。")
        except Exception as exc:
            st.warning(f"ログ取得エラー: {exc}")

        if status == "completed":
            try:
                candidate = _evaluation_get(
                    api_url,
                    f"/evaluations/runs/{target_run}/retrieval-replay",
                )
            except Exception as exc:
                st.error(f"検索比較の結果取得エラー: {exc}")
                return False
            artifact_job_id = candidate.get("job_id")
            if artifact_job_id and artifact_job_id != job_id:
                st.warning(
                    "完了ジョブと保存結果が一致しません。"
                    "同じ条件で再実行してください。"
                )
                return False
            result = candidate
            st.session_state.evaluation_replay_result = result
            st.session_state.evaluation_replay_result_job_id = job_id
            st.session_state.evaluation_replay_result_run_id = target_run
        elif status in _EVALUATION_ACTIVE_STATUSES:
            st.info("検索比較はバックグラウンドで実行中です。")
            return True
        elif status == "failed":
            st.error(
                str(
                    job.get("message")
                    or "検索比較に失敗しました。"
                )
            )
            return False
        else:
            st.warning(
                str(job.get("message") or f"検索比較: {status}")
            )
            return False
    else:
        cached_run = st.session_state.get(
            "evaluation_replay_result_run_id"
        )
        if cached_run == target_run:
            result = st.session_state.get("evaluation_replay_result")
        if not isinstance(result, dict):
            try:
                result = _evaluation_get(
                    api_url,
                    f"/evaluations/runs/{target_run}/retrieval-replay",
                )
            except Exception:
                st.info("このrunの保存済み検索比較はまだありません。")
                return False
            st.session_state.evaluation_replay_result = result
            st.session_state.evaluation_replay_result_job_id = result.get(
                "job_id"
            )
            st.session_state.evaluation_replay_result_run_id = target_run

    if isinstance(result, dict):
        result_run = Path(str(result.get("run") or "")).name
        if result_run and result_run != target_run:
            st.warning("保存結果のrun IDが選択中のrunと一致しません。")
            return False
        _render_retrieval_replay_result(result)
    return False


def _render_retrieval_replay_status(
    api_url: str,
    target_run: str,
) -> None:
    """Poll one replay job without rerunning the rest of the page."""
    polling_key = "evaluation_replay_polling"
    polling = bool(st.session_state.get(polling_key, True))

    @st.fragment(run_every=2 if polling else None)
    def _replay_fragment() -> None:
        active = _render_retrieval_replay_status_content(api_url, target_run)
        if active is not None and active != polling:
            st.session_state[polling_key] = active
            # Recreate the fragment so its timer is removed on completion (or
            # enabled when a new/restarted job appears).
            st.rerun()

    _replay_fragment()


def _render_retrieval_replay(api_url: str, runs: list) -> None:
    """QA を回さずに検索だけ比較する（検索改修計画 §8 の足切り）。"""
    import requests

    replayable = [
        run["run_id"]
        for run in runs
        if run.get("run_id")
        and (
            run.get("question_count")
            or run.get("retrieval_question_count")
        )
    ]
    if not replayable:
        return

    with st.expander("検索だけ比較（offline retrieval replay）", expanded=False):
        st.caption(
            "回答を生成せず、同じカードに対する Recall@k だけを測ります。"
            "workflow=retrieval_prep のrunも選択でき、その場合はReplay/"
            "Sleeptime後に固定した質問manifestを使います。"
            "bm25 は embedding を呼びません。reranked は質問ごとにembeddingと"
            "リランカーを1回ずつ呼び、vector top-N→top3を評価します。"
            "evidence_rerank はvector top-Nを、カードのEpisodeと紐づく"
            "RAW会話断片のEmbeddingでtop3へ再順位付けします。"
            "hybrid_evidence_rerank は同じ処理をhybrid top-Nへ適用します。"
            "hybrid_evidence_fusion はhybrid順位を主軸にEvidence順位を"
            "重み付き融合します。w40/w50/w60は比率を一度に比較する固定値、"
            "Fusion + MMRは既存カード/Evidenceベクトルでnear-dupを減点し、"
            "注入top3の多様性を高めます。"
            "dual_query は保存済み検索文を再利用し、無い旧runでは元runの"
            "Gatekeeperを1回呼んでからembeddingを2回実行します。"
            " 実行はバックグラウンドで継続し、画面を閉じても中断しません。"
        )
        replay_cols = st.columns([2, 2, 1])
        with replay_cols[0]:
            target_run = st.selectbox(
                "対象run",
                options=replayable,
                key="evaluation_replay_run",
            )
        with replay_cols[1]:
            modes = st.multiselect(
                "モード",
                options=[
                    "bm25",
                    "vector",
                    "hybrid",
                    "dual_query",
                    "reranked",
                    "evidence_rerank",
                    "hybrid_evidence_rerank",
                    "hybrid_evidence_fusion",
                    "hybrid_evidence_fusion_w40",
                    "hybrid_evidence_fusion_w50",
                    "hybrid_evidence_fusion_w60",
                    "hybrid_evidence_fusion_mmr",
                ],
                default=["bm25"],
                format_func=lambda value: _RETRIEVAL_MODE_LABELS.get(
                    value, value
                ),
                key="evaluation_replay_modes",
            )
        with replay_cols[2]:
            replay_limit = st.number_input(
                "候補数",
                min_value=1,
                max_value=100,
                value=20,
                step=1,
                key="evaluation_replay_limit",
            )
        st.caption(
            f"候補数={int(replay_limit)}では、各モードの統合候補を最大"
            f"{int(replay_limit)}件まで順位付けします。"
            "Evidence/Fusion/MMRもこの候補集合を評価し、指標上の選択はtop3固定です。"
        )
        replay_reranker_choice = None
        replay_cross_encoder_model = None
        replay_reranker_engine = "cross_encoder"
        replay_reranker_max_tokens = 2048
        replay_reranker_max_chars = 1600
        replay_reranker_batch_size = 20
        replay_reranker_device = "auto"
        replay_threshold_enabled = False
        replay_score_threshold = 0.0
        evidence_raw_chunk_chars = 1800
        evidence_fusion_base_weight = 0.7
        evidence_mmr_lambda = 0.8
        if _RETRIEVAL_EVIDENCE_MODES & set(modes):
            evidence_raw_chunk_chars = st.number_input(
                "RAW chunk max chars",
                min_value=200,
                max_value=10000,
                value=1800,
                step=100,
                key="evaluation_replay_evidence_raw_chunk_chars",
            )
            st.caption(
                "EpisodeとRAW会話断片を、このrunが使用したEmbedding "
                "Connectionへ送信します。評価run内のキャッシュには本文を保存せず、"
                "ハッシュとベクトルだけを保存します。問題別レビュー用の結果JSONには、"
                "選択根拠の先頭最大600文字を保存します。API keyは通常のConnection"
                "認証にだけ使用します。質問Embeddingは二段の検索で共有します。"
            )
        if {
            "hybrid_evidence_fusion",
            "hybrid_evidence_fusion_mmr",
        } & set(modes):
            evidence_fusion_base_weight = st.number_input(
                "Fusion hybrid weight",
                min_value=0.0,
                max_value=1.0,
                value=0.7,
                step=0.05,
                key="evaluation_replay_evidence_fusion_base_weight",
                help=(
                    "hybrid順位の重み。Evidence順位は1からこの値を引いた"
                    "重みになります。v29での初期候補は0.70です。"
                ),
            )
        if "hybrid_evidence_fusion_mmr" in modes:
            evidence_mmr_lambda = st.number_input(
                "MMR relevance weight",
                min_value=0.0,
                max_value=1.0,
                value=0.8,
                step=0.05,
                key="evaluation_replay_evidence_mmr_lambda",
                help=(
                    "Fusion関連度の重み。残りを候補同士の類似度減点に使います。"
                    "追加のLLM・Embedding呼び出しはありません。"
                ),
            )
        if "reranked" in modes:
            replay_engine_options = ["cross_encoder", "llm"]
            replay_reranker_engine = st.selectbox(
                "Reranker engine",
                options=replay_engine_options,
                format_func=lambda value: (
                    "Cross-Encoder（推奨）"
                    if value == "cross_encoder"
                    else "生成LLM（旧方式・比較用）"
                ),
                key="evaluation_replay_reranker_engine",
            )
            if replay_reranker_engine == "cross_encoder":
                replay_cross_encoder_model = _evaluation_cross_encoder_model(
                    "",
                    key="evaluation_replay_cross_encoder_model",
                )
                replay_cross_cols = st.columns(3)
                with replay_cross_cols[0]:
                    replay_reranker_batch_size = st.number_input(
                        "Batch size",
                        min_value=1,
                        max_value=100,
                        value=20,
                        step=1,
                        key="evaluation_replay_reranker_batch_size",
                    )
                with replay_cross_cols[1]:
                    replay_reranker_device = st.selectbox(
                        "Device",
                        options=["auto", "cpu", "cuda", "mps"],
                        key="evaluation_replay_reranker_device",
                    )
                with replay_cross_cols[2]:
                    replay_reranker_max_chars = st.number_input(
                        "Max chars / candidate",
                        min_value=100,
                        max_value=10000,
                        value=1600,
                        step=100,
                        key="evaluation_replay_reranker_max_chars",
                    )
                replay_threshold_enabled = st.checkbox(
                    "関連度しきい値を使う",
                    value=False,
                    key="evaluation_replay_threshold_enabled",
                )
                if replay_threshold_enabled:
                    replay_score_threshold = st.number_input(
                        "Score threshold",
                        value=0.0,
                        step=0.1,
                        key="evaluation_replay_score_threshold",
                    )
            else:
                replay_reranker_choice = _evaluation_reranker_model(
                    api_url,
                    {},
                    _get_connections(api_url),
                    key_prefix="evaluation_replay_reranker_model",
                )
                replay_reranker_cols = st.columns(2)
                with replay_reranker_cols[0]:
                    replay_reranker_max_tokens = st.number_input(
                        "Reranker max output tokens",
                        min_value=1,
                        value=2048,
                        step=256,
                        key="evaluation_replay_reranker_max_tokens",
                    )
                with replay_reranker_cols[1]:
                    replay_reranker_max_chars = st.number_input(
                        "Max chars / candidate",
                        min_value=100,
                        max_value=10000,
                        value=1600,
                        step=100,
                        key="evaluation_replay_reranker_max_chars",
                    )
            if replay_reranker_engine == "cross_encoder":
                st.caption(
                    "候補数は上の値（推奨20）。質問と候補カードを"
                    "ローカルのCross-Encoderで一括採点します。"
                )
            else:
                st.caption(
                    "候補数は上の値（推奨20）、注入順位はtop3固定。"
                    "候補カード本文を選択したConnectionへ送ります。"
                )
        if st.button(
            "検索を比較",
            disabled=(
                not modes
                or (
                    "reranked" in modes
                    and (
                        (
                            replay_reranker_engine == "cross_encoder"
                            and not replay_cross_encoder_model
                        )
                        or (
                            replay_reranker_engine == "llm"
                            and (
                                replay_reranker_choice is None
                                or not replay_reranker_choice.model_name.strip()
                            )
                        )
                    )
                )
            ),
            key="evaluation_replay_start",
        ):
            try:
                payload = {
                    "run_id": target_run,
                    "modes": modes,
                    "limit": int(replay_limit),
                    "reranker_max_candidate_chars": int(
                        replay_reranker_max_chars
                    ),
                    "evidence_raw_chunk_chars": int(
                        evidence_raw_chunk_chars
                    ),
                    "evidence_fusion_base_weight": float(
                        evidence_fusion_base_weight
                    ),
                    "evidence_mmr_lambda": float(evidence_mmr_lambda),
                }
                if (
                    "reranked" in modes
                    and replay_reranker_engine == "cross_encoder"
                ):
                    payload["reranker"] = {
                        "engine": "cross_encoder",
                        "model_name": replay_cross_encoder_model,
                        "batch_size": int(replay_reranker_batch_size),
                        "device": replay_reranker_device,
                        "score_threshold": (
                            float(replay_score_threshold)
                            if replay_threshold_enabled
                            else None
                        ),
                    }
                elif (
                    "reranked" in modes
                    and replay_reranker_choice is not None
                ):
                    payload["reranker"] = {
                        "engine": "llm",
                        "connection": replay_reranker_choice.connection_id,
                        "model_name": replay_reranker_choice.model_name.strip(),
                        "generation_config": {
                            "temperature": 0.0,
                            "max_output_tokens": int(
                                replay_reranker_max_tokens
                            ),
                        },
                    }
                response = requests.post(
                    f"{api_url}/evaluations/runs/retrieval-replay/jobs",
                    json=payload,
                    timeout=10,
                )
                if response.ok:
                    job = response.json()
                    st.session_state.evaluation_replay_job_id = job["job_id"]
                    st.session_state.evaluation_selected_job = job["job_id"]
                    st.session_state.evaluation_replay_result = None
                    st.session_state.evaluation_replay_result_job_id = None
                    st.session_state.evaluation_replay_result_run_id = (
                        target_run
                    )
                    st.success(
                        f"検索比較を開始しました: {target_run} "
                        f"(job {job['job_id'][:8]})"
                    )
                else:
                    st.error(_api_error_detail(response))
            except Exception as exc:
                st.error(f"replay開始エラー: {exc}")

        _render_retrieval_replay_status(api_url, str(target_run))


def _render_retrieval_replay_comparison(api_url: str, runs: list) -> None:
    """Compare saved offline retrieval results across multiple runs."""
    import requests

    comparable_runs = [
        str(run["run_id"])
        for run in runs
        if run.get("run_id") and run.get("has_retrieval_replay")
    ]
    if len(comparable_runs) < 2:
        return

    with st.expander("検索比較runの横断比較", expanded=False):
        st.caption(
            "保存済み retrieval_replay.json を横並びにします。選択順の先頭を"
            "baseline、末尾を比較対象として問題別差分を計算します。Sample ID・"
            "質問集合・候補数limitが違う場合は参考比較として警告します。"
        )
        selected_runs = st.multiselect(
            "比較する検索run（先頭がbaseline、末尾が比較対象）",
            options=comparable_runs,
            max_selections=8,
            key="evaluation_retrieval_compare_runs",
        )
        if st.button(
            "検索結果を横断比較",
            disabled=len(selected_runs) < 2,
            key="evaluation_retrieval_compare",
        ):
            try:
                response = requests.post(
                    f"{api_url}/evaluations/runs/retrieval-replay/compare",
                    json={"run_ids": selected_runs},
                    timeout=20,
                )
                if response.ok:
                    st.session_state.evaluation_retrieval_comparison = (
                        response.json()
                    )
                else:
                    st.error(_api_error_detail(response))
            except Exception as exc:
                st.error(f"検索run比較エラー: {exc}")

        comparison = st.session_state.get(
            "evaluation_retrieval_comparison"
        )
        if not isinstance(comparison, dict):
            return
        compared_ids = [
            str(run.get("run_id") or "")
            for run in comparison.get("runs") or []
        ]
        if selected_runs and compared_ids != selected_runs:
            st.caption("表示中の結果は前回選択した検索runの比較です。")

        for warning in comparison.get("warnings") or []:
            st.warning(str(warning))
        if comparison.get("comparable"):
            st.success(
                "Sample ID・質問集合・候補数limitが一致しています。"
                "同条件のrun比較として読めます。"
            )

        scope_rows = []
        for run in comparison.get("runs") or []:
            scope_rows.append(
                {
                    "run_id": run.get("run_id"),
                    "sample IDs": ", ".join(run.get("sample_ids") or []),
                    "questions": run.get("question_count"),
                    "oracle questions": run.get("oracle_questions"),
                    "limit": run.get("limit"),
                    "modes": ", ".join(run.get("modes") or []),
                    "generated_at": run.get("generated_at"),
                }
            )
        st.markdown("#### 評価範囲")
        st.dataframe(scope_rows, width="stretch", hide_index=True)

        metric_rows = []
        for item in comparison.get("metrics") or []:
            metric_rows.append(
                {
                    "run_id": item.get("run_id"),
                    "mode": item.get("mode"),
                    "recall@1": item.get("recall_at_1"),
                    "Δ@1 vs baseline": item.get(
                        "delta_vs_baseline_at_1"
                    ),
                    "recall@3": item.get("recall_at_3"),
                    "Δ@3 vs baseline": item.get(
                        "delta_vs_baseline_at_3"
                    ),
                    "recall@5": item.get("recall_at_5"),
                    "Δ@5 vs baseline": item.get(
                        "delta_vs_baseline_at_5"
                    ),
                    "recall@20": item.get("recall_at_20"),
                    "Δ@20 vs baseline": item.get(
                        "delta_vs_baseline_at_20"
                    ),
                    "hit@3": item.get("hit_at_3"),
                    "base mode": item.get("base_search_mode"),
                    "rescue@3": item.get("rescued_at_3"),
                    "harm@3": item.get("harmed_at_3"),
                    "fallback": item.get("fallback_rate"),
                }
            )
        st.markdown("#### モード別指標")
        st.dataframe(metric_rows, width="stretch", hide_index=True)
        st.caption(
            f"Δはbaseline={comparison.get('baseline_run_id')}との差です。"
            "共通しないモードのΔは空欄になります。"
        )

        common_modes = list(comparison.get("common_modes") or [])
        if not common_modes:
            return
        selected_mode = st.selectbox(
            "問題別差分を表示する共通モード",
            options=common_modes,
            key="evaluation_retrieval_comparison_mode",
        )
        question_rows = []
        for item in comparison.get("questions") or []:
            if item.get("mode") != selected_mode:
                continue
            question_rows.append(
                {
                    "sample": item.get("sample_id"),
                    "question_id": item.get("question_id"),
                    "question": item.get("question"),
                    "baseline @1": item.get("baseline_recall_at_1"),
                    "target @1": item.get("comparison_recall_at_1"),
                    "Δ@1": item.get("delta_at_1"),
                    "baseline @3": item.get("baseline_recall_at_3"),
                    "target @3": item.get("comparison_recall_at_3"),
                    "Δ@3": item.get("delta_at_3"),
                    "baseline @5": item.get("baseline_recall_at_5"),
                    "target @5": item.get("comparison_recall_at_5"),
                    "Δ@5": item.get("delta_at_5"),
                    "baseline @20": item.get("baseline_recall_at_20"),
                    "target @20": item.get("comparison_recall_at_20"),
                    "Δ@20": item.get("delta_at_20"),
                }
            )
        st.markdown("#### 問題別の改善・悪化")
        st.caption(
            f"baseline={comparison.get('baseline_run_id')} / "
            f"target={comparison.get('comparison_run_id')}。"
            "Δ@3の悪化順に表示します。"
        )
        st.dataframe(question_rows, width="stretch", hide_index=True)


def _render_posthoc_judge_controls(
    api_url: str,
    runs: list,
    *,
    dialogue: bool,
) -> None:
    """Run or re-run semantic judging without regenerating QA answers."""
    import requests

    completed = [
        str(run["run_id"])
        for run in runs
        if run.get("has_scores") and run.get("run_id")
    ]
    if not completed:
        return
    prefix = "dialogue_ab_posthoc_judge" if dialogue else "locomo_posthoc_judge"
    with st.expander("🧠 既存runをSemantic Judgeで判定 / 再判定"):
        run_id = st.selectbox(
            "判定するrun",
            options=completed,
            key=f"{prefix}_run",
        )
        choice = _evaluation_judge_model(
            api_url,
            {},
            _get_connections(api_url),
            key_prefix=f"{prefix}_model",
        )
        max_output_tokens = st.number_input(
            "Judge max output tokens",
            min_value=1,
            value=2048,
            step=256,
            key=f"{prefix}_max_output_tokens",
        )
        st.caption(
            "QAと記憶構築は再実行しません。temperatureは0固定で、"
            "既存の公式スコアも保持します。"
        )
        st.caption(
            (
                "JudgeのConnectionへprompt・最小限の参照事実・A/B両回答を"
                if dialogue
                else "JudgeのConnectionへquestion・reference answer・predictionを"
            )
            + "送信します。記憶DB全体は送信しません。APIキーは判定promptや"
            "成果物に含めず、Connection認証にだけ使います。"
        )
        if st.button(
            "Semantic Judgeを開始",
            type="primary",
            disabled=not choice.model_name.strip(),
            key=f"{prefix}_start",
        ):
            path = (
                f"/evaluations/dialogue-ab/runs/{run_id}/judge"
                if dialogue
                else f"/evaluations/runs/{run_id}/judge"
            )
            try:
                response = requests.post(
                    f"{api_url}{path}",
                    json={
                        "connection": choice.connection_id,
                        "model_name": choice.model_name.strip(),
                        "max_output_tokens": int(max_output_tokens),
                    },
                    timeout=10,
                )
            except requests.RequestException as exc:
                st.error(f"Judge開始エラー: {exc}")
                return
            if not response.ok:
                st.error(_api_error_detail(response))
                return
            job = response.json()
            st.session_state.evaluation_selected_job = job["job_id"]
            st.success(
                f"Semantic Judgeを開始しました: {run_id} "
                f"(job {job['job_id'][:8]})"
            )


def _render_locomo_semantic_details(api_url: str, runs: list) -> None:
    """Show current per-question judge results and review candidates."""
    scoreable = [
        str(run["run_id"])
        for run in runs
        if run.get("has_scores") and run.get("run_id")
    ]
    if not scoreable:
        return

    with st.expander("🔎 問題別Semantic判定・レビュー候補"):
        run_id = st.selectbox(
            "詳細を表示するrun",
            options=scoreable,
            key="locomo_semantic_detail_run",
        )
        try:
            result = _evaluation_get(
                api_url,
                f"/evaluations/runs/{run_id}",
            )
        except Exception as exc:
            st.error(f"Semantic詳細取得エラー: {exc}")
            return

        semantic = result.get("semantic_judge")
        if not isinstance(semantic, dict):
            st.info("このrunはまだSemantic Judgeで判定されていません。")
            return
        if semantic.get("status") == "stale":
            st.warning(
                "保存済みSemantic判定は、現在の質問・参照回答・予測または"
                "Judge設定と一致しないため表示しません。再判定してください。\n\n"
                f"理由: {semantic.get('stale_reason') or '入力が変更されました'}"
            )
            return
        if semantic.get("status") == "partial":
            st.warning(
                "Semantic判定は一部未完了です。エラー項目を含むため、"
                "同じモデルで再判定すると未完了分を再試行します。"
            )

        review_count = result.get("review_required_count")
        st.caption(
            f"status={semantic.get('status') or 'unknown'} / "
            f"coverage={semantic.get('coverage')} / "
            f"レビュー候補={review_count if review_count is not None else '—'}"
        )
        review_only = st.checkbox(
            "レビュー候補だけ表示",
            value=True,
            key="locomo_semantic_review_only",
        )
        rows = []
        for item in result.get("questions") or []:
            if review_only and not item.get("review_required"):
                continue
            rows.append(
                {
                    "sample": item.get("sample_id"),
                    "question_id": item.get("question_id"),
                    "category": item.get("category"),
                    "question": item.get("question"),
                    "reference": item.get("expected_answer"),
                    "prediction": item.get("prediction"),
                    "official score": item.get("official_score"),
                    "semantic verdict": item.get("semantic_verdict"),
                    "semantic score": item.get("semantic_score"),
                    "confidence": item.get("semantic_confidence"),
                    "contradiction": item.get("semantic_contradiction"),
                    "critical missing": item.get(
                        "semantic_missing_critical"
                    ),
                    "official disagreement": item.get(
                        "official_disagreement"
                    ),
                    "review reasons": ", ".join(
                        str(reason)
                        for reason in (item.get("review_reasons") or [])
                    ),
                    "judge reason": item.get("semantic_reason"),
                    "judge error": item.get("semantic_error"),
                }
            )
        if not rows:
            st.success(
                "現在の条件に一致するレビュー候補はありません。"
                if review_only
                else "表示できる問題別判定はありません。"
            )
            return
        st.dataframe(rows, width="stretch", hide_index=True)
        st.caption(
            "レビュー候補: partial、矛盾、重要情報欠落、low confidence、"
            "公式スコアとのpossible false positive / false negative。"
        )


def _render_evaluation_history(api_url: str, runs: list) -> None:
    import requests

    st.subheader("Run履歴・スコア比較")
    if not runs:
        st.info("評価runがありません。")
        return

    history_rows = [
        {
            "run_id": run.get("run_id"),
            "workflow": run.get("workflow"),
            "mode": run.get("run_mode"),
            "status": run.get("status"),
            "overall": run.get("overall"),
            "questions": run.get("question_count"),
            "retrieval questions": run.get("retrieval_question_count"),
            "sample IDs": ", ".join(run.get("selected_sample_ids") or []),
            "exact_match": run.get("exact_match_rate"),
            "evidence": run.get("evidence_retrieval_rate"),
            "rag_trigger": run.get("rag_trigger_rate"),
            "search_exec": run.get("search_execution_rate"),
            "recall@3": run.get("retrieval_recall_at_3"),
            "query生成": run.get("retrieval_query_rate"),
            "dual original@3": run.get("dual_query_original_recall_at_3"),
            "dual rewrite@3": run.get("dual_query_rewrite_recall_at_3"),
            "dual rescue": run.get("dual_query_rescue_rate_at_3"),
            "dual harm": run.get("dual_query_harm_rate_at_3"),
            "rerank done": run.get("reranker_completion_rate"),
            "rerank fallback": run.get("reranker_fallback_rate"),
            "rerank rescue": run.get("reranker_rescue_rate_at_3"),
            "rerank harm": run.get("reranker_harm_rate_at_3"),
            "fusion done": run.get("evidence_fusion_completion_rate"),
            "fusion fallback": run.get("evidence_fusion_fallback_rate"),
            "fusion rescue": run.get("evidence_fusion_rescue_rate_at_3"),
            "fusion harm": run.get("evidence_fusion_harm_rate_at_3"),
            "clf_fallback": run.get("classifier_fallback_rate"),
            "QA retries": run.get("qa_retry_total"),
            "QA retry rate": run.get("qa_retry_question_rate"),
            "latency_ms": run.get("latency_ms_mean"),
            "prompt_tokens": run.get("prompt_tokens_total"),
            "cards": run.get("knowledge_cards_created"),
            "semantic score": run.get("semantic_score_mean"),
            "semantic pass": run.get("semantic_pass_rate"),
            "semantic coverage": run.get("semantic_coverage"),
            "semantic status": run.get("semantic_status"),
            "judge review": run.get("semantic_review_count"),
            "judge model": run.get("semantic_judge_model"),
            "source_run": run.get("source_run_id"),
            "created_at": run.get("created_at"),
        }
        for run in runs
    ]
    st.dataframe(history_rows, width="stretch", hide_index=True)
    st.caption(
        "evidence は全問で割った値です。rag_trigger が低い run では検索品質と"
        "無関係に下がるため、2列を併せて読んでください。search_exec は検索の"
        "実行率（注入率とは別）、recall@3 は注入前の候補で測ったランキング品質です。"
        "fusion rescue/harm は元hybrid top3との比較です。"
    )
    _render_posthoc_judge_controls(api_url, runs, dialogue=False)

    _render_locomo_semantic_details(api_url, runs)

    _render_retrieval_replay(api_url, runs)

    _render_retrieval_replay_comparison(api_url, runs)

    scoreable = [
        run["run_id"]
        for run in runs
        if run.get("has_scores") and run.get("run_id")
    ]
    selected_runs = st.multiselect(
        "比較するrun（先頭がbaseline、末尾が比較対象）",
        options=scoreable,
        max_selections=8,
        key="evaluation_compare_runs",
    )
    if st.button(
        "スコアを比較",
        disabled=len(selected_runs) < 2,
        key="evaluation_compare",
    ):
        try:
            response = requests.post(
                f"{api_url}/evaluations/runs/compare",
                json={"run_ids": selected_runs},
                timeout=20,
            )
            if response.ok:
                st.session_state.evaluation_comparison = response.json()
            else:
                st.error(_api_error_detail(response))
        except Exception as exc:
            st.error(f"比較エラー: {exc}")

    comparison = st.session_state.get("evaluation_comparison")
    if not comparison:
        return
    compared_ids = [
        run.get("run_id") for run in comparison.get("runs", [])
    ]
    if selected_runs and compared_ids != selected_runs:
        st.caption("表示中の比較は前回選択したrunです。")

    metric_rows = [
        {
            "run_id": run.get("run_id"),
            "overall": run.get("overall"),
            "exact_match": run.get("exact_match_rate"),
            "containment": run.get("answer_containment_rate"),
            "evidence": run.get("evidence_retrieval_rate"),
            "rag_trigger": run.get("rag_trigger_rate"),
            "search_exec": run.get("search_execution_rate"),
            "recall@3": run.get("retrieval_recall_at_3"),
            "bm25_rescue": run.get("bm25_rescue_rate"),
            "rerank done": run.get("reranker_completion_rate"),
            "rerank fallback": run.get("reranker_fallback_rate"),
            "rerank rescue": run.get("reranker_rescue_rate_at_3"),
            "rerank harm": run.get("reranker_harm_rate_at_3"),
            "rerank p95 ms": run.get("reranker_latency_ms_p95"),
            "fusion done": run.get("evidence_fusion_completion_rate"),
            "fusion fallback": run.get("evidence_fusion_fallback_rate"),
            "fusion rescue": run.get("evidence_fusion_rescue_rate_at_3"),
            "fusion harm": run.get("evidence_fusion_harm_rate_at_3"),
            "fusion p95 ms": run.get("evidence_fusion_latency_ms_p95"),
            "clf_fallback": run.get("classifier_fallback_rate"),
            "QA retries": run.get("qa_retry_total"),
            "QA retry rate": run.get("qa_retry_question_rate"),
            "latency_ms": run.get("latency_ms_mean"),
            "prompt_tokens": run.get("prompt_tokens_total"),
            "completion_tokens": run.get("completion_tokens_total"),
            "cards": run.get("knowledge_cards_created"),
            "failures": run.get("sleeptime_failures"),
            "semantic score": run.get("semantic_score_mean"),
            "semantic pass": run.get("semantic_pass_rate"),
            "semantic coverage": run.get("semantic_coverage"),
            "semantic status": run.get("semantic_status"),
            "judge review": run.get("semantic_review_count"),
        }
        for run in comparison.get("runs", [])
    ]
    st.markdown("#### 指標比較")
    st.dataframe(metric_rows, width="stretch", hide_index=True)

    baseline = comparison.get("baseline_run_id")
    target = comparison.get("comparison_run_id")
    question_rows = []
    for question in comparison.get("questions", []):
        by_run = question.get("runs", {})
        question_rows.append(
            {
                "sample_id": question.get("sample_id"),
                "question_id": question.get("question_id"),
                "question": question.get("question"),
                "expected": question.get("expected_answer"),
                f"{baseline} score": by_run.get(baseline, {}).get("score"),
                f"{target} score": by_run.get(target, {}).get("score"),
                "delta": question.get("delta"),
                f"{baseline} recall@3": by_run.get(baseline, {}).get(
                    "retrieval_recall_at_3"
                ),
                f"{target} recall@3": by_run.get(target, {}).get(
                    "retrieval_recall_at_3"
                ),
                f"{target} hybrid@3": by_run.get(target, {}).get(
                    "hybrid_recall_at_3"
                ),
                f"{target} fusion": by_run.get(target, {}).get(
                    "evidence_fusion_status"
                ),
                f"{target} fusion fallback": by_run.get(target, {}).get(
                    "evidence_fusion_fallback"
                ),
                f"{target} fusion ms": by_run.get(target, {}).get(
                    "evidence_fusion_latency_ms"
                ),
                f"{baseline} answer": by_run.get(baseline, {}).get(
                    "prediction"
                ),
                f"{target} answer": by_run.get(target, {}).get(
                    "prediction"
                ),
            }
        )
    st.markdown("#### 問題別差分")
    st.caption("deltaの小さい順（悪化した問題が先）")
    st.dataframe(question_rows, width="stretch", hide_index=True)


@st.fragment
def _render_dialogue_ab_form(
    api_url: str,
    evaluation_config: dict,
    runs: list,
) -> None:
    import requests

    config = evaluation_config.get("dialogue_ab") or {}
    previous = config.get("last_request")
    if not isinstance(previous, dict):
        previous = {}
    candidates = config.get("dataset_candidates") or []
    existing_run_ids = [
        str(run["run_id"]) for run in runs if run.get("run_id")
    ]
    previous_run_id = str(previous.get("run_id") or "")
    suggested_run_id = (
        _evaluation_next_run_id(previous_run_id, existing_run_ids)
        if previous_run_id
        else "ja_dialogue_ab_v1"
    )
    # RUN_ID の更新は widget キーへ直接書けない（インスタンス化後の代入は
    # StreamlitAPIException）。開始成功時は pending キーへ置き、次の描画の
    # 冒頭＝widget を作る前に反映する（LoCoMo 側の evaluation_pending_run_id と同じ）。
    pending_run_id = st.session_state.pop("dialogue_ab_pending_run_id", None)
    if pending_run_id:
        st.session_state.dialogue_ab_run_id = pending_run_id
    elif "dialogue_ab_run_id" not in st.session_state:
        st.session_state.dialogue_ab_run_id = suggested_run_id

    st.subheader("日本語対話 injection policy A/B")
    st.caption(
        "同一の日本語メモリを一度だけ生成し、30件の各プロンプトを毎回"
        "クリーンな複製へ入力します。比較条件は intent_gated と candidates、"
        "検索条件は両armで共通です。"
    )
    st.info(
        "従来のトークン・RAG注入・対象語recallに加え、"
        "Semantic Judgeを有効にすると正誤・部分正解・不要な"
        "記憶の持ち出しを匿名A/Bで判定します。"
    )

    dataset_default = str(
        previous.get("dataset_path")
        or (candidates[0] if candidates else "")
    )
    dataset_path = st.text_input(
        "日本語対話dataset path",
        value=dataset_default,
        key="dialogue_ab_dataset_path",
    )
    top_cols = st.columns(2)
    with top_cols[0]:
        run_id = st.text_input(
            "RUN_ID",
            key="dialogue_ab_run_id",
        )
    with top_cols[1]:
        stage3_enabled = st.checkbox(
            "Stage3を有効化",
            value=bool(previous.get("stage3_enabled", True)),
            key="dialogue_ab_stage3_enabled",
            help=(
                "記憶生成時にノードも作り、両policyで同じノードを利用します。"
                "既存インスタンスを種にする場合は Sleeptime を回さないため無視されます。"
            ),
        )

    seed_options = ["（datasetのmemory_seedから作る）"] + list(
        config.get("seed_instances") or []
    )
    seed_cols = st.columns([2, 1])
    with seed_cols[0]:
        seed_choice = st.selectbox(
            "記憶の種",
            options=seed_options,
            index=_evaluation_option_index(
                seed_options,
                previous.get("seed_instance") or seed_options[0],
            ),
            key="dialogue_ab_seed_instance",
            help=(
                "既存インスタンスを選ぶと、その記憶を run workspace へ複製して"
                "そのまま使います（本番インスタンスは変更しません／Sleeptimeも回しません）。"
            ),
        )
    seed_instance = None if seed_choice == seed_options[0] else seed_choice
    with seed_cols[1]:
        reembed = st.checkbox(
            "カードを再embedding",
            value=bool(previous.get("reembed", False)),
            key="dialogue_ab_reembed",
            disabled=seed_instance is None,
            help=(
                "複製側のカードだけを上のembeddingモデルで貼り直します。"
                "既定OFF＝保存済みベクトルをそのまま使う（本番の検索を再現）。"
            ),
        )
    if seed_instance:
        st.caption(
            f"種: `butly_core/instances/{seed_instance}` を複製して使います。"
            "検索を本番と揃えるなら再embeddingはOFFのまま、"
            "別の埋め込みモデルで比較したいときだけONにしてください。"
        )

    with st.expander("RAG・コンテキスト設定", expanded=True):
        context_cols = st.columns(4)
        with context_cols[0]:
            context_current_time = st.checkbox(
                "Current Time",
                value=bool(previous.get("context_current_time", True)),
                key="dialogue_ab_context_current_time",
            )
        with context_cols[1]:
            context_mid_term = st.checkbox(
                "Mid-term",
                value=bool(previous.get("context_mid_term", True)),
                key="dialogue_ab_context_mid_term",
            )
        with context_cols[2]:
            context_session_digest = st.checkbox(
                "Session Digest",
                value=bool(previous.get("context_session_digest", True)),
                key="dialogue_ab_context_session_digest",
            )
        with context_cols[3]:
            context_rag = st.checkbox(
                "RAG",
                value=bool(previous.get("context_rag", True)),
                key="dialogue_ab_context_rag",
            )
        rag_cols = st.columns(5)
        with rag_cols[0]:
            rag_source_mode = st.selectbox(
                "RAG source",
                options=["cards", "raw", "both"],
                index=_evaluation_option_index(
                    ["cards", "raw", "both"],
                    previous.get("rag_source_mode", "both"),
                ),
                key="dialogue_ab_rag_source_mode",
            )
        with rag_cols[1]:
            rag_raw_top_k = st.number_input(
                "RAW top-k",
                min_value=0,
                value=int(previous.get("rag_raw_top_k", 1)),
                step=1,
                key="dialogue_ab_rag_raw_top_k",
            )
        with rag_cols[2]:
            rag_raw_max_chars = st.number_input(
                "RAW max chars",
                min_value=0,
                value=int(previous.get("rag_raw_max_chars", 2500)),
                step=100,
                key="dialogue_ab_rag_raw_max_chars",
            )
        with rag_cols[3]:
            rag_raw_neighbor_radius = st.number_input(
                "RAW neighbor ±N",
                min_value=0,
                max_value=10,
                value=int(previous.get("rag_raw_neighbor_radius", 0)),
                step=1,
                help=(
                    "0で無効。1なら正確なsource_filesを優先した後、"
                    "同一source_date内の前後1ファイルを追加します。"
                ),
                key="dialogue_ab_rag_raw_neighbor_radius",
            )
        with rag_cols[4]:
            time_decay_rate = st.number_input(
                "Time decay rate",
                min_value=0.0,
                value=float(previous.get("time_decay_rate", 0.003)),
                step=0.001,
                format="%.3f",
                key="dialogue_ab_time_decay_rate",
            )

        st.markdown("##### 検索設定")
        search_mode_options = (
            config.get("search_modes")
            or evaluation_config.get("search_modes")
            or [
                "vector",
                "hybrid",
                "dual_query",
                "hybrid_evidence_fusion",
            ]
        )
        retrieval_execution_options = (
            config.get("retrieval_executions")
            or evaluation_config.get("retrieval_executions")
            or ["always", "intent_gated"]
        )
        search_cols = st.columns(3)
        with search_cols[0]:
            search_mode = st.selectbox(
                "Search mode",
                options=search_mode_options,
                index=_evaluation_option_index(
                    search_mode_options,
                    previous.get("search_mode", "vector"),
                ),
                key="dialogue_ab_search_mode",
                help=(
                    "vectorはベクトル検索のみ、hybridはBM25とベクトルを"
                    "RRFで融合します。dual_queryは元発話とGatekeeper検索文を"
                    "各15件検索し、重複排除してRRF融合します。"
                    "hybrid_evidence_fusionはhybrid上位候補をEpisode/RAWで"
                    "再評価して順位融合します。"
                ),
            )
        with search_cols[1]:
            retrieval_execution = st.selectbox(
                "Retrieval execution",
                options=retrieval_execution_options,
                index=_evaluation_option_index(
                    retrieval_execution_options,
                    previous.get("retrieval_execution", "always"),
                ),
                key="dialogue_ab_retrieval_execution",
                help=(
                    "alwaysは分類結果にかかわらず検索します。intent_gatedは"
                    "Gatekeeperが過去記憶を必要と判断した場合だけ検索します。"
                ),
            )
        with search_cols[2]:
            vector_search_threshold = st.number_input(
                "Vector threshold",
                min_value=0.0,
                max_value=1.0,
                value=float(previous.get("vector_search_threshold", 0.4)),
                step=0.05,
                format="%.2f",
                key="dialogue_ab_vector_search_threshold",
            )
        previous_common_limit = int(
            previous.get("vector_search_limit", 3)
        )
        limit_cols = st.columns(2)
        with limit_cols[0]:
            intent_gated_vector_search_limit = st.number_input(
                "intent_gated 注入候補上限",
                min_value=1,
                value=int(
                    previous.get(
                        "intent_gated_vector_search_limit",
                        previous_common_limit,
                    )
                ),
                step=1,
                key="dialogue_ab_intent_gated_vector_search_limit",
                help="intent_gated armのQuick Retrievalカード件数です。",
            )
        with limit_cols[1]:
            candidates_vector_search_limit = st.number_input(
                "candidates 注入候補上限",
                min_value=1,
                value=int(
                    previous.get(
                        "candidates_vector_search_limit",
                        previous_common_limit,
                    )
                ),
                step=1,
                key="dialogue_ab_candidates_vector_search_limit",
                help="candidates armのQuick Retrievalカード件数です。",
            )
        deep_search_enabled = st.checkbox(
            "Quick Retrievalが0件のときDeep Searchを行う",
            value=bool(previous.get("deep_search_enabled", True)),
            key="dialogue_ab_deep_search_enabled",
        )
        if retrieval_execution != "always":
            st.warning(
                "Retrieval executionがintent_gatedの場合、分類器がnullの質問では"
                "candidates armでも検索・注入されません。"
            )

        bm25_candidates = int(previous.get("bm25_candidates", 20))
        vector_candidates = int(previous.get("vector_candidates", 20))
        dual_query_candidates = int(
            previous.get("dual_query_candidates", 15)
        )
        dual_query_pool_limit = int(
            previous.get("dual_query_pool_limit", 25)
        )
        rrf_k = int(previous.get("rrf_k", 60))
        bm25_max_df_ratio = float(
            previous.get("bm25_max_df_ratio", 0.5)
        )
        evidence_fusion_base_weight = float(
            previous.get("evidence_fusion_base_weight", 0.7)
        )
        evidence_raw_chunk_chars = int(
            previous.get("evidence_raw_chunk_chars", 1800)
        )
        if search_mode in {"hybrid", "hybrid_evidence_fusion"}:
            hybrid_cols = st.columns(4)
            with hybrid_cols[0]:
                bm25_candidates = st.number_input(
                    "BM25 candidates",
                    min_value=1,
                    value=bm25_candidates,
                    step=1,
                    key="dialogue_ab_bm25_candidates",
                )
            with hybrid_cols[1]:
                vector_candidates = st.number_input(
                    "Vector candidates",
                    min_value=1,
                    value=vector_candidates,
                    step=1,
                    key="dialogue_ab_vector_candidates",
                )
            with hybrid_cols[2]:
                rrf_k = st.number_input(
                    "RRF k",
                    min_value=1,
                    value=rrf_k,
                    step=1,
                    key="dialogue_ab_rrf_k",
                )
            with hybrid_cols[3]:
                bm25_max_df_ratio = st.number_input(
                    "BM25 max DF ratio",
                    min_value=0.01,
                    max_value=1.0,
                    value=bm25_max_df_ratio,
                    step=0.05,
                    format="%.2f",
                    key="dialogue_ab_bm25_max_df_ratio",
                )
            if search_mode == "hybrid_evidence_fusion":
                fusion_cols = st.columns(2)
                with fusion_cols[0]:
                    evidence_fusion_base_weight = st.number_input(
                        "Fusion hybrid weight",
                        min_value=0.0,
                        max_value=1.0,
                        value=evidence_fusion_base_weight,
                        step=0.05,
                        key="dialogue_ab_evidence_fusion_base_weight",
                    )
                with fusion_cols[1]:
                    evidence_raw_chunk_chars = st.number_input(
                        "Evidence RAW chunk chars",
                        min_value=200,
                        max_value=10000,
                        value=evidence_raw_chunk_chars,
                        step=100,
                        key="dialogue_ab_evidence_raw_chunk_chars",
                    )
                st.caption(
                    "両armは同じEvidence設定とrun内embedding cacheを共有します。"
                    "質問と候補Episode/RAWはEmbedding Connectionへ送信されます。"
                    "RAW本文/API keyはcacheへ保存せず、失敗時はhybridへ戻ります。"
                )
        elif search_mode == "dual_query":
            dual_cols = st.columns(3)
            with dual_cols[0]:
                dual_query_candidates = st.number_input(
                    "Candidates / query",
                    min_value=1,
                    value=dual_query_candidates,
                    step=1,
                    key="dialogue_ab_dual_query_candidates",
                )
            with dual_cols[1]:
                dual_query_pool_limit = st.number_input(
                    "Deduped pool limit",
                    min_value=1,
                    value=dual_query_pool_limit,
                    step=1,
                    key="dialogue_ab_dual_query_pool_limit",
                )
            with dual_cols[2]:
                rrf_k = st.number_input(
                    "RRF k",
                    min_value=1,
                    value=rrf_k,
                    step=1,
                    key="dialogue_ab_dual_query_rrf_k",
                )
            st.caption(
                "両armで同じprompt・Gatekeeper設定を使います。生成された検索文と"
                "両ランキングはarm別に保存するため、差が出た場合も確認できます。"
            )

    stage3_batch_size = int(previous.get("stage3_batch_size", 10))
    stage3_bootstrap_max_cards = int(
        previous.get("stage3_bootstrap_max_cards", 2000)
    )
    if stage3_enabled:
        with st.expander("Stage3設定"):
            stage_cols = st.columns(2)
            with stage_cols[0]:
                stage3_batch_size = st.number_input(
                    "Batch size",
                    min_value=1,
                    value=stage3_batch_size,
                    step=1,
                    key="dialogue_ab_stage3_batch_size",
                )
            with stage_cols[1]:
                stage3_bootstrap_max_cards = st.number_input(
                    "Bootstrap max cards",
                    min_value=1,
                    value=stage3_bootstrap_max_cards,
                    step=10,
                    key="dialogue_ab_stage3_bootstrap_max_cards",
                )

    try:
        provider_config = _cached_api_json(api_url, "/config").get(
            "AI_CONFIG",
            {},
        )
    except Exception:
        provider_config = {}
    provider_config = {
        role: dict(role_config)
        for role, role_config in provider_config.items()
        if isinstance(role_config, dict)
    }
    previous_models = previous.get("role_models") or {}
    if isinstance(previous_models, dict):
        for role, role_config in previous_models.items():
            if isinstance(role_config, dict):
                provider_config[role] = dict(role_config)

    with st.expander("モデル割り当て", expanded=True):
        role_choices = _evaluation_role_models(
            api_url,
            provider_config,
            _get_connections(api_url),
            key_prefix="dialogue_ab_model",
        )
        embedding_profile = _evaluation_embedding_profile(
            role_choices["embedding"].model_name,
            provider_config.get("embedding", {}).get("profile"),
            widget_key="dialogue_ab_embedding_profile",
        )
        generation_values = {}
        generation_cols = st.columns(4)
        temperatures = {
            "chat": 0.0,
            "gatekeeper": 0.0,
            "summary": 0.3,
            "knowledge": 0.2,
        }
        for column, (role, fallback) in zip(
            generation_cols,
            temperatures.items(),
        ):
            previous_generation = (
                previous_models.get(role, {}).get("generation_config", {})
                if isinstance(previous_models.get(role), dict)
                else {}
            )
            current_generation = (
                previous_generation
                if isinstance(previous_generation, dict)
                else {}
            )
            with column:
                temperature = st.number_input(
                    f"{role} temperature",
                    min_value=0.0,
                    max_value=2.0,
                    value=float(
                        current_generation.get("temperature", fallback)
                    ),
                    step=0.1,
                    key=f"dialogue_ab_temperature_{role}",
                )
                generation_values[role] = {
                    "temperature": float(temperature)
                }
        gatekeeper_max_tokens = st.number_input(
            "Gatekeeper max output tokens",
            min_value=1,
            value=int(
                (
                    previous_models.get("gatekeeper", {})
                    .get("generation_config", {})
                    if isinstance(previous_models.get("gatekeeper"), dict)
                    else {}
                ).get("max_output_tokens", 2048)
            ),
            step=128,
            key="dialogue_ab_gatekeeper_max_tokens",
        )
        generation_values["gatekeeper"]["max_output_tokens"] = int(
            gatekeeper_max_tokens
        )
        from evals.locomo.web_jobs import gatekeeper_token_warning

        warning = gatekeeper_token_warning(
            role_choices["gatekeeper"].model_name,
            gatekeeper_max_tokens,
        )
        if warning:
            st.warning(warning)

        st.markdown("##### Semantic Judge（任意）")
        dialogue_judge_enabled = st.checkbox(
            "A/B回答をAIで匿名・順序反転判定する",
            value="judge" in previous_models,
            key="dialogue_ab_judge_enabled",
            help=(
                "A/Bの表示順を入れ替えて2回判定し、不一致は"
                "要人手確認として残します。"
            ),
        )
        dialogue_judge_choice = None
        dialogue_judge_max_output_tokens = 2048
        if dialogue_judge_enabled:
            current_judge = previous_models.get("judge") or {}
            dialogue_judge_choice = _evaluation_judge_model(
                api_url,
                current_judge,
                _get_connections(api_url),
                key_prefix="dialogue_ab_judge_model",
            )
            dialogue_judge_max_output_tokens = st.number_input(
                "Judge max output tokens",
                min_value=1,
                value=int(
                    (current_judge.get("generation_config") or {}).get(
                        "max_output_tokens", 2048
                    )
                ),
                step=256,
                key="dialogue_ab_judge_max_output_tokens",
            )
            st.caption(
                "temperatureは0に固定。回答生成とは別系統の"
                "モデルを推奨します。"
            )
            st.caption(
                "JudgeのConnectionへprompt・最小限の参照事実・A/B両回答を"
                "送信します。記憶DB全体は送信しません。APIキーは判定promptや"
                "成果物に含めず、Connection認証にだけ使います。"
            )

    if st.button(
        "▶ 日本語対話A/Bを開始",
        type="primary",
        width="stretch",
        disabled=not dataset_path.strip() or not run_id.strip(),
        key="dialogue_ab_start",
    ):
        role_models = {}
        empty_roles = []
        for role, choice in role_choices.items():
            if not choice.model_name.strip():
                empty_roles.append(role)
                continue
            role_models[role] = {
                "connection": choice.connection_id,
                "model_name": choice.model_name.strip(),
                "generation_config": generation_values.get(role, {}),
            }
            if role == "embedding" and embedding_profile:
                role_models[role]["profile"] = embedding_profile
        if dialogue_judge_enabled:
            if (
                dialogue_judge_choice is None
                or not dialogue_judge_choice.model_name.strip()
            ):
                empty_roles.append("judge")
            else:
                role_models["judge"] = {
                    "connection": dialogue_judge_choice.connection_id,
                    "model_name": dialogue_judge_choice.model_name.strip(),
                    "generation_config": {
                        "temperature": 0.0,
                        "max_output_tokens": int(
                            dialogue_judge_max_output_tokens
                        ),
                    },
                }
        if empty_roles:
            st.error(
                "モデルが未設定のロールがあります: "
                + ", ".join(empty_roles)
            )
            return
        payload = {
            "dataset_path": dataset_path.strip(),
            "run_id": run_id.strip(),
            "time_decay_rate": float(time_decay_rate),
            "context_current_time": context_current_time,
            "context_mid_term": context_mid_term,
            "context_session_digest": context_session_digest,
            "context_rag": context_rag,
            "rag_source_mode": rag_source_mode,
            "rag_raw_top_k": int(rag_raw_top_k),
            "rag_raw_max_chars": int(rag_raw_max_chars),
            "rag_raw_neighbor_radius": int(rag_raw_neighbor_radius),
            "stage3_enabled": stage3_enabled,
            "stage3_batch_size": int(stage3_batch_size),
            "stage3_bootstrap_max_cards": int(
                stage3_bootstrap_max_cards
            ),
            "search_mode": search_mode,
            "retrieval_execution": retrieval_execution,
            # profileの共通初期値。runnerが各arm開始時に個別値へ上書きする。
            "vector_search_limit": int(
                intent_gated_vector_search_limit
            ),
            "intent_gated_vector_search_limit": int(
                intent_gated_vector_search_limit
            ),
            "candidates_vector_search_limit": int(
                candidates_vector_search_limit
            ),
            "vector_search_threshold": float(vector_search_threshold),
            "deep_search_enabled": bool(deep_search_enabled),
            "bm25_candidates": int(bm25_candidates),
            "vector_candidates": int(vector_candidates),
            "dual_query_candidates": int(dual_query_candidates),
            "dual_query_pool_limit": int(dual_query_pool_limit),
            "rrf_k": int(rrf_k),
            "bm25_max_df_ratio": float(bm25_max_df_ratio),
            "evidence_fusion_base_weight": float(
                evidence_fusion_base_weight
            ),
            "evidence_raw_chunk_chars": int(evidence_raw_chunk_chars),
            "role_models": role_models,
            "seed_instance": seed_instance,
            "reembed": bool(reembed) and seed_instance is not None,
        }
        # 起動リクエストの失敗だけを「開始エラー」として扱う。開始後の UI 更新まで
        # 同じ except で包むと、ジョブは走っているのに「開始エラー」と出て誤解する。
        try:
            response = requests.post(
                f"{api_url}/evaluations/dialogue-ab/jobs",
                json=payload,
                timeout=10,
            )
        except requests.RequestException as exc:
            st.error(f"開始エラー: {exc}")
            return
        if not response.ok:
            st.error(_api_error_detail(response))
            return

        job = response.json()
        _evaluation_get.clear()
        st.session_state.evaluation_selected_job = job["job_id"]
        st.session_state.dialogue_ab_pending_run_id = _evaluation_next_run_id(
            str(job["run_id"]),
            [*existing_run_ids, str(job["run_id"])],
        )
        st.success(
            f"日本語対話A/Bを開始しました: {job['run_id']} "
            f"(job {job['job_id'][:8]})"
        )


def _render_dialogue_ab_history(api_url: str, runs: list) -> None:
    if not runs:
        st.info("日本語対話A/B runはまだありません。")
        return
    rows = [
        {
            "run_id": run.get("run_id"),
            "status": run.get("status"),
            "prompts": run.get("prompt_count"),
            "cards": run.get("knowledge_cards_created"),
            "intent RAG": run.get("intent_rag_trigger_rate"),
            "candidates RAG": run.get("candidates_rag_trigger_rate"),
            "token Δ/turn": run.get("prompt_tokens_mean_delta"),
            "required recall Δ": run.get("required_recall_delta"),
            "irrelevant mention Δ": run.get("irrelevant_mention_delta"),
            "judge score Δ": run.get("semantic_score_delta"),
            "judge review": run.get("semantic_review_count"),
            "judge winners": run.get("semantic_winner_counts"),
            "judge model": run.get("semantic_judge_model"),
            "created_at": run.get("created_at"),
        }
        for run in runs
    ]
    st.markdown("#### A/B履歴")
    st.dataframe(rows, width="stretch", hide_index=True)

    _render_posthoc_judge_controls(api_url, runs, dialogue=True)

    completed = [
        run["run_id"]
        for run in runs
        if run.get("has_scores") and run.get("run_id")
    ]
    if not completed:
        return
    selected_run = st.selectbox(
        "回答差分を表示するrun",
        options=completed,
        key="dialogue_ab_result_run",
    )
    try:
        scores = _evaluation_get(
            api_url,
            f"/evaluations/dialogue-ab/runs/{selected_run}",
        )
    except Exception as exc:
        st.error(f"A/B結果取得エラー: {exc}")
        return

    policy_rows = []
    for policy in ("intent_gated", "candidates"):
        stats = scores.get("policies", {}).get(policy, {})
        policy_rows.append(
            {
                "policy": policy,
                "RAG trigger": stats.get("rag_trigger_rate"),
                "search exec": stats.get("search_execution_rate"),
                "retrieval query": stats.get("retrieval_query_rate"),
                "dual fusion exec": stats.get("dual_query_execution_rate"),
                "prompt tokens mean": stats.get("prompt_tokens_mean"),
                "latency ms mean": stats.get("latency_ms_mean"),
                "latency p95": stats.get("latency_ms_p95"),
                "required recall": stats.get("required_target_recall"),
                "irrelevant mention": stats.get(
                    "irrelevant_seed_mention_rate"
                ),
            }
        )
    st.markdown("#### Policy指標")
    st.dataframe(policy_rows, width="stretch", hide_index=True)

    category = st.selectbox(
        "表示カテゴリ",
        options=[
            "all",
            "memory_required",
            "memory_irrelevant",
            "memory_optional",
        ],
        key="dialogue_ab_result_category",
    )
    prompt_rows = []
    for prompt in scores.get("prompts", []):
        if category != "all" and prompt.get("category") != category:
            continue
        arms = prompt.get("arms") or {}
        intent = arms.get("intent_gated") or {}
        candidates = arms.get("candidates") or {}
        judgment = prompt.get("judgment") or {}
        judged_arms = judgment.get("arms") or {}
        judged_intent = judged_arms.get("intent_gated") or {}
        judged_candidates = judged_arms.get("candidates") or {}
        prompt_rows.append(
            {
                "id": prompt.get("prompt_id"),
                "category": prompt.get("category"),
                "prompt": prompt.get("prompt"),
                "review": prompt.get("review_point"),
                "intent RAG": intent.get("rag_triggered"),
                "candidate RAG": candidates.get("rag_triggered"),
                "intent query": intent.get("retrieval_query"),
                "intent query status": intent.get(
                    "retrieval_query_status"
                ),
                "candidate query": candidates.get("retrieval_query"),
                "candidate query status": candidates.get(
                    "retrieval_query_status"
                ),
                "intent original top3": ", ".join(
                    str(value)
                    for value in (intent.get("original_candidate_ids") or [])[:3]
                ),
                "intent rewrite top3": ", ".join(
                    str(value)
                    for value in (
                        intent.get("retrieval_query_candidate_ids") or []
                    )[:3]
                ),
                "intent fused top3": ", ".join(
                    str(value)
                    for value in (intent.get("fused_candidate_ids") or [])[:3]
                ),
                "candidate original top3": ", ".join(
                    str(value)
                    for value in (
                        candidates.get("original_candidate_ids") or []
                    )[:3]
                ),
                "candidate rewrite top3": ", ".join(
                    str(value)
                    for value in (
                        candidates.get("retrieval_query_candidate_ids") or []
                    )[:3]
                ),
                "candidate fused top3": ", ".join(
                    str(value)
                    for value in (candidates.get("fused_candidate_ids") or [])[:3]
                ),
                "token Δ": prompt.get("prompt_tokens_delta"),
                "intent answer": intent.get("response"),
                "candidates answer": candidates.get("response"),
                "judge winner": judgment.get("winner"),
                "needs review": judgment.get("review_required"),
                "intent verdict": judged_intent.get("label"),
                "candidates verdict": judged_candidates.get("label"),
                "intent reason": " / ".join(
                    str(item)
                    for item in (judged_intent.get("reasons") or [])
                ),
                "candidates reason": " / ".join(
                    str(item)
                    for item in (judged_candidates.get("reasons") or [])
                ),
            }
        )
    st.markdown("#### プロンプト別回答")
    st.dataframe(prompt_rows, width="stretch", hide_index=True)


def render_evaluation_screen():
    api_url = st.session_state.api_base_url
    header_cols = st.columns([1, 8])
    with header_cols[0]:
        if st.button("＜ 戻る", key="evaluation_back"):
            navigate_to("home")
    with header_cols[1]:
        st.markdown(
            '<h1 class="app-title">📊 Butly Evaluation Console</h1>',
            unsafe_allow_html=True,
        )
    st.divider()

    sections = [
        "▶ LoCoMo評価",
        "🗣 日本語対話A/B",
        "⚙️ ジョブ",
        "📈 LoCoMo履歴・比較",
    ]
    section = st.segmented_control(
        "評価画面",
        options=sections,
        default=sections[0],
        key="evaluation_console_section",
        label_visibility="collapsed",
        width="stretch",
    )

    try:
        if section == sections[0]:
            evaluation_config = _evaluation_get(
                api_url,
                "/evaluations/config",
            )
            runs_payload = _evaluation_get(
                api_url,
                "/evaluations/runs",
            )
            runs = runs_payload.get("runs", [])
            st.caption(
                "LoCoMo保存先: "
                f"`{runs_payload.get('output_dir') or evaluation_config.get('output_dir')}`"
            )
            _render_model_catalog_refresh(
                api_url,
                key="evaluation_locomo_model_catalog_refresh",
            )
            _render_evaluation_start_form(
                api_url,
                evaluation_config,
                runs,
            )
        elif section == sections[1]:
            evaluation_config = _evaluation_get(
                api_url,
                "/evaluations/config",
            )
            dialogue_runs_payload = _evaluation_get(
                api_url,
                "/evaluations/dialogue-ab/runs",
            )
            dialogue_runs = dialogue_runs_payload.get("runs", [])
            st.caption(
                "日本語対話A/B保存先: "
                f"`{dialogue_runs_payload.get('output_dir')}`"
            )
            _render_model_catalog_refresh(
                api_url,
                key="evaluation_dialogue_model_catalog_refresh",
            )
            _render_dialogue_ab_form(
                api_url,
                evaluation_config,
                dialogue_runs,
            )
            _render_dialogue_ab_history(api_url, dialogue_runs)
        elif section == sections[2]:
            _render_evaluation_jobs(api_url)
        else:
            runs_payload = _evaluation_get(
                api_url,
                "/evaluations/runs",
            )
            runs = runs_payload.get("runs", [])
            st.caption(
                "LoCoMo保存先: "
                f"`{runs_payload.get('output_dir')}`"
            )
            _render_evaluation_history(api_url, runs)
    except Exception as exc:
        st.error(
            "評価APIへ接続できません。backendを再起動して最新コードを"
            f"読み込んでください。\n\n{exc}"
        )


# ==========================================
# 🧹 Sleeptime画面
# ==========================================
def render_sleeptime_screen():
    import requests

    instance_name = (
        st.session_state.sleeptime_instance or st.session_state.current_instance
    )
    api_url = st.session_state.api_base_url

    col1, col2 = st.columns([1, 8])
    with col1:
        if st.button("＜ 戻る", key="hk_back"):
            navigate_to("chat")
    with col2:
        st.markdown(
            f'<h1 class="app-title">🧹 記憶の整理: {instance_name}</h1>',
            unsafe_allow_html=True,
        )
    st.divider()

    # 実行中かどうかをまずサーバーに確認
    is_running = st.session_state.get("hk_running", False)
    try:
        status_resp = requests.get(
            f"{api_url}/sleeptime/status/{instance_name}", timeout=5
        )
        if status_resp.ok:
            server_status = status_resp.json()
            if server_status.get("state") == "running":
                is_running = True
                st.session_state["hk_running"] = True
    except Exception:
        pass

    if is_running:
        # --- 実行中UI ---
        st.warning("⚠️ 記憶の整理を実行中です。完了するまでチャットは控えてください。")
        status_placeholder = st.empty()
        progress_bar = st.progress(0)
        for _ in range(600):  # 最大10分ポーリング
            try:
                r = requests.get(
                    f"{api_url}/sleeptime/status/{instance_name}", timeout=5
                )
                status = r.json() if r.ok else {}
            except Exception:
                status = {}
            state = status.get("state", "running")
            msg = status.get("message", "処理中...")
            prog = int(status.get("progress", 0))
            status_placeholder.markdown(f"**🔄 {msg}**")
            progress_bar.progress(min(prog / 100.0, 1.0))
            if state in ("completed", "error"):
                st.session_state["hk_running"] = False
                if state == "completed":
                    st.success("✅ 記憶の整理が完了しました！")
                    st.balloons()
                else:
                    st.error(f"エラー: {msg}")
                break
            time.sleep(1)
    else:
        # --- 待機中UI ---
        # 推定情報の取得
        try:
            resp = requests.get(
                f"{api_url}/sleeptime/estimate/{instance_name}", timeout=5
            )
            est = resp.json() if resp.ok else {}
        except Exception:
            est = {}

        group_count = est.get("group_count", "?")
        est_seconds = est.get("estimated_seconds", "?")

        col_m1, col_m2 = st.columns(2)
        with col_m1:
            st.metric("未処理の記憶グループ", group_count)
        with col_m2:
            if isinstance(est_seconds, (int, float)) and est_seconds > 0:
                minutes = est_seconds // 60
                secs = est_seconds % 60
                time_str = (
                    f"{int(minutes)}分 {int(secs)}秒"
                    if minutes > 0
                    else f"約 {int(secs)} 秒"
                )
            else:
                time_str = f"約 {est_seconds} 秒"
            st.metric("予測所要時間", time_str)

        st.divider()

        st.caption(
            "短期記憶のログを整理し、知識カードとして長期記憶データベースに保存します。"
        )
        st.info(
            "💡 実行中はチャットを控えてください。記憶データへの同時アクセスで不整合が起きる可能性があります。"
        )

        if group_count == 0:
            st.success("整理する記憶はありません。すべて最新の状態です。")
        else:
            if st.button("▶ 整理を開始する", type="primary", width="stretch"):
                try:
                    r = requests.post(
                        f"{api_url}/sleeptime/run",
                        json={"instance_name": instance_name},
                        timeout=5,
                    )
                    if r.ok:
                        st.session_state["hk_running"] = True
                        st.rerun()
                    else:
                        st.error(f"エラー: {r.text}")
                except Exception as e:
                    st.error(f"サーバー接続エラー: {e}")


# ==========================================
# 🗋 DBブラウザ (Database Browser Screen)
# ==========================================
def render_database_browser_screen():
    from butly_core.core.database import ButlyDatabase

    col1, col2 = st.columns([1, 8])
    with col1:
        if st.button("＜ 戻る", key="db_back"):
            navigate_to("home")
    with col2:
        st.markdown(
            '<h1 class="app-title">🗋 データベースブラウザ</h1>', unsafe_allow_html=True
        )
    st.divider()

    # インスタンス選択 & フィルター
    col_f1, col_f2, col_f3 = st.columns([2, 2, 3])
    with col_f1:
        sel_inst = st.selectbox(
            "対象AI",
            available_instances,
            index=(
                available_instances.index(st.session_state.db_browser_instance)
                if st.session_state.db_browser_instance in available_instances
                else 0
            ),
            key="db_inst_sel",
        )
        st.session_state.db_browser_instance = sel_inst
    CATEGORIES = [
        "",
        "Unclassified",
        "UserPreference",
        "LifeEvent",
        "Task",
        "Thought",
        "Project",
    ]
    with col_f2:
        sel_cat = st.selectbox(
            "カテゴリ",
            CATEGORIES,
            format_func=lambda c: "すべて" if c == "" else c,
            key="db_cat_sel",
        )
    with col_f3:
        search_q = st.text_input(
            "🔍 検索", placeholder="キーワードを入力...", key="db_search"
        )

    # API経由でカード一覧を取得
    import requests

    api_url = st.session_state.api_base_url

    try:
        req_params = {"limit": 100, "offset": 0}
        if sel_cat:
            req_params["category"] = sel_cat
        if search_q:
            req_params["search"] = search_q

        resp = requests.get(
            f"{api_url}/database/cards/{sel_inst}", params=req_params, timeout=10
        )
        if resp.ok:
            rows = resp.json()
            # 取得した辞書のリストをソート
            rows = sorted(
                rows,
                key=lambda x: (x.get("is_pinned") or 0, x.get("ai_importance") or 0),
                reverse=True,
            )
        elif resp.status_code == 404:
            st.info("データベースがまだ存在しません。")
            rows = []
        else:
            st.error(f"サーバーエラー: {resp.text}")
            rows = []
    except Exception as e:
        st.error(f"API接続エラー: {e}")
        rows = []

    st.caption(f"{len(rows)} 件表示中")
    for row in rows:
        cid = row.get("id")
        title = row.get("title")
        cat = row.get("category")
        episode = row.get("episode")
        ai_imp = row.get("ai_importance")
        hu_imp = row.get("humanity_importance")
        pinned = row.get("is_pinned")
        pinned_icon = "📌" if pinned else ""
        with st.container(border=True):
            c1, c2 = st.columns([8, 1])
            with c1:
                st.markdown(
                    f"**{pinned_icon} {title}** `{cat}` &nbsp; ⭐{ai_imp} 💓{hu_imp}",
                    unsafe_allow_html=True,
                )
                st.caption(
                    (episode or "")[:120]
                    + ("..." if episode and len(episode) > 120 else "")
                )
            with c2:
                if st.button("✏️", key=f"edit_card_{cid}"):
                    st.session_state.card_edit_id = cid
                    st.session_state.db_browser_instance = sel_inst
                    navigate_to("card_edit")


# ==========================================
# ✏️ カード編集画面 (Card Edit Screen)
# ==========================================
def render_card_edit_screen():
    from butly_core.core.database import ButlyDatabase

    card_id = st.session_state.card_edit_id
    inst = st.session_state.db_browser_instance or st.session_state.current_instance

    col1, col2 = st.columns([1, 8])
    with col1:
        if st.button("＜ 戻る", key="ce_back"):
            navigate_to("database_browser")
    with col2:
        st.markdown(f'<h1 class="app-title">✏️ カード編集</h1>', unsafe_allow_html=True)
    st.divider()

    import requests

    api_url = st.session_state.api_base_url

    try:
        resp = requests.get(f"{api_url}/database/cards/{inst}/{card_id}", timeout=5)
        if resp.ok:
            card_info = resp.json()
        else:
            st.error(f"カードが見つかりません: {resp.text}")
            return
    except Exception as e:
        st.error(f"API接続エラー: {e}")
        return

    CATEGORIES = [
        "Unclassified",
        "UserPreference",
        "LifeEvent",
        "Task",
        "Thought",
        "Project",
    ]
    cat_val = card_info.get("category", "")
    title = st.text_input("タイトル", value=card_info.get("title", ""))
    cat = st.selectbox(
        "カテゴリ",
        CATEGORIES,
        index=CATEGORIES.index(cat_val) if cat_val in CATEGORIES else 0,
    )
    episode = st.text_area(
        "エピソード", value=card_info.get("episode") or "", height=200
    )
    col_i1, col_i2 = st.columns(2)
    with col_i1:
        ai_imp = st.slider("AI重要度", 0, 10, int(card_info.get("ai_importance") or 5))
    with col_i2:
        hu_imp = st.slider(
            "人間重要度", 0, 10, int(card_info.get("humanity_importance") or 5)
        )
    pinned = st.checkbox("📌 ピン留め", value=bool(card_info.get("is_pinned")))

    col_a1, col_a2 = st.columns([6, 2])
    with col_a1:
        if st.button("💾 保存", type="primary", width="stretch"):
            try:
                update_data = {
                    "title": title,
                    "category": cat,
                    "episode": episode,
                    "ai_importance": ai_imp,
                    "humanity_importance": hu_imp,
                }

                # Check for pin update
                is_pinned_prev = bool(card_info.get("is_pinned"))
                if pinned != is_pinned_prev:
                    pin_resp = requests.post(
                        f"{api_url}/database/cards/{inst}/{card_id}/pin",
                        json={"is_pinned": pinned},
                        timeout=5,
                    )
                    if not pin_resp.ok:
                        st.error(f"ピン留め更新エラー: {pin_resp.text}")

                upd_resp = requests.put(
                    f"{api_url}/database/cards/{inst}/{card_id}",
                    json=update_data,
                    timeout=5,
                )

                if upd_resp.ok:
                    st.success("保存しました。")
                    time.sleep(1)
                    navigate_to("database_browser")
                else:
                    st.error(f"保存エラー: {upd_resp.text}")
            except Exception as e:
                st.error(f"保存エラー: {e}")
    with col_a2:
        with st.popover("🗑️ 削除"):
            st.warning("このカードを削除します。この操作は取り消せません。")
            if st.button("完全に削除", type="primary"):
                try:
                    del_resp = requests.delete(
                        f"{api_url}/database/cards/{inst}/{card_id}", timeout=5
                    )
                    if del_resp.ok:
                        st.success("削除しました。")
                        time.sleep(1)
                        navigate_to("database_browser")
                    else:
                        st.error(f"削除エラー: {del_resp.text}")
                except Exception as e:
                    st.error(f"削除エラー: {e}")


# ==========================================
# 🛠 インスタンス設定画面 (Instance Settings Screen)
# ==========================================
@st.fragment
def render_instance_settings_screen():
    instance_name = st.session_state.current_instance

    col1, col2 = st.columns([1, 8])
    with col1:
        if st.button("＜ 戻る", key="btn_back_inst_settings"):
            navigate_to("chat")
    with col2:
        st.markdown(
            f'<h1 class="app-title">設定: {instance_name}</h1>', unsafe_allow_html=True
        )

    st.divider()

    import requests

    api_url = st.session_state.api_base_url

    # Load Configs
    try:
        config = _cached_api_json(
            api_url,
            f"/instances/{instance_name}/config",
        )
        prompts = _cached_api_json(
            api_url,
            f"/instances/{instance_name}/prompts",
        )
    except Exception as e:
        st.error(f"API接続エラー: {e}")
        config = {}
        prompts = {"system_instruction": "", "key_memory": ""}

    # Initialize defaults if empty
    if "brain" not in config:
        config["brain"] = {"readable_instances": ["self"]}
    if "chat" not in config:
        config["chat"] = {
            "model_name": "gemini-3.5-flash",
            "generation_config": {
                "temperature": 1.0,
                "top_p": 0.95,
                "top_k": 40,
                "max_output_tokens": 8192,
            },
        }
    config["chat"].setdefault(
        "generation_config", {"temperature": 1.0, "max_output_tokens": 8192}
    )
    if "memory" not in config:
        config["memory"] = {
            "max_raw_tokens": 4096,
            "raw_injection_format": "plaintext",
            "short_term_limit": 6,
        }
    # 旧 agent セクション → agent_profile / user_profile 変換（UI 表示用に in-memory マイグレーション）
    from butly_core.core.memory import _migrate_legacy_agent

    config = _migrate_legacy_agent(config)
    if "agent_profile" not in config:
        config["agent_profile"] = {}
    if "user_profile" not in config:
        config["user_profile"] = {}
    if "sleeptime" not in config:
        config["sleeptime"] = {}

    # --- 全プロバイダーのモデル候補リスト ---
    # Phase 3: ハードコード撤廃。backend `/settings/model_candidates?role=<role>` から
    # role 毎に取得し、Connection 追加で増えるモデルもそのまま反映される。
    _render_model_catalog_refresh(
        api_url,
        key="instance_model_catalog_refresh",
    )
    from butly_core.config import AI_CONFIG as _candidate_global_ai

    def _instance_role_connection(role: str) -> str | None:
        role_config = config.get(role, {})
        global_config = _candidate_global_ai.get(role, {})
        return (
            role_config.get("connection")
            if isinstance(role_config, dict)
            else None
        ) or (
            global_config.get("connection")
            if isinstance(global_config, dict)
            else None
        )

    _chat_candidates = _get_selector_candidates(
        api_url,
        "chat",
        _instance_role_connection("chat"),
        "inst_chat_model",
    )
    _summary_candidates = _get_selector_candidates(
        api_url,
        "summary",
        _instance_role_connection("summary"),
        "inst_sum_model",
    )
    _knowledge_candidates = _get_selector_candidates(
        api_url,
        "knowledge",
        _instance_role_connection("knowledge"),
        "inst_know_model",
    )
    _gatekeeper_candidates = _get_selector_candidates(
        api_url,
        "gatekeeper",
        _instance_role_connection("gatekeeper"),
        "inst_gk_model",
    )
    _embedding_candidates = _get_selector_candidates(
        api_url,
        "embedding",
        _instance_role_connection("embedding"),
        "inst_emb_model",
    )
    _instance_connections = _get_connections(api_url)

    # =====================
    # タブ分割: 基本設定 / 詳細設定
    # =====================
    tab_basic, tab_advanced = st.tabs(["⚙️ 基本設定", "🔧 詳細設定"])

    # ==========================================
    # ⚙️ 基本設定タブ
    # ==========================================
    with tab_basic:
        # ---- エージェントプロファイル（AI 側）----
        st.subheader("🤖 エージェントプロファイル")
        st.caption("AI 側の自己認識情報。SI 先頭に注入されます。")
        _ap = config.get("agent_profile", {})
        _ap_col1, _ap_col2 = st.columns(2)
        with _ap_col1:
            pf_ai_name = st.text_input(
                "AIの名前", value=_ap.get("ai_name", ""), key="pf_ai_name"
            )
            pf_ai_gender = st.text_input(
                "AIの性別表現（任意）",
                value=_ap.get("ai_gender", ""),
                key="pf_ai_gender",
                help="空欄なら SI に出力されません。",
            )
        with _ap_col2:
            _locale_opts = ["ja", "en"]
            _locale_idx = (
                _locale_opts.index(_ap.get("locale", "ja"))
                if _ap.get("locale", "ja") in _locale_opts
                else 0
            )
            pf_locale = st.selectbox(
                "ロケール", options=_locale_opts, index=_locale_idx, key="pf_locale"
            )

        st.divider()

        # ---- ユーザープロファイル（ユーザー側）----
        st.subheader("👤 ユーザープロファイル")
        st.caption("ユーザーに関する事実情報。Key Memory 先頭に注入されます。")
        _up = config.get("user_profile", {})
        _up_col1, _up_col2 = st.columns(2)
        with _up_col1:
            pf_user_name = st.text_input(
                "あなたの名前", value=_up.get("user_name", ""), key="pf_user_name"
            )
            pf_preferred_call = st.text_input(
                "呼ばれたい名前",
                value=_up.get("preferred_call", ""),
                key="pf_preferred_call",
                help="空欄なら「あなたの名前」を使います。",
            )
            pf_location = st.text_input(
                "居住地（任意）", value=_up.get("location", ""), key="pf_location"
            )
        with _up_col2:
            _gender_opts_pf = ["", "男性", "女性", "その他"]
            _current_gender = _up.get("gender", "")
            _gender_idx = (
                _gender_opts_pf.index(_current_gender)
                if _current_gender in _gender_opts_pf
                else 0
            )
            pf_gender = st.selectbox(
                "性別", options=_gender_opts_pf, index=_gender_idx, key="pf_gender"
            )
            _current_bd = _up.get("birthday", "")
            try:
                from datetime import date as _date

                _bd_val = (
                    _date.fromisoformat(_current_bd.replace("/", "-"))
                    if _current_bd
                    else None
                )
            except Exception:
                _bd_val = None
            pf_birthday = st.date_input(
                "生年月日", value=_bd_val, key="pf_birthday", format="YYYY/MM/DD"
            )

        st.divider()

        # State elements
        sys_inst = st.text_area(
            "System Instruction (性格設定)",
            value=prompts.get("system_instruction", ""),
            height=150,
        )
        key_mem = st.text_area(
            "Key Memory (根幹記憶 — プロファイル以外の内容)",
            value=prompts.get("key_memory", ""),
            height=150,
            help="AIの名前・ユーザー名などのプロファイル情報は上の「エージェントプロファイル」セクションで管理されます。",
        )

        st.divider()
        st.subheader("🤖 生成モデル設定")
        st.caption(
            "各ロールのモデルを個別に設定できます。「グローバル設定を使う」をONにすると user_config.json / config.py のデフォルト値が使われます。"
        )

        from butly_core.config import AI_CONFIG as _global_ai

        # ---- Chat モデル（メイン応答） ----
        with st.expander("💬 Chat（メイン応答）", expanded=True):
            _chat_gen = config["chat"].get("generation_config", {})
            model_choice = _model_selector(
                "モデル名",
                config["chat"].get("model_name", "gemini-3.5-flash"),
                config["chat"].get("connection"),
                _chat_candidates,
                _instance_connections,
                "inst_chat_model",
            )
            temp = st.slider(
                "Temperature",
                min_value=0.0,
                max_value=2.0,
                step=0.1,
                value=float(_chat_gen.get("temperature", 1.0)),
                key="chat_temp",
            )
            max_tokens = st.number_input(
                "最大出力トークン数",
                min_value=1,
                value=int(_chat_gen.get("max_output_tokens", 8192)),
                key="chat_max_tokens",
            )

        # ---- Gatekeeper モデル（Tier分類） ----
        with st.expander("🛡 Gatekeeper（Tier分類）"):
            if "gatekeeper" not in config:
                config["gatekeeper"] = {}
            _gk_use_global = st.toggle(
                "グローバル設定を使う",
                value=("model_name" not in config.get("gatekeeper", {})),
                key="gk_global",
            )
            gk_enabled = st.toggle(
                "Gatekeeper (Tier自動判定)",
                value=config.get("gatekeeper", {}).get("enabled", True),
                help="OFF にすると常に mid tier で動作します（RAG 検索は行われません）",
            )
            if _gk_use_global:
                _gk_default = _global_ai.get("gatekeeper", {})
                st.info(f"グローバル設定: {_gk_default.get('model_name', '未設定')}")
                gk_model_choice = None
                gk_temp = None
            else:
                _gk_cur = config.get("gatekeeper", {})
                gk_model_choice = _model_selector(
                    "モデル名",
                    _gk_cur.get(
                        "model_name",
                        _global_ai.get("gatekeeper", {}).get("model_name", ""),
                    ),
                    _gk_cur.get(
                        "connection",
                        _global_ai.get("gatekeeper", {}).get("connection"),
                    ),
                    _gatekeeper_candidates,
                    _instance_connections,
                    "inst_gk_model",
                )
                gk_temp = st.slider(
                    "Temperature",
                    min_value=0.0,
                    max_value=2.0,
                    step=0.1,
                    value=float(
                        _gk_cur.get("generation_config", {}).get("temperature", 0.0)
                    ),
                    key="gk_temp",
                )

        # ---- Summary モデル（要約） ----
        with st.expander("📝 Summary（要約）"):
            _sum_use_global = st.toggle(
                "グローバル設定を使う",
                value=("summary" not in config),
                key="sum_global",
            )
            if _sum_use_global:
                _sum_default = _global_ai.get("summary", {})
                st.info(f"グローバル設定: {_sum_default.get('model_name', '未設定')}")
                sum_model_choice = None
                sum_temp = None
            else:
                _sum_cur = config.get("summary", {})
                sum_model_choice = _model_selector(
                    "モデル名",
                    _sum_cur.get(
                        "model_name",
                        _global_ai.get("summary", {}).get("model_name", ""),
                    ),
                    _sum_cur.get(
                        "connection",
                        _global_ai.get("summary", {}).get("connection"),
                    ),
                    _summary_candidates,
                    _instance_connections,
                    "inst_sum_model",
                )
                sum_temp = st.slider(
                    "Temperature",
                    min_value=0.0,
                    max_value=2.0,
                    step=0.1,
                    value=float(
                        _sum_cur.get("generation_config", {}).get("temperature", 0.3)
                    ),
                    key="sum_temp",
                )

        # ---- Knowledge モデル（知識抽出） ----
        with st.expander("🧠 Knowledge（知識抽出）"):
            _know_use_global = st.toggle(
                "グローバル設定を使う",
                value=("knowledge" not in config),
                key="know_global",
            )
            if _know_use_global:
                _know_default = _global_ai.get("knowledge", {})
                st.info(f"グローバル設定: {_know_default.get('model_name', '未設定')}")
                know_model_choice = None
                know_temp = None
            else:
                _know_cur = config.get("knowledge", {})
                know_model_choice = _model_selector(
                    "モデル名",
                    _know_cur.get(
                        "model_name",
                        _global_ai.get("knowledge", {}).get("model_name", ""),
                    ),
                    _know_cur.get(
                        "connection",
                        _global_ai.get("knowledge", {}).get("connection"),
                    ),
                    _knowledge_candidates,
                    _instance_connections,
                    "inst_know_model",
                )
                know_temp = st.slider(
                    "Temperature",
                    min_value=0.0,
                    max_value=2.0,
                    step=0.1,
                    value=float(
                        _know_cur.get("generation_config", {}).get("temperature", 0.7)
                    ),
                    key="know_temp",
                )

        # ---- Embedding モデル（ベクトル検索） ----
        with st.expander("🔢 Embedding（ベクトル検索）"):
            st.caption(
                "Ollama で embedding を使う場合は専用モデル (nomic-embed-text 等) が必要です。\n`ollama pull nomic-embed-text` を実行し `ollama/nomic-embed-text` を選択してください。"
            )
            _emb_use_global = st.toggle(
                "グローバル設定を使う",
                value=("embedding" not in config),
                key="emb_global",
            )
            if _emb_use_global:
                _emb_default = _global_ai.get("embedding", {})
                st.info(f"グローバル設定: {_emb_default.get('model_name', '未設定')}")
                emb_model_choice = None
            else:
                _emb_cur = config.get("embedding", {})
                emb_model_choice = _model_selector(
                    "モデル名",
                    _emb_cur.get(
                        "model_name",
                        _global_ai.get("embedding", {}).get("model_name", ""),
                    ),
                    _emb_cur.get(
                        "connection",
                        _global_ai.get("embedding", {}).get("connection"),
                    ),
                    _embedding_candidates,
                    _instance_connections,
                    "inst_emb_model",
                    embeddings_only=True,
                )

        st.divider()
        st.subheader("📚 記憶の参照範囲")
        st.caption("このインスタンスがRAG検索時にアクセスできる記憶DBを選択します。")
        current_readable = config["brain"].get("readable_instances", ["self"])
        readable_selected = []
        for inst in available_instances:
            is_self = inst == instance_name
            is_checked = is_self or inst in current_readable
            disabled = is_self
            if st.checkbox(
                f"{'📌 ' if is_self else ''}{inst}",
                value=is_checked,
                disabled=disabled,
                key=f"readable_{inst}",
            ):
                if is_self:
                    readable_selected.append("self")
                else:
                    readable_selected.append(inst)
        if "self" not in readable_selected:
            readable_selected.insert(0, "self")

        st.divider()

        # ---- 基本設定タブの保存・削除ボタン ----
        col_a1, col_a2, col_a3 = st.columns([6, 2, 2])
        with col_a2:
            save_basic = st.button(
                "設定を保存", type="primary", width="stretch", key="save_basic"
            )
        with col_a3:
            with st.popover("🗑️ インスタンスを完全に削除"):
                st.warning(
                    "この操作は取り消せません。インスタンスのフォルダ、設定、短期記憶・長期記憶がすべて完全に削除されます。"
                )
                if st.button("完全に削除する", type="primary", key="delete_basic"):
                    try:
                        del_resp = requests.delete(
                            f"{api_url}/instances/{instance_name}", timeout=5
                        )
                        if del_resp.ok:
                            msg = del_resp.json().get("message", "Deleted")
                            st.success(msg)
                            time.sleep(1)
                            st.session_state.current_instance = None
                            navigate_to("home")
                        else:
                            st.error(f"削除エラー: {del_resp.text}")
                    except Exception as e:
                        st.error(f"削除エラー: {e}")

    # ==========================================
    # 🔧 詳細設定タブ
    # ==========================================
    with tab_advanced:
        st.subheader("🧠 RAG・記憶チューニング")
        use_rag_setting = st.toggle(
            "RAG検索 (長期記憶の検索)",
            value=config.get("brain", {}).get("use_rag", True),
            help="OFF にすると過去の記憶DBからの検索を行いません。短期記憶・中期記憶のみで応答します。",
        )
        _is_gemini_inst = model_choice.connection_id == "google"
        if _is_gemini_inst:
            default_gs = st.toggle(
                "Google検索のデフォルト有効化",
                value=config["brain"].get("default_use_google_search", False),
            )
        else:
            default_gs = False
            st.toggle(
                "Google検索のデフォルト有効化",
                value=False,
                disabled=True,
                help="Google検索はGeminiモデル専用です",
            )
        col_r1, col_r2 = st.columns(2)
        with col_r1:
            search_lim = st.number_input(
                "検索リミット",
                min_value=1,
                max_value=10,
                value=config["brain"].get("search_limit", 3),
            )
            fallback_lim = st.slider(
                "フォールバック取得数",
                min_value=10,
                max_value=100,
                step=10,
                value=config["brain"].get("fallback_fetch_limit", 50),
            )
            st_limit = st.number_input(
                "短期記憶 保存数",
                min_value=1,
                max_value=12,
                step=1,
                value=config["memory"].get("short_term_limit", 6),
                help="保持するセッション数",
            )
        with col_r2:
            keyword_thr = st.number_input(
                "記憶検索の感度",
                min_value=1,
                max_value=10,
                value=config["brain"].get("keyword_hit_threshold", 5),
                help="値が大きいほど直近の会話記憶が検索補完に加わりやすくなります。",
            )
            use_summarized = st.toggle(
                "中期記憶に要約版を使用",
                value=config["memory"].get("use_summarized_mid_term", True),
                help="ON: digest + relationship（トークン効率◎）/ OFF: RAWキャッシュ（詳細だがトークン消費大）",
            )
            raw_tokens = st.slider(
                "RAW記憶 トークン上限",
                min_value=1024,
                max_value=16384,
                step=1024,
                value=config["memory"].get("max_raw_tokens", 4096),
                disabled=use_summarized,
            )
            raw_format = st.selectbox(
                "RAW注入形式",
                ["plaintext", "markdown", "compact"],
                index=["plaintext", "markdown", "compact"].index(
                    config["memory"].get("raw_injection_format", "plaintext")
                ),
                disabled=use_summarized,
            )
            if st.button(
                "🔄 RAWキャッシュ再生成",
                help="現在のスライダー/セレクトの値で即座に再生成します",
                disabled=use_summarized,
            ):
                try:
                    resp = requests.post(
                        f"{st.session_state.api_base_url}/instances/{instance_name}/rebuild_raw_cache",
                        json={"max_tokens": raw_tokens, "injection_format": raw_format},
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        st.success(
                            f"再生成完了: {data.get('sessions', 0)}セッション, {data.get('tokens', 0)}トークン ({data.get('format', 'plaintext')})"
                        )
                    else:
                        st.error(f"再生成失敗: {resp.text}")
                except Exception as e:
                    st.error(f"エラー: {e}")

        st.divider()

        # ==========================================
        # 📝 Sleeptime 設定
        # ==========================================
        st.subheader("📝 Sleeptime 設定")
        st.caption("記憶の整理・要約・ナレッジ化のパラメータを調整します。")
        _hk_conf = config.get("sleeptime", {})

        hk_skip_knowledge = st.checkbox(
            "ナレッジ化 (Stage 2) をスキップ",
            value=_hk_conf.get("skip_knowledge_generation", False),
            help="有効にすると Stage 2 をスキップし、RAWデータを 1_integrated に保持します。後日高性能モデルで一括処理可能。",
            key="hk_skip_knowledge",
        )

        hk_col1, hk_col2 = st.columns(2)
        with hk_col1:
            hk_max_digest = st.number_input(
                "Digest 最大文字数",
                min_value=500,
                max_value=20000,
                step=500,
                value=_hk_conf.get("max_digest_chars", 3000),
                help="中期記憶の要約版（日次ダイジェスト）の最大文字数",
                key="hk_max_digest",
            )
            hk_max_relationship = st.number_input(
                "Recent Snapshot 最大文字数",
                min_value=500,
                max_value=20000,
                step=500,
                value=_hk_conf.get("max_relationship_chars", 5000),
                help="近況スナップショットの最大文字数",
                key="hk_max_relationship",
            )
            hk_relationship_interval = st.number_input(
                "Recent Snapshot 更新間隔 (日)",
                min_value=1,
                max_value=30,
                step=1,
                value=_hk_conf.get("relationship_update_interval_days", 7),
                help="近況スナップショットを更新する間隔",
                key="hk_rel_interval",
            )
            hk_digest_max_input = st.number_input(
                "Digest 1回あたり最大入力文字数",
                min_value=0,
                max_value=100000,
                step=1000,
                value=_hk_conf.get("digest_max_input_chars", 0),
                help="Stage 1 (Digest) の1回あたりの最大入力文字数。0 = 無制限。",
                key="hk_digest_max_input",
            )
        with hk_col2:
            hk_summary_tokens = st.number_input(
                "Summary 最大出力トークン数",
                min_value=256,
                max_value=16384,
                step=256,
                value=_hk_conf.get("summary_max_output_tokens", 4096),
                help="Digest / Headlines 生成時の最大出力トークン (classify API)",
                key="hk_summary_tokens",
            )
            hk_knowledge_tokens = st.number_input(
                "Knowledge 最大出力トークン数",
                min_value=256,
                max_value=16384,
                step=256,
                value=_hk_conf.get("knowledge_max_output_tokens", 8192),
                help="ナレッジ抽出 / Recent Snapshot 生成時の最大出力トークン (classify API)",
                key="hk_knowledge_tokens",
            )
            hk_knowledge_max_input = st.number_input(
                "Knowledge 1回あたり最大入力文字数",
                min_value=0,
                max_value=100000,
                step=1000,
                value=_hk_conf.get("knowledge_max_input_chars", 0),
                help="Stage 2 (Knowledge) の1回あたりの最大入力文字数。0 = 無制限。",
                key="hk_knowledge_max_input",
            )

        st.divider()

        # ==========================================
        # 🧩 コンテキスト注入設定
        # ==========================================
        st.subheader("🧩 コンテキスト注入設定")
        st.caption("LLMに渡すコンテキストのプリセットとレベルを設定できます。")

        from butly_core.core.gatekeeper.memory_builder import (
            DEFAULT_CONTEXT_ORDER,
            CONTEXT_LEVEL_PRESETS,
        )

        # config から読み込み（context_levels 優先、なければ context_order から変換）
        ctx_cfg = config.get("context_levels", {})
        if not ctx_cfg and "context_order" in config:
            from butly_core.core.gatekeeper.memory_builder import (
                migrate_context_order_to_levels,
            )

            config = migrate_context_order_to_levels(config)
            ctx_cfg = config.get("context_levels", {})

        current_preset = ctx_cfg.get("preset", "normal")
        _si_order = list(
            ctx_cfg.get("order", {}).get(
                "system_instruction", DEFAULT_CONTEXT_ORDER["system_instruction"]
            )
        )
        _cp_order = list(
            ctx_cfg.get("order", {}).get(
                "context_prefix", DEFAULT_CONTEXT_ORDER["context_prefix"]
            )
        )
        _cp_order = [
            "session_digest" if sid == "floating" else sid for sid in _cp_order
        ]
        _cp_order = list(dict.fromkeys(_cp_order))
        if "session_digest" not in _cp_order:
            _cp_order = list(DEFAULT_CONTEXT_ORDER["context_prefix"])
        _si_position = ctx_cfg.get(
            "system_instruction_position",
            DEFAULT_CONTEXT_ORDER.get("system_instruction_position", "top"),
        )

        # セクションの日本語ラベル
        _SECTION_LABELS = {
            "system_instruction": "性格設定 (system_instruction)",
            "key_memory": "根幹記憶 (key_memory)",
            "label_notes": "背景ラベル (label_notes)",
            "current_time": "現在時刻 (current_time)",
            "glossary": "共通言語辞書 (glossary)",
            "mid_term": "中期記憶 (mid_term)",
            "rag": "長期記憶 RAG (rag)",
            "session_digest": "会話圧縮ログ (session_digest)",
            "tier_info": "Tier情報 (tier_info)",
            "web_search": "Web検索 (web_search)",
        }

        # --- プリセット選択 ---
        _preset_labels = {
            "normal": "Normal（API向け・フル情報）",
            "compact": "Compact（情報量を抑制）",
            "low": "Low（小規模LLM向け・最小限）",
            "custom": "Custom（個別設定）",
        }
        preset = st.selectbox(
            "プリセット",
            options=["normal", "compact", "low", "custom"],
            index=(
                ["normal", "compact", "low", "custom"].index(current_preset)
                if current_preset in ["normal", "compact", "low", "custom"]
                else 0
            ),
            format_func=lambda x: _preset_labels[x],
            key="ctx_preset",
        )

        if preset == "low":
            st.warning("💡 LOWプリセットではGatekeeper OFFを推奨します。")

        if preset != "custom":
            preset_levels = CONTEXT_LEVEL_PRESETS[preset]
        else:
            preset_levels = ctx_cfg.get("levels", CONTEXT_LEVEL_PRESETS["normal"])
            if "floating" in preset_levels and "session_digest" not in preset_levels:
                preset_levels = {
                    **preset_levels,
                    "session_digest": preset_levels.get("floating", "high"),
                }

        # --- 個別レベル設定 ---
        _LEVEL_OPTIONS = ["high", "mid", "low", "off"]
        with st.expander("詳細設定（各要素のレベル）", expanded=(preset == "custom")):
            level_settings = {}
            for section_id, label in _SECTION_LABELS.items():
                current = preset_levels.get(section_id, "high")
                level_settings[section_id] = st.selectbox(
                    label,
                    options=_LEVEL_OPTIONS,
                    index=(
                        _LEVEL_OPTIONS.index(current)
                        if current in _LEVEL_OPTIONS
                        else 0
                    ),
                    key=f"ctx_level_{section_id}",
                    disabled=(preset != "custom"),
                )

        # --- system_instruction_position ---
        st.markdown("**▎ System Instruction の配置**")
        _pos_options = {
            "top": "先頭 (top) — 標準・Gemini推奨",
            "bottom": "末尾 (bottom) — SillyTavern方式・OpenAI/Ollama向け",
        }
        _si_position = st.radio(
            "配置位置",
            options=list(_pos_options.keys()),
            index=0 if _si_position == "top" else 1,
            format_func=lambda x: _pos_options[x],
            key="ctx_si_position",
            horizontal=True,
        )
        st.caption(
            "ℹ️ Gemini API では system_instruction は常に独立パラメータとして渡されるため、この設定は無視されます。"
        )

        # --- session_state 初期化 ---
        if "ctx_si_order" not in st.session_state:
            st.session_state.ctx_si_order = _si_order
        if "ctx_cp_order" not in st.session_state:
            st.session_state.ctx_cp_order = _cp_order

        # 並べ替えヘルパー
        def _swap_items(lst_key, idx, direction):
            lst = st.session_state[lst_key]
            new_idx = idx + direction
            if 0 <= new_idx < len(lst):
                lst[idx], lst[new_idx] = lst[new_idx], lst[idx]

        # --- System Instruction セクション ---
        with st.expander("順序設定（↑↓で並べ替え）"):
            st.markdown("**▎ System Instruction**")
            for i, sid in enumerate(st.session_state.ctx_si_order):
                with st.container(border=True):
                    c1, c2, c3 = st.columns([6, 1, 1])
                    with c1:
                        st.markdown(f"**{i+1}.** {_SECTION_LABELS.get(sid, sid)}")
                    with c2:
                        if i > 0:
                            if st.button("↑", key=f"si_up_{sid}"):
                                _swap_items("ctx_si_order", i, -1)
                                st.rerun()
                    with c3:
                        if i < len(st.session_state.ctx_si_order) - 1:
                            if st.button("↓", key=f"si_down_{sid}"):
                                _swap_items("ctx_si_order", i, 1)
                                st.rerun()

            # --- Context Prefix セクション ---
            st.markdown("**▎ Context Prefix（会話コンテキスト）**")
            for i, sid in enumerate(st.session_state.ctx_cp_order):
                with st.container(border=True):
                    c1, c2, c3 = st.columns([6, 1, 1])
                    with c1:
                        st.markdown(f"**{i+1}.** {_SECTION_LABELS.get(sid, sid)}")
                    with c2:
                        if i > 0:
                            if st.button("↑", key=f"cp_up_{sid}"):
                                _swap_items("ctx_cp_order", i, -1)
                                st.rerun()
                    with c3:
                        if i < len(st.session_state.ctx_cp_order) - 1:
                            if st.button("↓", key=f"cp_down_{sid}"):
                                _swap_items("ctx_cp_order", i, 1)
                                st.rerun()

            # デフォルトに戻すボタン
            if st.button("デフォルトに戻す", key="ctx_reset_default"):
                st.session_state.ctx_si_order = list(
                    DEFAULT_CONTEXT_ORDER["system_instruction"]
                )
                st.session_state.ctx_cp_order = list(
                    DEFAULT_CONTEXT_ORDER["context_prefix"]
                )
                st.rerun()

        st.divider()

        # ==========================================
        # 📖 Glossary (共通言語辞書 / Lorebook) 管理
        # ==========================================
        st.subheader("📖 Glossary（共通言語辞書 / Lorebook）")
        st.caption(
            "キーワード一致で context に注入されます。短い定義は『用語説明』、複数行の定義は『関連設定』として注入されます。"
        )

        glossary_data = {"version": 1, "entries": []}
        try:
            gl_resp = requests.get(
                f"{api_url}/instances/{instance_name}/glossary", timeout=5
            )
            if gl_resp.ok:
                glossary_data = gl_resp.json()
        except Exception:
            pass

        entries = glossary_data.get("entries", [])

        if (
            "glossary_entries" not in st.session_state
            or st.session_state.get("glossary_instance") != instance_name
        ):
            st.session_state.glossary_entries = [dict(e) for e in entries]
            st.session_state.glossary_instance = instance_name

        gl_entries = st.session_state.glossary_entries

        gl_filter_col1, gl_filter_col2 = st.columns([2, 2])
        with gl_filter_col1:
            gl_status_filter = st.selectbox(
                "ステータス",
                ["all", "active", "pending", "archived"],
                key="gl_status_filter",
            )
        with gl_filter_col2:
            gl_search = st.text_input(
                "🔍 用語検索", key="gl_search", placeholder="用語名で絞り込み"
            )

        filtered_indices = []
        for i, entry in enumerate(gl_entries):
            if gl_status_filter != "all" and entry.get("status") != gl_status_filter:
                continue
            if gl_search and gl_search.lower() not in entry.get("term", "").lower():
                continue
            filtered_indices.append(i)

        st.caption(f"{len(filtered_indices)} / {len(gl_entries)} 件表示")

        for idx in filtered_indices:
            entry = gl_entries[idx]
            definition = entry.get("definition", "") or ""
            is_long = "\n" in definition.strip()
            badge = "📖 関連設定" if is_long else "🔤 用語説明"

            with st.container(border=True):
                gc1, gc2, gc3 = st.columns([5, 2, 1])
                with gc1:
                    st.markdown(
                        f"**{entry.get('term', '')}** ・ {badge} ・ priority: {entry.get('priority', 100)}"
                    )
                    if is_long:
                        with st.expander("定義を表示", expanded=False):
                            st.text(definition.strip())
                    else:
                        st.caption(definition.strip())
                    aliases = entry.get("aliases", [])
                    if aliases:
                        st.caption(f"別名: {', '.join(aliases)}")
                with gc2:
                    new_status = st.selectbox(
                        "status",
                        ["active", "pending", "archived"],
                        index=["active", "pending", "archived"].index(
                            entry.get("status", "active")
                        ),
                        key=f"gl_status_{idx}",
                        label_visibility="collapsed",
                    )
                    if new_status != entry.get("status"):
                        gl_entries[idx]["status"] = new_status
                with gc3:
                    if st.button("🗑️", key=f"gl_del_{idx}"):
                        gl_entries.pop(idx)
                        st.rerun()

                with st.expander("✏️ 編集", expanded=False):
                    new_term_val = st.text_input(
                        "用語名", value=entry.get("term", ""), key=f"gl_edit_term_{idx}"
                    )
                    new_def_val = st.text_area(
                        "定義（複数行で『関連設定』扱い）",
                        value=definition,
                        height=120,
                        key=f"gl_edit_def_{idx}",
                    )
                    new_aliases_val = st.text_input(
                        "別名（カンマ区切り）",
                        value=", ".join(entry.get("aliases", []) or []),
                        key=f"gl_edit_aliases_{idx}",
                    )
                    new_priority_val = st.number_input(
                        "priority（小さいほど先に注入）",
                        min_value=0,
                        max_value=9999,
                        value=int(entry.get("priority", 100)),
                        step=10,
                        key=f"gl_edit_prio_{idx}",
                    )
                    cat_options = [
                        "system",
                        "hardware",
                        "project",
                        "tool",
                        "world",
                        "character",
                        "other",
                    ]
                    cur_cat = entry.get("category", "other")
                    cat_index = (
                        cat_options.index(cur_cat)
                        if cur_cat in cat_options
                        else len(cat_options) - 1
                    )
                    new_cat_val = st.selectbox(
                        "カテゴリ",
                        cat_options,
                        index=cat_index,
                        key=f"gl_edit_cat_{idx}",
                    )
                    if st.button("変更を反映", key=f"gl_edit_apply_{idx}"):
                        gl_entries[idx]["term"] = new_term_val
                        gl_entries[idx]["definition"] = new_def_val
                        gl_entries[idx]["aliases"] = [
                            a.strip()
                            for a in (new_aliases_val or "").split(",")
                            if a.strip()
                        ]
                        gl_entries[idx]["priority"] = int(new_priority_val)
                        gl_entries[idx]["category"] = new_cat_val
                        st.rerun()

        with st.expander("➕ 新しいエントリを追加"):
            new_term = st.text_input("用語名", key="gl_new_term")
            new_def = st.text_area(
                "定義（複数行で『関連設定』扱い）",
                height=120,
                key="gl_new_def",
                placeholder="一行で書くと『用語説明』、複数行で書くと『関連設定』として注入されます",
            )
            new_aliases = st.text_input("別名（カンマ区切り）", key="gl_new_aliases")
            new_cat = st.selectbox(
                "カテゴリ",
                [
                    "system",
                    "hardware",
                    "project",
                    "tool",
                    "world",
                    "character",
                    "other",
                ],
                key="gl_new_cat",
            )
            new_prio = st.number_input(
                "priority（小さいほど先に注入）",
                min_value=0,
                max_value=9999,
                value=100,
                step=10,
                key="gl_new_prio",
            )
            if st.button("追加", key="gl_add_entry"):
                if new_term and new_def:
                    alias_list = (
                        [a.strip() for a in new_aliases.split(",") if a.strip()]
                        if new_aliases
                        else []
                    )
                    gl_entries.append(
                        {
                            "term": new_term,
                            "definition": new_def,
                            "aliases": alias_list,
                            "category": new_cat,
                            "status": "active",
                            "priority": int(new_prio),
                        }
                    )
                    st.rerun()
                else:
                    st.warning("用語名と定義は必須です。")

        # --- スキャン設定 (instance config の glossary セクション) ---
        with st.expander("⚙️ スキャン設定（このインスタンス）"):
            st.caption("チャット時の glossary 注入の挙動を制御します。")
            current_gl_cfg = (config or {}).get("glossary", {})
            sys_default = {
                "scan_depth": 2,
                "scan_target": "both",
                "max_entries": 20,
                "max_chars": 4000,
            }

            cfg_col1, cfg_col2 = st.columns(2)
            with cfg_col1:
                sd = st.number_input(
                    "scan_depth（直近何ターン分の履歴をスキャン）",
                    min_value=0,
                    max_value=20,
                    value=int(
                        current_gl_cfg.get("scan_depth", sys_default["scan_depth"])
                    ),
                    step=1,
                    key="gl_cfg_scan_depth",
                )
                me = st.number_input(
                    "max_entries（注入する最大エントリ数）",
                    min_value=0,
                    max_value=200,
                    value=int(
                        current_gl_cfg.get("max_entries", sys_default["max_entries"])
                    ),
                    step=1,
                    key="gl_cfg_max_entries",
                )
            with cfg_col2:
                tgt_options = ["both", "user", "assistant"]
                cur_tgt = current_gl_cfg.get("scan_target", sys_default["scan_target"])
                tgt_idx = tgt_options.index(cur_tgt) if cur_tgt in tgt_options else 0
                st_val = st.selectbox(
                    "scan_target（履歴のどちらをスキャン）",
                    tgt_options,
                    index=tgt_idx,
                    key="gl_cfg_scan_target",
                )
                mc = st.number_input(
                    "max_chars（注入合計文字数の上限）",
                    min_value=0,
                    max_value=100000,
                    value=int(
                        current_gl_cfg.get("max_chars", sys_default["max_chars"])
                    ),
                    step=100,
                    key="gl_cfg_max_chars",
                )

            if st.button("スキャン設定を保存", key="gl_cfg_save"):
                new_cfg = dict(config or {})
                new_cfg["glossary"] = {
                    "scan_depth": int(sd),
                    "scan_target": st_val,
                    "max_entries": int(me),
                    "max_chars": int(mc),
                }
                try:
                    cfg_resp = requests.post(
                        f"{api_url}/instances/{instance_name}/config",
                        json=new_cfg,
                        timeout=5,
                    )
                    if cfg_resp.ok:
                        _cached_api_json.clear()
                        st.success("スキャン設定を保存しました。")
                    else:
                        st.error(f"保存エラー: {cfg_resp.text}")
                except Exception as e:
                    st.error(f"保存エラー: {e}")

        if st.button("💾 Glossary を保存", key="gl_save", width="stretch"):
            # スキーマバージョンを 2 に上げる (priority / 複数行 definition 対応)
            save_data = {"version": 2, "entries": gl_entries}
            try:
                sv_resp = requests.post(
                    f"{api_url}/instances/{instance_name}/glossary",
                    json=save_data,
                    timeout=5,
                )
                if sv_resp.ok:
                    st.success("Glossary を保存しました。")
                    st.session_state.pop("glossary_entries", None)
                    st.session_state.pop("glossary_instance", None)
                else:
                    st.error(f"保存エラー: {sv_resp.text}")
            except Exception as e:
                st.error(f"保存エラー: {e}")

        st.divider()

        # Sleeptime Button
        if st.button("🧹 記憶の整理 (Sleeptime)", width="stretch"):
            st.session_state.sleeptime_instance = instance_name
            navigate_to("sleeptime")
        st.caption("短期記憶を整理し、知識カードとして長期記憶に保存します。")

        st.divider()

        # Rename Instance
        st.subheader("✏️ インスタンス名の変更")
        rename_col1, rename_col2 = st.columns([3, 1])
        with rename_col1:
            new_inst_name = st.text_input(
                "新しいインスタンス名（半角英数字・_）",
                placeholder=instance_name,
                key="rename_input",
            )
        with rename_col2:
            st.write("")
            st.write("")
            if st.button("変更", key="btn_rename", width="stretch"):
                if new_inst_name and new_inst_name != instance_name:
                    try:
                        ren_resp = requests.post(
                            f"{api_url}/instances/{instance_name}/rename",
                            json={"new_name": new_inst_name},
                            timeout=10,
                        )
                        if ren_resp.ok:
                            result = ren_resp.json()
                            new_name = result.get("new_instance_name", new_inst_name)
                            st.success(f"名前を '{new_name}' に変更しました。")
                            st.session_state.current_instance = new_name
                            st.cache_resource.clear()
                            time.sleep(1)
                            navigate_to("chat")
                        else:
                            st.error(f"リネームエラー: {ren_resp.text}")
                    except Exception as e:
                        st.error(f"リネームエラー: {e}")
                elif new_inst_name == instance_name:
                    st.warning("現在の名前と同じです。")
                else:
                    st.warning("名前を入力してください。")
        st.caption("記憶DB内のデータと他インスタンスの参照設定も自動で追従します。")

        st.divider()

        # ---- 詳細設定タブの保存ボタン ----
        col_b1, col_b2 = st.columns([8, 2])
        with col_b2:
            save_advanced = st.button(
                "設定を保存",
                type="primary",
                width="stretch",
                key="save_advanced",
            )

    # ==========================================
    # 保存処理（両タブ共通）
    # ==========================================
    if save_basic or save_advanced:
        # Update values
        config["brain"]["default_use_google_search"] = default_gs
        config["brain"]["readable_instances"] = readable_selected
        config["brain"].pop("filter_memory_by_type", None)
        config["brain"]["use_rag"] = use_rag_setting
        config["brain"]["search_limit"] = search_lim
        config["brain"]["fallback_fetch_limit"] = fallback_lim
        config["brain"]["keyword_hit_threshold"] = keyword_thr

        # --- Chat ---
        set_model_choice(config["chat"], model_choice)
        config["chat"].setdefault("generation_config", {})
        config["chat"]["generation_config"]["temperature"] = temp
        config["chat"]["generation_config"]["max_output_tokens"] = max_tokens

        # --- Gatekeeper ---
        _gk_save = {"enabled": gk_enabled}
        if gk_model_choice is not None:  # "グローバル設定を使う" がOFF
            set_model_choice(_gk_save, gk_model_choice)
            _gk_save["generation_config"] = {
                "temperature": gk_temp,
                "max_output_tokens": 512,
            }
        config["gatekeeper"] = _gk_save

        # --- Summary ---
        if sum_model_choice is not None:
            config["summary"] = {
                "generation_config": {"temperature": sum_temp},
            }
            set_model_choice(config["summary"], sum_model_choice)
        else:
            config.pop("summary", None)

        # --- Knowledge ---
        if know_model_choice is not None:
            config["knowledge"] = {
                "generation_config": {"temperature": know_temp},
            }
            set_model_choice(config["knowledge"], know_model_choice)
        else:
            config.pop("knowledge", None)

        # --- Embedding ---
        if emb_model_choice is not None:
            config["embedding"] = {}
            set_model_choice(config["embedding"], emb_model_choice)
        else:
            config.pop("embedding", None)

        config["memory"]["short_term_limit"] = st_limit
        config["memory"]["use_summarized_mid_term"] = use_summarized
        config["memory"]["max_raw_tokens"] = raw_tokens
        config["memory"]["raw_injection_format"] = raw_format

        # profile の保存（新スキーマ。旧 agent は除去）
        config["agent_profile"] = {
            "ai_name": pf_ai_name,
            "ai_gender": pf_ai_gender,
            "locale": pf_locale,
        }
        config["user_profile"] = {
            "user_name": pf_user_name,
            "preferred_call": pf_preferred_call if pf_preferred_call else pf_user_name,
            "gender": pf_gender,
            "birthday": pf_birthday.strftime("%Y/%m/%d") if pf_birthday else "",
            "location": pf_location,
        }
        config.pop("agent", None)

        # sleeptime 設定の保存
        config["sleeptime"] = {
            "skip_knowledge_generation": hk_skip_knowledge,
            "max_digest_chars": hk_max_digest,
            "max_relationship_chars": hk_max_relationship,
            "relationship_update_interval_days": hk_relationship_interval,
            "summary_max_output_tokens": hk_summary_tokens,
            "knowledge_max_output_tokens": hk_knowledge_tokens,
            "digest_max_input_chars": hk_digest_max_input,
            "knowledge_max_input_chars": hk_knowledge_max_input,
        }

        # context_levels の保存
        config["context_levels"] = {
            "preset": preset,
            "levels": (
                level_settings if preset == "custom" else CONTEXT_LEVEL_PRESETS[preset]
            ),
            "order": {
                "system_instruction": list(st.session_state.ctx_si_order),
                "context_prefix": list(st.session_state.ctx_cp_order),
            },
            "system_instruction_position": _si_position,
        }
        config.pop("context_order", None)  # 旧キー削除

        # Save configs
        try:
            c_resp = requests.post(
                f"{api_url}/instances/{instance_name}/config", json=config, timeout=5
            )
            p_resp = requests.post(
                f"{api_url}/instances/{instance_name}/prompts",
                json={"system_instruction": sys_inst, "key_memory": key_mem},
                timeout=5,
            )

            if c_resp.ok and p_resp.ok:
                _cached_api_json.clear()
                st.success("設定を保存しました。")
                time.sleep(1)
                navigate_to("chat")
            else:
                st.error(
                    f"保存エラー: Config[{c_resp.status_code}], Prompts[{p_resp.status_code}]"
                )
        except Exception as e:
            st.error(f"保存エラー: {e}")


def render_chat_screen():
    instance_name = st.session_state.current_instance
    # 履歴表示用に memory のみ初期化（brain は FastAPI 側で管理）
    memory, brain, chronos = initialize_system(BASE_DIR, instance_name)
    api_url = st.session_state.api_base_url

    # streaming_enabled の初期化 (まだなら True)
    if "streaming_enabled" not in st.session_state:
        st.session_state.streaming_enabled = True

    # --- チャットヘッダー ---
    col1, col2, col3, col4, col5, col_stream, col6 = st.columns([1, 4, 1, 1, 1, 1, 1])
    with col1:
        if st.button("＜", help="戻る"):
            navigate_to("home")
    with col2:
        st.markdown(
            f'<h1 class="app-title">{instance_name}</h1>', unsafe_allow_html=True
        )
    with col3:
        if st.button("⚙️", help="インスタンス設定"):
            navigate_to("instance_settings")
    with col4:
        if st.button("🧹", help="記憶の整理 (Sleeptime)"):
            st.session_state.sleeptime_instance = instance_name
            navigate_to("sleeptime")
    with col5:
        if st.button("🔄", help="履歴をリロード"):
            st.session_state.messages = []
            if "last_interaction_time" in st.session_state:
                del st.session_state.last_interaction_time
            st.rerun()
    with col_stream:
        # ⚡ Streaming 表示切替ボタン (ON/OFF)
        _streaming_on = st.session_state.streaming_enabled
        _stream_label = "⚡ ON" if _streaming_on else "⚡"
        _stream_help = (
            "Streaming: ON（クリックでOFF）"
            if _streaming_on
            else "Streaming: OFF（クリックでON、応答を逐次表示）"
        )
        if st.button(_stream_label, help=_stream_help, key="header_streaming_toggle"):
            st.session_state.streaming_enabled = not _streaming_on
            st.rerun()
    with col6:
        # Google Search toggle / Web Search toggle
        # プロバイダーに応じて使い分ける
        active_model = get_active_chat_model(api_url, instance_name)
        is_gemini = is_gemini_provider(active_model)

        gs_on = st.session_state.get("use_google_search", False)

        if is_gemini:
            # Gemini: 通常通りトグル可能（Native Grounding）
            gs_label = "🌐 ON" if gs_on else "🌐"
            gs_help = (
                "Google検索: ON（クリックでOFF）"
                if gs_on
                else "Google検索: OFF（クリックでON）"
            )
            if st.button(gs_label, help=gs_help):
                st.session_state.use_google_search = not gs_on
                st.rerun()
        else:
            # 非Gemini: Google検索を強制OFF + 汎用Web検索トグル
            if gs_on:
                st.session_state.use_google_search = False

            import os

            tavily_available = bool(os.environ.get("TAVILY_API_KEY", ""))
            ollama_ws_available = bool(os.environ.get("OLLAMA_WEB_SEARCH_API_KEY", ""))
            web_search_available = tavily_available or ollama_ws_available
            ws_on = st.session_state.get("use_web_search", False)

            if web_search_available:
                ws_label = "🔍 ON" if ws_on else "🔍"
                ws_help = (
                    "Web検索: ON（クリックでOFF）"
                    if ws_on
                    else "Web検索: OFF（クリックでON）"
                )
                if st.button(ws_label, help=ws_help):
                    st.session_state.use_web_search = not ws_on
                    st.rerun()
            else:
                st.button(
                    "🔍",
                    help="Web検索を使用するには TAVILY_API_KEY または OLLAMA_WEB_SEARCH_API_KEY を設定してください",
                    disabled=True,
                )

    st.divider()

    # --- 履歴の読み込み処理 ---
    # 新 API（GET /api/v1/instances/{name}/messages）経由。
    # ButlyMemory.load_recent_sessions() 直読みの置換（移行計画 §3.3）。
    if not st.session_state.messages:
        import requests as _req_hist

        try:
            resp = _req_hist.get(
                f"{api_url}/api/v1/instances/{instance_name}/messages",
                params={"limit": 6},
                timeout=10,
            )
            resp.raise_for_status()
            page = resp.json()
        except Exception as e:
            st.error(f"履歴の取得に失敗しました: {e}")
            page = {"items": [], "last_interaction_at": None}

        last_at = page.get("last_interaction_at")
        last_ts = None
        if last_at:
            try:
                # chronos は naive local datetime を前提とするため変換する
                last_ts = (
                    datetime.fromisoformat(last_at).astimezone().replace(tzinfo=None)
                )
            except ValueError:
                pass
        st.session_state.last_interaction_time = last_ts

        for item in page.get("items", []):
            if not isinstance(item, dict):
                continue
            # API の "assistant" はセッション内 message と同じ "model" に揃える
            role = "user" if item.get("role") == "user" else "model"
            msg_to_append = {"role": role, "parts": [item.get("text", "")]}
            if item.get("created_at"):
                msg_to_append["timestamp"] = item["created_at"]
            st.session_state.messages.append(msg_to_append)

    sys_note = chronos.get_system_note(
        is_holiday=st.session_state.is_holiday,
        last_interaction_time=st.session_state.get("last_interaction_time"),
    )

    if False:  # Chronos debug — removed, now part of unified debug panel
        pass

    # --- メッセージの表示 (カスタムCSSを使用) ---
    st.markdown('<div class="clearfix">', unsafe_allow_html=True)
    for msg in st.session_state.messages:
        role = msg["role"]
        text = msg["parts"][0]

        ts_display = ""
        msg_ts = msg.get("timestamp")
        if msg_ts:
            try:
                dt = datetime.fromisoformat(msg_ts)
                ts_display = f'<div style="font-size: 0.75rem; color: #888; margin-bottom: 4px;">{dt.strftime("%Y-%m-%d %H:%M")}</div>'
            except:
                pass

        if role == "user":
            # 添付画像のサムネイルHTML
            img_html = ""
            for att in msg.get("attachments", []):
                img_html += f'<img class="attachment-thumb" src="data:{att["mime_type"]};base64,{att["data_base64"]}" alt="{att.get("name", "image")}" />'
            st.markdown(
                f'<div class="chat-bubble-user">{ts_display}{text}{img_html}</div>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                f'<div class="chat-bubble-ai">{ts_display}{text}</div>',
                unsafe_allow_html=True,
            )

            # --- Debug Panel (永続表示: メッセージに紐付けて保存済みデータを描画) ---
            if st.session_state.get("debug_mode"):
                debug = msg.get("debug_info") or {}
                if debug:
                    with st.expander("🐛 DEBUG", expanded=False):
                        timing = debug.get("timing", {})
                        tokens = debug.get("token_estimate", {})
                        gk = debug.get("gatekeeper", {})

                        st.markdown(f"""
**Provider:** `{debug.get('provider', '?')}` | **Model:** `{debug.get('model', '?')}`  
**Tier:** `{gk.get('tier', '?')}` | **Total:** `{timing.get('total_ms', 0)}ms`  
**Tokens (est.):** Prompt ~{tokens.get('prompt', 0)} / Response ~{tokens.get('response', 0)}
""")

                        st.caption("⏱️ Timing (Gen ∥ State は並列実行)")
                        cols = st.columns(5)
                        cols[0].metric(
                            "Gatekeeper", f"{timing.get('gatekeeper_ms', 0)}ms"
                        )
                        cols[1].metric(
                            "Memory Build", f"{timing.get('memory_build_ms', 0)}ms"
                        )
                        cols[2].metric(
                            "RAG Search", f"{timing.get('rag_search_ms', 0)}ms"
                        )
                        cols[3].metric(
                            "Generation", f"{timing.get('generation_ms', 0)}ms"
                        )
                        cols[4].metric(
                            "State Update", f"{timing.get('state_update_ms', 0)}ms"
                        )

                        st.caption("🧠 Gatekeeper")
                        if not gk.get("enabled", True):
                            st.text("  (disabled)")
                        else:
                            scores = gk.get("scores", {})
                            if scores:
                                for key in [
                                    "response_complexity",
                                    "emotional_weight",
                                    "continuity_need",
                                ]:
                                    val = scores.get(key, 0.0)
                                    filled = int(val * 10)
                                    bar = "█" * filled + "░" * (10 - filled)
                                    st.text(f"  {key:>30s}: {val:.2f} {bar}")
                            st.text(
                                f"  {'need_intent':>30s}: {gk.get('need_intent') or '(null — probe skipped)'}"
                            )
                            st.text(
                                f"  {'memory_probe_status':>30s}: {gk.get('memory_probe_status') or '(n/a)'}"
                            )
                            if gk.get("need"):
                                st.text(f"  Need: {gk['need']}")
                            if gk.get("search_targets"):
                                st.text(f"  Search Targets: {gk['search_targets']}")

                            # MemoryProbe layer 詳細 (vector の閾値判定など)
                            probe_layers = gk.get("memory_probe_layers")
                            if probe_layers:
                                with st.expander(
                                    "📊 MemoryProbe Layers (詳細診断)", expanded=False
                                ):
                                    _gl = probe_layers.get("glossary", {})
                                    st.text(
                                        f"glossary: executed={_gl.get('executed')} matches={_gl.get('matches', 0)}"
                                    )
                                    _v = probe_layers.get("vector")
                                    if _v:
                                        if _v.get("executed"):
                                            st.text(
                                                f"vector: fetched={_v.get('fetched_count', 0)} "
                                                f"passed={_v.get('passed_threshold', 0)} "
                                                f"thresh={_v.get('threshold', '?')} "
                                                f"decay={_v.get('decay_rate', '?')}"
                                            )
                                            if _v.get("top_raw_scores"):
                                                st.text(
                                                    f"  top raw     : {_v['top_raw_scores']}"
                                                )
                                            if _v.get("top_final_scores"):
                                                st.text(
                                                    f"  top w/decay : {_v['top_final_scores']}"
                                                )
                                        else:
                                            st.text(
                                                f"vector: skipped ({_v.get('reason', '?')})"
                                            )
                                    _d = probe_layers.get("deep")
                                    if _d:
                                        if _d.get("executed"):
                                            st.text(
                                                f"deep: trigger={_d.get('trigger', '?')} "
                                                f"keywords={_d.get('keywords', [])} "
                                                f"hits={_d.get('result_count', 0)}"
                                            )
                                        else:
                                            st.text(
                                                f"deep: skipped ({_d.get('reason', '?')})"
                                            )

                        rag = debug.get("rag", {})
                        if rag.get("results"):
                            st.caption("🔍 RAG Results")
                            for r in rag["results"]:
                                st.markdown(
                                    f"<div class='rag-ref'>"
                                    f"<b>{r['title']}</b> (score: {r.get('score', '?')})<br>"
                                    f"<i>{r.get('episode', '')[:200]}</i></div>",
                                    unsafe_allow_html=True,
                                )

                        st.caption("📝 Sent Prompt")
                        prompt_msgs = debug.get("prompt", [])
                        if prompt_msgs:
                            st.json(prompt_msgs)

                        prompt_full = debug.get("prompt_full", [])
                        if prompt_full:
                            with st.expander(
                                "📝 Full Prompt (untruncated)", expanded=False
                            ):
                                for i, m in enumerate(prompt_full):
                                    st.text(f"--- [{i}] role={m.get('role','?')} ---")
                                    st.code(m.get("content", ""), language=None)

                        st.caption("💬 Raw Response")
                        raw = debug.get("raw_response", "")
                        if raw:
                            st.code(raw[:2000], language=None)

                        ss = gk.get("session_state", {})
                        if ss:
                            st.caption("📊 Session State")
                            st.json(ss)

            # --- Google検索ソース (永続表示) ---
            msg_sources = msg.get("sources", [])
            if msg_sources:
                with st.expander("🌐 参照元 (Google検索)", expanded=False):
                    for src in msg_sources:
                        url = src.get("url", "")
                        title = src.get("title", url)
                        st.markdown(f"- [{title}]({url})")

    st.markdown("</div>", unsafe_allow_html=True)

    # 上記の描画が行われた後、少しスペースを空ける
    st.write("")

    # --- 画像添付エリア ---
    uploaded_files = st.file_uploader(
        "📎 画像を添付",
        type=["jpg", "jpeg", "png", "webp"],
        accept_multiple_files=True,
        key=f"img_uploader_{st.session_state.input_key_counter}",
        label_visibility="collapsed",
    )
    if uploaded_files:
        new_attachments = []
        for uf in uploaded_files[:3]:  # 最大3枚
            raw = uf.read()
            new_attachments.append(
                {
                    "kind": "image",
                    "mime_type": uf.type,
                    "data_base64": base64.b64encode(raw).decode(),
                    "name": uf.name,
                    "size": len(raw),
                }
            )
        st.session_state.pending_attachments = new_attachments
        st.caption(f"📎 {len(new_attachments)} 枚の画像を添付中")
    else:
        st.session_state.pending_attachments = []

    # --- チャット入力エリア ---
    # st.chat_input は画面下部に固定される仕様
    if prompt := st.chat_input(f"メッセージを入力... ({instance_name})"):
        # UIに即座に表示
        msg = {
            "role": "user",
            "parts": [prompt],
            "timestamp": datetime.now().isoformat(),
        }
        if st.session_state.pending_attachments:
            msg["attachments"] = st.session_state.pending_attachments
        st.session_state.messages.append(msg)
        st.session_state.input_key_counter += 1  # uploaderをリセット
        st.rerun()

    # --- 直前のユーザー入力があった場合に応答を生成 ---
    if st.session_state.messages and st.session_state.messages[-1]["role"] == "user":
        prompt_text = st.session_state.messages[-1]["parts"][0]

        with st.spinner("Thinking..."):
            try:
                import requests

                use_rag = True  # ChatRequestには常にTrueを渡す（実際のON/OFFはconfig.brain.use_ragでサーバー側が制御）
                use_gs = st.session_state.get("use_google_search", False)

                # 安全弁: 非Geminiプロバイダーでは強制的にFalseにする
                if use_gs and not is_gemini:
                    use_gs = False

                use_ws = (
                    st.session_state.get("use_web_search", False)
                    if not is_gemini
                    else False
                )

                # 添付画像をペイロードに変換
                last_msg = st.session_state.messages[-1]
                att_list = [
                    {
                        "kind": a["kind"],
                        "mime_type": a["mime_type"],
                        "data_base64": a["data_base64"],
                        "name": a.get("name"),
                        "size": a.get("size"),
                    }
                    for a in last_msg.get("attachments", [])
                ]
                payload = {
                    "message": prompt_text,
                    "instance_name": instance_name,
                    "use_rag": use_rag,
                    "use_google_search": use_gs,
                    "use_web_search": use_ws,
                    "attachments": att_list,
                }

                if st.session_state.get("streaming_enabled", True):
                    # --- ストリーミング経路 (/chat/stream SSE) ---
                    stream_state = {
                        "metadata": {},
                        "done": {},
                        "error": None,
                        "full_text": "",
                    }

                    def _sse_generator():
                        import json as _json

                        try:
                            with requests.post(
                                f"{api_url}/chat/stream",
                                json=payload,
                                stream=True,
                                timeout=180,
                            ) as resp:
                                if not resp.ok:
                                    stream_state["error"] = (
                                        f"APIエラー [{resp.status_code}]: {resp.text}"
                                    )
                                    return
                                current_event = None
                                event_data_lines = []
                                for raw_line in resp.iter_lines(decode_unicode=True):
                                    if raw_line is None:
                                        continue
                                    if raw_line == "":
                                        if (
                                            current_event is None
                                            or not event_data_lines
                                        ):
                                            current_event = None
                                            event_data_lines = []
                                            continue
                                        try:
                                            data = _json.loads(
                                                "\n".join(event_data_lines)
                                            )
                                        except Exception:
                                            current_event = None
                                            event_data_lines = []
                                            continue
                                        if current_event == "chunk":
                                            text = data.get("text", "")
                                            stream_state["full_text"] += text
                                            yield text
                                        elif current_event == "metadata":
                                            stream_state["metadata"] = data
                                        elif current_event == "done":
                                            stream_state["done"] = data
                                        elif current_event == "error":
                                            stream_state["error"] = data.get(
                                                "message", "stream error"
                                            )
                                        current_event = None
                                        event_data_lines = []
                                    elif raw_line.startswith("event:"):
                                        current_event = raw_line[
                                            len("event:") :
                                        ].strip()
                                    elif raw_line.startswith("data:"):
                                        event_data_lines.append(
                                            raw_line[len("data:") :].strip()
                                        )
                        except Exception as e:
                            stream_state["error"] = f"ストリーミングエラー: {e}"

                    # write_stream は generator をその場で消費し、yielded text を表示
                    with st.chat_message("assistant"):
                        st.write_stream(_sse_generator())

                    if stream_state["error"]:
                        st.error(stream_state["error"])
                        st.stop()

                    done_data = stream_state["done"] or {}
                    response_text = done_data.get(
                        "full_text", stream_state["full_text"]
                    )
                    sources = done_data.get("sources", []) or []
                    debug_info = done_data.get("debug_info")

                    st.session_state.messages.append(
                        {
                            "role": "model",
                            "parts": [response_text],
                            "timestamp": datetime.now().isoformat(),
                            "debug_info": debug_info,
                            "sources": sources,
                        }
                    )
                    st.session_state.last_interaction_time = datetime.now()
                    st.rerun()
                else:
                    # --- 非ストリーミング経路 (/chat) ---
                    resp = requests.post(
                        f"{api_url}/chat",
                        json=payload,
                        timeout=180,  # Gatekeeper（Ollama）の応答方式を考慮して長めに設定
                    )

                    if not resp.ok:
                        st.error(f"APIエラー [{resp.status_code}]: {resp.text}")
                        st.stop()

                    data = resp.json()
                    response_text = data.get("response", "")
                    keywords = data.get("keywords", [])
                    refs = data.get("references", [])
                    sources = data.get("sources", [])
                    tier = data.get("tier", "")

                    st.session_state.messages.append(
                        {
                            "role": "model",
                            "parts": [response_text],
                            "timestamp": datetime.now().isoformat(),
                            "debug_info": data.get("debug_info"),
                            "sources": sources,
                        }
                    )
                    st.session_state.last_interaction_time = datetime.now()

                    # 記憶の保存・整理は main.py 内の /chat で完結しているためここでは不要

                    st.rerun()

            except requests.exceptions.ConnectionError:
                st.error(
                    f"⚠️ FastAPIサーバーに接続できません。`uvicorn main:app --port 8000` が起動しているか確認してください。\n\n接続先: {api_url}"
                )
            except requests.exceptions.Timeout:
                st.error(
                    "⚠️ タイムアウトしました。Ollamaの応答やネットワークの状態を確認してください。"
                )
            except Exception as e:
                error_msg = str(e)
                if "403" in error_msg or "404" in error_msg:
                    st.warning(
                        "⚠️ 脳のキャッシュが期限切れです。記憶を再構築してリロードします... (Auto-healing)"
                    )
                    st.cache_resource.clear()
                    time.sleep(1)
                    st.rerun()
                else:
                    st.error(f"Error: {error_msg}")


# ==========================================
# 🎉 オンボーディング画面 (Onboarding Screen)
# ==========================================
def render_onboarding_screen():
    st.markdown('<h1 class="app-title">Butly へようこそ！</h1>', unsafe_allow_html=True)
    st.write("まずは最初のAIインスタンスを作成しましょう。")
    st.divider()

    new_proj_name = st.text_input(
        "インスタンス名（半角英数字・_）", placeholder="e.g. my_agent"
    )
    from butly_core import prompts

    new_template = st.text_area(
        "性格テンプレート",
        value=prompts.WEB_UI_DEFAULT_TEMPLATE.format(agent_name="{agent_name}"),
        height=100,
    )

    if st.button("作成", type="primary"):
        if new_proj_name:
            import requests

            api_url = st.session_state.api_base_url
            try:
                res = requests.post(
                    f"{api_url}/instances",
                    json={"name": new_proj_name, "template": new_template},
                    timeout=5,
                )
                if res.ok:
                    st.session_state.current_instance = new_proj_name
                    st.rerun()
                else:
                    st.error(f"作成エラー: {res.text}")
            except Exception as e:
                st.error(f"作成エラー: {e}")


# ==========================================
# 🔀 メインルーティング
# ==========================================
def main():
    # Re-scan instances (may have changed since startup)
    global available_instances
    try:
        available_instances = fetch_instance_names(
            st.session_state.get("api_base_url", DEFAULT_API_URL)
        )
        st.session_state.api_connection_error = None
    except Exception as e:
        available_instances = []
        st.session_state.api_connection_error = str(e)

    if not available_instances:
        if st.session_state.current_page not in {
            "home",
            "settings",
            "evaluations",
        }:
            st.session_state.current_page = "home"
            st.session_state.current_instance = None
        # インスタンス未作成でもホーム画面を表示（新規作成UIがホーム画面にある）
        if st.session_state.current_page == "settings":
            render_settings_screen()
            return
        if st.session_state.current_page == "evaluations":
            render_evaluation_screen()
            return
        render_home_screen()
        return

    if st.session_state.current_instance not in available_instances:
        st.session_state.current_instance = available_instances[0]

    if st.session_state.current_page == "home":
        render_home_screen()
    elif st.session_state.current_page == "chat":
        render_chat_screen()
    elif st.session_state.current_page == "settings":
        render_settings_screen()
    elif st.session_state.current_page == "evaluations":
        render_evaluation_screen()
    elif st.session_state.current_page == "pairing":
        render_pairing_screen()
    elif st.session_state.current_page == "instance_settings":
        render_instance_settings_screen()
    elif st.session_state.current_page == "sleeptime":
        render_sleeptime_screen()
    elif st.session_state.current_page == "database_browser":
        render_database_browser_screen()
    elif st.session_state.current_page == "card_edit":
        render_card_edit_screen()
    else:
        st.session_state.current_page = "home"
        st.rerun()


if __name__ == "__main__":
    main()

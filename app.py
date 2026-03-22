import streamlit as st
import time
from datetime import datetime
from pathlib import Path
import asyncio

# 自作モジュールのインポート
from butly_core.core.memory import ButlyMemory
from butly_core.core.brain import ButlyBrain
from butly_core.core.chronos import ButlyChronos
from butly_core.core.instance_manager import InstanceManager

# --- 基本設定 ---
BASE_DIR = Path(__file__).resolve().parent
INSTANCES_DIR = BASE_DIR / "butly_core" / "instances"

# --- UI設定 ---
st.set_page_config(page_title="Butly Web Console", page_icon="🤖", layout="wide", initial_sidebar_state="collapsed")

# --- マテリアル3風のカスタムCSS ---
st.markdown("""
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

    /* 汎用クラス */
    .clearfix::after {
        content: "";
        clear: both;
        display: table;
    }
    
    .debug-box { background-color: #262730; border-radius: 5px; padding: 10px; border: 1px solid #444; margin-top: 10px;}
    .rag-ref { font-size: 0.9em; color: #aaa; border-left: 2px solid #00ff00; padding-left: 10px; margin-bottom: 5px;}
</style>
""", unsafe_allow_html=True)

# --- マネージャー初期化 ---
instance_manager = InstanceManager(BASE_DIR)

# --- システム初期化 ---
@st.cache_resource
def initialize_system(base_dir, instance_name):
    print(f"[System] Initializing instance: {instance_name}")
    memory = ButlyMemory(base_dir, instance_name=instance_name)
    brain = ButlyBrain(base_dir) 
    chronos = ButlyChronos()
    from butly_core.llm.factory import ProviderFactory
    from butly_core.config import AI_CONFIG
    provider = ProviderFactory.create(AI_CONFIG["chat"]["model_name"])
    cached_content = provider.prepare_cache(memory, ttl_hours=3) if hasattr(provider, "prepare_cache") else None
    return memory, brain, chronos, cached_content

# --- 初期化・ディレクトリ作成 ---
if not INSTANCES_DIR.exists():
    INSTANCES_DIR.mkdir(parents=True, exist_ok=True)
    (INSTANCES_DIR / "00_master").mkdir(exist_ok=True)

available_instances = sorted([p.name for p in INSTANCES_DIR.iterdir() if p.is_dir() and not p.name.startswith(".")])
if not available_instances: available_instances = ["00_master"]

# --- セッションステートの初期化 ---
if "current_page" not in st.session_state:
    st.session_state.current_page = "home"
if "current_instance" not in st.session_state:
    st.session_state.current_instance = available_instances[0]
if "messages" not in st.session_state:
    st.session_state.messages = []
if "is_holiday" not in st.session_state:
    st.session_state.is_holiday = False
if "debug_mode" not in st.session_state:
    st.session_state.debug_mode = True
if "use_interactions_api" not in st.session_state:
    st.session_state.use_interactions_api = False
if "use_google_search" not in st.session_state:
    # インスタンス設定のdefault_use_google_searchを初期値に使用
    st.session_state.use_google_search = False
if "input_key_counter" not in st.session_state:
    st.session_state.input_key_counter = 0 # チャット入力欄クリア用
# デフォルトAPI接続先
DEFAULT_API_URL = "http://127.0.0.1:8000"
if "api_base_url" not in st.session_state:
    st.session_state.api_base_url = DEFAULT_API_URL
# テーマカラー (Butlyアプリ対応)
if "theme_color" not in st.session_state:
    st.session_state.theme_color = "teal"
if "housekeeper_instance" not in st.session_state:
    st.session_state.housekeeper_instance = None
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

# ==========================================
# 🏠 ホーム画面 (Home Screen)
# ==========================================
def render_home_screen():
    # ヘッダー
    col1, col2, col3, col4 = st.columns([6, 1, 1, 1])
    with col1:
        st.markdown('<h1 class="app-title">Butly</h1>', unsafe_allow_html=True)
    with col2:
        if st.button("🗄️", help="データベースブラウザ"):
            st.session_state.db_browser_instance = st.session_state.current_instance
            navigate_to("database_browser")
    with col3:
        if st.button("⚙️", help="設定"):
            navigate_to("settings")
    with col4:
        if st.button("🚪", help="終了 (セッションクリア)"):
            st.session_state.messages = []
            st.cache_resource.clear()
            st.success("セッションをクリアしました")

    st.divider()

    # インスタンス一覧
    st.subheader("Your AI Instances")
    if not available_instances:
        st.write("インスタンスがありません。")
    else:
        for name in available_instances:
            # st.button()を使った簡易なカード風リスト
            if st.button(f"🤖 {name}", key=f"btn_inst_{name}", use_container_width=True):
                navigate_to("chat", instance=name)

    st.divider()
    
    # 新規インスタンス作成 (FABの代わり)
    with st.expander("➕ 新しいインスタンスを作成"):
        new_proj_name = st.text_input("インスタンス名（半角英数字・_）", placeholder="e.g. new_agent")
        from butly_core import prompts
        new_template = st.text_area("性格テンプレート", value=prompts.WEB_UI_DEFAULT_TEMPLATE.format(agent_name="{agent_name}"), height=100)
        
        if st.button("作成", type="primary"):
            if new_proj_name:
                import requests
                api_url = st.session_state.api_base_url
                try:
                    res = requests.post(f"{api_url}/instances", json={"name": new_proj_name, "template": new_template}, timeout=5)
                    if res.ok:
                        st.rerun()
                    else:
                        st.error(f"作成エラー: {res.text}")
                except Exception as e:
                    st.error(f"作成エラー: {e}")

# ==========================================
# ⚙️ 設定画面 (タブ3構成) - Butly Client準拠
# ==========================================
def render_settings_screen():
    col1, col2 = st.columns([1, 8])
    with col1:
        if st.button("＜ 戻る"): navigate_to("home")
    with col2:
        st.markdown('<h1 class="app-title">⚙️ 設定</h1>', unsafe_allow_html=True)

    tab1, tab2, tab3, tab4 = st.tabs(["🏠 基本設定", "📝 プロンプト", "🤖 LLMプロバイダー", "🔧 詳細設定"])

    # ========================
    # タブ1: 基本設定
    # ========================
    with tab1:
        st.subheader("🔗 API接続先 (サーバーアドレス)")
        st.caption("ラズパイなどのバックエンドサーバのアドレスを入力してください。")
        new_url = st.text_input("サーバアドレス (URL)", value=st.session_state.api_base_url, placeholder="http://127.0.0.1:8000")
        if st.button("💾 接続先を保存", key="save_url"):
            st.session_state.api_base_url = new_url
            st.success(f"接続先を {new_url} に変更しました。")

        st.divider()
        st.subheader("🏖️ Holiday Mode (休暇設定)")
        st.session_state.is_holiday = st.toggle("🏖️ 休暇モードを有効にする", value=st.session_state.is_holiday)
        st.caption("有効にすると、AIは今日が休日であると認識して応答します。")

        st.divider()
        st.subheader("🔧 System Toggles")
        st.session_state.debug_mode = st.toggle("🐛 Debug Mode", value=st.session_state.debug_mode)
        st.session_state.use_interactions_api = st.toggle("🌐 Enable Interactions API (ステートフル)", value=st.session_state.use_interactions_api, help="オンにすると旧来のローカルメモリ(4層構造)を無効にし、Geminiサーバー側の会話セッション管理を利用します。")
        if st.button("🗑️ Clear Cache"):
            st.cache_resource.clear()
            st.success("Cache cleared!")

    # ========================
    # タブ2: プロンプト編集
    # ========================
    with tab2:
        st.subheader("📝 グローバルプロンプト編集")
        st.caption("各インスタンス共通のタイムコンテキストなどのグローバルプロンプトを編集できます。")
        import requests
        api_url = st.session_state.api_base_url
        try:
            resp = requests.get(f"{api_url}/prompts", timeout=5)
            if resp.ok:
                raw_prompts = resp.json()
                for key, val in raw_prompts.items():
                    with st.expander(f"📌 {key}"):
                        new_val = st.text_area(key, value=val, height=200, key=f"prompt_{key}")
                        if st.button("💾 保存", key=f"save_prompt_{key}"):
                            update_resp = requests.post(f"{api_url}/prompts", json={key: new_val}, timeout=5)
                            if update_resp.ok:
                                st.success(f"{key} を保存しました。")
                            else:
                                st.error(f"保存エラー: {update_resp.text}")
            else:
                st.error("プロンプト情報の取得に失敗しました。")
        except Exception as e:
            st.error(f"サーバー接続エラー: {e}")

    # ========================
    # タブ3: LLMプロバイダー設定
    # ========================
    with tab3:
        import requests
        api_url = st.session_state.api_base_url

        # --- プロバイダー判定ヘルパー ---
        def get_provider_label(model_name: str) -> str:
            if model_name.startswith("gemini") or model_name.startswith("models/gemini"):
                return "🟦 Gemini"
            elif model_name.startswith(("gpt-", "o1-", "o3-", "o4-", "text-embedding")):
                return "🟩 OpenAI"
            elif model_name in ("llama3.2", "mistral", "qwen2.5", "nomic-embed-text") or model_name.startswith("ollama/"):
                return "🟧 Ollama"
            else:
                return "❓ 不明"

        # --- セクション1: APIキー管理 ---
        st.subheader("🔑 APIキー設定")

        # ステータス取得
        key_status = {"gemini": False, "openai": False}
        try:
            status_resp = requests.get(f"{api_url}/settings/api_key_status", timeout=5)
            if status_resp.ok:
                key_status = status_resp.json()
        except Exception:
            pass

        gemini_status = "✅ 設定済み" if key_status.get("gemini") else "❌ 未設定"
        openai_status = "✅ 設定済み" if key_status.get("openai") else "❌ 未設定"
        st.caption(f"Gemini: {gemini_status} / OpenAI: {openai_status}")

        col_k1, col_k2 = st.columns(2)
        with col_k1:
            gemini_key = st.text_input("Google Gemini API Key", type="password", placeholder="AIza...", key="provider_gemini_key")
            if st.button("💾 保存", key="save_gemini_key"):
                if gemini_key:
                    try:
                        resp = requests.post(f"{api_url}/settings/api_key", json={"api_key": gemini_key, "key_type": "gemini"}, timeout=5)
                        if resp.ok:
                            st.success("✅ Gemini APIキーを保存しました。")
                            st.rerun()
                        else:
                            st.error(f"保存エラー: {resp.text}")
                    except Exception as e:
                        st.error(f"サーバー接続エラー: {e}")
                else:
                    st.warning("キーが入力されていません。")
        with col_k2:
            openai_key = st.text_input("OpenAI API Key", type="password", placeholder="sk-...", key="provider_openai_key")
            if st.button("💾 保存", key="save_openai_key"):
                if openai_key:
                    try:
                        resp = requests.post(f"{api_url}/settings/api_key", json={"api_key": openai_key, "key_type": "openai"}, timeout=5)
                        if resp.ok:
                            st.success("✅ OpenAI APIキーを保存しました。")
                            st.rerun()
                        else:
                            st.error(f"保存エラー: {resp.text}")
                    except Exception as e:
                        st.error(f"サーバー接続エラー: {e}")
                else:
                    st.warning("キーが入力されていません。")

        st.divider()

        # --- セクション2: Ollama接続設定 ---
        st.subheader("🖥️ Ollama (ローカルLLM)")

        # 現在の設定を取得
        try:
            cfg_resp = requests.get(f"{api_url}/config", timeout=5)
            provider_cfg = cfg_resp.json() if cfg_resp.ok else {"AI_CONFIG": {}, "SYSTEM_CONFIG": {}}
        except Exception:
            provider_cfg = {"AI_CONFIG": {}, "SYSTEM_CONFIG": {}}

        ollama_url = st.text_input(
            "接続先URL",
            value="http://localhost:11434",
            placeholder="http://localhost:11434",
            key="ollama_url",
        )
        if st.button("🔍 接続テスト", key="test_ollama"):
            try:
                resp = requests.post(f"{api_url}/settings/ollama_test", json={"url": ollama_url}, timeout=10)
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

        MODEL_PRESETS = {
            "chat": [
                "gemini-3.1-pro-preview",
                "gemini-3-flash-preview",
                "gemini-3.1-flash-lite-preview",
                "gemini-2.5-pro",
                "gemini-2.5-flash",
                "gpt-4o",
                "gpt-4o-mini",
                "o3-mini",
                "llama3.2",
                "mistral",
                "qwen2.5",
            ],
            "summary": [
                "gemini-3.1-flash-lite-preview",
                "gemini-2.5-flash",
                "gpt-4o-mini",
                "llama3.2",
                "mistral",
            ],
            "gatekeeper": [
                "gemini-3.1-flash-lite-preview",
                "gemini-2.5-flash-lite",
                "gpt-4o-mini",
                "llama3.2",
            ],
            "embedding": [
                "models/gemini-embedding-001",
                "text-embedding-3-small",
                "text-embedding-3-large",
                "nomic-embed-text",
            ],
        }

        ROLE_LABELS = {
            "chat": "Chat (メイン応答)",
            "summary": "Summary (要約)",
            "gatekeeper": "Gatekeeper (Tier判定)",
            "embedding": "Embedding (ベクトル化)",
        }

        provider_ai_cfg = provider_cfg.get("AI_CONFIG", {})
        model_selections = {}

        for role in ["chat", "summary", "gatekeeper", "embedding"]:
            current_model = provider_ai_cfg.get(role, {}).get("model_name", MODEL_PRESETS[role][0])
            opts = list(MODEL_PRESETS[role])
            if current_model not in opts:
                opts.append(current_model)
            selected = st.selectbox(
                ROLE_LABELS[role],
                opts,
                index=opts.index(current_model),
                key=f"provider_model_{role}",
            )
            st.caption(f"プロバイダー: {get_provider_label(selected)}")
            model_selections[role] = selected

        if st.button("💾 モデル設定を保存", type="primary", key="save_provider_models"):
            # provider_cfg のAI_CONFIGを更新
            for role, model_name in model_selections.items():
                provider_ai_cfg.setdefault(role, {})["model_name"] = model_name
            provider_cfg["AI_CONFIG"] = provider_ai_cfg
            try:
                save_resp = requests.post(f"{api_url}/config", json=provider_cfg, timeout=5)
                if save_resp.ok:
                    st.success("モデル設定を保存しました。")
                else:
                    st.error(f"保存エラー: {save_resp.text}")
            except Exception as e:
                st.error(f"サーバー接続エラー: {e}")

        st.divider()

        # --- セクション4: Embedding再生成 ---
        st.subheader("🔄 Embeddingの再生成")
        st.caption("Embeddingモデルを変更した場合、既存の記憶データベースのベクトルを再生成する必要があります。")

        reindex_target = st.selectbox(
            "対象インスタンス",
            ["__all__"] + available_instances,
            format_func=lambda x: "全インスタンス" if x == "__all__" else x,
            key="reindex_target",
        )
        if st.button("🔄 再生成を実行", key="run_reindex"):
            try:
                resp = requests.post(
                    f"{api_url}/settings/reindex_embeddings",
                    json={"instance_name": reindex_target},
                    timeout=10,
                )
                if resp.ok:
                    st.success(f"Embedding再生成を開始しました。(対象: {reindex_target})")
                else:
                    st.error(f"エラー: {resp.text}")
            except Exception as e:
                st.error(f"サーバー接続エラー: {e}")

    # ========================
    # タブ4: 詳細設定 (AIモデル/システム)
    # ========================
    with tab4:
        import requests
        api_url = st.session_state.api_base_url
        try:
            resp = requests.get(f"{api_url}/config", timeout=5)
            cfg = resp.json() if resp.ok else {"AI_CONFIG": {}, "SYSTEM_CONFIG": {}}
        except Exception as e:
            st.error(f"サーバー接続エラー: {e}")
            cfg = {"AI_CONFIG": {}, "SYSTEM_CONFIG": {}}

        ai_cfg = cfg.get("AI_CONFIG", {})
        sys_cfg = cfg.get("SYSTEM_CONFIG", {})

        CHAT_MODELS = ['gemini-3.1-pro-preview', 'gemini-3-flash-preview', 'gemini-3.1-flash-lite-preview', 'gemini-2.5-pro', 'gemini-2.5-flash', 'gemini-2.5-flash-lite']

        st.subheader("🤖 AIモデル設定")
        st.caption("チャット/性格設定は各インスタンスの設定画面から変更できます。※メインのモデル設定は「LLMプロバイダー」タブで変更できます。")

        for model_key in ['summary', 'knowledge']:
            mc = ai_cfg.get(model_key, {})
            with st.expander(f"**{model_key.upper()}** ({mc.get('model_name', '(unset)')})", expanded=False):
                curr = mc.get('model_name', 'gemini-3.1-flash-lite-preview')
                opts = CHAT_MODELS if curr in CHAT_MODELS else CHAT_MODELS + [curr]
                ai_cfg.setdefault(model_key, {})['model_name'] = st.selectbox( 
                    "モデル名", opts, index=opts.index(curr), key=f"adv_model_{model_key}"
                )
                gen = mc.get('generation_config', {})
                ai_cfg[model_key]['generation_config'] = ai_cfg[model_key].get('generation_config', {})
                ai_cfg[model_key]['generation_config']['temperature'] = st.slider(
                    "Temperature", 0.0, 2.0, step=0.1,
                    value=float(gen.get('temperature', 0.7)), key=f"adv_temp_{model_key}"
                )

        st.divider()
        st.subheader("🗠 システム設定 (Brain / Memory)")
        brain_s = sys_cfg.get('brain', {})
        memory_s = sys_cfg.get('memory', {})

        col_s1, col_s2 = st.columns(2)
        with col_s1:
            brain_s['search_limit'] = st.number_input("検索リミット", 1, 20, int(brain_s.get('search_limit', 3)), key="adv_sl")
            brain_s['keyword_hit_threshold'] = st.number_input( 
                "キーワード閾値", 1, 20, int(brain_s.get('keyword_hit_threshold', 5)), key="adv_kht"
            )
            brain_s['cache_ttl_hours'] = st.number_input("キャッシュ有効期限(時間)", 1, 72, int(brain_s.get('cache_ttl_hours', 3)), key="adv_cttl")
        with col_s2:
            brain_s['fallback_fetch_limit'] = st.slider( 
                "フォールバック上限", 10, 200, int(brain_s.get('fallback_fetch_limit', 50)), step=10, key="adv_ffl"
            )
            brain_s['summary_char_limit'] = st.slider( 
                "要約文字数上限", 50, 500, int(brain_s.get('summary_char_limit', 200)), step=50, key="adv_scl"
            )
            memory_s['max_mid_term_chars'] = st.slider( 
                "長期記憶最大文字数", 5000, 100000, int(memory_s.get('max_mid_term_chars', 30000)), step=5000, key="adv_mmt"
            )
            memory_s['use_summarized_mid_term'] = st.toggle(
                "中期記憶の二層要約注入を有効化", value=memory_s.get('use_summarized_mid_term', True), key="adv_umt", help="RAWテキストの代わりに生成された要約をプロンプトに注入します。"
            )

        brain_s['dynamic_threshold'] = st.slider( 
            "Google動的閾値 (dynamic_threshold)", 0.0, 1.0,
            float(brain_s.get('dynamic_threshold', 0.6)), step=0.05, key="adv_dt"
        )

        st.divider()
        if st.button("💾 詳細設定を保存", type="primary"):
            cfg['AI_CONFIG'] = ai_cfg
            cfg['SYSTEM_CONFIG']['brain'] = brain_s
            cfg['SYSTEM_CONFIG']['memory'] = memory_s
            try:
                save_resp = requests.post(f"{api_url}/config", json=cfg, timeout=5)
                if save_resp.ok:
                    st.success("詳細設定を保存しました。")
                else:
                    st.error(f"保存エラー: {save_resp.text}")
            except Exception as e:
                st.error(f"サーバー接続エラー: {e}")


# ==========================================
# 🧹 Housekeeper画面
# ==========================================
def render_housekeeper_screen():
    import requests
    instance_name = st.session_state.housekeeper_instance or st.session_state.current_instance
    api_url = st.session_state.api_base_url

    col1, col2 = st.columns([1, 8])
    with col1:
        if st.button("＜ 戻る", key="hk_back"): navigate_to("instance_settings")
    with col2:
        st.markdown(f'<h1 class="app-title">🧹 記憶の整理: {instance_name}</h1>', unsafe_allow_html=True)
    st.divider()

    # 推定情報の取得
    try:
        resp = requests.get(f"{api_url}/housekeeper/estimate/{instance_name}", timeout=5)
        est = resp.json() if resp.ok else {}
    except Exception:
        est = {}

    group_count = est.get("group_count", "?") 
    est_seconds = est.get("estimated_seconds", "?")

    st.metric("未処理の記憶グループ", group_count)
    st.metric("予測所要時間", f"約 {est_seconds} 秒")
    st.divider()

    if st.button("▶ 整理を開始する", type="primary", use_container_width=True):
        try:
            r = requests.post(f"{api_url}/housekeeper/run/{instance_name}", timeout=5)
            if r.ok:
                st.session_state["hk_running"] = True
                st.rerun()
            else:
                st.error(f"エラー: {r.text}")
        except Exception as e:
            st.error(f"サーバー接続エラー: {e}")

    if st.session_state.get("hk_running"):
        status_placeholder = st.empty()
        progress_bar = st.progress(0)
        for _ in range(120):  # 最大2分ポーリング
            try:
                r = requests.get(f"{api_url}/housekeeper/status/{instance_name}", timeout=5)
                status = r.json() if r.ok else {}
            except Exception:
                status = {}
            state = status.get("state", "running")
            msg = status.get("message", "処理中...")
            prog = int(status.get("progress", 0))
            status_placeholder.markdown(f"**{msg}**")
            progress_bar.progress(prog / 100.0)
            if state in ("completed", "error"):
                st.session_state["hk_running"] = False
                if state == "completed":
                    st.success("整理が完了しました！")
                else:
                    st.error(f"エラー: {msg}")
                break
            time.sleep(1)

# ==========================================
# 🗋 DBブラウザ (Database Browser Screen)
# ==========================================
def render_database_browser_screen():
    from butly_core.core.database import ButlyDatabase

    col1, col2 = st.columns([1, 8])
    with col1:
        if st.button("＜ 戻る", key="db_back"): navigate_to("home")
    with col2:
        st.markdown('<h1 class="app-title">🗋 データベースブラウザ</h1>', unsafe_allow_html=True)
    st.divider()

    # インスタンス選択 & フィルター
    col_f1, col_f2, col_f3 = st.columns([2, 2, 3])
    with col_f1:
        sel_inst = st.selectbox("対象AI", available_instances,
            index=available_instances.index(st.session_state.db_browser_instance)
            if st.session_state.db_browser_instance in available_instances else 0,
            key="db_inst_sel")
        st.session_state.db_browser_instance = sel_inst
    CATEGORIES = ["", "Unclassified", "UserPreference", "LifeEvent", "Task", "Thought", "Project"]
    with col_f2:
        sel_cat = st.selectbox("カテゴリ", CATEGORIES,
            format_func=lambda c: "すべて" if c == "" else c, key="db_cat_sel")
    with col_f3:
        search_q = st.text_input("🔍 検索", placeholder="キーワードを入力...", key="db_search")

    # API経由でカード一覧を取得
    import requests
    api_url = st.session_state.api_base_url
    
    try:
        req_params = {"limit": 100, "offset": 0}
        if sel_cat: req_params["category"] = sel_cat
        if search_q: req_params["search"] = search_q
        
        resp = requests.get(f"{api_url}/database/cards/{sel_inst}", params=req_params, timeout=10)
        if resp.ok:
            rows = resp.json()
            # 取得した辞書のリストをソート
            rows = sorted(rows, key=lambda x: (x.get('is_pinned') or 0, x.get('ai_importance') or 0), reverse=True)
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
                st.markdown(f"**{pinned_icon} {title}** `{cat}` &nbsp; ⭐{ai_imp} 💓{hu_imp}", unsafe_allow_html=True)
                st.caption((episode or "")[:120] + ("..." if episode and len(episode) > 120 else ""))
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
        if st.button("＜ 戻る", key="ce_back"): navigate_to("database_browser")
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

    CATEGORIES = ["Unclassified", "UserPreference", "LifeEvent", "Task", "Thought", "Project"]
    cat_val = card_info.get("category", "")
    title = st.text_input("タイトル", value=card_info.get("title", ""))
    cat = st.selectbox("カテゴリ", CATEGORIES,
        index=CATEGORIES.index(cat_val) if cat_val in CATEGORIES else 0)
    episode = st.text_area("エピソード", value=card_info.get("episode") or "", height=200)
    col_i1, col_i2 = st.columns(2)
    with col_i1:
        ai_imp = st.slider("AI重要度", 0, 10, int(card_info.get("ai_importance") or 5))
    with col_i2:
        hu_imp = st.slider("人間重要度", 0, 10, int(card_info.get("humanity_importance") or 5))
    pinned = st.checkbox("📌 ピン留め", value=bool(card_info.get("is_pinned")))

    col_a1, col_a2 = st.columns([6, 2])
    with col_a1:
        if st.button("💾 保存", type="primary", use_container_width=True):
            try:
                update_data = {
                    "title": title,
                    "category": cat,
                    "episode": episode,
                    "ai_importance": ai_imp,
                    "humanity_importance": hu_imp
                }
                
                # Check for pin update
                is_pinned_prev = bool(card_info.get("is_pinned"))
                if pinned != is_pinned_prev:
                    pin_resp = requests.post(f"{api_url}/database/cards/{inst}/{card_id}/pin", json={"is_pinned": pinned}, timeout=5)
                    if not pin_resp.ok:
                         st.error(f"ピン留め更新エラー: {pin_resp.text}")
                
                upd_resp = requests.put(f"{api_url}/database/cards/{inst}/{card_id}", json=update_data, timeout=5)
                
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
                    del_resp = requests.delete(f"{api_url}/database/cards/{inst}/{card_id}", timeout=5)
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
def render_instance_settings_screen():
    instance_name = st.session_state.current_instance
    
    col1, col2 = st.columns([1, 8])
    with col1:
        if st.button("＜ 戻る", key="btn_back_inst_settings"): navigate_to("chat")
    with col2:
        st.markdown(f'<h1 class="app-title">設定: {instance_name}</h1>', unsafe_allow_html=True)
    
    st.divider()

    import requests
    api_url = st.session_state.api_base_url
    
    # Load Configs
    try:
        cfg_resp = requests.get(f"{api_url}/instances/{instance_name}/config", timeout=5)
        config = cfg_resp.json() if cfg_resp.ok else {}
        prm_resp = requests.get(f"{api_url}/instances/{instance_name}/prompts", timeout=5)
        prompts = prm_resp.json() if prm_resp.ok else {"system_instruction": "", "key_memory": ""}
    except Exception as e:
        st.error(f"API接続エラー: {e}")
        config = {}
        prompts = {"system_instruction": "", "key_memory": ""}
    
    # Initialize defaults if empty
    if "brain" not in config:
        config["brain"] = {"use_context_cache": False, "filter_memory_by_type": True} # Context Cache DEFAULT OFF
    if "chat" not in config:
        config["chat"] = {
            "model_name": "gemini-3-flash-preview",
            "generation_config": {"temperature": 1.0, "top_p": 0.95, "top_k": 40, "max_output_tokens": 8192}
        }
    if "memory" not in config:
        config["memory"] = {"max_mid_term_chars": 30000, "short_term_limit": 6}

    # State elements
    sys_inst = st.text_area("System Instruction (性格設定)", value=prompts.get("system_instruction", ""), height=150)
    key_mem = st.text_area("Key Memory (根幹記憶)", value=prompts.get("key_memory", ""), height=150)
    
    st.divider()
    st.subheader("脳・記憶設定 (Brain)")
    
    # Context Cache (Default OFF as requested)
    use_cache = st.toggle("コンテキストキャッシュ (Mid-term Memory)", value=config["brain"].get("use_context_cache", False))
    filter_mem = st.toggle("インスタンス別記憶フィルタ", value=config["brain"].get("filter_memory_by_type", True), help="無効にすると他のインスタンスの記憶も検索対象になります。")
    default_gs = st.toggle("Google検索のデフォルト有効化", value=config["brain"].get("default_use_google_search", False))
    
    col_b1, col_b2 = st.columns(2)
    with col_b1:
        search_lim = st.number_input("検索リミット (Search Limit)", min_value=1, max_value=10, value=config["brain"].get("search_limit", 3))
        fallback_lim = st.slider("フォールバック取得数", min_value=10, max_value=100, step=10, value=config["brain"].get("fallback_fetch_limit", 50))
    with col_b2:
        keyword_thr = st.number_input("キーワードヒット閾値", min_value=1, max_value=10, value=config["brain"].get("keyword_hit_threshold", 5))
        cache_ttl = st.slider("キャッシュ有効期限 (時間)", min_value=1, max_value=24, step=1, value=config["brain"].get("cache_ttl_hours", 3))
        
    st.divider()
    st.subheader("メモリ容量設定 (Memory Capacity)")
    col_m1, col_m2 = st.columns(2)
    with col_m1:
        st_limit = st.number_input("短期記憶 (Short-term) 保存数", min_value=2, max_value=12, step=2, value=config["memory"].get("short_term_limit", 6))
    with col_m2:
        mt_max = st.slider("長期記憶 (Mid-term) 最大文字数", min_value=5000, max_value=100000, step=5000, value=config["memory"].get("max_mid_term_chars", 30000))

    st.divider()
    st.subheader("生成モデル設定 (Generation)")
    models = [
        # 最新世代 (Gemini 3 / latest)
        'gemini-3.1-pro-preview',
        'gemini-3-flash-preview',
        'gemini-3.1-flash-lite-preview',
        # Gemini 2.5系 (最新安定プレビュー)
        'gemini-2.5-pro',
        'gemini-2.5-flash',
        'gemini-2.5-flash-lite',
    ]
    current_model = config["chat"].get("model_name", "gemini-3-flash-preview")
    if current_model not in models: models.append(current_model)
    
    model_name = st.selectbox("モデル名", models, index=models.index(current_model))
    
    gen_config = config["chat"].get("generation_config", {})
    temp = st.slider("Temperature", min_value=0.0, max_value=2.0, step=0.1, value=float(gen_config.get("temperature", 1.0)))
    top_p = st.slider("Top P", min_value=0.0, max_value=1.0, step=0.05, value=float(gen_config.get("top_p", 0.95)))
    
    col_g1, col_g2 = st.columns(2)
    with col_g1:
        top_k = st.number_input("Top K", min_value=1, value=int(gen_config.get("top_k", 40)))
    with col_g2:
        max_tokens = st.number_input("最大出力トークン数", min_value=1, value=int(gen_config.get("max_output_tokens", 8192)))

    st.divider()
    
    # Action Buttons
    col_a1, col_a2, col_a3 = st.columns([6, 2, 2])
    with col_a2:
        if st.button("設定を保存", type="primary", use_container_width=True):
            # Update values
            config["brain"]["use_context_cache"] = use_cache
            config["brain"]["filter_memory_by_type"] = filter_mem
            config["brain"]["default_use_google_search"] = default_gs
            config["brain"]["search_limit"] = search_lim
            config["brain"]["fallback_fetch_limit"] = fallback_lim
            config["brain"]["keyword_hit_threshold"] = keyword_thr
            config["brain"]["cache_ttl_hours"] = cache_ttl
            
            config["memory"]["short_term_limit"] = st_limit
            config["memory"]["max_mid_term_chars"] = mt_max
            
            config["chat"]["model_name"] = model_name
            config["chat"]["generation_config"]["temperature"] = temp
            config["chat"]["generation_config"]["top_p"] = top_p
            config["chat"]["generation_config"]["top_k"] = top_k
            config["chat"]["generation_config"]["max_output_tokens"] = max_tokens
            
            # Save configs
            try:
                c_resp = requests.post(f"{api_url}/instances/{instance_name}/config", json=config, timeout=5)
                p_resp = requests.post(f"{api_url}/instances/{instance_name}/prompts", json={
                    "system_instruction": sys_inst,
                    "key_memory": key_mem
                }, timeout=5)
                
                if c_resp.ok and p_resp.ok:
                    st.success("設定を保存しました。")
                    time.sleep(1)
                    navigate_to("chat")
                else:
                    st.error(f"保存エラー: Config[{c_resp.status_code}], Prompts[{p_resp.status_code}]")
            except Exception as e:
                st.error(f"保存エラー: {e}")
            
    with col_a3:
        # Delete Instance (Danger Zone)
        with st.popover("🗑️ インスタンスを完全に削除"):
            st.warning("この操作は取り消せません。インスタンスのフォルダ、設定、短期記憶・長期記憶がすべて完全に削除されます。")
            if st.button("完全に削除する", type="primary"):
                try:
                    del_resp = requests.delete(f"{api_url}/instances/{instance_name}", timeout=5)
                    if del_resp.ok:
                        msg = del_resp.json().get("message", "Deleted")
                        st.success(msg)
                        time.sleep(1)
                        st.session_state.current_instance = "00_master"
                        navigate_to("home")
                    else:
                        st.error(f"削除エラー: {del_resp.text}")
                except Exception as e:
                    st.error(f"削除エラー: {e}")

# ==========================================
# 💬 チャット画面 (Chat Screen)
# ==========================================
def render_chat_screen():
    instance_name = st.session_state.current_instance
    # 履歴表示用に memory のみ初期化（brain / cached_content は FastAPI 側で管理）
    memory, brain, chronos, cached_content = initialize_system(BASE_DIR, instance_name)
    api_url = st.session_state.api_base_url

    # --- チャットヘッダー ---
    col1, col2, col3, col4, col5 = st.columns([1, 6, 1, 1, 1])
    with col1:
        if st.button("＜", help="戻る"): navigate_to("home")
    with col2:
        st.markdown(f'<h1 class="app-title">{instance_name}</h1>', unsafe_allow_html=True)
    with col3:
        if st.button("⚙️", help="インスタンス設定"):
            navigate_to("instance_settings")
    with col4:
        if st.button("🔄", help="履歴をリロード"):
            st.session_state.messages = []
            if "last_interaction_time" in st.session_state:
                del st.session_state.last_interaction_time
            st.rerun()
    with col5:
        # Google Search toggle: 押すたびにON/OFF切り替え
        gs_on = st.session_state.get("use_google_search", False)
        gs_label = "🌐 ON" if gs_on else "🌐"
        gs_help = "Google検索: ON（クリックでOFF）" if gs_on else "Google検索: OFF（クリックでON）"
        if st.button(gs_label, help=gs_help):
            st.session_state.use_google_search = not gs_on
            st.rerun()

    st.divider()
    
    # --- 履歴の読み込み処理 ---
    if not st.session_state.messages:
        history_msgs, last_ts = memory.load_recent_sessions(limit=6)
        if last_ts is None:
            last_ts = memory.get_last_interaction_time()
        
        st.session_state.last_interaction_time = last_ts
        for msg in history_msgs:
            role = msg.get("role")
            content = msg.get("parts", [""])[0]
            if isinstance(content, dict): content = content.get("text", "")
            msg_to_append = {"role": role, "parts": [content]}
            if "timestamp" in msg:
                msg_to_append["timestamp"] = msg["timestamp"]
            st.session_state.messages.append(msg_to_append)

    sys_note = chronos.get_system_note(
        is_holiday=st.session_state.is_holiday,
        last_interaction_time=st.session_state.get("last_interaction_time")
    )

    if st.session_state.debug_mode:
        with st.expander("🕒 Time Context (Debug)"):
            st.text(sys_note)

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
            except: pass
            
        if role == "user":
            st.markdown(f'<div class="chat-bubble-user">{ts_display}{text}</div>', unsafe_allow_html=True)
        else:
            st.markdown(f'<div class="chat-bubble-ai">{ts_display}{text}</div>', unsafe_allow_html=True)
            
    st.markdown('</div>', unsafe_allow_html=True)

    # 上記の描画が行われた後、少しスペースを空ける
    st.write("")

    # --- チャット入力エリア ---
    # st.chat_input は画面下部に固定される仕様
    if prompt := st.chat_input(f"メッセージを入力... ({instance_name})"):
        # UIに即座に表示
        st.session_state.messages.append({
            "role": "user", 
            "parts": [prompt],
            "timestamp": datetime.now().isoformat()
        })
        st.rerun()

    # --- 直前のユーザー入力があった場合に応答を生成 ---
    if st.session_state.messages and st.session_state.messages[-1]["role"] == "user":
        prompt_text = st.session_state.messages[-1]["parts"][0]
        
        with st.spinner("Thinking..."):
            try:
                import requests
                use_rag = not st.session_state.use_interactions_api
                use_gs = st.session_state.get("use_google_search", False)

                # 画像添付は現在の実装では未対応のため空配列
                payload = {
                    "message": prompt_text,
                    "instance_name": instance_name,
                    "use_rag": use_rag,
                    "use_google_search": use_gs,
                    "images": [],
                }

                resp = requests.post(
                    f"{api_url}/chat",
                    json=payload,
                    timeout=180,  # Gatekeeper（Ollama）の忪touch方式を考慮して長めに設定
                )

                if not resp.ok:
                    st.error(f"APIエラー [{resp.status_code}]: {resp.text}")
                    st.stop()

                data = resp.json()
                response_text = data.get("response", "")
                keywords     = data.get("keywords", [])
                refs         = data.get("references", [])
                sources      = data.get("sources", [])
                tier         = data.get("tier", "")

                st.session_state.messages.append({
                    "role": "model",
                    "parts": [response_text],
                    "timestamp": datetime.now().isoformat()
                })
                st.session_state.last_interaction_time = datetime.now()

                # 記憶の保存・整理は main.py 内の /chat で完結しているためここでは不要

                # --- Debug 表示 ---
                if st.session_state.debug_mode:
                    if tier:
                        st.caption(f"🧠 Tier: `{tier}`")

                    # ★ Phase 2: Gatekeeper v2 Details
                    need = data.get("need")
                    search_targets = data.get("search_targets")
                    session_st = data.get("session_state", {})

                    if need or search_targets or session_st:
                        with st.expander("🧠 Gatekeeper v2 Details", expanded=False):
                            if need:
                                st.write(f"**Need:** {need}")
                            if search_targets:
                                st.write(f"**Search Targets:** {search_targets}")
                            if session_st:
                                st.json(session_st)

                    if not st.session_state.use_interactions_api and (keywords or refs):
                        with st.expander("🧠 Brain Process (RAG / Local Memory)", expanded=False):
                            st.write(f"**Keywords:** {keywords}")
                            if refs:
                                for r in refs:
                                    st.markdown(
                                        f"<div class='rag-ref'><b>{r['title']}</b><br><i>{r.get('episode','')}</i></div>",
                                        unsafe_allow_html=True
                                    )
                            else:
                                st.info("No relevant long-term memories found.")

                # Google検索ソース
                if sources:
                    with st.expander("🌐 参照元 (Google検索)", expanded=False):
                        for src in sources:
                            url   = src.get("url", "")
                            title = src.get("title", url)
                            st.markdown(f"- [{title}]({url})")

                st.rerun()

            except requests.exceptions.ConnectionError:
                st.error(f"⚠️ FastAPIサーバーに接続できません。`uvicorn main:app --port 8000` が起動しているか確認してください。\n\n接続先: {api_url}")
            except requests.exceptions.Timeout:
                st.error("⚠️ タイムアウトしました。Ollamaの応答やネットワークの状態を確認してください。")
            except Exception as e:
                error_msg = str(e)
                if "403" in error_msg or "404" in error_msg:
                    st.warning("⚠️ 脳のキャッシュが期限切れです。記憶を再構築してリロードします... (Auto-healing)")
                    st.cache_resource.clear()
                    time.sleep(1)
                    st.rerun()
                else:
                    st.error(f"Error: {error_msg}")



# ==========================================
# 🔀 メインルーティング
# ==========================================
def main():
    if st.session_state.current_page == "home":
        render_home_screen()
    elif st.session_state.current_page == "chat":
        render_chat_screen()
    elif st.session_state.current_page == "settings":
        render_settings_screen()
    elif st.session_state.current_page == "instance_settings":
        render_instance_settings_screen()
    elif st.session_state.current_page == "housekeeper":
        render_housekeeper_screen()
    elif st.session_state.current_page == "database_browser":
        render_database_browser_screen()
    elif st.session_state.current_page == "card_edit":
        render_card_edit_screen()
    else:
        st.session_state.current_page = "home"
        st.rerun()

if __name__ == "__main__":
    main()
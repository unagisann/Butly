import streamlit as st
import time
import base64
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
""", unsafe_allow_html=True)

# --- 共通ヘルパー関数 ---
@st.cache_data(ttl=30)
def _fetch_ollama_models(api_url: str) -> list:
    """Ollama サーバーからインストール済みモデル一覧を取得する。"""
    try:
        import requests as _req
        import json as _json
        _user_cfg_path = Path(__file__).parent / "user_config.json"
        _ollama_url = "http://localhost:11434"
        if _user_cfg_path.exists():
            _uc = _json.loads(_user_cfg_path.read_text(encoding="utf-8"))
            _ollama_url = _uc.get("LLM_PROVIDERS", {}).get("ollama", {}).get("base_url", _ollama_url)
        resp = _req.post(f"{api_url}/settings/ollama_test", json={"url": _ollama_url}, timeout=5)
        if resp.ok:
            data = resp.json()
            if data.get("status") == "ok":
                return [f"ollama/{m}" for m in data.get("models", [])]
    except Exception:
        pass
    return []


def get_provider_label(model_name: str) -> str:
    """モデル名からプロバイダーラベルを返す。"""
    if not model_name:
        return "❓ 不明"
    if model_name.startswith("gemini") or model_name.startswith("models/gemini"):
        return "🟦 Gemini"
    elif model_name.startswith(("gpt-", "o1-", "o3-", "o4-", "text-embedding")):
        return "🟩 OpenAI"
    elif model_name.startswith("ollama/"):
        return "🟧 Ollama"
    else:
        return "❓ 不明"


def _model_selector(label: str, current_value: str, candidates: list, key_prefix: str) -> str:
    """
    モデル選択の共通ウィジェット。
    selectbox + 直接入力欄を表示し、最終的なモデル名を返す。
    """
    opts = list(candidates)

    # 現在値がリストになければ追加（過去に設定したカスタム値の保持）
    if current_value and current_value not in opts:
        opts.append(current_value)

    idx = opts.index(current_value) if current_value in opts else 0
    selected = st.selectbox(label, opts, index=idx, key=f"{key_prefix}_select")

    # 直接入力欄
    custom = st.text_input(
        "または直接入力:",
        value="",
        placeholder="例: ollama/my-model / gemini-2.5-pro",
        key=f"{key_prefix}_custom",
        label_visibility="visible",
    )

    # 直接入力があればそちらを優先
    final = custom.strip() if custom.strip() else selected

    # プロバイダー表示
    st.caption(f"プロバイダー: {get_provider_label(final)}")

    return final


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

available_instances = sorted([p.name for p in INSTANCES_DIR.iterdir() if p.is_dir() and not p.name.startswith(".")])

# --- セッションステートの初期化 ---
if "current_page" not in st.session_state:
    st.session_state.current_page = "home"
if "current_instance" not in st.session_state:
    st.session_state.current_instance = available_instances[0] if available_instances else None
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
    st.session_state.input_key_counter = 0 # チャット入力欄クリア用
if "pending_attachments" not in st.session_state:
    st.session_state.pending_attachments = []
# デフォルトAPI接続先
DEFAULT_API_URL = "http://127.0.0.1:8000"
if "api_base_url" not in st.session_state:
    st.session_state.api_base_url = DEFAULT_API_URL
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
    import requests as _requests
    # 1. インスタンス固有config
    try:
        resp = _requests.get(f"{api_url}/instances/{instance_name}/config", timeout=5)
        if resp.ok:
            inst_cfg = resp.json()
            inst_model = inst_cfg.get("chat", {}).get("model_name", "")
            if inst_model:
                return inst_model
    except Exception:
        pass
    # 2. グローバルconfig
    try:
        resp = _requests.get(f"{api_url}/config", timeout=5)
        if resp.ok:
            global_cfg = resp.json()
            return global_cfg.get("AI_CONFIG", {}).get("chat", {}).get("model_name", "gemini-3-flash-preview")
    except Exception:
        pass
    return "gemini-3-flash-preview"  # 最終フォールバック

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

    # locale 取得（テンプレート選択に使用）
    import requests as _req_home
    try:
        _home_cfg = _req_home.get(f"{st.session_state.api_base_url}/config", timeout=5).json()
    except Exception:
        _home_cfg = {"SYSTEM_CONFIG": {}}
    _home_locale = _home_cfg.get("SYSTEM_CONFIG", {}).get("agent", {}).get("locale", "ja")

    # 新規インスタンス作成 (FABの代わり)
    with st.expander("➕ 新しいインスタンスを作成"):
        new_proj_name = st.text_input(
            "インスタンス名（半角英数字・_）" if _home_locale != "en" else "Instance Name (alphanumeric & _)",
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
            st.session_state.prev_template_choice = _options[0] if _options else "custom"

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
        st.markdown("**🧠 初期設定**" if _home_locale == "ja" else "**🧠 Initial Setup**")

        ai_name_input = st.text_input(
            "AIの名前" if _home_locale == "ja" else "AI Name",
            placeholder="ジャービス、ルナ、アトラス..." if _home_locale == "ja" else "Jarvis, Luna, Atlas...",
            help="AIの呼び名を決めてください。" if _home_locale == "ja" else "Choose a name for your AI.",
        )

        _col_name, _col_nick = st.columns(2)
        with _col_name:
            user_name_input = st.text_input(
                "あなたの名前" if _home_locale == "ja" else "Your Name",
                placeholder="太郎" if _home_locale == "ja" else "John",
                help="AIがあなたを認識するための名前です。" if _home_locale == "ja"
                     else "The name your AI will use to recognize you.",
            )
        with _col_nick:
            nickname_input = st.text_input(
                "呼ばれたい名前" if _home_locale == "ja" else "Preferred Name",
                placeholder="たろ、マスター、〇〇さん" if _home_locale == "ja" else "Johnny, Boss, Mr. Smith",
                help="AIがあなたを呼ぶときの名前です。空欄なら「あなたの名前」を使います。"
                     if _home_locale == "ja"
                     else "How your AI will address you. Leave blank to use your name.",
            )

        with st.expander("🎁 追加設定（任意）" if _home_locale == "ja" else "🎁 Additional Settings (Optional)"):
            _gender_opts = ["", "男性", "女性", "その他"] if _home_locale == "ja" else ["", "Male", "Female", "Other"]
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
                st.error("インスタンス名を入力してください。" if _home_locale == "ja" else "Please enter an instance name.")
            elif not ai_name_input:
                st.error("AIの名前を入力してください。" if _home_locale == "ja" else "Please enter an AI name.")
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
                        "preferred_call": nickname_input if nickname_input else user_name_input,
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
                        st.error(f"エラー: {res.text}" if _home_locale == "ja" else f"Error: {res.text}")
                except Exception as e:
                    st.error(f"エラー: {e}" if _home_locale == "ja" else f"Error: {e}")

# ==========================================
# ⚙️ 設定画面 (タブ3構成) - Butly Client準拠
# ==========================================
def render_settings_screen():
    col1, col2 = st.columns([1, 8])
    with col1:
        if st.button("＜ 戻る"): navigate_to("home")
    with col2:
        st.markdown('<h1 class="app-title">⚙️ 設定</h1>', unsafe_allow_html=True)

    tab1, tab2, tab3 = st.tabs(["🏠 基本設定", "📝 プロンプト", "🤖 LLMプロバイダー"])

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

        if st.button("🗑️ Clear Cache"):
            st.cache_resource.clear()
            st.success("Cache cleared!")

        st.divider()
        st.subheader("🌐 言語設定")
        import requests as _req_lang
        _lang_api_url = st.session_state.api_base_url
        try:
            _lang_cfg = _req_lang.get(f"{_lang_api_url}/config", timeout=5).json()
        except Exception:
            _lang_cfg = {"SYSTEM_CONFIG": {}}
        _lang_agent_s = _lang_cfg.get("SYSTEM_CONFIG", {}).get("agent", {})
        _locale_options = {"ja": "日本語", "en": "English"}
        _locale_keys = list(_locale_options.keys())
        _current_locale = _lang_agent_s.get("locale", "ja")
        _locale_index = _locale_keys.index(_current_locale) if _current_locale in _locale_keys else 0
        _selected_locale = st.selectbox(
            "Language / 言語",
            options=_locale_keys,
            index=_locale_index,
            format_func=lambda x: _locale_options.get(x, x),
            key="tab1_locale",
        )
        if st.button("💾 言語設定を保存", key="save_locale_tab1"):
            _lang_cfg.setdefault("SYSTEM_CONFIG", {}).setdefault("agent", {})["locale"] = _selected_locale
            try:
                _r = _req_lang.post(f"{_lang_api_url}/config", json=_lang_cfg, timeout=5)
                if _r.ok:
                    st.success("言語設定を保存しました。")
                else:
                    st.error(f"保存エラー: {_r.text}")
            except Exception as _e:
                st.error(f"サーバー接続エラー: {_e}")

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
        st.caption("💡 ローカルLLM (Ollama) のモデルは自動検出されます。表示されない場合は接続テストを確認してください。")

        # API プロバイダーのプリセット（ハードコード）
        _API_PRESETS = {
            "chat": [
                "gemini-3.1-pro-preview",
                "gemini-3-flash-preview",
                "gemini-3.1-flash-lite-preview",
                "gemini-2.5-pro",
                "gemini-2.5-flash",
                "gpt-4o",
                "gpt-4o-mini",
                "o3",
                "o4-mini",
            ],
            "summary": [
                "gemini-3.1-flash-lite-preview",
                "gemini-2.5-flash",
                "gpt-4o-mini",
            ],
            "gatekeeper": [
                "gemini-3.1-flash-lite-preview",
                "gemini-2.5-flash-lite",
                "gpt-4o-mini",
            ],
            "embedding": [
                "models/gemini-embedding-001",
                "text-embedding-3-small",
                "text-embedding-3-large",
            ],
        }

        # Ollama モデル（動的取得）
        _ollama_models_global = _fetch_ollama_models(api_url)

        # 各ロールの候補リスト = APIプリセット + Ollama動的
        def _get_candidates(role: str) -> list:
            base = list(_API_PRESETS.get(role, []))
            base += _ollama_models_global
            return base

        ROLE_LABELS = {
            "chat": "Chat (メイン応答)",
            "summary": "Summary (要約)",
            "gatekeeper": "Gatekeeper (Tier判定)",
            "embedding": "Embedding (ベクトル化)",
        }

        provider_ai_cfg = provider_cfg.get("AI_CONFIG", {})
        model_selections = {}

        for role in ["chat", "summary", "gatekeeper", "embedding"]:
            current_model = provider_ai_cfg.get(role, {}).get("model_name", _API_PRESETS[role][0])
            model_selections[role] = _model_selector(
                label=ROLE_LABELS[role],
                current_value=current_model,
                candidates=_get_candidates(role),
                key_prefix=f"provider_model_{role}",
            )

        st.divider()
        st.subheader("🌡️ Temperature 設定")
        st.caption("バックグラウンドロールの生成パラメータを設定します（Chat の Temperature はインスタンス設定から変更できます）。")
        _TEMP_ROLES = {"summary": "Summary (要約)", "gatekeeper": "Gatekeeper (Tier判定)", "knowledge": "Knowledge (知識蒸留)"}
        _TEMP_DEFAULTS = {"summary": 0.3, "gatekeeper": 0.0, "knowledge": 0.7}
        for _t_role, _t_label in _TEMP_ROLES.items():
            _mc = provider_ai_cfg.get(_t_role, {})
            _cur_temp = float(_mc.get("generation_config", {}).get("temperature", _TEMP_DEFAULTS[_t_role]))
            _new_temp = st.slider(_t_label, 0.0, 2.0, value=_cur_temp, step=0.1, key=f"provider_temp_{_t_role}")
            provider_ai_cfg.setdefault(_t_role, {}).setdefault("generation_config", {})["temperature"] = _new_temp

        if st.button("💾 モデル設定を保存", type="primary", key="save_provider_models"):
            # 空白ガード
            empty_roles = [r for r, m in model_selections.items() if not m or not m.strip()]
            if empty_roles:
                st.error(f"モデル名が未入力のロールがあります: {', '.join(empty_roles)}")
            else:
                # provider_cfg のAI_CONFIGを更新
                for role, model_name in model_selections.items():
                    provider_ai_cfg.setdefault(role, {})["model_name"] = model_name.strip()
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


# ==========================================
# 🧹 Sleeptime画面
# ==========================================
def render_sleeptime_screen():
    import requests
    instance_name = st.session_state.sleeptime_instance or st.session_state.current_instance
    api_url = st.session_state.api_base_url

    col1, col2 = st.columns([1, 8])
    with col1:
        if st.button("＜ 戻る", key="hk_back"): navigate_to("chat")
    with col2:
        st.markdown(f'<h1 class="app-title">🧹 記憶の整理: {instance_name}</h1>', unsafe_allow_html=True)
    st.divider()

    # 実行中かどうかをまずサーバーに確認
    is_running = st.session_state.get("hk_running", False)
    try:
        status_resp = requests.get(f"{api_url}/sleeptime/status/{instance_name}", timeout=5)
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
                r = requests.get(f"{api_url}/sleeptime/status/{instance_name}", timeout=5)
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
            resp = requests.get(f"{api_url}/sleeptime/estimate/{instance_name}", timeout=5)
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
                time_str = f"{int(minutes)}分 {int(secs)}秒" if minutes > 0 else f"約 {int(secs)} 秒"
            else:
                time_str = f"約 {est_seconds} 秒"
            st.metric("予測所要時間", time_str)

        st.divider()

        st.caption("短期記憶のログを整理し、知識カードとして長期記憶データベースに保存します。")
        st.info("💡 実行中はチャットを控えてください。記憶データへの同時アクセスで不整合が起きる可能性があります。")

        if group_count == 0:
            st.success("整理する記憶はありません。すべて最新の状態です。")
        else:
            if st.button("▶ 整理を開始する", type="primary", use_container_width=True):
                try:
                    r = requests.post(f"{api_url}/sleeptime/run", json={"instance_name": instance_name}, timeout=5)
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
        config["brain"] = {"readable_instances": ["self"]}
    if "chat" not in config:
        config["chat"] = {
            "model_name": "gemini-3-flash-preview",
            "generation_config": {"temperature": 1.0, "top_p": 0.95, "top_k": 40, "max_output_tokens": 8192}
        }
    config["chat"].setdefault("generation_config", {"temperature": 1.0, "max_output_tokens": 8192})
    if "memory" not in config:
        config["memory"] = {"max_raw_tokens": 4096, "raw_injection_format": "plaintext", "short_term_limit": 6}
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
    _gemini_models = [
        'gemini-3.1-pro-preview',
        'gemini-3-flash-preview',
        'gemini-3.1-flash-lite-preview',
        'gemini-2.5-pro',
        'gemini-2.5-flash',
        'gemini-2.5-flash-lite',
    ]
    _openai_models = [
        'gpt-4o',
        'gpt-4o-mini',
        'o3',
        'o4-mini',
    ]
    _ollama_models = _fetch_ollama_models(api_url)
    _all_models = _gemini_models + _openai_models + _ollama_models
    _all_embedding_models = [
        'models/gemini-embedding-001',
        'text-embedding-3-small',
        'text-embedding-3-large',
    ] + _ollama_models

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
            pf_ai_name = st.text_input("AIの名前", value=_ap.get("ai_name", ""), key="pf_ai_name")
            pf_ai_gender = st.text_input("AIの性別表現（任意）", value=_ap.get("ai_gender", ""), key="pf_ai_gender",
                                          help="空欄なら SI に出力されません。")
        with _ap_col2:
            _locale_opts = ["ja", "en"]
            _locale_idx = _locale_opts.index(_ap.get("locale", "ja")) if _ap.get("locale", "ja") in _locale_opts else 0
            pf_locale = st.selectbox("ロケール", options=_locale_opts, index=_locale_idx, key="pf_locale")

        st.divider()

        # ---- ユーザープロファイル（ユーザー側）----
        st.subheader("👤 ユーザープロファイル")
        st.caption("ユーザーに関する事実情報。Key Memory 先頭に注入されます。")
        _up = config.get("user_profile", {})
        _up_col1, _up_col2 = st.columns(2)
        with _up_col1:
            pf_user_name = st.text_input("あなたの名前", value=_up.get("user_name", ""), key="pf_user_name")
            pf_preferred_call = st.text_input("呼ばれたい名前", value=_up.get("preferred_call", ""), key="pf_preferred_call",
                                               help="空欄なら「あなたの名前」を使います。")
            pf_location = st.text_input("居住地（任意）", value=_up.get("location", ""), key="pf_location")
        with _up_col2:
            _gender_opts_pf = ["", "男性", "女性", "その他"]
            _current_gender = _up.get("gender", "")
            _gender_idx = _gender_opts_pf.index(_current_gender) if _current_gender in _gender_opts_pf else 0
            pf_gender = st.selectbox("性別", options=_gender_opts_pf, index=_gender_idx, key="pf_gender")
            _current_bd = _up.get("birthday", "")
            try:
                from datetime import date as _date
                _bd_val = _date.fromisoformat(_current_bd.replace("/", "-")) if _current_bd else None
            except Exception:
                _bd_val = None
            pf_birthday = st.date_input("生年月日", value=_bd_val, key="pf_birthday", format="YYYY/MM/DD")

        st.divider()

        # State elements
        sys_inst = st.text_area("System Instruction (性格設定)", value=prompts.get("system_instruction", ""), height=150)
        key_mem = st.text_area("Key Memory (根幹記憶 — プロファイル以外の内容)", value=prompts.get("key_memory", ""), height=150,
                                help="AIの名前・ユーザー名などのプロファイル情報は上の「エージェントプロファイル」セクションで管理されます。")

        st.divider()
        st.subheader("🤖 生成モデル設定")
        st.caption("各ロールのモデルを個別に設定できます。「グローバル設定を使う」をONにすると user_config.json / config.py のデフォルト値が使われます。")

        from butly_core.config import AI_CONFIG as _global_ai

        # ---- Chat モデル（メイン応答） ----
        with st.expander("💬 Chat（メイン応答）", expanded=True):
            _chat_gen = config["chat"].get("generation_config", {})
            model_name = _model_selector("モデル名", config["chat"].get("model_name", "gemini-3-flash-preview"), _all_models, "inst_chat_model")
            temp = st.slider("Temperature", min_value=0.0, max_value=2.0, step=0.1, value=float(_chat_gen.get("temperature", 1.0)), key="chat_temp")
            max_tokens = st.number_input("最大出力トークン数", min_value=1, value=int(_chat_gen.get("max_output_tokens", 8192)), key="chat_max_tokens")

        # ---- Gatekeeper モデル（Tier分類） ----
        with st.expander("🛡 Gatekeeper（Tier分類）"):
            if "gatekeeper" not in config:
                config["gatekeeper"] = {}
            _gk_use_global = st.toggle("グローバル設定を使う", value=("model_name" not in config.get("gatekeeper", {})), key="gk_global")
            gk_enabled = st.toggle(
                "Gatekeeper (Tier自動判定)",
                value=config.get("gatekeeper", {}).get("enabled", True),
                help="OFF にすると常に mid tier で動作します（RAG 検索は行われません）",
            )
            if _gk_use_global:
                _gk_default = _global_ai.get("gatekeeper", {})
                st.info(f"グローバル設定: {_gk_default.get('model_name', '未設定')}")
                gk_model_name = None
                gk_temp = None
            else:
                _gk_cur = config.get("gatekeeper", {})
                gk_model_name = _model_selector("モデル名", _gk_cur.get("model_name", _global_ai.get("gatekeeper", {}).get("model_name", "")), _all_models, "inst_gk_model")
                gk_temp = st.slider("Temperature", min_value=0.0, max_value=2.0, step=0.1, value=float(_gk_cur.get("generation_config", {}).get("temperature", 0.0)), key="gk_temp")

        # ---- Summary モデル（要約） ----
        with st.expander("📝 Summary（要約）"):
            _sum_use_global = st.toggle("グローバル設定を使う", value=("summary" not in config), key="sum_global")
            if _sum_use_global:
                _sum_default = _global_ai.get("summary", {})
                st.info(f"グローバル設定: {_sum_default.get('model_name', '未設定')}")
                sum_model_name = None
                sum_temp = None
            else:
                _sum_cur = config.get("summary", {})
                sum_model_name = _model_selector("モデル名", _sum_cur.get("model_name", _global_ai.get("summary", {}).get("model_name", "")), _all_models, "inst_sum_model")
                sum_temp = st.slider("Temperature", min_value=0.0, max_value=2.0, step=0.1, value=float(_sum_cur.get("generation_config", {}).get("temperature", 0.3)), key="sum_temp")

        # ---- Knowledge モデル（知識抽出） ----
        with st.expander("🧠 Knowledge（知識抽出）"):
            _know_use_global = st.toggle("グローバル設定を使う", value=("knowledge" not in config), key="know_global")
            if _know_use_global:
                _know_default = _global_ai.get("knowledge", {})
                st.info(f"グローバル設定: {_know_default.get('model_name', '未設定')}")
                know_model_name = None
                know_temp = None
            else:
                _know_cur = config.get("knowledge", {})
                know_model_name = _model_selector("モデル名", _know_cur.get("model_name", _global_ai.get("knowledge", {}).get("model_name", "")), _all_models, "inst_know_model")
                know_temp = st.slider("Temperature", min_value=0.0, max_value=2.0, step=0.1, value=float(_know_cur.get("generation_config", {}).get("temperature", 0.7)), key="know_temp")

        # ---- Embedding モデル（ベクトル検索） ----
        with st.expander("🔢 Embedding（ベクトル検索）"):
            st.caption("Ollama で embedding を使う場合は専用モデル (nomic-embed-text 等) が必要です。\n`ollama pull nomic-embed-text` を実行し `ollama/nomic-embed-text` を選択してください。")
            _emb_use_global = st.toggle("グローバル設定を使う", value=("embedding" not in config), key="emb_global")
            if _emb_use_global:
                _emb_default = _global_ai.get("embedding", {})
                st.info(f"グローバル設定: {_emb_default.get('model_name', '未設定')}")
                emb_model_name = None
            else:
                _emb_cur = config.get("embedding", {})
                emb_model_name = _model_selector("モデル名", _emb_cur.get("model_name", _global_ai.get("embedding", {}).get("model_name", "")), _all_embedding_models, "inst_emb_model")

        st.divider()
        st.subheader("📚 記憶の参照範囲")
        st.caption("このインスタンスがRAG検索時にアクセスできる記憶DBを選択します。")
        current_readable = config["brain"].get("readable_instances", ["self"])
        readable_selected = []
        for inst in available_instances:
            is_self = (inst == instance_name)
            is_checked = is_self or inst in current_readable
            disabled = is_self
            if st.checkbox(
                f"{'📌 ' if is_self else ''}{inst}",
                value=is_checked,
                disabled=disabled,
                key=f"readable_{inst}"
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
            save_basic = st.button("設定を保存", type="primary", use_container_width=True, key="save_basic")
        with col_a3:
            with st.popover("🗑️ インスタンスを完全に削除"):
                st.warning("この操作は取り消せません。インスタンスのフォルダ、設定、短期記憶・長期記憶がすべて完全に削除されます。")
                if st.button("完全に削除する", type="primary", key="delete_basic"):
                    try:
                        del_resp = requests.delete(f"{api_url}/instances/{instance_name}", timeout=5)
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
        _is_gemini_inst = is_gemini_provider(model_name)
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
            search_lim = st.number_input("検索リミット", min_value=1, max_value=10, value=config["brain"].get("search_limit", 3))
            fallback_lim = st.slider("フォールバック取得数", min_value=10, max_value=100, step=10, value=config["brain"].get("fallback_fetch_limit", 50))
            st_limit = st.number_input("短期記憶 保存数", min_value=1, max_value=12, step=1, value=config["memory"].get("short_term_limit", 6), help="保持するセッション数")
        with col_r2:
            keyword_thr = st.number_input("記憶検索の感度", min_value=1, max_value=10, value=config["brain"].get("keyword_hit_threshold", 5), help="値が大きいほど直近の会話記憶が検索補完に加わりやすくなります。")
            use_summarized = st.toggle(
                "中期記憶に要約版を使用",
                value=config["memory"].get("use_summarized_mid_term", True),
                help="ON: digest + relationship（トークン効率◎）/ OFF: RAWキャッシュ（詳細だがトークン消費大）",
            )
            raw_tokens = st.slider("RAW記憶 トークン上限", min_value=1024, max_value=16384, step=1024, value=config["memory"].get("max_raw_tokens", 4096), disabled=use_summarized)
            raw_format = st.selectbox("RAW注入形式", ["plaintext", "markdown", "compact"], index=["plaintext", "markdown", "compact"].index(config["memory"].get("raw_injection_format", "plaintext")), disabled=use_summarized)
            if st.button("🔄 RAWキャッシュ再生成", help="現在のスライダー/セレクトの値で即座に再生成します", disabled=use_summarized):
                try:
                    resp = requests.post(
                        f"{st.session_state.api_base_url}/instances/{instance_name}/rebuild_raw_cache",
                        json={"max_tokens": raw_tokens, "injection_format": raw_format},
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        st.success(f"再生成完了: {data.get('sessions', 0)}セッション, {data.get('tokens', 0)}トークン ({data.get('format', 'plaintext')})")
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
                min_value=500, max_value=20000, step=500,
                value=_hk_conf.get("max_digest_chars", 3000),
                help="中期記憶の要約版（日次ダイジェスト）の最大文字数",
                key="hk_max_digest",
            )
            hk_max_relationship = st.number_input(
                "Recent Snapshot 最大文字数",
                min_value=500, max_value=20000, step=500,
                value=_hk_conf.get("max_relationship_chars", 5000),
                help="近況スナップショットの最大文字数",
                key="hk_max_relationship",
            )
            hk_relationship_interval = st.number_input(
                "Recent Snapshot 更新間隔 (日)",
                min_value=1, max_value=30, step=1,
                value=_hk_conf.get("relationship_update_interval_days", 7),
                help="近況スナップショットを更新する間隔",
                key="hk_rel_interval",
            )
            hk_digest_max_input = st.number_input(
                "Digest 1回あたり最大入力文字数",
                min_value=0, max_value=100000, step=1000,
                value=_hk_conf.get("digest_max_input_chars", 0),
                help="Stage 1 (Digest) の1回あたりの最大入力文字数。0 = 無制限。",
                key="hk_digest_max_input",
            )
        with hk_col2:
            hk_summary_tokens = st.number_input(
                "Summary 最大出力トークン数",
                min_value=256, max_value=16384, step=256,
                value=_hk_conf.get("summary_max_output_tokens", 4096),
                help="Digest / Headlines 生成時の最大出力トークン (classify API)",
                key="hk_summary_tokens",
            )
            hk_knowledge_tokens = st.number_input(
                "Knowledge 最大出力トークン数",
                min_value=256, max_value=16384, step=256,
                value=_hk_conf.get("knowledge_max_output_tokens", 8192),
                help="ナレッジ抽出 / Recent Snapshot 生成時の最大出力トークン (classify API)",
                key="hk_knowledge_tokens",
            )
            hk_knowledge_max_input = st.number_input(
                "Knowledge 1回あたり最大入力文字数",
                min_value=0, max_value=100000, step=1000,
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

        from butly_core.core.gatekeeper.memory_builder import DEFAULT_CONTEXT_ORDER, CONTEXT_LEVEL_PRESETS

        # config から読み込み（context_levels 優先、なければ context_order から変換）
        ctx_cfg = config.get("context_levels", {})
        if not ctx_cfg and "context_order" in config:
            from butly_core.core.gatekeeper.memory_builder import migrate_context_order_to_levels
            config = migrate_context_order_to_levels(config)
            ctx_cfg = config.get("context_levels", {})

        current_preset = ctx_cfg.get("preset", "normal")
        _si_order = list(ctx_cfg.get("order", {}).get("system_instruction", DEFAULT_CONTEXT_ORDER["system_instruction"]))
        _cp_order = list(ctx_cfg.get("order", {}).get("context_prefix", DEFAULT_CONTEXT_ORDER["context_prefix"]))
        _si_position = ctx_cfg.get("system_instruction_position", DEFAULT_CONTEXT_ORDER.get("system_instruction_position", "top"))

        # セクションの日本語ラベル
        _SECTION_LABELS = {
            "system_instruction": "性格設定 (system_instruction)",
            "key_memory": "根幹記憶 (key_memory)",
            "label_notes": "ラベル・注意文 (label_notes)",
            "current_time": "現在時刻 (current_time)",
            "glossary": "共通言語辞書 (glossary)",
            "mid_term": "中期記憶 (mid_term)",
            "rag": "長期記憶 RAG (rag)",
            "floating": "浮動要約 (floating)",
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
            index=["normal", "compact", "low", "custom"].index(current_preset) if current_preset in ["normal", "compact", "low", "custom"] else 0,
            format_func=lambda x: _preset_labels[x],
            key="ctx_preset",
        )

        if preset == "low":
            st.warning("💡 LOWプリセットではGatekeeper OFFを推奨します。")

        if preset != "custom":
            preset_levels = CONTEXT_LEVEL_PRESETS[preset]
        else:
            preset_levels = ctx_cfg.get("levels", CONTEXT_LEVEL_PRESETS["normal"])

        # --- 個別レベル設定 ---
        _LEVEL_OPTIONS = ["high", "mid", "low", "off"]
        with st.expander("詳細設定（各要素のレベル）", expanded=(preset == "custom")):
            level_settings = {}
            for section_id, label in _SECTION_LABELS.items():
                current = preset_levels.get(section_id, "high")
                level_settings[section_id] = st.selectbox(
                    label,
                    options=_LEVEL_OPTIONS,
                    index=_LEVEL_OPTIONS.index(current) if current in _LEVEL_OPTIONS else 0,
                    key=f"ctx_level_{section_id}",
                    disabled=(preset != "custom"),
                )

        # --- system_instruction_position ---
        st.markdown("**▎ System Instruction の配置**")
        _pos_options = {"top": "先頭 (top) — 標準・Gemini推奨", "bottom": "末尾 (bottom) — SillyTavern方式・OpenAI/Ollama向け"}
        _si_position = st.radio(
            "配置位置",
            options=list(_pos_options.keys()),
            index=0 if _si_position == "top" else 1,
            format_func=lambda x: _pos_options[x],
            key="ctx_si_position",
            horizontal=True,
        )
        st.caption("ℹ️ Gemini API では system_instruction は常に独立パラメータとして渡されるため、この設定は無視されます。")

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
                st.session_state.ctx_si_order = list(DEFAULT_CONTEXT_ORDER["system_instruction"])
                st.session_state.ctx_cp_order = list(DEFAULT_CONTEXT_ORDER["context_prefix"])
                st.rerun()

        st.divider()

        # ==========================================
        # 📖 Glossary (共通言語辞書) 管理
        # ==========================================
        st.subheader("📖 Glossary（共通言語辞書）")
        st.caption("GatekeeperとBrainに注入される意味記憶です。未知語のパニックを防ぎます。")

        glossary_data = {"version": 1, "entries": []}
        try:
            gl_resp = requests.get(f"{api_url}/instances/{instance_name}/glossary", timeout=5)
            if gl_resp.ok:
                glossary_data = gl_resp.json()
        except Exception:
            pass

        entries = glossary_data.get("entries", [])

        if "glossary_entries" not in st.session_state or st.session_state.get("glossary_instance") != instance_name:
            st.session_state.glossary_entries = [dict(e) for e in entries]
            st.session_state.glossary_instance = instance_name

        gl_entries = st.session_state.glossary_entries

        gl_filter_col1, gl_filter_col2 = st.columns([2, 2])
        with gl_filter_col1:
            gl_status_filter = st.selectbox("ステータス", ["all", "active", "pending", "archived"], key="gl_status_filter")
        with gl_filter_col2:
            gl_search = st.text_input("🔍 用語検索", key="gl_search", placeholder="用語名で絞り込み")

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
            with st.container(border=True):
                gc1, gc2, gc3 = st.columns([5, 2, 1])
                with gc1:
                    st.markdown(f"**{entry.get('term', '')}**")
                    st.caption(entry.get("definition", ""))
                    aliases = entry.get("aliases", [])
                    if aliases:
                        st.caption(f"別名: {', '.join(aliases)}")
                with gc2:
                    new_status = st.selectbox(
                        "status",
                        ["active", "pending", "archived"],
                        index=["active", "pending", "archived"].index(entry.get("status", "active")),
                        key=f"gl_status_{idx}",
                        label_visibility="collapsed",
                    )
                    if new_status != entry.get("status"):
                        gl_entries[idx]["status"] = new_status
                with gc3:
                    if st.button("🗑️", key=f"gl_del_{idx}"):
                        gl_entries.pop(idx)
                        st.rerun()

        with st.expander("➕ 新しい用語を追加"):
            new_term = st.text_input("用語名", key="gl_new_term")
            new_def = st.text_input("定義", key="gl_new_def")
            new_aliases = st.text_input("別名（カンマ区切り）", key="gl_new_aliases")
            new_cat = st.selectbox("カテゴリ", ["system", "hardware", "project", "tool", "world", "character", "other"], key="gl_new_cat")
            if st.button("追加", key="gl_add_entry"):
                if new_term and new_def:
                    alias_list = [a.strip() for a in new_aliases.split(",") if a.strip()] if new_aliases else []
                    gl_entries.append({
                        "term": new_term,
                        "definition": new_def,
                        "aliases": alias_list,
                        "category": new_cat,
                        "status": "active",
                    })
                    st.rerun()
                else:
                    st.warning("用語名と定義は必須です。")

        if st.button("💾 Glossary を保存", key="gl_save", use_container_width=True):
            save_data = {"version": glossary_data.get("version", 1), "entries": gl_entries}
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
        if st.button("🧹 記憶の整理 (Sleeptime)", use_container_width=True):
            st.session_state.sleeptime_instance = instance_name
            navigate_to("sleeptime")
        st.caption("短期記憶を整理し、知識カードとして長期記憶に保存します。")

        st.divider()

        # Rename Instance
        st.subheader("✏️ インスタンス名の変更")
        rename_col1, rename_col2 = st.columns([3, 1])
        with rename_col1:
            new_inst_name = st.text_input("新しいインスタンス名（半角英数字・_）", placeholder=instance_name, key="rename_input")
        with rename_col2:
            st.write("")
            st.write("")
            if st.button("変更", key="btn_rename", use_container_width=True):
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
            save_advanced = st.button("設定を保存", type="primary", use_container_width=True, key="save_advanced")

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
        config["chat"]["model_name"] = model_name
        config["chat"].setdefault("generation_config", {})
        config["chat"]["generation_config"]["temperature"] = temp
        config["chat"]["generation_config"]["max_output_tokens"] = max_tokens

        # --- Gatekeeper ---
        _gk_save = {"enabled": gk_enabled}
        if gk_model_name is not None:  # "グローバル設定を使う" がOFF
            _gk_save["model_name"] = gk_model_name
            _gk_save["generation_config"] = {"temperature": gk_temp, "max_output_tokens": 512}
        config["gatekeeper"] = _gk_save

        # --- Summary ---
        if sum_model_name is not None:
            config["summary"] = {
                "model_name": sum_model_name,
                "generation_config": {"temperature": sum_temp},
            }
        else:
            config.pop("summary", None)

        # --- Knowledge ---
        if know_model_name is not None:
            config["knowledge"] = {
                "model_name": know_model_name,
                "generation_config": {"temperature": know_temp},
            }
        else:
            config.pop("knowledge", None)

        # --- Embedding ---
        if emb_model_name is not None:
            config["embedding"] = {"model_name": emb_model_name}
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
            "levels": level_settings if preset == "custom" else CONTEXT_LEVEL_PRESETS[preset],
            "order": {
                "system_instruction": list(st.session_state.ctx_si_order),
                "context_prefix": list(st.session_state.ctx_cp_order),
            },
            "system_instruction_position": _si_position,
        }
        config.pop("context_order", None)  # 旧キー削除

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

def render_chat_screen():
    instance_name = st.session_state.current_instance
    # 履歴表示用に memory のみ初期化（brain は FastAPI 側で管理）
    memory, brain, chronos = initialize_system(BASE_DIR, instance_name)
    api_url = st.session_state.api_base_url

    # --- チャットヘッダー ---
    col1, col2, col3, col4, col5, col6 = st.columns([1, 5, 1, 1, 1, 1])
    with col1:
        if st.button("＜", help="戻る"): navigate_to("home")
    with col2:
        st.markdown(f'<h1 class="app-title">{instance_name}</h1>', unsafe_allow_html=True)
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
    with col6:
        # Google Search toggle / Web Search toggle
        # プロバイダーに応じて使い分ける
        active_model = get_active_chat_model(api_url, instance_name)
        is_gemini = is_gemini_provider(active_model)

        gs_on = st.session_state.get("use_google_search", False)

        if is_gemini:
            # Gemini: 通常通りトグル可能（Native Grounding）
            gs_label = "🌐 ON" if gs_on else "🌐"
            gs_help = "Google検索: ON（クリックでOFF）" if gs_on else "Google検索: OFF（クリックでON）"
            if st.button(gs_label, help=gs_help):
                st.session_state.use_google_search = not gs_on
                st.rerun()
        else:
            # 非Gemini: Google検索を強制OFF + 汎用Web検索トグル
            if gs_on:
                st.session_state.use_google_search = False

            import os
            tavily_available = bool(os.environ.get("TAVILY_API_KEY", ""))
            ws_on = st.session_state.get("use_web_search", False)

            if tavily_available:
                ws_label = "🔍 ON" if ws_on else "🔍"
                ws_help = "Web検索: ON（クリックでOFF）" if ws_on else "Web検索: OFF（クリックでON）"
                if st.button(ws_label, help=ws_help):
                    st.session_state.use_web_search = not ws_on
                    st.rerun()
            else:
                st.button(
                    "🔍",
                    help="Web検索を使用するには TAVILY_API_KEY を設定してください",
                    disabled=True,
                )

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
            except: pass
            
        if role == "user":
            # 添付画像のサムネイルHTML
            img_html = ""
            for att in msg.get("attachments", []):
                img_html += f'<img class="attachment-thumb" src="data:{att["mime_type"]};base64,{att["data_base64"]}" alt="{att.get("name", "image")}" />'
            st.markdown(f'<div class="chat-bubble-user">{ts_display}{text}{img_html}</div>', unsafe_allow_html=True)
        else:
            st.markdown(f'<div class="chat-bubble-ai">{ts_display}{text}</div>', unsafe_allow_html=True)

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

                        st.caption("⏱️ Timing")
                        cols = st.columns(4)
                        cols[0].metric("Gatekeeper", f"{timing.get('gatekeeper_ms', 0)}ms")
                        cols[1].metric("Memory Build", f"{timing.get('memory_build_ms', 0)}ms")
                        cols[2].metric("RAG Search", f"{timing.get('rag_search_ms', 0)}ms")
                        cols[3].metric("Generation", f"{timing.get('generation_ms', 0)}ms")

                        st.caption("🧠 Gatekeeper")
                        if not gk.get("enabled", True):
                            st.text("  (disabled)")
                        else:
                            scores = gk.get("scores", {})
                            if scores:
                                for key in ["response_complexity", "emotional_weight",
                                            "memory_reference_likelihood", "continuity_need"]:
                                    val = scores.get(key, 0.0)
                                    filled = int(val * 10)
                                    bar = "█" * filled + "░" * (10 - filled)
                                    st.text(f"  {key:>30s}: {val:.2f} {bar}")
                            if gk.get("need"):
                                st.text(f"  Need: {gk['need']}")
                            if gk.get("search_targets"):
                                st.text(f"  Search Targets: {gk['search_targets']}")

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
                            with st.expander("📝 Full Prompt (untruncated)", expanded=False):
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

    st.markdown('</div>', unsafe_allow_html=True)

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
            new_attachments.append({
                "kind": "image",
                "mime_type": uf.type,
                "data_base64": base64.b64encode(raw).decode(),
                "name": uf.name,
                "size": len(raw),
            })
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

                use_ws = st.session_state.get("use_web_search", False) if not is_gemini else False

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
                    "timestamp": datetime.now().isoformat(),
                    "debug_info": data.get("debug_info"),
                    "sources": sources,
                })
                st.session_state.last_interaction_time = datetime.now()

                # 記憶の保存・整理は main.py 内の /chat で完結しているためここでは不要

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
# 🎉 オンボーディング画面 (Onboarding Screen)
# ==========================================
def render_onboarding_screen():
    st.markdown('<h1 class="app-title">Butly へようこそ！</h1>', unsafe_allow_html=True)
    st.write("まずは最初のAIインスタンスを作成しましょう。")
    st.divider()

    new_proj_name = st.text_input("インスタンス名（半角英数字・_）", placeholder="e.g. my_agent")
    from butly_core import prompts
    new_template = st.text_area("性格テンプレート", value=prompts.WEB_UI_DEFAULT_TEMPLATE.format(agent_name="{agent_name}"), height=100)

    if st.button("作成", type="primary"):
        if new_proj_name:
            import requests
            api_url = st.session_state.api_base_url
            try:
                res = requests.post(f"{api_url}/instances", json={"name": new_proj_name, "template": new_template}, timeout=5)
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
    available_instances = sorted([p.name for p in INSTANCES_DIR.iterdir() if p.is_dir() and not p.name.startswith(".")]) if INSTANCES_DIR.exists() else []

    if not available_instances:
        # インスタンス未作成でもホーム画面を表示（新規作成UIがホーム画面にある）
        render_home_screen()
        return

    if st.session_state.current_instance is None:
        st.session_state.current_instance = available_instances[0]

    if st.session_state.current_page == "home":
        render_home_screen()
    elif st.session_state.current_page == "chat":
        render_chat_screen()
    elif st.session_state.current_page == "settings":
        render_settings_screen()
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
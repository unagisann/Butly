# Butly — アーキテクチャ図集

🌐 **日本語** | [English](DIAGRAMS.en.md)

このファイルには Butly のシステム設計を可視化した Mermaid 図をまとめています。

---

## 1. 全体アーキテクチャ（メインフロー）

ユーザー発言から返答生成・記憶保存までの処理フローです。

```mermaid
flowchart TD
    A((ユーザー発言)) --> B["⧫ Gatekeeper<br/>Provider.classify()"]
    B --> C["構造化出力<br/>tier / need / search_targets / state_delta"]
    C -.->|state_delta| D["◈ Session State<br/>topic / mood / goals / unresolved"]
    D -.->|参照| B
    C --> E{tier}
    E -->|reflex| F["⚡ reflex<br/>最小コンテキスト"]
    E -->|mid| G["◎ mid<br/>注入記憶あり"]
    E -->|cortex| H["⌕ 不足前提検索<br/>need-driven retrieval"]
    H --> I[("⛁ 統合記憶DB<br/>episode / reflection<br/>generalization / self_model")]
    F --> J["◆ ChatService<br/>Provider.generate()"]
    G --> J
    I -->|検索結果| J
    J --> K((返答))
    K --> L["▣ short_term_json 保存"]
    L -.->|定期処理| M["⚙ Housekeeper<br/>日次 + 週次バッチ"]
    M -.->|統合記憶生成| I
```

---

## 2. system_instruction 注入順序

LLM に渡すコンテキストブロックの構築順序（上部が不変、下部が可変）です。

```mermaid
block-beta
    columns 1
    A["1. SYSTEM INSTRUCTION — 性格設定（不変）"]
    B["2. KEY MEMORY — 根幹記憶（不変）"]
    C["3. MID-TERM — 中期記憶 digest + relationship（低頻度更新）"]
    D["4. CURRENT TIME — 現在時刻"]
    E["5. RAG — 長期記憶検索結果（※参考情報注釈付き）"]
    F["6. FLOATING — 直近の会話要約（※直近文脈注釈付き）"]
    G["7. TIER INFO — 思考モード"]

    style A fill:#1a1a2e,color:#ec4899
    style B fill:#1a1a2e,color:#f59e0b
    style C fill:#1a1a2e,color:#10b981
    style D fill:#1a1a2e,color:#8899aa
    style E fill:#1a1a2e,color:#ef4444
    style F fill:#1a1a2e,color:#3b82f6
    style G fill:#1a1a2e,color:#556677
```

---

## 3. Mid-term 二層要約構造

short_term_json（RAW）からの記憶パイプラインです。

```mermaid
flowchart LR
    RAW["short_term_json<br/>(RAW)"] --> INT["1_integrated<br/>(RAW保管)"]
    INT --> S1a["Stage 1a<br/>mid_term.txt<br/>RAW蓄積"]
    INT --> S1b["Stage 1b ★<br/>mid_term_digest.txt<br/>エピソード付き差分追記"]
    S1b --> S1c["Stage 1c ★<br/>mid_term_relationship.txt<br/>週次上書き"]
    INT --> S2["Stage 2<br/>knowledge_cards<br/>episode生成"]
    INT --> KN["2_knowledgeized<br/>(RAW永久保管)"]

    style S1b fill:#065f46,color:#10b981
    style S1c fill:#4c1d95,color:#8b5cf6
    style S1a fill:#1f2937,color:#8899aa
    style S2 fill:#065f46,color:#10b981
```

---

## 4. Housekeeper ステージ構成

日次・週次バッチ処理の構成です。

```mermaid
flowchart TD
    subgraph "日次バッチ"
        S1a["Stage 1a<br/>Mid-term RAW蓄積"]
        S1b["Stage 1b ★<br/>エピソード付きDigest生成<br/><i>当日RAW → digest追記</i>"]
        S2["Stage 2<br/>ナレッジカード生成<br/><i>RAW → episode cards</i>"]
    end
    subgraph "週次バッチ"
        S1c["Stage 1c ★<br/>関係性Snapshot更新<br/><i>digest → relationship上書き</i>"]
        S3["Stage 3 ※未実装<br/>統合記憶生成<br/><i>episodes → reflection等</i>"]
    end

    S1a --> S1b --> S1c
    S1a --> S2
    S2 -.-> S3

    style S1b fill:#065f46,color:#10b981
    style S1c fill:#4c1d95,color:#8b5cf6
    style S3 fill:#1f2937,color:#556677,stroke-dasharray: 5 5
```

---

## 5. マルチプロバイダー構成

LLM プロバイダー抽象化レイヤーの構成です。

```mermaid
flowchart TD
    CS["ChatService<br/>(オーケストレーション)"]
    GK["Gatekeeper<br/>(tier 判定)"]
    HK["Housekeeper<br/>(記憶整理)"]
    PF["ProviderFactory<br/>(model_name → プロバイダー自動ルーティング)"]
    GE["GeminiProvider<br/>gemini-*"]
    OA["OpenAIProvider<br/>gpt-* / o1 / o3 / o4"]
    OL["OllamaProvider<br/>ollama/*"]

    CS --> PF
    GK --> PF
    HK --> PF
    PF --> GE
    PF --> OA
    PF --> OL
```

---

## 6. インスタンス別ディレクトリ構成

各 AI インスタンスの記憶ファイル配置です。

```mermaid
flowchart TD
    ROOT["butly_core/instances/"]
    I1["instance_name/"]
    CFG["config.json"]
    SI["system_instruction.txt"]
    KM["Key_Memory.txt"]
    MT["mid_term.txt"]
    MD["mid_term_digest.txt"]
    MR["mid_term_relationship.txt"]
    SS["session_state.json"]
    DB["butly_memory.db"]
    ST["short_term_json/"]
    FS["floating_summaries/"]
    AR["memory_archive/"]
    A1["1_integrated/"]
    A2["2_knowledgeized/"]
    A3["3_log/"]

    ROOT --> I1
    I1 --> CFG
    I1 --> SI
    I1 --> KM
    I1 --> MT
    I1 --> MD
    I1 --> MR
    I1 --> SS
    I1 --> DB
    I1 --> ST
    I1 --> FS
    I1 --> AR
    AR --> A1
    AR --> A2
    AR --> A3
```

---

## 7. 実装ロードマップ

```mermaid
timeline
    title Butly Memory Architecture v2 — 実装ロードマップ
    Phase 1 ✅ : Gatekeeper v2
                : Gemini API移行
                : 構造化JSON出力
                : SessionState導入
    Phase 2 ✅ : 呼び出し側統合
                : classify()切替
                : SessionState実稼働
                : 記憶注入順序最適化
    Phase 3 ✅ : 二層要約パイプライン
                : エピソード付きDigest（日次）
                : 関係性Snapshot（週次）
                : sys_inst+key_memory参照
    Phase 4 ✅ : 要約注入切替
             : build_system_instruction改修
             : RAW→要約の切替スイッチ
             : 品質検証
    Multi-Provider ✅ : マルチプロバイダー対応
             : OpenAI / Ollama 追加
             : google.genai 隔離
             : 埋め込みマイグレーション
    Phase 5 : 統合記憶生成
             : Housekeeper Stage3
             : reflection / generalization
             : self_model蓄積開始
    Phase 6 : GK脳科学チューニング
             : 意味記憶 vs エピソード記憶
             : tier判定精度向上
    最終形 : 完全自律
            : system_instruction 1行化
            : 人格の記憶からの自律再構成
```

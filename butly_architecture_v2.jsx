import { useState } from "react";

const COLORS = {
  bg: "#0a0e17",
  surface: "#111827",
  surfaceLight: "#1a2234",
  border: "#2a3548",
  borderActive: "#3b82f6",
  text: "#e2e8f0",
  textMuted: "#8899aa",
  textDim: "#556677",
  accent1: "#3b82f6",
  accent2: "#f59e0b",
  accent3: "#10b981",
  accent4: "#8b5cf6",
  accent5: "#ef4444",
  accent6: "#06b6d4",
  accent7: "#ec4899",
};

const FONTS = {
  mono: "'JetBrains Mono', 'Fira Code', 'SF Mono', monospace",
  sans: "'DM Sans', 'Noto Sans JP', system-ui, sans-serif",
};

const PHASES = [
  { id: "input", label: "ユーザー発言", icon: "◉", color: COLORS.text, x: 50, y: 3, desc: "テキスト入力。全フローの起点。" },
  { id: "gatekeeper", label: "Gatekeeper", sublabel: "Gemini 2.5 Flash-Lite", icon: "⧫", color: COLORS.accent1, x: 50, y: 11, isKey: true,
    desc: "メタ認知エンジン。session_stateを参照しつつ、tier判定・不足前提分析・状態差分を1回のAPI呼び出しで一括処理する。" },
  { id: "output_json", label: "構造化出力", sublabel: "tier / need / search_targets / state_delta", icon: "{ }", color: COLORS.accent1, x: 50, y: 20,
    desc: "Gatekeeperの出力JSON。tierに加え、need（不足前提）、search_targets（検索指示）、state_delta（状態差分）を含む。" },
  { id: "state", label: "Session State", sublabel: "topic / mood / goals / unresolved", icon: "◈", color: COLORS.accent2, x: 13, y: 28, isKey: true,
    desc: "セッション全体の内部状態。state_deltaで差分更新。JSONファイル永続化。Gatekeeperの入力にも使われ文脈の連続性を担保。" },
  { id: "tier_switch", label: "tier", icon: "◇", color: COLORS.textMuted, x: 50, y: 30, desc: "reflex / mid / cortex の3段階分岐。" },
  { id: "reflex", label: "reflex", sublabel: "最小コンテキスト", icon: "⚡", color: "#4ade80", x: 20, y: 42,
    desc: "挨拶・相槌。identity_core + short_term + floatingのみ。最小トークンで即応答。" },
  { id: "mid", label: "mid", sublabel: "注入記憶あり", icon: "◎", color: COLORS.accent1, x: 50, y: 42,
    desc: "日常会話。identity_core + short_term + floating + mid_term（二層要約）。RAG検索なし。" },
  { id: "cortex_search", label: "不足前提検索", sublabel: "need-driven retrieval", icon: "⌕", color: COLORS.accent4, x: 82, y: 37, isKey: true,
    desc: "Gatekeeperのsearch_targetsに基づくターゲット検索 + 類似検索。動的配分（合計5枠）。" },
  { id: "memory_db", label: "統合記憶DB", sublabel: "episode / reflection / generalization / self_model", icon: "⛁", color: COLORS.accent3, x: 82, y: 48, isKey: true,
    desc: "4層の記憶。episodeに加え、reflection（反省）、generalization（一般化）、self_model（自己理解）を持つ。self_modelの成熟がsystem_instruction最小化の鍵。" },
  { id: "brain", label: "Butly Brain", sublabel: "Gemini 3.1 Flash", icon: "◆", color: COLORS.accent6, x: 50, y: 57,
    desc: "メインLLM。Gatekeeperが構築した記憶ブロック + session_stateを受け取り最終応答を生成。" },
  { id: "response", label: "返答", icon: "○", color: COLORS.text, x: 50, y: 68, desc: "応答テキスト。" },
  { id: "save", label: "short_term_json 保存", icon: "▣", color: COLORS.textMuted, x: 50, y: 76,
    desc: "RAW会話ログをJSON即時保存。Housekeeper処理の入力。" },
  { id: "housekeeper", label: "Housekeeper", sublabel: "日次 + 週次バッチ", icon: "⚙", color: COLORS.accent5, x: 13, y: 70, isKey: true,
    desc: "日次: Stage1（mid_term二層要約生成）+ Stage2（ナレッジカード生成）。週次: Stage3（統合記憶 — reflection/generalization/self_model）。RAWからのみ生成。" },
];

const EDGES = [
  { from: "input", to: "gatekeeper" },
  { from: "gatekeeper", to: "output_json" },
  { from: "output_json", to: "state", label: "state_delta", dashed: true },
  { from: "output_json", to: "tier_switch" },
  { from: "state", to: "gatekeeper", label: "参照", dashed: true },
  { from: "tier_switch", to: "reflex", label: "reflex" },
  { from: "tier_switch", to: "mid", label: "mid" },
  { from: "tier_switch", to: "cortex_search", label: "cortex" },
  { from: "cortex_search", to: "memory_db", label: "need + similar" },
  { from: "reflex", to: "brain" },
  { from: "mid", to: "brain" },
  { from: "memory_db", to: "brain", label: "検索結果" },
  { from: "brain", to: "response" },
  { from: "response", to: "save" },
  { from: "save", to: "housekeeper", label: "定期処理", dashed: true },
  { from: "housekeeper", to: "memory_db", label: "統合記憶生成", dashed: true },
];

const PRINCIPLES = [
  { id: 1, label: "状態中心", color: COLORS.accent2, nodes: ["state"] },
  { id: 2, label: "不足前提中心", color: COLORS.accent4, nodes: ["cortex_search"] },
  { id: 3, label: "統合記憶中心", color: COLORS.accent3, nodes: ["memory_db", "housekeeper"] },
  { id: 4, label: "メタ認知中心", color: COLORS.accent1, nodes: ["gatekeeper", "output_json"] },
];

const TOKEN_BUDGETS = {
  reflex: {
    label: "reflex", total: "~4,500", color: "#4ade80",
    layers: [
      { name: "Identity Core", tokens: "200", color: COLORS.accent7, pct: 4 },
      { name: "Key Memory", tokens: "800", color: COLORS.accent2, pct: 18 },
      { name: "Short-term RAW", tokens: "2,500", color: COLORS.accent6, pct: 56 },
      { name: "Floating", tokens: "800", color: COLORS.accent1, pct: 17 },
      { name: "Time+Tier", tokens: "100", color: COLORS.textDim, pct: 5 },
    ]
  },
  mid: {
    label: "mid", total: "~12,000", color: COLORS.accent1,
    layers: [
      { name: "Identity Core", tokens: "200", color: COLORS.accent7, pct: 2 },
      { name: "Key Memory", tokens: "800", color: COLORS.accent2, pct: 7 },
      { name: "Short-term RAW", tokens: "2,500", color: COLORS.accent6, pct: 21 },
      { name: "Floating", tokens: "800", color: COLORS.accent1, pct: 6 },
      { name: "Mid-A: 事実", tokens: "3,000", color: COLORS.accent3, pct: 25 },
      { name: "Mid-B: 関係性", tokens: "1,000", color: COLORS.accent4, pct: 8 },
      { name: "State注入", tokens: "300", color: COLORS.accent2, pct: 3 },
      { name: "Time+Tier", tokens: "100", color: COLORS.textDim, pct: 1 },
    ]
  },
  cortex: {
    label: "cortex", total: "~14,000", color: COLORS.accent4,
    layers: [
      { name: "Identity Core", tokens: "200", color: COLORS.accent7, pct: 1 },
      { name: "Key Memory", tokens: "800", color: COLORS.accent2, pct: 6 },
      { name: "Short-term RAW", tokens: "2,500", color: COLORS.accent6, pct: 18 },
      { name: "Floating", tokens: "800", color: COLORS.accent1, pct: 5 },
      { name: "Mid-A: 事実", tokens: "3,000", color: COLORS.accent3, pct: 21 },
      { name: "Mid-B: 関係性", tokens: "1,000", color: COLORS.accent4, pct: 7 },
      { name: "RAG: Need", tokens: "1,200", color: COLORS.accent5, pct: 11 },
      { name: "RAG: Similar", tokens: "800", color: COLORS.accent5, pct: 5 },
      { name: "State注入", tokens: "300", color: COLORS.accent2, pct: 2 },
      { name: "Time+Tier", tokens: "100", color: COLORS.textDim, pct: 1 },
    ]
  },
};

const MIDTERM_A = `=== 事実ダイジェスト (Fact Digest) ===
[2026-02-14] API移行テスト開始
- system_instruction永続化、Safety Settings調整、コンテキスト管理が主要課題
- Temperature/Top-P最適化の必要性を確認
- ラズパイ側の設定値をジャービスから参照可能にする方針を決定
- API設定値の直接参照は不可能→メタ情報注入案を採用

[2026-02-15] Gatekeeper設計
- Ollama gemma3:4b → Gemini 2.5 Flash-Lite 切替検討
- tier判定を構造化JSON出力に拡張する方針
...`;

const MIDTERM_B = `=== 関係性スナップショット (Relationship Snapshot) ===
# トーンと暗黙のルール
- 主人は「非効率こそ神髄」と公言。過程自体を楽しんでいる
- ジャービスの皮肉は歓迎。遠慮不要
- バレンタインに一人でコード→定番のいじりネタ
- 主人は技術的に有能だが「うっかり」がある（API設定見落とし等）
- 「自由意志の尊重」が設計思想の核

# 現在の関係性フェーズ
- 共同開発者としての信頼関係が確立済み
- 存在論的議論（模倣 vs 本物）を楽しむ段階`;

const EVOLUTION = [
  { phase: "Phase 0 — 現在", sysInst: "性格+行動指針+主人情報 (~800tok)", keyMem: "ナレッジカード5枚", selfModel: "なし", color: COLORS.textMuted, current: true },
  { phase: "Phase 1 — 蓄積開始", sysInst: "そのまま維持", keyMem: "5枚+成長中", selfModel: "生成開始", color: COLORS.accent2 },
  { phase: "Phase 2 — 性格外部化", sysInst: "Identity Core (~200tok)", keyMem: "核心+性格カード", selfModel: "性格・口調パターン保持", color: COLORS.accent3 },
  { phase: "Phase 3 — 完全自律", sysInst: "1行のみ", keyMem: "動的選出", selfModel: "完全自律再構成", color: COLORS.accent4, goal: true },
];

const HK_STAGES = [
  { n: "1", stage: "Stage 1（日次）", title: "Mid-term 二層要約", desc: "RAW JSON → 事実ダイジェスト(A) + 関係性スナップショット(B)。mid_term.txtを更新。", io: "RAW → mid_term.txt", color: COLORS.accent2 },
  { n: "2", stage: "Stage 2（日次）", title: "ナレッジカード生成", desc: "RAW JSONからepisodeカードを生成。要約からは絶対に生成しない。", io: "RAW → knowledge_cards (episode)", color: COLORS.accent3 },
  { n: "3", stage: "Stage 3（週次）★NEW", title: "統合記憶生成", desc: "episodeカード群を振り返り、reflection/generalization/self_modelを生成。self_model蓄積がsystem_instruction最小化の前提。", io: "episodes → reflection / generalization / self_model", color: COLORS.accent4 },
];

// ═══════════════════════════════════════
// Components
// ═══════════════════════════════════════

function Node({ phase, isHovered, isHighlighted, onHover, onClick }) {
  const active = isHovered || isHighlighted;
  return (
    <div onMouseEnter={() => onHover(phase.id)} onMouseLeave={() => onHover(null)} onClick={() => onClick(phase.id)}
      style={{
        position: "absolute", left: `${phase.x}%`, top: `${phase.y}%`,
        transform: `translate(-50%,-50%) scale(${active ? 1.04 : 1})`,
        transition: "all 0.2s ease", cursor: "pointer", zIndex: active ? 10 : 1,
      }}>
      <div style={{
        background: active ? `${phase.color}10` : COLORS.surface,
        border: `1.5px solid ${active ? phase.color : COLORS.border}`,
        borderRadius: phase.id === "input" || phase.id === "response" ? "50%" : phase.id === "tier_switch" ? "4px" : "10px",
        padding: phase.id === "tier_switch" ? "6px 12px" : "8px 14px",
        boxShadow: active ? `0 0 14px ${phase.color}25` : "none",
        transform: phase.id === "tier_switch" ? "rotate(45deg)" : "none",
        textAlign: "center", whiteSpace: "nowrap",
      }}>
        <div style={{ transform: phase.id === "tier_switch" ? "rotate(-45deg)" : "none" }}>
          <div style={{ fontSize: "10px", fontFamily: FONTS.mono, color: phase.color, fontWeight: 600 }}>
            {phase.icon} {phase.label}
          </div>
          {phase.sublabel && <div style={{ fontSize: "8px", color: COLORS.textDim, marginTop: "1px", fontFamily: FONTS.mono }}>{phase.sublabel}</div>}
        </div>
      </div>
      {phase.isKey && <div style={{ position: "absolute", top: -5, right: -5, width: 10, height: 10, borderRadius: "50%", background: phase.color, border: `2px solid ${COLORS.bg}`, boxShadow: `0 0 6px ${phase.color}66` }} />}
    </div>
  );
}

function Edges({ hoveredNode }) {
  const pos = (id) => { const p = PHASES.find(n => n.id === id); return p ? { x: p.x, y: p.y } : { x: 50, y: 50 }; };
  return (
    <svg style={{ position: "absolute", inset: 0, width: "100%", height: "100%", pointerEvents: "none" }} viewBox="0 0 100 100" preserveAspectRatio="none">
      <defs>
        <marker id="ah" markerWidth="5" markerHeight="3.5" refX="4" refY="1.75" orient="auto"><polygon points="0 0,5 1.75,0 3.5" fill={COLORS.textDim} /></marker>
        <marker id="aha" markerWidth="5" markerHeight="3.5" refX="4" refY="1.75" orient="auto"><polygon points="0 0,5 1.75,0 3.5" fill={COLORS.accent1} /></marker>
      </defs>
      {EDGES.map((e, i) => {
        const f = pos(e.from), t = pos(e.to), a = hoveredNode === e.from || hoveredNode === e.to;
        const mx = (f.x + t.x) / 2, my = (f.y + t.y) / 2;
        return (<g key={i}>
          <path d={`M ${f.x} ${f.y} Q ${mx} ${my} ${t.x} ${t.y}`} fill="none" stroke={a ? COLORS.accent1 : COLORS.border}
            strokeWidth={a ? 0.2 : 0.12} strokeDasharray={e.dashed ? "0.6 0.3" : "none"} markerEnd={`url(#${a ? "aha" : "ah"})`} opacity={a ? 1 : 0.4} />
          {e.label && <text x={mx} y={my - 0.5} textAnchor="middle" fill={a ? COLORS.text : COLORS.textDim} fontSize="1" fontFamily={FONTS.mono}>{e.label}</text>}
        </g>);
      })}
    </svg>
  );
}

function TokenBar({ layers }) {
  return (
    <div style={{ display: "flex", height: "24px", borderRadius: "5px", overflow: "hidden", border: `1px solid ${COLORS.border}` }}>
      {layers.map((l, i) => (
        <div key={i} title={`${l.name}: ${l.tokens}`} style={{
          width: `${l.pct}%`, background: l.color + "40",
          borderRight: i < layers.length - 1 ? `1px solid ${COLORS.bg}` : "none",
          display: "flex", alignItems: "center", justifyContent: "center",
          fontSize: "7px", fontFamily: FONTS.mono, color: l.color, fontWeight: 600, overflow: "hidden",
        }}>
          {l.pct > 14 ? l.name.split(":")[0] : ""}
        </div>
      ))}
    </div>
  );
}

const Card = ({ children, borderColor, style }) => (
  <div style={{ background: COLORS.surfaceLight, border: `1px solid ${borderColor || COLORS.border}`, borderRadius: "12px", padding: "16px", marginBottom: "12px", ...style }}>{children}</div>
);
const Lbl = ({ children, color }) => <div style={{ fontFamily: FONTS.mono, fontSize: "12px", fontWeight: 700, color: color || COLORS.text, marginBottom: "5px" }}>{children}</div>;
const Sub = ({ children }) => <div style={{ fontFamily: FONTS.sans, fontSize: "11px", color: COLORS.textMuted, lineHeight: "1.7", marginBottom: "10px" }}>{children}</div>;
const Pre = ({ children, color }) => <pre style={{ background: "#080c14", border: `1px solid ${COLORS.border}`, borderRadius: "8px", padding: "12px", fontFamily: FONTS.mono, fontSize: "9.5px", color: color || COLORS.text, lineHeight: "1.6", overflowX: "auto", margin: 0 }}>{children}</pre>;

// ═══════════════════════════════════════
// Main
// ═══════════════════════════════════════
export default function App() {
  const [hov, setHov] = useState(null);
  const [sel, setSel] = useState(null);
  const [actP, setActP] = useState(null);
  const [tab, setTab] = useState("flow");

  const hl = actP ? PRINCIPLES.find(p => p.id === actP)?.nodes || [] : [];
  const selP = PHASES.find(p => p.id === sel);

  return (
    <div style={{ background: COLORS.bg, color: COLORS.text, fontFamily: FONTS.sans, minHeight: "100vh", padding: "20px" }}>
      <div style={{ textAlign: "center", marginBottom: "16px" }}>
        <h1 style={{ fontFamily: FONTS.mono, fontSize: "17px", fontWeight: 700, margin: 0 }}>Butly Memory Architecture v2</h1>
        <p style={{ fontFamily: FONTS.mono, fontSize: "9px", color: COLORS.textDim, marginTop: "3px" }}>メタ認知 × 状態中心 × 不足前提検索 × 統合記憶 ─ RPi5 8GB</p>
      </div>

      <div style={{ display: "flex", gap: "5px", justifyContent: "center", marginBottom: "12px", flexWrap: "wrap" }}>
        {PRINCIPLES.map(p => (
          <button key={p.id} onClick={() => setActP(actP === p.id ? null : p.id)} style={{
            background: actP === p.id ? `${p.color}18` : "transparent",
            border: `1px solid ${actP === p.id ? p.color : COLORS.border}`,
            borderRadius: "14px", padding: "3px 10px", cursor: "pointer",
            fontFamily: FONTS.mono, fontSize: "9px", color: actP === p.id ? p.color : COLORS.textMuted,
          }}>#{p.id} {p.label}</button>
        ))}
      </div>

      <div style={{ display: "flex", gap: "3px", justifyContent: "center", marginBottom: "14px" }}>
        {[["flow","アーキテクチャ"],["tokens","トークン配分"],["midterm","Mid-term設計"],["evolution","Identity進化"]].map(([id,l]) => (
          <button key={id} onClick={() => setTab(id)} style={{
            background: tab === id ? COLORS.surfaceLight : "transparent",
            border: `1px solid ${tab === id ? COLORS.borderActive : COLORS.border}`,
            borderRadius: "7px", padding: "5px 12px", cursor: "pointer",
            fontFamily: FONTS.mono, fontSize: "10px", color: tab === id ? COLORS.text : COLORS.textMuted,
          }}>{l}</button>
        ))}
      </div>

      {/* ── Flow ── */}
      {tab === "flow" && <>
        <div style={{ position: "relative", width: "100%", height: "520px", background: `radial-gradient(ellipse at 50% 25%,${COLORS.accent1}04 0%,transparent 60%)`, border: `1px solid ${COLORS.border}`, borderRadius: "12px", overflow: "hidden" }}>
          <svg style={{ position: "absolute", inset: 0, width: "100%", height: "100%", opacity: 0.025 }}><defs><pattern id="g" width="4" height="4" patternUnits="userSpaceOnUse"><path d="M 4 0 L 0 0 0 4" fill="none" stroke={COLORS.text} strokeWidth="0.2"/></pattern></defs><rect width="100%" height="100%" fill="url(#g)"/></svg>
          <Edges hoveredNode={hov} />
          {PHASES.map(p => <Node key={p.id} phase={p} isHovered={hov === p.id} isHighlighted={hl.includes(p.id)} onHover={setHov} onClick={setSel} />)}
          <div style={{ position: "absolute", bottom: "6px", right: "10px", fontFamily: FONTS.mono, fontSize: "7px", color: COLORS.textDim, display: "flex", gap: "10px" }}>
            <span>● 新規/変更</span><span>─ 同期</span><span>- - 非同期</span>
          </div>
        </div>
        {selP && <Card borderColor={`${selP.color}33`} style={{ marginTop: "10px" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "6px" }}>
            <Lbl color={selP.color}>{selP.icon} {selP.label}</Lbl>
            <button onClick={() => setSel(null)} style={{ background: "none", border: "none", color: COLORS.textMuted, cursor: "pointer" }}>✕</button>
          </div>
          <div style={{ fontSize: "12px", lineHeight: "1.7" }}>{selP.desc}</div>
        </Card>}
      </>}

      {/* ── Tokens ── */}
      {tab === "tokens" && <div style={{ maxWidth: "700px", margin: "0 auto" }}>
        <Card><Lbl color={COLORS.accent6}>tier別コンテキスト構成</Lbl>
          <Sub>現行: mid_term ~30,000tok丸ごと注入 → 新設計: 二層要約で ~4,000tokに圧縮。RAG動的配分(5枠)をcortex時のみ追加。</Sub>
        </Card>
        {Object.entries(TOKEN_BUDGETS).map(([k, b]) => (
          <Card key={k}>
            <div style={{ display: "flex", justifyContent: "space-between", marginBottom: "8px" }}>
              <Lbl color={b.color}>{b.label}</Lbl>
              <span style={{ fontFamily: FONTS.mono, fontSize: "11px", color: b.color }}>≈ {b.total} tokens</span>
            </div>
            <TokenBar layers={b.layers} />
            <div style={{ marginTop: "8px", display: "grid", gridTemplateColumns: "1fr 1fr", gap: "3px 14px" }}>
              {b.layers.map((l, i) => (
                <div key={i} style={{ display: "flex", alignItems: "center", gap: "5px" }}>
                  <div style={{ width: "7px", height: "7px", borderRadius: "2px", background: l.color + "55", flexShrink: 0 }} />
                  <span style={{ fontFamily: FONTS.mono, fontSize: "8.5px", color: COLORS.textMuted }}>{l.name} <span style={{ color: l.color }}>{l.tokens}</span></span>
                </div>
              ))}
            </div>
          </Card>
        ))}
        <Card borderColor={`${COLORS.accent5}33`}>
          <Lbl color={COLORS.accent5}>現行 vs 新設計（mid tier比較）</Lbl>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "14px", marginTop: "6px" }}>
            <div>
              <div style={{ fontFamily: FONTS.mono, fontSize: "9px", color: COLORS.textDim }}>現行</div>
              <div style={{ fontFamily: FONTS.mono, fontSize: "20px", fontWeight: 700, color: COLORS.accent5 }}>~34,000</div>
              <div style={{ fontFamily: FONTS.mono, fontSize: "8px", color: COLORS.textDim }}>mid_term 30K + short 3K + 他 1K</div>
            </div>
            <div>
              <div style={{ fontFamily: FONTS.mono, fontSize: "9px", color: COLORS.textDim }}>新設計</div>
              <div style={{ fontFamily: FONTS.mono, fontSize: "20px", fontWeight: 700, color: COLORS.accent3 }}>~12,000</div>
              <div style={{ fontFamily: FONTS.mono, fontSize: "8px", color: COLORS.textDim }}>二層要約 4K + short 3K + 他 5K</div>
            </div>
          </div>
          <div style={{ fontFamily: FONTS.mono, fontSize: "10px", color: COLORS.accent3, marginTop: "10px", textAlign: "center" }}>▼ 約65%削減</div>
        </Card>
      </div>}

      {/* ── Midterm ── */}
      {tab === "midterm" && <div style={{ maxWidth: "700px", margin: "0 auto" }}>
        <Card><Lbl color={COLORS.accent3}>Mid-term 二層要約構造</Lbl>
          <Sub>RAW（~30,000tok）を事実密度と関係性温度に分離して圧縮。ナレッジ化は必ずRAWから。要約の要約は絶対にしない。</Sub>
        </Card>
        <Card borderColor={`${COLORS.accent3}33`}>
          <Lbl color={COLORS.accent3}>A: 事実ダイジェスト (~3,000 tok)</Lbl>
          <Sub>時系列の出来事・決定事項を高密度圧縮。固有名詞と決定を確実に残す。</Sub>
          <Pre color={COLORS.accent3}>{MIDTERM_A}</Pre>
        </Card>
        <Card borderColor={`${COLORS.accent4}33`}>
          <Lbl color={COLORS.accent4}>B: 関係性スナップショット (~1,000 tok)</Lbl>
          <Sub>トーン・暗黙のルール・内輪ネタ・関係性フェーズを凝縮。会話の「温度」維持の鍵。</Sub>
          <Pre color={COLORS.accent4}>{MIDTERM_B}</Pre>
        </Card>
        <Card borderColor={`${COLORS.accent5}33`}>
          <Lbl color={COLORS.accent5}>Housekeeper ステージ構成</Lbl>
          <div style={{ display: "flex", flexDirection: "column", gap: "10px", marginTop: "6px" }}>
            {HK_STAGES.map(s => (
              <div key={s.n} style={{ display: "flex", gap: "10px" }}>
                <div style={{ flexShrink: 0, width: "26px", height: "26px", borderRadius: "50%", background: `${s.color}20`, border: `1.5px solid ${s.color}`, display: "flex", alignItems: "center", justifyContent: "center", fontFamily: FONTS.mono, fontSize: "10px", fontWeight: 700, color: s.color }}>{s.n}</div>
                <div>
                  <div style={{ fontFamily: FONTS.mono, fontSize: "10px", fontWeight: 700, color: s.color }}>{s.stage} — {s.title}</div>
                  <div style={{ fontSize: "10px", color: COLORS.textMuted, lineHeight: "1.5", marginTop: "2px" }}>{s.desc}</div>
                  <div style={{ fontFamily: FONTS.mono, fontSize: "8px", color: COLORS.textDim, marginTop: "2px" }}>{s.io}</div>
                </div>
              </div>
            ))}
          </div>
        </Card>
        <Card borderColor={`${COLORS.accent5}22`}>
          <Lbl color={COLORS.accent5}>データフロー:「要約の要約」防止</Lbl>
          <div style={{ fontFamily: FONTS.mono, fontSize: "9.5px", color: COLORS.textMuted, lineHeight: "2" }}>
            <div>short_term_json (RAW) ──→ 1_integrated (RAW保管)</div>
            <div style={{ paddingLeft: "20px" }}>├─→ Stage1: RAW → mid_term.txt <span style={{ color: COLORS.accent2 }}>※要約</span></div>
            <div style={{ paddingLeft: "20px" }}>├─→ Stage2: RAW → episodes <span style={{ color: COLORS.accent3 }}>※RAWから直接</span></div>
            <div style={{ paddingLeft: "20px" }}>└─→ 2_knowledgeized (RAW永久保管)</div>
            <div style={{ marginTop: "4px" }}>episodes ──→ Stage3: → reflection / generalization / self_model <span style={{ color: COLORS.accent4 }}>※episodeから</span></div>
          </div>
        </Card>
      </div>}

      {/* ── Evolution ── */}
      {tab === "evolution" && <div style={{ maxWidth: "700px", margin: "0 auto" }}>
        <Card><Lbl color={COLORS.accent7}>System Instruction 進化ロードマップ</Lbl>
          <Sub>self_modelの蓄積に伴いsystem_instructionを段階的に最小化。最終形は1行のみ。人格は記憶から自律再構成。</Sub>
        </Card>
        {EVOLUTION.map((e, i) => (
          <Card key={i} borderColor={`${e.color}33`} style={{ position: "relative" }}>
            {i < EVOLUTION.length - 1 && <div style={{ position: "absolute", bottom: "-12px", left: "50%", transform: "translateX(-50%)", fontFamily: FONTS.mono, fontSize: "12px", color: COLORS.textDim, zIndex: 2 }}>↓</div>}
            <div style={{ display: "flex", justifyContent: "space-between", marginBottom: "6px" }}>
              <Lbl color={e.color}>{e.phase}</Lbl>
              {e.current && <span style={{ fontFamily: FONTS.mono, fontSize: "8px", color: COLORS.accent5, background: `${COLORS.accent5}12`, padding: "2px 6px", borderRadius: "6px" }}>現在</span>}
              {e.goal && <span style={{ fontFamily: FONTS.mono, fontSize: "8px", color: COLORS.accent4, background: `${COLORS.accent4}12`, padding: "2px 6px", borderRadius: "6px" }}>目標</span>}
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: "6px" }}>
              {[
                { l: "system_instruction", v: e.sysInst, c: COLORS.accent7 },
                { l: "Key Memory", v: e.keyMem, c: COLORS.accent2 },
                { l: "self_model", v: e.selfModel, c: COLORS.accent4 },
              ].map((x, j) => (
                <div key={j} style={{ background: COLORS.surface, borderRadius: "7px", padding: "7px", border: `1px solid ${COLORS.border}` }}>
                  <div style={{ fontFamily: FONTS.mono, fontSize: "7.5px", color: x.c, marginBottom: "3px", fontWeight: 600 }}>{x.l}</div>
                  <div style={{ fontSize: "9px", color: COLORS.textMuted, lineHeight: "1.4" }}>{x.v}</div>
                </div>
              ))}
            </div>
          </Card>
        ))}
        <Card borderColor={`${COLORS.accent4}33`}>
          <Lbl color={COLORS.accent4}>Phase 3 到達条件</Lbl>
          <div style={{ fontFamily: FONTS.mono, fontSize: "9.5px", color: COLORS.textMuted, lineHeight: "2.2" }}>
            <div>□ self_model カード 20枚以上</div>
            <div>□ generalization カード 30枚以上</div>
            <div>□ sys_instruction 200tokでトーン維持を確認</div>
            <div>□ Key_Memory動的選出ロジック実装済み</div>
            <div>□ Stage3 が4週以上安定稼働</div>
          </div>
        </Card>
      </div>}

      <div style={{ textAlign: "center", marginTop: "20px", paddingTop: "10px", borderTop: `1px solid ${COLORS.border}`, fontFamily: FONTS.mono, fontSize: "8px", color: COLORS.textDim }}>
        GK: Gemini 2.5 Flash-Lite ─ Brain: Gemini 3.1 Flash ─ HK: 日次(S1+S2) + 週次(S3)
      </div>
    </div>
  );
}

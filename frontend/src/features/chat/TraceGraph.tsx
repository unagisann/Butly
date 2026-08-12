// Trace Graph の描画（issue #51）。
//
// Mermaid 文字列の生成は backend（butly_core/trace/mermaid.py）が正本で、
// ここは描画だけを担う。frontend で組み立て直すと 2 つの定義が育つため。
//
// mermaid 本体は bundle が大きいので、**開いた瞬間に動的 import** して別 chunk に
// 落とす。閉じている間は読み込まない。

import { useCallback, useEffect, useRef, useState } from "react";
import { Network, RefreshCw } from "lucide-react";

import type { ApiTransport } from "../../api/transport";
import type { TraceGraphResponse } from "../../api/generated";
import { ButlyApiError, normalizeApiError } from "../../api/errors";
import { useI18n } from "../../i18n/strings";

type LoadState =
  | { phase: "idle" }
  | { phase: "loading" }
  | { phase: "ready"; trace: TraceGraphResponse; svg: string }
  | { phase: "failed"; error: ButlyApiError };

let renderSeq = 0;

async function renderMermaid(source: string): Promise<string> {
  const mermaid = (await import("mermaid")).default;
  mermaid.initialize({
    startOnLoad: false,
    // 応答テキスト由来のラベルが入るので、HTML としては解釈させない。
    securityLevel: "strict",
    htmlLabels: false,
    theme: "neutral",
    flowchart: { useMaxWidth: true },
  });
  renderSeq += 1;
  const { svg } = await mermaid.render(`butly-trace-${renderSeq}`, source);
  return svg;
}

export function TraceGraph({
  transport,
  instanceName,
}: {
  transport: ApiTransport;
  instanceName: string;
}) {
  const { t } = useI18n();
  const [state, setState] = useState<LoadState>({ phase: "idle" });
  const disposedRef = useRef(false);

  useEffect(() => {
    disposedRef.current = false;
    return () => {
      disposedRef.current = true;
    };
  }, []);

  const load = useCallback(async () => {
    setState({ phase: "loading" });
    try {
      const trace = await transport.getTrace(instanceName);
      const svg = await renderMermaid(trace.mermaid);
      if (!disposedRef.current) setState({ phase: "ready", trace, svg });
    } catch (error) {
      if (!disposedRef.current) {
        setState({ phase: "failed", error: normalizeApiError(error) });
      }
    }
  }, [instanceName, transport]);

  // instance を切り替えたら前のグラフを残さない。
  useEffect(() => {
    setState({ phase: "idle" });
  }, [instanceName]);

  return (
    <section className="trace-graph">
      <h3>
        <Network size={15} aria-hidden="true" /> {t("debug.trace")}
      </h3>
      <div className="trace-actions">
        <button className="text-button compact" type="button" onClick={() => void load()}>
          <RefreshCw size={13} aria-hidden="true" />{" "}
          {state.phase === "ready" ? t("debug.trace_reload") : t("debug.trace_load")}
        </button>
        {state.phase === "ready" && (
          <span className="trace-meta">
            {state.trace.trace_id}
            {state.trace.created_at ? ` · ${state.trace.created_at}` : ""}
            {" · "}
            {Object.entries(state.trace.node_counts ?? {})
              .map(([status, count]) => `${status} ${count}`)
              .join(" / ")}
          </span>
        )}
      </div>

      {state.phase === "loading" && <p className="muted">{t("debug.trace_loading")}</p>}
      {state.phase === "failed" && (
        <p className="inline-error">
          {state.error.code === "trace_not_found"
            ? t("debug.trace_empty")
            : state.error.message}
        </p>
      )}
      {state.phase === "ready" && (
        <div
          className="trace-canvas"
          role="img"
          aria-label={t("debug.trace")}
          // SVG は mermaid が securityLevel:"strict" で生成したもの。
          // 元になる Mermaid 文字列も backend 側で sanitize 済み。
          dangerouslySetInnerHTML={{ __html: state.svg }}
        />
      )}
    </section>
  );
}

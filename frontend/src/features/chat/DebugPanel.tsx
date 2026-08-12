import { BrainCircuit, ChevronDown, DatabaseZap, Route } from "lucide-react";

import { useI18n } from "../../i18n/strings";
import type { ChatDebugView } from "./types";

export function DebugPanel({ debug }: { debug: ChatDebugView | null }) {
  const { t } = useI18n();
  return (
    <details className="debug-panel">
      <summary>
        <BrainCircuit size={17} aria-hidden="true" />
        <span>{t("debug.title")}</span>
        <ChevronDown className="summary-chevron" size={16} aria-hidden="true" />
      </summary>
      {!debug ? (
        <p className="muted">{t("debug.empty")}</p>
      ) : (
        <div className="debug-grid">
          <section>
            <h3>
              <Route size={15} aria-hidden="true" /> Gatekeeper
            </h3>
            <dl className="debug-values">
              <dt>{t("debug.tier")}</dt>
              <dd>{debug.gatekeeper.tier || t("common.none")}</dd>
              <dt>{t("debug.need")}</dt>
              <dd>{debug.gatekeeper.need || t("common.none")}</dd>
              <dt>{t("debug.probe")}</dt>
              <dd>{debug.gatekeeper.memoryProbeStatus || t("common.unknown")}</dd>
              {debug.gatekeeper.fallbackReason && (
                <>
                  <dt>{t("debug.fallback")}</dt>
                  <dd>{debug.gatekeeper.fallbackReason}</dd>
                </>
              )}
            </dl>
            {debug.gatekeeper.searchTargets.length > 0 && (
              <div className="debug-detail">
                <strong>{t("debug.search_targets")}</strong>
                <span>{debug.gatekeeper.searchTargets.join(" · ")}</span>
              </div>
            )}
            {Object.keys(debug.gatekeeper.scores).length > 0 && (
              <div className="debug-detail">
                <strong>{t("debug.scores")}</strong>
                <span>
                  {Object.entries(debug.gatekeeper.scores)
                    .map(([key, value]) => `${key} ${value.toFixed(2)}`)
                    .join(" · ")}
                </span>
              </div>
            )}
          </section>
          <section>
            <h3>
              <DatabaseZap size={15} aria-hidden="true" /> {t("debug.rag")}
            </h3>
            <div className="debug-metrics">
              <span>{debug.rag.enabled ? "ON" : "OFF"}</span>
              <span>{t("debug.candidates", { count: debug.rag.candidateCount })}</span>
              <span>{t("debug.injected", { count: debug.rag.injectedCount })}</span>
            </div>
            {debug.rag.activeNodes.length > 0 && (
              <div className="debug-detail">
                <strong>{t("debug.active_nodes")}</strong>
                <span>{debug.rag.activeNodes.join(" · ")}</span>
              </div>
            )}
          </section>
        </div>
      )}
    </details>
  );
}

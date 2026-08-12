import {
  Activity,
  AlertCircle,
  CheckCircle2,
  ChevronDown,
  Cpu,
  RefreshCw,
  Unplug,
} from "lucide-react";

import type {
  ConnectionPreflight,
  EmbeddingPreflight,
  PreflightResponse,
} from "../../api/generated";
import { useI18n } from "../../i18n/strings";
import { localizeReason } from "../../i18n/reasons";

interface PreflightPanelProps {
  report: PreflightResponse | null;
  loading: boolean;
  error: string | null;
  onRefresh: () => void;
}

type PreflightCheckStatus = ConnectionPreflight["status"];

function CheckIcon({ status }: { status: PreflightCheckStatus }) {
  if (status === "ready") return <CheckCircle2 size={16} aria-hidden="true" />;
  if (status === "unreachable") return <Unplug size={16} aria-hidden="true" />;
  return <AlertCircle size={16} aria-hidden="true" />;
}

function statusClass(status: PreflightCheckStatus): string {
  return status === "ready"
    ? "ok"
    : status === "unreachable" || status === "unavailable"
      ? "error"
      : "warning";
}

function ConnectionRow({ check }: { check: ConnectionPreflight }) {
  const { t } = useI18n();
  return (
    <li className="preflight-row">
      <span className={`check-icon ${statusClass(check.status)}`}>
        <CheckIcon status={check.status} />
      </span>
      <span className="preflight-copy">
        <strong>{check.label}</strong>
        <small>
          {check.configured ? t("preflight.configured") : t("preflight.not_configured")}
          {check.reason ? ` · ${localizeReason(check.reason, t)}` : ""}
        </small>
      </span>
      <span className="preflight-metrics">
        {check.model_count != null && (
          <span>{t("preflight.models", { count: check.model_count })}</span>
        )}
        {check.latency_ms != null && (
          <span>{t("preflight.latency", { value: check.latency_ms })}</span>
        )}
      </span>
    </li>
  );
}

function EmbeddingRow({ check }: { check: EmbeddingPreflight }) {
  const { t } = useI18n();
  return (
    <div className="preflight-row embedding-row">
      <span className={`check-icon ${statusClass(check.status)}`}>
        <Cpu size={16} aria-hidden="true" />
      </span>
      <span className="preflight-copy">
        <strong>{check.model_name || t("preflight.embedding")}</strong>
        <small>
          {check.connection_id || t("common.unknown")}
          {check.reason ? ` · ${localizeReason(check.reason, t)}` : ""}
        </small>
      </span>
      <span className="preflight-metrics">
        {check.dimension != null && (
          <span>{t("preflight.dimension", { value: check.dimension })}</span>
        )}
        {check.latency_ms != null && (
          <span>{t("preflight.latency", { value: check.latency_ms })}</span>
        )}
      </span>
    </div>
  );
}

export function PreflightPanel({
  report,
  loading,
  error,
  onRefresh,
}: PreflightPanelProps) {
  const { t } = useI18n();
  const label = report
    ? report.status === "ready"
      ? t("preflight.ready")
      : report.status === "degraded"
        ? t("preflight.degraded")
        : t("preflight.offline")
    : loading
      ? t("preflight.loading")
      : t("preflight.title");

  return (
    <details className="preflight-panel">
      <summary>
        <span className={`preflight-summary-icon ${report?.status ?? "unknown"}`}>
          {report?.status === "ready" ? (
            <Activity size={17} aria-hidden="true" />
          ) : (
            <AlertCircle size={17} aria-hidden="true" />
          )}
        </span>
        <span>
          <strong>{t("preflight.title")}</strong>
          <small>{label}</small>
        </span>
        <ChevronDown className="summary-chevron" size={16} aria-hidden="true" />
      </summary>
      <div className="preflight-content">
        <button
          type="button"
          className="text-button compact"
          onClick={onRefresh}
          disabled={loading}
        >
          <RefreshCw size={14} aria-hidden="true" className={loading ? "spin" : ""} />
          {t("preflight.refresh")}
        </button>
        {error && (
          <p className="inline-error">
            {t("preflight.error")}: {error}
          </p>
        )}
        {report && (
          <>
            <h3>{t("preflight.connections")}</h3>
            <ul className="preflight-list">
              {(report.connections ?? []).map((check) => (
                <ConnectionRow key={check.connection_id} check={check} />
              ))}
            </ul>
            <h3>{t("preflight.embedding")}</h3>
            <EmbeddingRow check={report.embedding} />
          </>
        )}
      </div>
    </details>
  );
}

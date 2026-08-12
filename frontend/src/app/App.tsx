import { Languages, Power, RefreshCw, RotateCcw, Wifi, WifiOff } from "lucide-react";

import { createApiTransport } from "../api/transport";
import type { ApiTransportFactory } from "../api/transport";
import type { LifecycleBridge } from "../lifecycle/bridge";
import { useBackendState } from "../lifecycle/useBackendState";
import { I18nProvider, useI18n } from "../i18n/strings";
import { Workspace } from "./Workspace";
import { useApiConnection } from "./useApiConnection";

interface AppProps {
  bridge: LifecycleBridge;
  transportFactory?: ApiTransportFactory;
}

const DEFAULT_TRANSPORT_FACTORY: ApiTransportFactory = (info) => createApiTransport(info);

function AppContent({ bridge, transportFactory = DEFAULT_TRANSPORT_FACTORY }: AppProps) {
  const { locale, setLocale, t } = useI18n();
  const { state, restart } = useBackendState(bridge);
  const api = useApiConnection(state, bridge, transportFactory);

  const lifecycleLabel = (() => {
    switch (state.phase) {
      case "starting":
        return t("backend.starting");
      case "ready":
        return t("backend.ready");
      case "unavailable":
        return t("backend.unavailable");
      case "crashed":
        return t("backend.crashed");
      case "version_mismatch":
        return t("backend.version_mismatch");
    }
  })();

  const apiLabel = (() => {
    switch (api.phase) {
      case "connecting":
        return t("backend.connecting");
      case "reconnecting":
        return t("backend.reconnecting");
      case "disconnected":
        return t("backend.disconnected");
      case "connected":
        return t("backend.ready");
      case "idle":
        return lifecycleLabel;
    }
  })();

  const showRestart =
    state.phase === "crashed" ||
    state.phase === "unavailable" ||
    state.phase === "version_mismatch";
  const lifecycleReady = state.phase === "ready";
  const showInitialGate = !lifecycleReady || api.connectionRevision === 0;

  return (
    <main className="app-shell">
      <header className="app-bar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">B</span>
          <div>
            <strong>{t("app.title")}</strong>
            <small>{t("app.subtitle")}</small>
          </div>
        </div>
        <div className="app-actions">
          <span
            className={`global-status ${lifecycleReady && api.phase === "connected" ? "connected" : "offline"}`}
            data-testid="backend-status"
            data-phase={state.phase}
            data-api-phase={api.phase}
          >
            {lifecycleReady && api.phase === "connected" ? (
              <Wifi size={14} aria-hidden="true" />
            ) : (
              <WifiOff size={14} aria-hidden="true" />
            )}
            {lifecycleReady ? apiLabel : lifecycleLabel}
          </span>
          <button
            className="language-button"
            type="button"
            onClick={() => setLocale(locale === "ja" ? "en" : "ja")}
            aria-label={t("app.language_label")}
          >
            <Languages size={16} aria-hidden="true" /> {t("app.language")}
          </button>
        </div>
      </header>

      {lifecycleReady && (api.phase === "reconnecting" || api.phase === "disconnected") && (
        <div className="connection-banner" role="status">
          <WifiOff size={16} aria-hidden="true" />
          <span>{apiLabel}</span>
          <button className="text-button" type="button" onClick={api.retryNow}>
            <RefreshCw size={14} aria-hidden="true" /> {t("backend.retry")}
          </button>
          <button className="text-button" type="button" onClick={() => void restart()}>
            <Power size={14} aria-hidden="true" /> {t("backend.restart")}
          </button>
        </div>
      )}

      {showInitialGate ? (
        <section className="lifecycle-gate" aria-live="polite">
          <span className="lifecycle-icon" data-state={state.phase} aria-hidden="true">
            {showRestart ? <WifiOff size={27} /> : <RefreshCw size={27} className="spin" />}
          </span>
          <h1>{lifecycleReady ? apiLabel : lifecycleLabel}</h1>
          {(state.detail || api.detail) && (
            <p data-testid="backend-detail">{state.detail || api.detail}</p>
          )}
          {state.phase === "ready" && (
            <dl data-testid="backend-versions">
              <dt>{t("backend.backend_version")}</dt>
              <dd>{state.backendVersion ?? "-"}</dd>
              <dt>{t("backend.api_version")}</dt>
              <dd>{state.apiVersion ?? "-"}</dd>
            </dl>
          )}
          {showRestart && (
            <button
              className="primary-button"
              type="button"
              data-testid="restart-button"
              onClick={() => void restart()}
            >
              <RotateCcw size={16} aria-hidden="true" /> {t("backend.restart")}
            </button>
          )}
          {lifecycleReady && api.phase === "disconnected" && (
            <button className="primary-button" type="button" onClick={api.retryNow}>
              <RefreshCw size={16} aria-hidden="true" /> {t("backend.retry")}
            </button>
          )}
        </section>
      ) : api.transport ? (
        <Workspace
          transport={api.transport}
          connectionPhase={api.phase}
          connectionRevision={api.connectionRevision}
        />
      ) : null}
    </main>
  );
}

export function App(props: AppProps) {
  return (
    <I18nProvider>
      <AppContent {...props} />
    </I18nProvider>
  );
}

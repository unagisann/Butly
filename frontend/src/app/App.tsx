// Phase 1 の app shell。最初の画面 = backend lifecycle の状態表示そのもの。
// Chat / 設定 / サイドバーはまだ作らない（Phase 2 以降）。

import { useEffect, useMemo, useState } from "react";

import { getHealth, getReadiness } from "../api/generated";
import { createApiClient } from "../api/client";
import type { LifecycleBridge } from "../lifecycle/bridge";
import { useBackendState } from "../lifecycle/useBackendState";
import { t } from "../i18n/strings";

interface AppProps {
  bridge: LifecycleBridge;
}

interface VerifiedHealth {
  backendVersion: string;
  apiVersion: string;
}

export function App({ bridge }: AppProps) {
  const { state, restart } = useBackendState(bridge);
  const [health, setHealth] = useState<VerifiedHealth | null>(null);

  // ready になったら webview からも health を 1 回叩き、
  // WebView → loopback → token の経路が実際に通ることを確認する。
  useEffect(() => {
    if (state.phase !== "ready") {
      setHealth(null);
      return;
    }
    let disposed = false;
    (async () => {
      const info = await bridge.getConnectionInfo();
      if (!info) return;
      const client = createApiClient(info);
      const [healthResult, readinessResult] = await Promise.all([
        getHealth({ client }),
        getReadiness({ client }),
      ]);
      if (
        !disposed &&
        healthResult.data &&
        readinessResult.data?.ready
      ) {
        setHealth({
          backendVersion: healthResult.data.backend_version,
          apiVersion: healthResult.data.api_version,
        });
      }
    })().catch(() => {
      // health 疎通失敗は Rust 側の監視が unavailable / crashed を emit する
    });
    return () => {
      disposed = true;
    };
  }, [state.phase, bridge]);

  const statusLabel = useMemo(() => {
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
  }, [state.phase]);

  const showRestart =
    state.phase === "crashed" ||
    state.phase === "unavailable" ||
    state.phase === "version_mismatch";

  const backendVersion = health?.backendVersion ?? state.backendVersion;
  const apiVersion = health?.apiVersion ?? state.apiVersion;

  return (
    <main className="shell">
      <h1>{t("app.title")}</h1>
      <section aria-live="polite">
        <p data-testid="backend-status" data-phase={state.phase}>
          {statusLabel}
        </p>
        {state.phase === "ready" && (
          <dl data-testid="backend-versions">
            <dt>{t("backend.backend_version")}</dt>
            <dd>{backendVersion ?? "-"}</dd>
            <dt>{t("backend.api_version")}</dt>
            <dd>{apiVersion ?? "-"}</dd>
          </dl>
        )}
        {state.detail && state.phase !== "ready" && (
          <p data-testid="backend-detail">{state.detail}</p>
        )}
        {showRestart && (
          <button
            type="button"
            data-testid="restart-button"
            onClick={() => {
              void restart();
            }}
          >
            {t("backend.restart")}
          </button>
        )}
      </section>
    </main>
  );
}

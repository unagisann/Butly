import { useEffect, useState } from "react";
import { Bot, Clock3, RefreshCw, Settings2, WifiOff } from "lucide-react";

import type { InstanceSummary } from "../../api/generated";
import type { ApiTransport } from "../../api/transport";
import type { ApiConnectionPhase } from "../../app/useApiConnection";
import { useI18n } from "../../i18n/strings";
import { localizeReason } from "../../i18n/reasons";
import { ChatComposer } from "./ChatComposer";
import { DebugPanel } from "./DebugPanel";
import { TraceGraph } from "./TraceGraph";
import { MessageList } from "./MessageList";
import { useCapabilities } from "./useCapabilities";
import { useChatSession } from "./useChatSession";
import { MemoryRetrievalSettingsDialog } from "../settings/MemoryRetrievalSettingsDialog";

interface ChatPageProps {
  transport: ApiTransport;
  instance: InstanceSummary;
  connectionPhase: ApiConnectionPhase;
  connectionRevision: number;
  onBusyChange: (busy: boolean) => void;
}

function formatLastInteraction(value: string | null, locale: "ja" | "en"): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return new Intl.DateTimeFormat(locale, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function localizedError(code: string, fallback: string, t: ReturnType<typeof useI18n>["t"]): string {
  if (code === "backend_not_ready") return t("error.backend_not_ready");
  if (code === "instance_not_found") return t("error.instance_not_found");
  if (code === "generation_failed") return t("error.generation_failed");
  if (code === "protocol_error") return t("error.protocol");
  if (code === "network_error") return t("error.network");
  if (code === "request_status_timeout") return t("error.request_status_timeout");
  if (code === "debug_not_available") return t("error.debug_not_available");
  return fallback || t("error.unknown");
}

export function ChatPage({
  transport,
  instance,
  connectionPhase,
  connectionRevision,
  onBusyChange,
}: ChatPageProps) {
  const { locale, t } = useI18n();
  const {
    capabilities,
    error: capabilitiesError,
    loading: capabilitiesLoading,
    refresh: refreshCapabilities,
  } = useCapabilities(
    transport,
    connectionRevision,
  );
  const session = useChatSession({
    transport,
    instanceName: instance.name,
    connectionPhase,
    connectionRevision,
  });
  const [debugEnabled, setDebugEnabled] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const lastInteraction = formatLastInteraction(session.lastInteractionAt, locale);
  const connected = connectionPhase === "connected";
  const debugAvailable = capabilities?.chat_debug.available ?? false;
  const chatUsable = Boolean(
    capabilities?.chat.available &&
      capabilities.streaming.available &&
      capabilities.streaming.mode !== "unsupported",
  );

  useEffect(() => onBusyChange(session.busy), [onBusyChange, session.busy]);
  useEffect(() => () => onBusyChange(false), [onBusyChange]);
  useEffect(() => {
    if (!debugAvailable) setDebugEnabled(false);
  }, [debugAvailable]);

  return (
    <section className="chat-page">
      <header className="chat-header">
        <div className="chat-identity">
          <span className="chat-avatar" aria-hidden="true">
            <Bot size={20} />
          </span>
          <div>
            <h1>{instance.ai_name || instance.name}</h1>
            <p>
              {lastInteraction ? (
                <>
                  <Clock3 size={13} aria-hidden="true" />
                  {t("chat.last_interaction", { value: lastInteraction })}
                </>
              ) : (
                instance.name
              )}
            </p>
          </div>
        </div>
        <div className="chat-header-actions">
          <span className={`connection-pill ${connected ? "connected" : "offline"}`}>
            {connected ? null : <WifiOff size={13} aria-hidden="true" />}
            {connected ? t("backend.ready") : t("backend.reconnecting")}
          </span>
          <button
            className="icon-button"
            type="button"
            onClick={() => setSettingsOpen(true)}
            disabled={session.busy || !connected}
            aria-label={t("settings.open")}
            title={t("settings.open")}
          >
            <Settings2 size={17} aria-hidden="true" />
          </button>
          <button
            className="icon-button"
            type="button"
            onClick={() => void session.reloadHistory()}
            disabled={session.busy || !connected}
            aria-label={t("chat.reload")}
            title={t("chat.reload")}
          >
            <RefreshCw size={17} aria-hidden="true" />
          </button>
        </div>
      </header>

      <div className="chat-notices">
        {session.historyError && (
          <div className="error-banner" role="alert">
            <span className="error-copy">
              <span>
                {t("chat.history_error")}: {localizedError(
                  session.historyError.code,
                  session.historyError.message,
                  t,
                )}
              </span>
              <small>
                {t("error.code")}: {session.historyError.code}
                {session.historyError.requestId
                  ? ` · ${t("error.request_id")}: ${session.historyError.requestId}`
                  : ""}
              </small>
            </span>
            <button
              className="text-button"
              type="button"
              onClick={() => void session.reloadHistory()}
            >
              {t("chat.reload")}
            </button>
          </div>
        )}
        {session.sendError && (
          <div className="error-banner" role="alert">
            <span className="error-copy">
              <span>
                {localizedError(
                  session.sendError.code,
                  session.sendError.message,
                  t,
                )}
              </span>
              <small>
                {t("error.code")}: {session.sendError.code}
                {session.sendError.requestId
                  ? ` · ${t("error.request_id")}: ${session.sendError.requestId}`
                  : ""}
              </small>
            </span>
          </div>
        )}
        {capabilitiesError && (
          <div className="error-banner" role="alert">
            <span>{capabilitiesError}</span>
            <button className="text-button" type="button" onClick={refreshCapabilities}>
              {t("capability.retry")}
            </button>
          </div>
        )}
        {capabilities && !capabilities.chat.available && (
          <div className="error-banner" role="alert">
            {t("capability.unavailable", {
              reason:
                localizeReason(capabilities.chat.reason, t) || t("reason.unknown"),
            })}
          </div>
        )}
        {capabilities && capabilities.chat.available && !chatUsable && (
          <div className="error-banner" role="alert">
            {t("capability.streaming_unavailable", {
              reason:
                localizeReason(capabilities.streaming.reason, t) ||
                t("reason.unknown"),
            })}
          </div>
        )}
      </div>

      <div className="conversation-scroll">
        <MessageList
          messages={session.messages}
          phase={session.phase}
          loading={session.phase === "loading_history"}
          assistantName={instance.ai_name || instance.name}
          userName={instance.user_display_name}
          canRetry={session.canRetry}
          onRetry={() => void session.retry()}
        />
      </div>

      {debugEnabled && (
        <DebugPanel
          debug={session.debug}
          trace={
            <TraceGraph transport={transport} instanceName={instance.name} />
          }
        />
      )}
      {capabilities ? (
        <ChatComposer
          capabilities={capabilities}
          disabled={
            !connected ||
            !chatUsable ||
            session.phase === "loading_history" ||
            capabilitiesLoading ||
            capabilitiesError !== null
          }
          busy={session.busy}
          canCancel={session.busy && session.phase !== "finalizing"}
          debugEnabled={debugEnabled}
          debugAvailable={debugAvailable}
          onDebugEnabledChange={setDebugEnabled}
          onSend={session.send}
          onCancel={session.cancel}
        />
      ) : (
        <div className="composer-skeleton" aria-label={t("backend.connecting")} />
      )}
      {settingsOpen && (
        <MemoryRetrievalSettingsDialog
          transport={transport}
          instanceName={instance.name}
          onClose={() => setSettingsOpen(false)}
        />
      )}
    </section>
  );
}

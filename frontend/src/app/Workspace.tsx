import { useCallback, useState } from "react";
import { Bot } from "lucide-react";

import type { ApiTransport } from "../api/transport";
import type { ApiConnectionPhase } from "./useApiConnection";
import { ChatPage } from "../features/chat/ChatPage";
import { InstanceSidebar } from "../features/instances/InstanceSidebar";
import { useInstances } from "../features/instances/useInstances";
import { PreflightPanel } from "../features/preflight/PreflightPanel";
import { usePreflight } from "../features/preflight/usePreflight";
import { useI18n } from "../i18n/strings";

interface WorkspaceProps {
  transport: ApiTransport;
  connectionPhase: ApiConnectionPhase;
  connectionRevision: number;
}

export function Workspace({
  transport,
  connectionPhase,
  connectionRevision,
}: WorkspaceProps) {
  const { t } = useI18n();
  const instances = useInstances({ transport, connectionRevision });
  const preflight = usePreflight({ transport, connectionRevision });
  const [chatBusy, setChatBusy] = useState(false);
  const onBusyChange = useCallback((busy: boolean) => setChatBusy(busy), []);
  const selected = instances.items.find((item) => item.name === instances.selectedName) ?? null;

  return (
    <div className="workspace">
      <div className="sidebar-column">
        <InstanceSidebar
          items={instances.items}
          selectedName={instances.selectedName}
          loading={instances.loading}
          error={instances.error}
          disabled={chatBusy}
          onSelect={instances.select}
          onRefresh={instances.refresh}
        />
        <PreflightPanel
          report={preflight.report}
          loading={preflight.loading}
          error={preflight.error}
          onRefresh={preflight.refresh}
        />
      </div>
      {selected ? (
        <ChatPage
          key={selected.name}
          transport={transport}
          instance={selected}
          connectionPhase={connectionPhase}
          connectionRevision={connectionRevision}
          onBusyChange={onBusyChange}
        />
      ) : (
        <section className="no-instance">
          <span aria-hidden="true"><Bot size={30} /></span>
          <h1>{t("instances.empty")}</h1>
          <p>{t("instances.empty_hint")}</p>
        </section>
      )}
    </div>
  );
}

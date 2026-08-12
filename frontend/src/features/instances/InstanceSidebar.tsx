import { Bot, Database, RefreshCw } from "lucide-react";

import type { InstanceSummary } from "../../api/generated";
import { useI18n } from "../../i18n/strings";

interface InstanceSidebarProps {
  items: InstanceSummary[];
  selectedName: string | null;
  loading: boolean;
  error: string | null;
  disabled: boolean;
  onSelect: (name: string) => void;
  onRefresh: () => void;
}

function formatUpdated(value: string | null | undefined, locale: "ja" | "en"): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return new Intl.DateTimeFormat(locale, {
    month: "short",
    day: "numeric",
  }).format(date);
}

export function InstanceSidebar({
  items,
  selectedName,
  loading,
  error,
  disabled,
  onSelect,
  onRefresh,
}: InstanceSidebarProps) {
  const { locale, t } = useI18n();
  return (
    <aside className="instance-sidebar" aria-label={t("instances.title")}>
      <div className="sidebar-heading">
        <h2>{t("instances.title")}</h2>
        <button
          className="icon-button"
          type="button"
          onClick={onRefresh}
          aria-label={t("instances.refresh")}
          title={t("instances.refresh")}
          disabled={loading}
        >
          <RefreshCw size={17} aria-hidden="true" className={loading ? "spin" : ""} />
        </button>
      </div>

      {loading && items.length === 0 && (
        <p className="muted sidebar-message">{t("instances.loading")}</p>
      )}
      {error && <p className="inline-error sidebar-message">{error}</p>}
      {!loading && items.length === 0 && (
        <div className="sidebar-empty">
          <Bot size={28} aria-hidden="true" />
          <strong>{t("instances.empty")}</strong>
          <span>{t("instances.empty_hint")}</span>
        </div>
      )}

      <nav className="instance-list">
        {items.map((instance) => {
          const selected = instance.name === selectedName;
          const updated = formatUpdated(instance.updated_at, locale);
          return (
            <button
              key={instance.name}
              type="button"
              className="instance-item"
              data-selected={selected}
              aria-current={selected ? "page" : undefined}
              aria-label={t("instances.select", { name: instance.name })}
              onClick={() => onSelect(instance.name)}
              disabled={disabled && !selected}
            >
              <span className="instance-avatar" aria-hidden="true">
                <Bot size={18} />
              </span>
              <span className="instance-copy">
                <strong>{instance.ai_name || instance.name}</strong>
                <small>
                  {instance.locale?.toUpperCase() || instance.name}
                  {updated ? ` · ${updated}` : ""}
                </small>
              </span>
              {instance.has_database && (
                <Database className="instance-database" size={14} aria-hidden="true" />
              )}
            </button>
          );
        })}
      </nav>
    </aside>
  );
}

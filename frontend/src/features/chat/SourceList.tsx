import { Copy, Link2 } from "lucide-react";

import type { CitationSource } from "../../api/generated";
import { useI18n } from "../../i18n/strings";

export function safeExternalUrl(value: string | undefined): string | null {
  if (!value || value.length > 2_048) return null;
  try {
    const url = new URL(value);
    if (url.username || url.password) return null;
    return url.protocol === "http:" || url.protocol === "https:"
      ? url.toString()
      : null;
  } catch {
    return null;
  }
}

export function SourceList({ sources }: { sources: CitationSource[] }) {
  const { t } = useI18n();
  const safe = sources.flatMap((source) => {
    const url = safeExternalUrl(source.url);
    return url ? [{ source, url }] : [];
  });
  if (safe.length === 0) return null;

  return (
    <div className="source-list" aria-label={t("chat.sources")}>
      <div className="source-heading">
        <Link2 size={14} aria-hidden="true" />
        <span>{t("chat.sources")}</span>
      </div>
      <div className="source-chips">
        {safe.map(({ source, url }, index) => (
          <button
            key={`${url}-${index}`}
            className="source-chip"
            type="button"
            title={url}
            aria-label={`${t("chat.copy_source")}: ${source.title || new URL(url).hostname}`}
            onClick={() => {
              void navigator.clipboard?.writeText(url).catch((error: unknown) => {
                console.warn("[frontend] failed to copy source URL", error);
              });
            }}
          >
            <span>{source.title || new URL(url).hostname}</span>
            <Copy size={12} aria-hidden="true" />
          </button>
        ))}
      </div>
    </div>
  );
}

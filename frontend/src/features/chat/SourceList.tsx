import { Copy, ExternalLink as ExternalLinkIcon, Link2 } from "lucide-react";

import type { CitationSource } from "../../api/generated";
import { useI18n } from "../../i18n/strings";
import { openExternal } from "./external";

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
          <span className="source-chip" key={`${url}-${index}`}>
            {/* 既定の操作は「開く」。href を持たせて中クリックや右クリックの
                ブラウザ標準操作も使えるようにし、遷移自体は外部へ委ねる。 */}
            <a
              className="source-open"
              href={url}
              target="_blank"
              rel="noopener noreferrer"
              title={url}
              onClick={(event) => {
                event.preventDefault();
                void openExternal(url);
              }}
            >
              <span>{source.title || new URL(url).hostname}</span>
              <ExternalLinkIcon size={12} aria-hidden="true" />
            </a>
            <button
              className="source-copy"
              type="button"
              aria-label={`${t("chat.copy_source")}: ${source.title || new URL(url).hostname}`}
              title={t("chat.copy_source")}
              onClick={() => {
                void navigator.clipboard?.writeText(url).catch((error: unknown) => {
                  console.warn("[frontend] failed to copy source URL", error);
                });
              }}
            >
              <Copy size={12} aria-hidden="true" />
            </button>
          </span>
        ))}
      </div>
    </div>
  );
}

import { useEffect, useRef } from "react";
import { Bot, CircleStop, Image as ImageIcon, UserRound } from "lucide-react";

import { useI18n } from "../../i18n/strings";
import { SourceList } from "./SourceList";
import type { ChatMessageView, ChatPhase } from "./types";

interface MessageListProps {
  messages: ChatMessageView[];
  phase: ChatPhase;
  loading: boolean;
}

function formatTime(value: string | null | undefined, locale: "ja" | "en"): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat(locale, {
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function formatAttachmentSize(
  value: number | null | undefined,
  locale: "ja" | "en",
): string | null {
  if (value == null || value < 0) return null;
  const unit = value >= 1024 * 1024 ? "MB" : "KB";
  const divisor = unit === "MB" ? 1024 * 1024 : 1024;
  return `${new Intl.NumberFormat(locale, { maximumFractionDigits: 1 }).format(
    value / divisor,
  )} ${unit}`;
}

export function MessageList({ messages, phase, loading }: MessageListProps) {
  const { locale, t } = useI18n();
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView?.({ block: "end", behavior: "smooth" });
  }, [messages]);

  if (loading) {
    return (
      <div className="conversation-state" role="status">
        <span className="loading-orb" aria-hidden="true" />
        <p>{t("chat.history_loading")}</p>
      </div>
    );
  }

  if (messages.length === 0) {
    return (
      <div className="conversation-state empty-chat">
        <span className="empty-chat-icon" aria-hidden="true">
          <Bot size={28} />
        </span>
        <h2>{t("chat.history_empty")}</h2>
        <p>{t("chat.history_empty_hint")}</p>
      </div>
    );
  }

  const liveLabel =
    phase === "waiting_metadata"
      ? t("chat.waiting")
      : phase === "streaming"
        ? t("chat.streaming")
        : "";

  return (
    <>
      <div
        className="message-list"
        role="region"
        aria-label={t("chat.title")}
        aria-live="off"
      >
        {messages.map((message) => {
          const assistant = message.role === "assistant";
          const showTyping =
            assistant && message.delivery === "sending" && !message.text;
          const summaryOnlyAttachments = (message.attachments ?? []).filter(
            (_attachment, index) => !message.attachmentPreviews?.[index],
          );
          return (
            <article
              key={message.id}
              className={`message-row ${assistant ? "assistant" : "user"}`}
              data-delivery={message.delivery}
            >
              <span className="message-avatar" aria-hidden="true">
                {assistant ? <Bot size={17} /> : <UserRound size={17} />}
              </span>
              <div className="message-content">
                <header>
                  <strong>{assistant ? t("chat.assistant") : t("chat.you")}</strong>
                  <time dateTime={message.created_at ?? undefined}>
                    {formatTime(message.created_at, locale)}
                  </time>
                </header>
                {(message.attachmentPreviews?.length ?? 0) > 0 && (
                  <div className="message-images">
                    {message.attachmentPreviews?.map((preview, index) => (
                      <img
                        key={`${message.id}-image-${index}`}
                        src={preview}
                        alt={message.attachments?.[index]?.name || ""}
                      />
                    ))}
                  </div>
                )}
                {summaryOnlyAttachments.length > 0 && (
                  <div className="message-attachment-summaries">
                    {summaryOnlyAttachments.map((attachment, index) => {
                      const size = formatAttachmentSize(attachment.size_bytes, locale);
                      return (
                        <span
                          className="message-attachment-chip"
                          key={`${attachment.name || attachment.mime_type}-${index}`}
                        >
                          <ImageIcon size={13} aria-hidden="true" />
                          <span>
                            {attachment.name || t("chat.image_attachment")}
                            {size ? ` · ${size}` : ""}
                          </span>
                        </span>
                      );
                    })}
                  </div>
                )}
                {showTyping ? (
                  <span className="typing-dots" aria-label={t("chat.waiting")}>
                    <i /> <i /> <i />
                  </span>
                ) : (
                  <p className="message-text">{message.text}</p>
                )}
                {message.delivery === "cancelled" && (
                  <p className="message-state">
                    <CircleStop size={13} aria-hidden="true" /> {t("chat.cancelled")}
                  </p>
                )}
                {message.delivery === "disconnected" && (
                  <p className="message-state error">{t("chat.disconnected")}</p>
                )}
                {message.delivery === "failed" && (
                  <p className="message-state error">{t("chat.failed")}</p>
                )}
                {assistant && <SourceList sources={message.sources ?? []} />}
              </div>
            </article>
          );
        })}
        <div ref={endRef} />
      </div>
      <div className="sr-only" role="status" aria-live="polite" aria-atomic="true">
        {liveLabel}
      </div>
    </>
  );
}

import { useRef, useState } from "react";
import type { ChangeEvent, KeyboardEvent } from "react";
import {
  Bug,
  ImagePlus,
  Search,
  Send,
  Square,
  X,
} from "lucide-react";

import type { CapabilitiesResponse } from "../../api/generated";
import { useI18n } from "../../i18n/strings";
import { prepareAttachments } from "./attachments";
import type { AttachmentPreparationError } from "./attachments";
import type { PreparedAttachment, SendChatInput } from "./types";

interface ChatComposerProps {
  capabilities: CapabilitiesResponse;
  disabled: boolean;
  busy: boolean;
  canCancel: boolean;
  debugEnabled: boolean;
  debugAvailable: boolean;
  onDebugEnabledChange: (enabled: boolean) => void;
  onSend: (input: SendChatInput) => Promise<void>;
  onCancel: () => void;
}

function errorText(
  error: AttachmentPreparationError,
  t: ReturnType<typeof useI18n>["t"],
): string {
  if (error.code === "attachment_limit") {
    return t("chat.attachment_limit", { count: error.count });
  }
  if (error.code === "attachment_size") {
    return t("chat.attachment_size", {
      name: error.name,
      size: Math.round((error.maxSizeBytes / 1024 / 1024) * 10) / 10,
    });
  }
  if (error.code === "attachment_type") {
    return t("chat.attachment_type", { name: error.name });
  }
  return t("chat.attachment_read");
}

export function ChatComposer({
  capabilities,
  disabled,
  busy,
  canCancel,
  debugEnabled,
  debugAvailable,
  onDebugEnabledChange,
  onSend,
  onCancel,
}: ChatComposerProps) {
  const { t } = useI18n();
  const [text, setText] = useState("");
  const [attachments, setAttachments] = useState<PreparedAttachment[]>([]);
  const [attachmentError, setAttachmentError] = useState<string | null>(null);
  const [useRag, setUseRag] = useState(true);
  const [useGoogleSearch, setUseGoogleSearch] = useState(false);
  const [useWebSearch, setUseWebSearch] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const canSend =
    !disabled && !busy && (text.trim().length > 0 || attachments.length > 0);

  const submit = () => {
    if (!canSend) return;
    const input: SendChatInput = {
      text,
      attachments,
      useRag,
      useGoogleSearch,
      useWebSearch,
      includeDebug: debugEnabled,
    };
    setText("");
    setAttachments([]);
    setAttachmentError(null);
    void onSend(input);
  };

  const onFiles = async (event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files ?? []);
    event.target.value = "";
    const prepared = await prepareAttachments(
      files,
      attachments.length,
      capabilities.attachments,
    );
    setAttachments((current) => [...current, ...prepared.attachments]);
    setAttachmentError(prepared.errors[0] ? errorText(prepared.errors[0], t) : null);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
  };

  return (
    <div className="composer-wrap">
      {attachments.length > 0 && (
        <div className="attachment-tray">
          {attachments.map((attachment, index) => (
            <div className="attachment-preview" key={`${attachment.input.name}-${index}`}>
              <img src={attachment.previewUrl} alt="" />
              <button
                type="button"
                onClick={() =>
                  setAttachments((current) => current.filter((_, itemIndex) => itemIndex !== index))
                }
                aria-label={t("chat.remove_attachment", {
                  name: attachment.input.name || `image ${index + 1}`,
                })}
              >
                <X size={13} aria-hidden="true" />
              </button>
            </div>
          ))}
        </div>
      )}
      {attachmentError && <p className="inline-error composer-error">{attachmentError}</p>}

      <div className="composer-options">
        <label className="toggle-chip">
          <input
            type="checkbox"
            checked={useRag}
            onChange={(event) => setUseRag(event.target.checked)}
          />
          <Search size={14} aria-hidden="true" /> {t("chat.use_rag")}
        </label>
        {capabilities.native_google_search.available && (
          <label className="toggle-chip">
            <input
              type="checkbox"
              checked={useGoogleSearch}
              onChange={(event) => {
                setUseGoogleSearch(event.target.checked);
                if (event.target.checked) setUseWebSearch(false);
              }}
            />
            {t("chat.search_google")}
          </label>
        )}
        {capabilities.generic_web_search.available && (
          <label className="toggle-chip">
            <input
              type="checkbox"
              checked={useWebSearch}
              onChange={(event) => {
                setUseWebSearch(event.target.checked);
                if (event.target.checked) setUseGoogleSearch(false);
              }}
            />
            {t("chat.search_web")}
          </label>
        )}
        {debugAvailable && (
          <label className="toggle-chip debug-toggle">
            <input
              type="checkbox"
              checked={debugEnabled}
              onChange={(event) => onDebugEnabledChange(event.target.checked)}
            />
            <Bug size={14} aria-hidden="true" /> {t("debug.enable")}
          </label>
        )}
      </div>

      {(useGoogleSearch || capabilities.streaming.mode === "buffered_fallback") && (
        <p className="composer-note">{t("chat.buffered")}</p>
      )}
      {!capabilities.vision.available && (
        <p className="composer-note">{t("chat.vision_unavailable")}</p>
      )}

      <div className="composer">
        <input
          ref={fileInputRef}
          className="sr-only"
          type="file"
          accept={capabilities.attachments.allowed_mime_types.join(",")}
          multiple
          onChange={(event) => void onFiles(event)}
          tabIndex={-1}
        />
        <button
          className="composer-icon-button"
          type="button"
          onClick={() => fileInputRef.current?.click()}
          disabled={disabled || busy || !capabilities.vision.available}
          aria-label={t("chat.attach")}
          title={t("chat.attach")}
        >
          <ImagePlus size={19} aria-hidden="true" />
        </button>
        <textarea
          value={text}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={onKeyDown}
          placeholder={t("chat.placeholder")}
          aria-label={t("chat.placeholder")}
          disabled={disabled}
          rows={1}
        />
        {busy ? (
          <button
            className="send-button stop"
            type="button"
            onClick={onCancel}
            disabled={!canCancel}
          >
            <Square size={16} fill="currentColor" aria-hidden="true" />
            <span>{canCancel ? t("chat.stop") : t("chat.finishing")}</span>
          </button>
        ) : (
          <button className="send-button" type="button" onClick={submit} disabled={!canSend}>
            <Send size={17} aria-hidden="true" />
            <span>{t("chat.send")}</span>
          </button>
        )}
      </div>
      <div className="composer-footer">
        <span>{t("chat.keyboard_hint")}</span>
      </div>
    </div>
  );
}

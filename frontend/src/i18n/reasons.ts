import type { StringKey } from "./strings";

type Translator = (key: StringKey, params?: Record<string, string | number>) => string;

const REASON_KEYS: Record<string, StringKey> = {
  api_key_not_configured: "reason.api_key_not_configured",
  base_url_not_configured: "reason.base_url_not_configured",
  authentication_failed: "reason.authentication_failed",
  models_endpoint_not_found: "reason.models_endpoint_not_found",
  provider_http_error: "reason.provider_http_error",
  connection_timeout: "reason.connection_timeout",
  connection_unreachable: "reason.connection_unreachable",
  invalid_provider_response: "reason.invalid_provider_response",
  unsupported_protocol: "reason.unsupported_protocol",
  embedding_connection_not_found: "reason.embedding_connection_not_found",
  embedding_model_not_configured: "reason.embedding_model_not_configured",
  embedding_not_supported: "reason.embedding_not_supported",
  invalid_embedding_response: "reason.invalid_embedding_response",
  developer_mode_disabled: "reason.developer_mode_disabled",
  runtime_not_initialized: "reason.runtime_not_initialized",
  active_connection_not_configured: "reason.active_connection_not_configured",
  desktop_token_required: "reason.desktop_token_required",
  active_model_does_not_support_vision:
    "reason.active_model_does_not_support_vision",
  active_connection_does_not_support_google_search:
    "reason.active_connection_does_not_support_google_search",
  web_search_api_key_not_configured:
    "reason.web_search_api_key_not_configured",
  embedding_model_not_available: "reason.embedding_model_not_available",
  embedding_model_not_confirmed: "reason.embedding_model_not_confirmed",
  chat_model_not_available: "reason.chat_model_not_available",
  chat_model_not_confirmed: "reason.chat_model_not_confirmed",
};

export function localizeReason(reason: string | null | undefined, t: Translator): string {
  if (!reason) return "";
  return t(REASON_KEYS[reason] ?? "reason.unknown");
}

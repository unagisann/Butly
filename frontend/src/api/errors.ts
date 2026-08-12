import type { ApiError } from "./generated";

export class ButlyApiError extends Error {
  readonly code: string;
  readonly details: Record<string, unknown>;
  readonly requestId: string | null;
  readonly status: number | null;
  readonly recoverable: boolean;

  constructor(
    code: string,
    message: string,
    options: {
      details?: Record<string, unknown>;
      requestId?: string | null;
      status?: number | null;
      recoverable?: boolean;
      cause?: unknown;
    } = {},
  ) {
    super(message, { cause: options.cause });
    this.name = "ButlyApiError";
    this.code = code;
    this.details = options.details ?? {};
    this.requestId = options.requestId ?? null;
    this.status = options.status ?? null;
    this.recoverable = options.recoverable ?? false;
  }
}

export class SseProtocolError extends ButlyApiError {
  constructor(message: string, details: Record<string, unknown> = {}) {
    super("protocol_error", message, { details });
    this.name = "SseProtocolError";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function isApiError(value: unknown): value is ApiError {
  return (
    isRecord(value) &&
    typeof value.code === "string" &&
    typeof value.message === "string" &&
    isRecord(value.details) &&
    (typeof value.request_id === "string" || value.request_id === null)
  );
}

export function normalizeApiError(
  value: unknown,
  options: { status?: number | null; fallbackCode?: string; fallbackMessage?: string } = {},
): ButlyApiError {
  if (value instanceof ButlyApiError) return value;
  if (isApiError(value)) {
    return new ButlyApiError(value.code, value.message, {
      details: value.details,
      requestId: value.request_id,
      status: options.status,
    });
  }
  if (value instanceof Error) {
    return new ButlyApiError(
      options.fallbackCode ?? "network_error",
      value.message || options.fallbackMessage || "Network request failed.",
      { status: options.status, cause: value, recoverable: true },
    );
  }
  return new ButlyApiError(
    options.fallbackCode ?? "unknown_error",
    options.fallbackMessage ?? "An unexpected error occurred.",
    { status: options.status, details: isRecord(value) ? value : {} },
  );
}

export function isAbortError(value: unknown): boolean {
  return (
    (value instanceof DOMException && value.name === "AbortError") ||
    (value instanceof Error && value.name === "AbortError")
  );
}

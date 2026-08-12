import {
  cancelChatRequest,
  getCapabilities,
  getChatRequestStatus,
  getPreflight,
  getReadiness,
  listInstanceMessages,
  listInstances,
} from "./generated";
import type {
  CapabilitiesResponse,
  ChatDoneEvent,
  ChatRequest,
  ChatRequestStatus,
  InstanceSummary,
  MessagePage,
  PreflightResponse,
  ReadinessResponse,
} from "./generated";
import { createApiClient } from "./client";
import { ButlyApiError, normalizeApiError } from "./errors";
import { parseChatSse } from "./sse";
import type { ChatStreamHandlers } from "./sse";
import type { ConnectionInfo } from "../lifecycle/types";

export interface ApiTransport {
  ping(signal?: AbortSignal): Promise<ReadinessResponse>;
  listInstances(signal?: AbortSignal): Promise<InstanceSummary[]>;
  getMessages(instanceName: string, signal?: AbortSignal): Promise<MessagePage>;
  getCapabilities(signal?: AbortSignal): Promise<CapabilitiesResponse>;
  getPreflight(signal?: AbortSignal, refresh?: boolean): Promise<PreflightResponse>;
  getChatRequest(requestId: string, signal?: AbortSignal): Promise<ChatRequestStatus>;
  streamChat(
    request: ChatRequest,
    handlers: ChatStreamHandlers,
    signal?: AbortSignal,
  ): Promise<ChatDoneEvent>;
  cancelChat(
    requestId: string,
    signal?: AbortSignal,
  ): Promise<ChatRequestStatus>;
}

export type ApiTransportFactory = (info: ConnectionInfo) => ApiTransport;

type FetchLike = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

interface ClientResult<T> {
  data?: T;
  error?: unknown;
  response: Response;
}

function unwrap<T>(result: ClientResult<T>, fallbackMessage: string): T {
  if (result.data !== undefined) return result.data;
  throw normalizeApiError(result.error, {
    status: result.response.status,
    fallbackMessage,
    fallbackCode: result.response.ok ? "invalid_response" : "http_error",
  });
}

async function responseError(response: Response): Promise<ButlyApiError> {
  let payload: unknown = null;
  try {
    payload = await response.json();
  } catch {
    // An empty/non-JSON error body is normalized below using the HTTP status.
  }
  return normalizeApiError(payload, {
    status: response.status,
    fallbackCode: `http_${response.status}`,
    fallbackMessage: `Backend request failed with HTTP ${response.status}.`,
  });
}

class HttpApiTransport implements ApiTransport {
  private readonly client;
  private readonly baseUrl: string;
  private readonly token: string | null;
  private readonly fetchImpl: FetchLike;

  constructor(info: ConnectionInfo, fetchImpl: FetchLike) {
    this.client = createApiClient(info);
    this.baseUrl = info.baseUrl.replace(/\/$/, "");
    this.token = info.token;
    this.fetchImpl = fetchImpl;
  }

  private headers(extra: HeadersInit = {}): Headers {
    const headers = new Headers(extra);
    if (this.token) headers.set("Authorization", `Bearer ${this.token}`);
    return headers;
  }

  async ping(signal?: AbortSignal): Promise<ReadinessResponse> {
    const result = await getReadiness({ client: this.client, signal });
    return unwrap(result, "Backend readiness response was empty.");
  }

  async listInstances(signal?: AbortSignal): Promise<InstanceSummary[]> {
    const result = await listInstances({ client: this.client, signal });
    return unwrap(result, "Instance list response was empty.").items ?? [];
  }

  async getMessages(instanceName: string, signal?: AbortSignal): Promise<MessagePage> {
    const result = await listInstanceMessages({
      client: this.client,
      path: { name: instanceName },
      query: { limit: 200 },
      signal,
    });
    return unwrap(result, "Message history response was empty.");
  }

  async getCapabilities(signal?: AbortSignal): Promise<CapabilitiesResponse> {
    const result = await getCapabilities({ client: this.client, signal });
    return unwrap(result, "Capabilities response was empty.");
  }

  async getPreflight(signal?: AbortSignal, refresh = false): Promise<PreflightResponse> {
    const result = await getPreflight({
      client: this.client,
      query: { refresh },
      signal,
    });
    return unwrap(result, "Preflight response was empty.");
  }

  async getChatRequest(requestId: string, signal?: AbortSignal): Promise<ChatRequestStatus> {
    const result = await getChatRequestStatus({
      client: this.client,
      path: { request_id: requestId },
      signal,
    });
    return unwrap(result, "Chat request status response was empty.");
  }

  async streamChat(
    request: ChatRequest,
    handlers: ChatStreamHandlers,
    signal?: AbortSignal,
  ): Promise<ChatDoneEvent> {
    const requestId =
      globalThis.crypto?.randomUUID?.() ??
      `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
    handlers.onRequestId?.(requestId, false);
    const response = await this.fetchImpl(`${this.baseUrl}/api/v1/chat/stream`, {
      method: "POST",
      headers: this.headers({
        Accept: "text/event-stream",
        "Content-Type": "application/json",
        "X-Request-ID": requestId,
      }),
      body: JSON.stringify(request),
      signal,
    });
    if (!response.ok) throw await responseError(response);
    const confirmedRequestId = response.headers.get("x-request-id") ?? requestId;
    handlers.onRequestId?.(confirmedRequestId, true);
    const contentType = response.headers.get("content-type") ?? "";
    if (!contentType.toLowerCase().includes("text/event-stream")) {
      throw new ButlyApiError("protocol_error", "Backend did not return text/event-stream.", {
        status: response.status,
      });
    }
    if (!response.body) {
      throw new ButlyApiError("protocol_error", "Backend returned an empty stream.");
    }
    return parseChatSse(response.body, handlers, confirmedRequestId);
  }

  async cancelChat(
    requestId: string,
    signal?: AbortSignal,
  ): Promise<ChatRequestStatus> {
    const result = await cancelChatRequest({
      client: this.client,
      path: { request_id: requestId },
      signal,
    });
    return unwrap(result, "Cancel response was empty.");
  }
}

export function createApiTransport(
  info: ConnectionInfo,
  fetchImpl: FetchLike = globalThis.fetch.bind(globalThis),
): ApiTransport {
  return new HttpApiTransport(info, fetchImpl);
}

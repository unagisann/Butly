import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ButlyApiError } from "../../api/errors";
import type {
  ChatDoneEvent,
  ChatRequestStatus,
  MessagePage,
  PreflightResponse,
} from "../../api/generated";
import type { ApiTransport } from "../../api/transport";
import type { ChatStreamHandlers } from "../../api/sse";
import type { SendChatInput } from "./types";
import { buildRetryRequest, useChatSession } from "./useChatSession";

const SEND_INPUT: SendChatInput = {
  text: "new question",
  attachments: [],
  useRag: true,
  useGoogleSearch: false,
  useWebSearch: false,
  includeDebug: true,
};

const EMPTY_PREFLIGHT: PreflightResponse = {
  status: "ready",
  checked_at: "2026-08-12T00:00:00Z",
  connections: [],
  embedding: {
    configured: true,
    reachable: true,
    status: "ready",
  },
};

function requestStatus(
  state: ChatRequestStatus["state"],
  requestId = "request-1",
): ChatRequestStatus {
  return {
    request_id: requestId,
    state,
    created_at: "2026-08-12T00:00:00Z",
  };
}

function makeTransport(overrides: Partial<ApiTransport> = {}): ApiTransport {
  return {
    ping: vi.fn(async () => ({ ready: true, checks: [] })),
    listInstances: vi.fn(async () => []),
    getMessages: vi.fn(async () => ({ items: [] })),
    getCapabilities: vi.fn(async () => ({
      attachments: {
        max_count: 3,
        max_size_bytes: 20 * 1024 * 1024,
        allowed_mime_types: ["image/png"],
      },
      chat: { available: true },
      chat_debug: { available: true },
      streaming: { available: true, mode: "incremental" as const },
      vision: { available: true },
      native_google_search: { available: true },
      generic_web_search: { available: true },
    })),
    getPreflight: vi.fn(async () => EMPTY_PREFLIGHT),
    getChatRequest: vi.fn(async () => requestStatus("running")),
    streamChat: vi.fn(async () => ({
      event: "done" as const,
      request_id: "request-1",
      data: { full_text: "" },
    })),
    cancelChat: vi.fn(async () => requestStatus("cancelled")),
    ...overrides,
  };
}

function renderSession(transport: ApiTransport) {
  return renderHook(() =>
    useChatSession({
      transport,
      instanceName: "assistant",
      connectionPhase: "connected",
      connectionRevision: 1,
    }),
  );
}

describe("useChatSession", () => {
  it("reconciles initial history, an optimistic turn, multi-chunk done, and stored history", async () => {
    const initial: MessagePage = {
      items: [
        {
          id: "old-assistant",
          role: "assistant",
          text: "old answer",
          created_at: "2026-08-11T00:00:00Z",
        },
      ],
    };
    const stored: MessagePage = {
      items: [
        ...initial.items,
        {
          id: "stored-user",
          role: "user",
          text: "new question",
          created_at: "2026-08-12T00:00:00Z",
        },
        {
          id: "stored-assistant",
          role: "assistant",
          text: "multi chunk",
          created_at: "2026-08-12T00:00:01Z",
        },
      ],
      last_interaction_at: "2026-08-12T00:00:01Z",
    };
    const getMessages = vi
      .fn<ApiTransport["getMessages"]>()
      .mockResolvedValueOnce(initial)
      .mockResolvedValue(stored);
    const streamChat = vi.fn<ApiTransport["streamChat"]>(
      async (_request, handlers) => {
        handlers.onRequestId?.("request-1", true);
        handlers.onMetadata?.({
          event: "metadata",
          request_id: "request-1",
          data: {
            tier: "mid",
            debug: {
              gatekeeper: { tier: "mid", memory_probe_status: "hit" },
              rag: { enabled: true, candidate_count: 2, injected_count: 1 },
            },
          },
        });
        handlers.onChunk?.({
          event: "chunk",
          request_id: "request-1",
          sequence: 0,
          data: { text: "multi " },
        });
        handlers.onChunk?.({
          event: "chunk",
          request_id: "request-1",
          sequence: 1,
          data: { text: "chunk" },
        });
        const done: ChatDoneEvent = {
          event: "done",
          request_id: "request-1",
          data: {
            full_text: "multi chunk",
            sources: [{ title: "Source", url: "https://example.com" }],
          },
        };
        handlers.onDone?.(done);
        return done;
      },
    );
    const transport = makeTransport({ getMessages, streamChat });
    const { result } = renderSession(transport);
    await waitFor(() => expect(result.current.phase).toBe("idle"));

    await act(async () => result.current.send(SEND_INPUT));
    await waitFor(() => expect(getMessages).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(result.current.messages).toHaveLength(3));

    expect(result.current.messages.map((message) => message.id)).toEqual([
      "old-assistant",
      "stored-user",
      "stored-assistant",
    ]);
    expect(result.current.messages[2]?.sources).toEqual([
      { title: "Source", url: "https://example.com" },
    ]);
    expect(result.current.debug?.rag).toMatchObject({
      candidateCount: 2,
      injectedCount: 1,
    });
  });

  it("accepts a one-chunk buffered fallback as a complete response", async () => {
    const stored: MessagePage = {
      items: [
        { id: "u1", role: "user", text: "new question" },
        { id: "a1", role: "assistant", text: "buffered answer" },
      ],
    };
    const getMessages = vi
      .fn<ApiTransport["getMessages"]>()
      .mockResolvedValueOnce({ items: [] })
      .mockResolvedValue(stored);
    const streamChat = vi.fn<ApiTransport["streamChat"]>(async (_request, handlers) => {
      handlers.onRequestId?.("gemini-search", true);
      handlers.onMetadata?.({ event: "metadata", request_id: "gemini-search", data: {} });
      handlers.onChunk?.({
        event: "chunk",
        request_id: "gemini-search",
        sequence: 0,
        data: { text: "buffered answer" },
      });
      const done: ChatDoneEvent = {
        event: "done",
        request_id: "gemini-search",
        data: { full_text: "buffered answer" },
      };
      handlers.onDone?.(done);
      return done;
    });
    const { result } = renderSession(makeTransport({ getMessages, streamChat }));
    await waitFor(() => expect(result.current.phase).toBe("idle"));

    await act(async () =>
      result.current.send({ ...SEND_INPUT, useGoogleSearch: true, includeDebug: false }),
    );
    await waitFor(() => expect(result.current.messages).toHaveLength(2));
    expect(result.current.messages[1]?.text).toBe("buffered answer");
  });

  it("does not let a slow completed-history refresh erase the next optimistic turn", async () => {
    let resolveFirstHistory: ((page: MessagePage) => void) | null = null;
    let secondHandlers: ChatStreamHandlers | null = null;
    let resolveSecondStream: ((done: ChatDoneEvent) => void) | null = null;
    const finalHistory: MessagePage = {
      items: [
        { id: "u1", role: "user", text: "first question" },
        { id: "a1", role: "assistant", text: "first answer" },
        { id: "u2", role: "user", text: "second question" },
        { id: "a2", role: "assistant", text: "second answer" },
      ],
    };
    const getMessages = vi
      .fn<ApiTransport["getMessages"]>()
      .mockResolvedValueOnce({ items: [] })
      .mockImplementationOnce(
        async () =>
          new Promise<MessagePage>((resolve) => {
            resolveFirstHistory = resolve;
          }),
      )
      .mockResolvedValue(finalHistory);
    const streamChat = vi.fn<ApiTransport["streamChat"]>(
      async (request, handlers) => {
        const second = request.text === "second question";
        const requestId = second ? "request-second" : "request-first";
        handlers.onRequestId?.(requestId, true);
        handlers.onMetadata?.({ event: "metadata", request_id: requestId, data: {} });
        if (second) {
          secondHandlers = handlers;
          return new Promise<ChatDoneEvent>((resolve) => {
            resolveSecondStream = resolve;
          });
        }
        handlers.onChunk?.({
          event: "chunk",
          request_id: requestId,
          sequence: 0,
          data: { text: "first answer" },
        });
        const done: ChatDoneEvent = {
          event: "done",
          request_id: requestId,
          data: { full_text: "first answer" },
        };
        handlers.onDone?.(done);
        return done;
      },
    );
    const { result } = renderSession(makeTransport({ getMessages, streamChat }));
    await waitFor(() => expect(result.current.phase).toBe("idle"));

    await act(async () =>
      result.current.send({ ...SEND_INPUT, text: "first question" }),
    );
    act(() => {
      void result.current.send({ ...SEND_INPUT, text: "second question" });
    });
    await waitFor(() => expect(result.current.phase).toBe("streaming"));
    act(() =>
      resolveFirstHistory?.({
        items: [
          { id: "u1", role: "user", text: "first question" },
          { id: "a1", role: "assistant", text: "first answer" },
        ],
      }),
    );
    await act(async () => Promise.resolve());
    expect(result.current.messages.some((message) => message.text === "second question"))
      .toBe(true);

    const secondDone: ChatDoneEvent = {
      event: "done",
      request_id: "request-second",
      data: { full_text: "second answer" },
    };
    act(() => {
      secondHandlers?.onChunk?.({
        event: "chunk",
        request_id: "request-second",
        sequence: 0,
        data: { text: "second answer" },
      });
      secondHandlers?.onDone?.(secondDone);
      resolveSecondStream?.(secondDone);
    });
    await waitFor(() => expect(result.current.messages).toHaveLength(4));
  });

  it("does not confirm cancellation when the backend has entered finalizing", async () => {
    let handlers: ChatStreamHandlers | null = null;
    let resolveStream: ((done: ChatDoneEvent) => void) | null = null;
    let streamSignal: AbortSignal | undefined;
    const streamChat = vi.fn<ApiTransport["streamChat"]>(
      async (_request, nextHandlers, signal) => {
        handlers = nextHandlers;
        streamSignal = signal;
        nextHandlers.onRequestId?.("request-finalizing", true);
        nextHandlers.onMetadata?.({
          event: "metadata",
          request_id: "request-finalizing",
          data: {},
        });
        return new Promise<ChatDoneEvent>((resolve) => {
          resolveStream = resolve;
        });
      },
    );
    const cancelChat = vi.fn(async () => requestStatus("finalizing", "request-finalizing"));
    const getMessages = vi
      .fn<ApiTransport["getMessages"]>()
      .mockResolvedValueOnce({ items: [] })
      .mockResolvedValue({
        items: [
          { id: "u1", role: "user", text: "new question" },
          { id: "a1", role: "assistant", text: "saved" },
        ],
      });
    const { result } = renderSession(
      makeTransport({ streamChat, cancelChat, getMessages }),
    );
    await waitFor(() => expect(result.current.phase).toBe("idle"));

    let sendPromise: Promise<void> | undefined;
    act(() => {
      sendPromise = result.current.send(SEND_INPUT);
    });
    await waitFor(() => expect(result.current.phase).toBe("streaming"));
    act(() => result.current.cancel());
    await waitFor(() => expect(cancelChat).toHaveBeenCalledTimes(1));
    expect(streamSignal?.aborted).toBe(false);
    expect(result.current.phase).toBe("finalizing");

    const done: ChatDoneEvent = {
      event: "done",
      request_id: "request-finalizing",
      data: { full_text: "saved" },
    };
    act(() => {
      handlers?.onChunk?.({
        event: "chunk",
        request_id: "request-finalizing",
        sequence: 0,
        data: { text: "saved" },
      });
      handlers?.onDone?.(done);
      resolveStream?.(done);
    });
    await act(async () => sendPromise);
    await waitFor(() => expect(result.current.phase).toBe("completed"));
  });

  it("confirms server cancellation and exposes retry", async () => {
    const streamChat = vi.fn<ApiTransport["streamChat"]>(
      async (_request, handlers, signal) => {
        handlers.onRequestId?.("request-cancel", true);
        handlers.onMetadata?.({ event: "metadata", request_id: "request-cancel", data: {} });
        return new Promise<ChatDoneEvent>((_resolve, reject) => {
          signal?.addEventListener("abort", () =>
            reject(new DOMException("Aborted", "AbortError")),
          );
        });
      },
    );
    const cancelChat = vi.fn(async () => requestStatus("cancelled", "request-cancel"));
    const { result } = renderSession(makeTransport({ streamChat, cancelChat }));
    await waitFor(() => expect(result.current.phase).toBe("idle"));

    act(() => {
      void result.current.send(SEND_INPUT);
    });
    await waitFor(() => expect(result.current.phase).toBe("streaming"));
    act(() => result.current.cancel());
    await waitFor(() => expect(result.current.phase).toBe("cancelled"));
    expect(result.current.canRetry).toBe(true);
    expect(result.current.messages.at(-1)?.delivery).toBe("cancelled");
  });

  it("treats a stop before server request confirmation as a safe local cancellation", async () => {
    const streamChat = vi.fn<ApiTransport["streamChat"]>(
      async (_request, handlers, signal) => {
        handlers.onRequestId?.("tentative-request", false);
        return new Promise<ChatDoneEvent>((_resolve, reject) => {
          signal?.addEventListener("abort", () =>
            reject(new DOMException("Aborted", "AbortError")),
          );
        });
      },
    );
    const cancelChat = vi.fn(async () => requestStatus("cancelled"));
    const { result } = renderSession(makeTransport({ streamChat, cancelChat }));
    await waitFor(() => expect(result.current.phase).toBe("idle"));

    act(() => {
      void result.current.send(SEND_INPUT);
    });
    await waitFor(() => expect(result.current.phase).toBe("waiting_metadata"));
    act(() => result.current.cancel());
    await waitFor(() => expect(result.current.phase).toBe("cancelled"));
    expect(cancelChat).not.toHaveBeenCalled();
  });

  it("reconciles terminal status when the stream fails during a finalizing cancel", async () => {
    let rejectStream: ((reason: unknown) => void) | null = null;
    let resolveCancel: ((status: ChatRequestStatus) => void) | null = null;
    const streamChat = vi.fn<ApiTransport["streamChat"]>(
      async (_request, handlers) => {
        handlers.onRequestId?.("request-race", true);
        handlers.onMetadata?.({ event: "metadata", request_id: "request-race", data: {} });
        return new Promise<ChatDoneEvent>((_resolve, reject) => {
          rejectStream = reject;
        });
      },
    );
    const cancelChat = vi.fn(
      async () =>
        new Promise<ChatRequestStatus>((resolve) => {
          resolveCancel = resolve;
        }),
    );
    const getChatRequest = vi.fn(async () => requestStatus("completed", "request-race"));
    const getMessages = vi
      .fn<ApiTransport["getMessages"]>()
      .mockResolvedValueOnce({ items: [] })
      .mockResolvedValue({
        items: [
          { id: "u1", role: "user", text: "new question" },
          { id: "a1", role: "assistant", text: "persisted answer" },
        ],
      });
    const { result } = renderSession(
      makeTransport({ streamChat, cancelChat, getChatRequest, getMessages }),
    );
    await waitFor(() => expect(result.current.phase).toBe("idle"));
    act(() => {
      void result.current.send(SEND_INPUT);
    });
    await waitFor(() => expect(result.current.phase).toBe("streaming"));
    act(() => result.current.cancel());
    act(() => rejectStream?.(new TypeError("stream disconnected")));
    await act(async () => Promise.resolve());
    act(() => resolveCancel?.(requestStatus("finalizing", "request-race")));

    await waitFor(() => expect(getChatRequest).toHaveBeenCalledWith("request-race"));
    await waitFor(() => expect(result.current.phase).toBe("completed"));
    expect(result.current.messages.map((message) => message.id)).toEqual(["u1", "a1"]);
  });

  it("reconciles a failed request when the stream is lost after cancel is declined", async () => {
    let rejectStream: ((reason: unknown) => void) | null = null;
    const streamChat = vi.fn<ApiTransport["streamChat"]>(
      async (_request, handlers) => {
        handlers.onRequestId?.("request-failed-race", true);
        handlers.onMetadata?.({
          event: "metadata",
          request_id: "request-failed-race",
          data: {},
        });
        return new Promise<ChatDoneEvent>((_resolve, reject) => {
          rejectStream = reject;
        });
      },
    );
    const cancelChat = vi.fn(async () =>
      requestStatus("finalizing", "request-failed-race"),
    );
    const getChatRequest = vi.fn(async (): Promise<ChatRequestStatus> => ({
      ...requestStatus("failed", "request-failed-race"),
      error: {
        code: "generation_failed",
        message: "Generation failed.",
        details: {},
        request_id: "request-failed-race",
      },
      retryable: false,
    }));
    const { result } = renderSession(
      makeTransport({ streamChat, cancelChat, getChatRequest }),
    );
    await waitFor(() => expect(result.current.phase).toBe("idle"));
    act(() => {
      void result.current.send(SEND_INPUT);
    });
    await waitFor(() => expect(result.current.phase).toBe("streaming"));

    act(() => result.current.cancel());
    await waitFor(() => expect(cancelChat).toHaveBeenCalledTimes(1));
    act(() => rejectStream?.(new TypeError("stream disconnected")));

    await waitFor(() => expect(getChatRequest).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(result.current.phase).toBe("failed"));
    expect(result.current.sendError?.code).toBe("generation_failed");
    expect(result.current.canRetry).toBe(false);
  });

  it("ignores a late cancel response after the stream has completed", async () => {
    let handlers: ChatStreamHandlers | null = null;
    let resolveStream: ((done: ChatDoneEvent) => void) | null = null;
    let resolveCancel: ((status: ChatRequestStatus) => void) | null = null;
    const streamChat = vi.fn<ApiTransport["streamChat"]>(
      async (_request, nextHandlers) => {
        handlers = nextHandlers;
        nextHandlers.onRequestId?.("request-late-cancel", true);
        nextHandlers.onMetadata?.({
          event: "metadata",
          request_id: "request-late-cancel",
          data: {},
        });
        return new Promise<ChatDoneEvent>((resolve) => {
          resolveStream = resolve;
        });
      },
    );
    const cancelChat = vi.fn(
      async () =>
        new Promise<ChatRequestStatus>((resolve) => {
          resolveCancel = resolve;
        }),
    );
    const getMessages = vi
      .fn<ApiTransport["getMessages"]>()
      .mockResolvedValueOnce({ items: [] })
      .mockResolvedValue({
        items: [
          { id: "late-user", role: "user", text: "new question" },
          { id: "late-assistant", role: "assistant", text: "saved" },
        ],
      });
    const { result } = renderSession(
      makeTransport({ streamChat, cancelChat, getMessages }),
    );
    await waitFor(() => expect(result.current.phase).toBe("idle"));
    let sendPromise: Promise<void> | null = null;
    act(() => {
      sendPromise = result.current.send(SEND_INPUT);
    });
    await waitFor(() => expect(result.current.phase).toBe("streaming"));
    act(() => result.current.cancel());

    const done: ChatDoneEvent = {
      event: "done",
      request_id: "request-late-cancel",
      data: { full_text: "saved" },
    };
    act(() => {
      handlers?.onChunk?.({
        event: "chunk",
        request_id: "request-late-cancel",
        sequence: 0,
        data: { text: "saved" },
      });
      handlers?.onDone?.(done);
      resolveStream?.(done);
    });
    await act(async () => sendPromise);
    await waitFor(() => expect(result.current.phase).toBe("completed"));

    act(() => resolveCancel?.(requestStatus("cancelled", "request-late-cancel")));
    await act(async () => Promise.resolve());
    expect(result.current.phase).toBe("completed");
    expect(result.current.messages.at(-1)?.delivery).toBe("completed");
  });

  it("prevents a synchronous double submit", async () => {
    const streamChat = vi.fn<ApiTransport["streamChat"]>(
      async (_request, handlers, signal) => {
        handlers.onRequestId?.("only-request", true);
        return new Promise<ChatDoneEvent>((_resolve, reject) => {
          signal?.addEventListener("abort", () =>
            reject(new DOMException("Aborted", "AbortError")),
          );
        });
      },
    );
    const transport = makeTransport({ streamChat });
    const { result } = renderSession(transport);
    await waitFor(() => expect(result.current.phase).toBe("idle"));

    act(() => {
      void result.current.send(SEND_INPUT);
      void result.current.send(SEND_INPUT);
    });
    expect(streamChat).toHaveBeenCalledTimes(1);
    act(() => result.current.cancel());
    await waitFor(() => expect(result.current.phase).toBe("cancelled"));
  });

  it("keeps an uncertain request retryable after API reconnection", async () => {
    const requestBodies: string[] = [];
    const streamChat = vi.fn<ApiTransport["streamChat"]>(
      async (request, handlers, signal) => {
        requestBodies.push(request.client_request_id ?? "");
        handlers.onRequestId?.(`attempt-${requestBodies.length}`, false);
        return new Promise<ChatDoneEvent>((_resolve, reject) => {
          signal?.addEventListener("abort", () =>
            reject(new DOMException("Aborted", "AbortError")),
          );
        });
      },
    );
    const transport = makeTransport({ streamChat });
    const { result, rerender } = renderHook(
      ({ connectionPhase, connectionRevision }) =>
        useChatSession({
          transport,
          instanceName: "assistant",
          connectionPhase,
          connectionRevision,
        }),
      {
        initialProps: {
          connectionPhase: "connected" as "connected" | "disconnected",
          connectionRevision: 1,
        },
      },
    );
    await waitFor(() => expect(result.current.phase).toBe("idle"));
    act(() => {
      void result.current.send(SEND_INPUT);
    });
    await waitFor(() => expect(result.current.phase).toBe("waiting_metadata"));

    rerender({ connectionPhase: "disconnected", connectionRevision: 1 });
    await waitFor(() => expect(result.current.phase).toBe("disconnected"));
    rerender({ connectionPhase: "connected", connectionRevision: 2 });
    await waitFor(() => expect(result.current.phase).toBe("idle"));
    expect(result.current.canRetry).toBe(true);

    act(() => {
      void result.current.retry();
    });
    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(2));
    expect(requestBodies[1]).toBe(requestBodies[0]);
    act(() => result.current.cancel());
    await waitFor(() => expect(result.current.phase).toBe("cancelled"));
  });

  it("preserves logical IDs except after a confirmed terminal failure", () => {
    const body = {
      instance_name: "assistant",
      text: "hello",
      client_request_id: "original-id",
    };
    expect(
      buildRetryRequest({ body, outcome: "uncertain" }, () => "new-id")
        .client_request_id,
    ).toBe("original-id");
    expect(
      buildRetryRequest({ body, outcome: "cancelled" }, () => "new-id")
        .client_request_id,
    ).toBe("original-id");
    expect(
      buildRetryRequest({ body, outcome: "terminal_failure" }, () => "new-id")
        .client_request_id,
    ).toBe("new-id");
  });

  it("marks a non-recoverable stream error as terminal", async () => {
    const streamChat = vi.fn<ApiTransport["streamChat"]>(async (_request, handlers) => {
      handlers.onRequestId?.("request-failed", true);
      throw new ButlyApiError("generation_failed", "failed", { recoverable: false });
    });
    const { result } = renderSession(makeTransport({ streamChat }));
    await waitFor(() => expect(result.current.phase).toBe("idle"));
    await act(async () => result.current.send(SEND_INPUT));
    expect(result.current.phase).toBe("failed");
    expect(result.current.sendError?.code).toBe("generation_failed");
    expect(result.current.canRetry).toBe(false);
  });

  it("retries a recoverable generation error with the same logical request ID", async () => {
    const requestIds: string[] = [];
    const streamChat = vi.fn<ApiTransport["streamChat"]>(async (request, handlers) => {
      requestIds.push(request.client_request_id ?? "");
      handlers.onRequestId?.(`recoverable-${requestIds.length}`, true);
      throw new ButlyApiError("generation_failed", "temporary failure", {
        recoverable: true,
      });
    });
    const { result } = renderSession(makeTransport({ streamChat }));
    await waitFor(() => expect(result.current.phase).toBe("idle"));
    await act(async () => result.current.send(SEND_INPUT));
    expect(result.current.canRetry).toBe(true);

    await act(async () => result.current.retry());
    expect(streamChat).toHaveBeenCalledTimes(2);
    expect(requestIds[1]).toBe(requestIds[0]);
  });

  it("offers an idempotent retry for a transient pre-stream HTTP failure", async () => {
    const requestIds: string[] = [];
    const streamChat = vi.fn<ApiTransport["streamChat"]>(async (request) => {
      requestIds.push(request.client_request_id ?? "");
      throw new ButlyApiError("backend_not_ready", "Backend is starting.", {
        status: 503,
      });
    });
    const { result } = renderSession(makeTransport({ streamChat }));
    await waitFor(() => expect(result.current.phase).toBe("idle"));
    await act(async () => result.current.send(SEND_INPUT));
    expect(result.current.canRetry).toBe(true);

    await act(async () => result.current.retry());
    expect(requestIds[1]).toBe(requestIds[0]);
  });
});

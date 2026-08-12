import { describe, expect, it, vi } from "vitest";

import { createApiTransport } from "./transport";

function sseResponse(requestId: string, text = "ok"): Response {
  const frames = [
    `event: metadata\ndata: ${JSON.stringify({ event: "metadata", request_id: requestId, data: {} })}\n\n`,
    `event: chunk\ndata: ${JSON.stringify({ event: "chunk", request_id: requestId, sequence: 0, data: { text } })}\n\n`,
    `event: done\ndata: ${JSON.stringify({ event: "done", request_id: requestId, data: { full_text: text } })}\n\n`,
  ].join("");
  return new Response(frames, {
    status: 200,
    headers: {
      "Content-Type": "text/event-stream; charset=utf-8",
      "X-Request-ID": requestId,
    },
  });
}

describe("HTTP ApiTransport streaming", () => {
  it("keeps client idempotency separate from per-attempt request IDs", async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValueOnce(sseResponse("server-request-1"))
      .mockResolvedValueOnce(sseResponse("server-request-2"));
    const transport = createApiTransport(
      { baseUrl: "http://127.0.0.1:43210", token: "desktop-secret" },
      fetchImpl,
    );
    const requestIds: Array<[string, boolean]> = [];
    const body = {
      instance_name: "assistant",
      text: "hello",
      client_request_id: "logical-request",
    };

    await transport.streamChat(body, {
      onRequestId: (id, confirmed) => requestIds.push([id, confirmed]),
    });
    await transport.streamChat(body, {});

    const firstInit = fetchImpl.mock.calls[0]?.[1] as RequestInit;
    const secondInit = fetchImpl.mock.calls[1]?.[1] as RequestInit;
    const firstHeaders = new Headers(firstInit.headers);
    const secondHeaders = new Headers(secondInit.headers);
    expect(firstHeaders.get("authorization")).toBe("Bearer desktop-secret");
    expect(firstHeaders.get("x-request-id")).not.toBe("logical-request");
    expect(firstHeaders.get("x-request-id")).not.toBe(secondHeaders.get("x-request-id"));
    expect(JSON.parse(firstInit.body as string).client_request_id).toBe("logical-request");
    expect(requestIds.at(-1)).toEqual(["server-request-1", true]);
  });

  it("normalizes an HTTP ApiError before opening the stream", async () => {
    const fetchImpl = vi.fn(async () =>
      new Response(
        JSON.stringify({
          code: "debug_not_available",
          message: "Debug is disabled.",
          details: {},
          request_id: "req-error",
        }),
        { status: 403, headers: { "Content-Type": "application/json" } },
      ),
    );
    const transport = createApiTransport(
      { baseUrl: "http://127.0.0.1:43210", token: null },
      fetchImpl,
    );

    await expect(
      transport.streamChat({ instance_name: "assistant", text: "hello" }, {}),
    ).rejects.toMatchObject({ code: "debug_not_available", status: 403 });
  });

  it("rejects a response header and SSE request-id mismatch", async () => {
    const fetchImpl = vi.fn(async () => sseResponse("event-request"));
    fetchImpl.mockResolvedValueOnce(
      new Response(await sseResponse("event-request").text(), {
        status: 200,
        headers: {
          "Content-Type": "text/event-stream",
          "X-Request-ID": "header-request",
        },
      }),
    );
    const transport = createApiTransport(
      { baseUrl: "http://127.0.0.1:43210", token: null },
      fetchImpl,
    );

    await expect(
      transport.streamChat({ instance_name: "assistant", text: "hello" }, {}),
    ).rejects.toThrow("request_id changed");
  });
});

import { describe, expect, it, vi } from "vitest";

import { SseProtocolError } from "./errors";
import { parseChatSse } from "./sse";

const encoder = new TextEncoder();

function frame(event: string, payload: unknown, newline = "\n"): string {
  return `event: ${event}${newline}data: ${JSON.stringify(payload)}${newline}${newline}`;
}

function chunkedStream(text: string, cuts: number[]): ReadableStream<Uint8Array> {
  const bytes = encoder.encode(text);
  return new ReadableStream({
    start(controller) {
      let offset = 0;
      for (const size of cuts) {
        controller.enqueue(bytes.slice(offset, offset + size));
        offset += size;
      }
      if (offset < bytes.length) controller.enqueue(bytes.slice(offset));
      controller.close();
    },
  });
}

describe("parseChatSse", () => {
  it("parses arbitrary UTF-8 chunks and validates a multi-chunk response", async () => {
    const payload =
      frame("metadata", {
        event: "metadata",
        request_id: "req-1",
        data: { tier: "mid", need: "past_fact" },
      }) +
      frame("chunk", {
        event: "chunk",
        request_id: "req-1",
        sequence: 0,
        data: { text: "こん" },
      }) +
      frame("chunk", {
        event: "chunk",
        request_id: "req-1",
        sequence: 1,
        data: { text: "にちは" },
      }) +
      frame("done", {
        event: "done",
        request_id: "req-1",
        data: { full_text: "こんにちは", sources: [] },
      });
    const onChunk = vi.fn();
    const done = await parseChatSse(chunkedStream(payload, [1, 2, 7, 3, 11, 1]), {
      onChunk,
    });

    expect(done.data.full_text).toBe("こんにちは");
    expect(onChunk).toHaveBeenCalledTimes(2);
  });

  it("supports CRLF, heartbeat comments, and multiple data lines", async () => {
    const metadataJson = JSON.stringify({
      event: "metadata",
      request_id: "req-crlf",
      data: { tier: "reflex" },
    });
    const split = metadataJson.indexOf(",") + 1;
    const payload =
      `: heartbeat\r\nevent: metadata\r\ndata: ${metadataJson.slice(0, split)}\r\n` +
      `data: ${metadataJson.slice(split)}\r\n\r\n` +
      frame(
        "chunk",
        {
          event: "chunk",
          request_id: "req-crlf",
          sequence: 0,
          data: { text: "ready" },
        },
        "\r\n",
      ) +
      frame(
        "done",
        {
          event: "done",
          request_id: "req-crlf",
          data: { full_text: "ready" },
        },
        "\r\n",
      );

    await expect(parseChatSse(chunkedStream(payload, [5, 8, 13]))).resolves.toMatchObject({
      request_id: "req-crlf",
    });
  });

  it("accepts an empty response with zero chunks", async () => {
    const payload =
      frame("metadata", {
        event: "metadata",
        request_id: "req-buffered",
        data: {},
      }) +
      frame("done", {
        event: "done",
        request_id: "req-buffered",
        data: { full_text: "" },
      });

    await expect(parseChatSse(chunkedStream(payload, [4, 9]))).resolves.toMatchObject({
      data: { full_text: "" },
    });
  });

  it("rejects unknown events and cancels the open reader", async () => {
    const cancel = vi.fn();
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(
          encoder.encode(
            frame("mystery", {
              event: "mystery",
              request_id: "req-bad",
              data: {},
            }),
          ),
        );
      },
      cancel,
    });

    await expect(parseChatSse(stream)).rejects.toBeInstanceOf(SseProtocolError);
    expect(cancel).toHaveBeenCalledTimes(1);
    expect(() => stream.getReader()).not.toThrow();
  });

  it("rejects a truncated stream and releases its lock", async () => {
    const stream = chunkedStream(
      frame("metadata", {
        event: "metadata",
        request_id: "req-short",
        data: {},
      }),
      [2, 3],
    );

    await expect(parseChatSse(stream)).rejects.toThrow("ended before a done event");
    expect(() => stream.getReader()).not.toThrow();
  });

  it("cancels the stream when a handler fails", async () => {
    const cancel = vi.fn();
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(
          encoder.encode(
            frame("metadata", {
              event: "metadata",
              request_id: "req-handler",
              data: {},
            }),
          ),
        );
      },
      cancel,
    });

    await expect(
      parseChatSse(stream, {
        onMetadata: () => {
          throw new Error("handler failed");
        },
      }),
    ).rejects.toThrow("handler failed");
    expect(cancel).toHaveBeenCalledTimes(1);
  });

  it("preserves an abort read error and releases the reader lock", async () => {
    const aborted = new DOMException("The operation was aborted.", "AbortError");
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const stream = new ReadableStream<Uint8Array>({
      pull() {
        throw aborted;
      },
    });

    await expect(parseChatSse(stream)).rejects.toBe(aborted);
    expect(() => stream.getReader()).not.toThrow();
    warn.mockRestore();
  });

  it("rejects non-contiguous sequence and full-text mismatches", async () => {
    const badSequence =
      frame("metadata", { event: "metadata", request_id: "req-seq", data: {} }) +
      frame("chunk", {
        event: "chunk",
        request_id: "req-seq",
        sequence: 2,
        data: { text: "x" },
      });
    await expect(parseChatSse(chunkedStream(badSequence, []))).rejects.toThrow(
      "sequence is not contiguous",
    );

    const badDone =
      frame("metadata", { event: "metadata", request_id: "req-text", data: {} }) +
      frame("chunk", {
        event: "chunk",
        request_id: "req-text",
        sequence: 0,
        data: { text: "one" },
      }) +
      frame("done", {
        event: "done",
        request_id: "req-text",
        data: { full_text: "different" },
      });
    await expect(parseChatSse(chunkedStream(badDone, []))).rejects.toThrow(
      "does not match streamed chunks",
    );
  });
});

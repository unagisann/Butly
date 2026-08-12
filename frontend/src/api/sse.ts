import type {
  ChatChunkEvent,
  ChatDoneEvent,
  ChatErrorEvent,
  ChatMetadataEvent,
  ChatStreamEvent,
} from "./generated";
import { ButlyApiError, SseProtocolError, isApiError } from "./errors";

export interface ChatStreamHandlers {
  onRequestId?: (requestId: string, confirmed: boolean) => void;
  onMetadata?: (event: ChatMetadataEvent) => void;
  onChunk?: (event: ChatChunkEvent) => void;
  onDone?: (event: ChatDoneEvent) => void;
}

interface ParsedFrame {
  event: string | null;
  data: string;
}

interface StreamContractState {
  requestId: string | null;
  metadataSeen: boolean;
  terminalSeen: boolean;
  nextSequence: number;
  fullText: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requireString(record: Record<string, unknown>, key: string): string {
  const value = record[key];
  if (typeof value !== "string") {
    throw new SseProtocolError(`SSE payload is missing string field '${key}'.`);
  }
  return value;
}

function parseFrame(lines: string[]): ParsedFrame | null {
  let event: string | null = null;
  const data: string[] = [];

  for (const line of lines) {
    if (line === "" || line.startsWith(":")) continue;
    const separator = line.indexOf(":");
    const field = separator === -1 ? line : line.slice(0, separator);
    let value = separator === -1 ? "" : line.slice(separator + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") event = value;
    if (field === "data") data.push(value);
  }

  if (event === null && data.length === 0) return null;
  return { event, data: data.join("\n") };
}

function validateCommon(
  payload: Record<string, unknown>,
  eventName: string,
  state: StreamContractState,
): string {
  if (payload.event !== undefined && payload.event !== eventName) {
    throw new SseProtocolError("SSE event name and JSON discriminator differ.", {
      frame_event: eventName,
      payload_event: payload.event,
    });
  }
  const requestId = requireString(payload, "request_id");
  if (state.requestId !== null && requestId !== state.requestId) {
    throw new SseProtocolError("SSE request_id changed during the stream.", {
      expected: state.requestId,
      received: requestId,
    });
  }
  state.requestId = requestId;
  return requestId;
}

function validateFrame(frame: ParsedFrame, state: StreamContractState): ChatStreamEvent {
  if (!frame.event) {
    throw new SseProtocolError("SSE frame is missing an event name.");
  }
  if (state.terminalSeen) {
    throw new SseProtocolError("SSE frame arrived after a terminal event.");
  }
  if (!["metadata", "chunk", "done", "error"].includes(frame.event)) {
    throw new SseProtocolError(`Unknown SSE event '${frame.event}'.`);
  }

  let decoded: unknown;
  try {
    decoded = JSON.parse(frame.data);
  } catch (cause) {
    throw new SseProtocolError("SSE data is not valid JSON.", {
      cause: cause instanceof Error ? cause.message : String(cause),
    });
  }
  if (!isRecord(decoded)) {
    throw new SseProtocolError("SSE JSON payload must be an object.");
  }

  const requestId = validateCommon(decoded, frame.event, state);
  if (!isRecord(decoded.data)) {
    throw new SseProtocolError("SSE payload is missing its data object.");
  }

  if (frame.event === "metadata") {
    if (state.metadataSeen || state.nextSequence > 0) {
      throw new SseProtocolError("metadata must be the first and only metadata event.");
    }
    state.metadataSeen = true;
    return { ...decoded, event: "metadata", request_id: requestId } as ChatMetadataEvent;
  }

  if (frame.event === "chunk") {
    if (!state.metadataSeen) {
      throw new SseProtocolError("chunk arrived before metadata.");
    }
    if (typeof decoded.sequence !== "number" || decoded.sequence !== state.nextSequence) {
      throw new SseProtocolError("SSE chunk sequence is not contiguous.", {
        expected: state.nextSequence,
        received: decoded.sequence,
      });
    }
    const text = requireString(decoded.data, "text");
    state.fullText += text;
    state.nextSequence += 1;
    return { ...decoded, event: "chunk", request_id: requestId } as ChatChunkEvent;
  }

  if (frame.event === "done") {
    if (!state.metadataSeen) {
      throw new SseProtocolError("done arrived before metadata.");
    }
    const fullText = requireString(decoded.data, "full_text");
    if (fullText !== state.fullText) {
      throw new SseProtocolError("done.full_text does not match streamed chunks.", {
        streamed_length: state.fullText.length,
        done_length: fullText.length,
      });
    }
    state.terminalSeen = true;
    return { ...decoded, event: "done", request_id: requestId } as ChatDoneEvent;
  }

  if (!isApiError(decoded.data)) {
    throw new SseProtocolError("error event does not contain an ApiError envelope.");
  }
  state.terminalSeen = true;
  return { ...decoded, event: "error", request_id: requestId } as ChatErrorEvent;
}

function dispatchEvent(
  event: ChatStreamEvent,
  handlers: ChatStreamHandlers,
): ChatDoneEvent | null {
  switch (event.event) {
    case "metadata":
      handlers.onMetadata?.(event as ChatMetadataEvent);
      return null;
    case "chunk":
      handlers.onChunk?.(event as ChatChunkEvent);
      return null;
    case "done":
      handlers.onDone?.(event as ChatDoneEvent);
      return event as ChatDoneEvent;
    case "error": {
      const error = event as ChatErrorEvent;
      throw new ButlyApiError(error.data.code, error.data.message, {
        details: error.data.details,
        requestId: error.data.request_id,
        recoverable: error.recoverable,
      });
    }
    default:
      throw new SseProtocolError("SSE event has no discriminator.");
  }
}

export async function parseChatSse(
  stream: ReadableStream<Uint8Array>,
  handlers: ChatStreamHandlers = {},
  expectedRequestId: string | null = null,
): Promise<ChatDoneEvent> {
  const reader = stream.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  const state: StreamContractState = {
    requestId: expectedRequestId,
    metadataSeen: false,
    terminalSeen: false,
    nextSequence: 0,
    fullText: "",
  };
  let pending = "";
  let frameLines: string[] = [];
  let doneEvent: ChatDoneEvent | null = null;

  const consumeLine = (line: string) => {
    if (line !== "") {
      frameLines.push(line);
      return;
    }
    const frame = parseFrame(frameLines);
    frameLines = [];
    if (!frame) return;
    const event = validateFrame(frame, state);
    doneEvent = dispatchEvent(event, handlers) ?? doneEvent;
  };

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      try {
        pending += decoder.decode(value, { stream: true });
      } catch (cause) {
        throw new SseProtocolError("SSE stream contains invalid UTF-8.", {
          cause: cause instanceof Error ? cause.message : String(cause),
        });
      }

      let newline = pending.indexOf("\n");
      while (newline !== -1) {
        let line = pending.slice(0, newline);
        pending = pending.slice(newline + 1);
        if (line.endsWith("\r")) line = line.slice(0, -1);
        consumeLine(line);
        newline = pending.indexOf("\n");
      }
    }

    try {
      pending += decoder.decode();
    } catch (cause) {
      throw new SseProtocolError("SSE stream ended with invalid UTF-8.", {
        cause: cause instanceof Error ? cause.message : String(cause),
      });
    }
    if (pending.length > 0) {
      if (pending.endsWith("\r")) pending = pending.slice(0, -1);
      consumeLine(pending);
    }
    if (frameLines.length > 0) {
      const frame = parseFrame(frameLines);
      if (frame) {
        const event = validateFrame(frame, state);
        doneEvent = dispatchEvent(event, handlers) ?? doneEvent;
      }
    }

    if (!doneEvent || !state.terminalSeen) {
      throw new SseProtocolError("SSE stream ended before a done event.", {
        request_id: state.requestId,
      });
    }
    return doneEvent;
  } catch (error) {
    try {
      await reader.cancel(error);
    } catch (cancelError) {
      console.warn("[frontend] failed to cancel an invalid SSE stream", cancelError);
    }
    throw error;
  } finally {
    reader.releaseLock();
  }
}

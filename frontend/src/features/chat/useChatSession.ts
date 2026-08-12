import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { ApiTransport } from "../../api/transport";
import { ButlyApiError, isAbortError, normalizeApiError } from "../../api/errors";
import type { ChatDoneEvent, ChatRequestStatus } from "../../api/generated";
import type { ApiConnectionPhase } from "../../app/useApiConnection";
import { extractChatDebug, mergeChatDebug } from "./debug";
import {
  attachmentSummary,
  completedMessage,
  safeSources,
} from "./types";
import type {
  ChatDebugView,
  ChatMessageView,
  ChatPhase,
  ChatRequestWithDebug,
  ChatSessionError,
  LastChatRequest,
  PreparedAttachment,
  SendChatInput,
} from "./types";

interface ActiveRequest {
  body: ChatRequestWithDebug;
  controller: AbortController;
  requestId: string | null;
  requestIdConfirmed: boolean;
  assistantMessageId: string;
  clientRequestId: string;
  messageRevision: number;
  preparedAttachments: PreparedAttachment[];
  stopReason: "cancel" | "disconnect" | null;
  cancelPending: boolean;
  cancelStatus: ChatRequestStatus | null;
  pendingStreamError: unknown | null;
  reconcileAfterStreamLoss: (() => void) | null;
}

interface UseChatSessionOptions {
  transport: ApiTransport;
  instanceName: string;
  connectionPhase: ApiConnectionPhase;
  connectionRevision: number;
}

export interface ChatSession {
  messages: ChatMessageView[];
  phase: ChatPhase;
  historyError: ChatSessionError | null;
  sendError: ChatSessionError | null;
  lastInteractionAt: string | null;
  debug: ChatDebugView | null;
  busy: boolean;
  canRetry: boolean;
  send: (input: SendChatInput) => Promise<void>;
  cancel: () => void;
  retry: () => Promise<void>;
  reloadHistory: () => Promise<void>;
}

function newId(): string {
  return globalThis.crypto?.randomUUID?.() ??
    `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

export function buildRetryRequest(
  lastRequest: LastChatRequest,
  idFactory: () => string = newId,
): ChatRequestWithDebug {
  const preserveId = lastRequest.outcome !== "terminal_failure";
  return {
    ...lastRequest.body,
    attachments: [...(lastRequest.body.attachments ?? [])],
    client_request_id: preserveId
      ? lastRequest.body.client_request_id
      : idFactory(),
  };
}

function sessionError(error: unknown): ChatSessionError {
  const normalized = normalizeApiError(error);
  return {
    code: normalized.code,
    message: normalized.message,
    recoverable: normalized.recoverable,
    requestId: normalized.requestId,
  };
}

function retryOutcome(error: ButlyApiError): LastChatRequest["outcome"] {
  if (
    error.recoverable ||
    error.code === "network_error" ||
    error.code === "protocol_error" ||
    error.code === "backend_not_ready" ||
    error.code === "chat_request_capacity_exceeded" ||
    error.status === 429 ||
    (error.status !== null && error.status >= 500)
  ) {
    return "uncertain";
  }
  return "terminal_failure";
}

function displayDelivery(error: ButlyApiError): ChatMessageView["delivery"] {
  return error.code === "network_error" ? "disconnected" : "failed";
}

export function useChatSession({
  transport,
  instanceName,
  connectionPhase,
  connectionRevision,
}: UseChatSessionOptions): ChatSession {
  const [messages, setMessages] = useState<ChatMessageView[]>([]);
  const [phase, setPhase] = useState<ChatPhase>("loading_history");
  const [historyError, setHistoryError] = useState<ChatSessionError | null>(null);
  const [sendError, setSendError] = useState<ChatSessionError | null>(null);
  const [lastInteractionAt, setLastInteractionAt] = useState<string | null>(null);
  const [debug, setDebug] = useState<ChatDebugView | null>(null);
  const [lastRequest, setLastRequest] = useState<LastChatRequest | null>(null);
  const activeRef = useRef<ActiveRequest | null>(null);
  const historyControllerRef = useRef<AbortController | null>(null);
  const instanceRef = useRef(instanceName);
  const previousInstanceRef = useRef(instanceName);
  const messageRevisionRef = useRef(0);
  instanceRef.current = instanceName;

  const updateMessage = useCallback(
    (messageId: string, update: (message: ChatMessageView) => ChatMessageView) => {
      setMessages((current) =>
        current.map((message) => (message.id === messageId ? update(message) : message)),
      );
    },
    [],
  );

  const reloadHistory = useCallback(async () => {
    historyControllerRef.current?.abort();
    const controller = new AbortController();
    const messageRevision = ++messageRevisionRef.current;
    historyControllerRef.current = controller;
    setPhase("loading_history");
    setHistoryError(null);
    try {
      const page = await transport.getMessages(instanceName, controller.signal);
      if (
        controller.signal.aborted ||
        instanceRef.current !== instanceName ||
        messageRevisionRef.current !== messageRevision
      ) {
        return;
      }
      setMessages(page.items.map(completedMessage));
      setLastInteractionAt(page.last_interaction_at ?? null);
      setPhase("idle");
    } catch (error) {
      if (isAbortError(error)) return;
      setHistoryError(sessionError(error));
      setPhase("failed");
    }
  }, [instanceName, transport]);

  useEffect(() => {
    const instanceChanged = previousInstanceRef.current !== instanceName;
    previousInstanceRef.current = instanceName;
    setMessages([]);
    setDebug(null);
    if (instanceChanged) setLastRequest(null);
    setSendError(null);
    if (connectionPhase === "connected") void reloadHistory();
    return () => {
      historyControllerRef.current?.abort();
      const active = activeRef.current;
      if (active) {
        active.stopReason = "disconnect";
        active.controller.abort();
      }
    };
  }, [connectionRevision, instanceName, reloadHistory]);

  const syncCompletedHistory = useCallback(
    async (
      active: ActiveRequest,
      done: ChatDoneEvent,
      doneDebug: ChatDebugView | null,
    ) => {
      try {
        const page = await transport.getMessages(instanceName);
        if (
          instanceRef.current !== instanceName ||
          messageRevisionRef.current !== active.messageRevision
        ) {
          return;
        }
        const synced = page.items.map(completedMessage);

        for (let index = synced.length - 1; index >= 0; index -= 1) {
          const message = synced[index];
          if (!message) continue;
          if (message.role === "assistant" && message.text === done.data.full_text) {
            synced[index] = {
              ...message,
              sources:
                safeSources(message.sources).length > 0
                  ? message.sources
                  : done.data.sources,
              requestId: done.request_id,
            };
            break;
          }
        }

        for (let index = synced.length - 1; index >= 0; index -= 1) {
          const message = synced[index];
          if (!message) continue;
          if (message.role === "user" && message.text === active.body.text) {
            synced[index] = {
              ...message,
              attachments:
                (message.attachments?.length ?? 0) > 0
                  ? message.attachments
                  : active.preparedAttachments.map(attachmentSummary),
              attachmentPreviews: active.preparedAttachments.map(
                (attachment) => attachment.previewUrl,
              ),
            };
            break;
          }
        }

        setMessages(synced);
        setLastInteractionAt(page.last_interaction_at ?? new Date().toISOString());
        setDebug((current) => mergeChatDebug(current, doneDebug));
      } catch (error) {
        if (!isAbortError(error)) {
          console.warn("[frontend] chat completed but history refresh failed", error);
        }
      }
    },
    [instanceName, transport],
  );

  const execute = useCallback(
    async (
      body: ChatRequestWithDebug,
      preparedAttachments: PreparedAttachment[],
      replacedClientRequestId?: string,
    ) => {
      if (activeRef.current) return;
      if (connectionPhase !== "connected") {
        setSendError({
          code: "network_error",
          message: "Backend is disconnected.",
          recoverable: true,
          requestId: null,
        });
        setPhase("disconnected");
        return;
      }

      const clientRequestId = body.client_request_id ?? newId();
      const messageRevision = ++messageRevisionRef.current;
      body = { ...body, client_request_id: clientRequestId };
      const userMessageId = `local-user-${clientRequestId}`;
      const assistantMessageId = `local-assistant-${clientRequestId}`;
      if (replacedClientRequestId) {
        setMessages((current) =>
          current.filter(
            (message) => message.clientRequestId !== replacedClientRequestId,
          ),
        );
      }

      const now = new Date().toISOString();
      setMessages((current) => [
        ...current,
        {
          id: userMessageId,
          role: "user",
          text: body.text ?? "",
          created_at: now,
          attachments: preparedAttachments.map(attachmentSummary),
          sources: [],
          status: "completed",
          delivery: "completed",
          clientRequestId,
          attachmentPreviews: preparedAttachments.map(
            (attachment) => attachment.previewUrl,
          ),
        },
        {
          id: assistantMessageId,
          role: "assistant",
          text: "",
          created_at: now,
          attachments: [],
          sources: [],
          status: "completed",
          delivery: "sending",
          clientRequestId,
        },
      ]);

      const active: ActiveRequest = {
        body,
        controller: new AbortController(),
        requestId: null,
        requestIdConfirmed: false,
        assistantMessageId,
        clientRequestId,
        messageRevision,
        preparedAttachments,
        stopReason: null,
        cancelPending: false,
        cancelStatus: null,
        pendingStreamError: null,
        reconcileAfterStreamLoss: null,
      };
      activeRef.current = active;
      setDebug(null);
      setLastRequest(null);
      setSendError(null);
      setPhase("submitting");

      try {
        setPhase("waiting_metadata");
        const done = await transport.streamChat(
          body,
          {
            onRequestId: (requestId, confirmed) => {
              active.requestId = requestId;
              active.requestIdConfirmed = confirmed;
            },
            onMetadata: (event) => {
              active.requestId = event.request_id;
              active.requestIdConfirmed = true;
              const metadataDebug = extractChatDebug(event.data);
              setDebug((current) => mergeChatDebug(current, metadataDebug));
              updateMessage(assistantMessageId, (message) => ({
                ...message,
                requestId: event.request_id,
                delivery: "streaming",
              }));
              setPhase("streaming");
            },
            onChunk: (event) => {
              active.requestId = event.request_id;
              active.requestIdConfirmed = true;
              updateMessage(assistantMessageId, (message) => ({
                ...message,
                text: message.text + event.data.text,
                requestId: event.request_id,
                delivery: "streaming",
              }));
              setPhase("streaming");
            },
            onDone: () => setPhase("finalizing"),
          },
          active.controller.signal,
        );
        if (active.stopReason) return;

        active.requestId = done.request_id;
        const doneDebug = extractChatDebug(done.data);
        setDebug((current) => mergeChatDebug(current, doneDebug));
        updateMessage(assistantMessageId, (message) => ({
          ...message,
          text: done.data.full_text,
          sources: done.data.sources,
          requestId: done.request_id,
          delivery: "completed",
          status: "completed",
        }));
        setLastRequest(null);
        setPhase("completed");
        activeRef.current = null;
        void syncCompletedHistory(active, done, doneDebug);
      } catch (error) {
        if (active.stopReason === "cancel") return;
        if (active.cancelPending || active.cancelStatus !== null) {
          active.pendingStreamError = error;
          active.reconcileAfterStreamLoss?.();
          return;
        }
        if (active.stopReason === "disconnect" || isAbortError(error)) {
          updateMessage(assistantMessageId, (message) => ({
            ...message,
            delivery: "disconnected",
            status: "failed",
          }));
          setLastRequest({ body, outcome: "uncertain" });
          setPhase("disconnected");
          return;
        }

        const normalized = normalizeApiError(error);
        setSendError(sessionError(normalized));
        updateMessage(assistantMessageId, (message) => ({
          ...message,
          delivery: displayDelivery(normalized),
          status: "failed",
        }));
        const outcome = retryOutcome(normalized);
        setLastRequest(
          outcome === "terminal_failure" ? null : { body, outcome },
        );
        setPhase(
          displayDelivery(normalized) === "disconnected" ? "disconnected" : "failed",
        );
      } finally {
        if (
          activeRef.current === active &&
          !active.cancelPending &&
          active.cancelStatus === null
        ) {
          activeRef.current = null;
        }
      }
    },
    [connectionPhase, syncCompletedHistory, transport, updateMessage],
  );

  const send = useCallback(
    async (input: SendChatInput) => {
      const body: ChatRequestWithDebug = {
        instance_name: instanceName,
        text: input.text.trim(),
        attachments: input.attachments.map((attachment) => attachment.input),
        use_rag: input.useRag,
        use_google_search: input.useGoogleSearch,
        use_web_search: input.useWebSearch,
        include_debug: input.includeDebug,
        client_request_id: newId(),
      };
      await execute(body, input.attachments);
    },
    [execute, instanceName],
  );

  const cancel = useCallback(() => {
    const active = activeRef.current;
    if (!active || active.cancelPending) return;

    const confirmCancelled = () => {
      active.stopReason = "cancel";
      active.cancelPending = false;
      active.controller.abort();
      updateMessage(active.assistantMessageId, (message) => ({
        ...message,
        delivery: "cancelled",
        status: "cancelled",
      }));
      setLastRequest({ body: active.body, outcome: "cancelled" });
      setSendError(null);
      setPhase("cancelled");
      if (activeRef.current === active) activeRef.current = null;
    };

    if (!active.requestId || !active.requestIdConfirmed) {
      confirmCancelled();
      return;
    }

    active.cancelPending = true;
    setPhase("finalizing");

    const markUncertain = (error: unknown) => {
      if (activeRef.current !== active) return;
      active.cancelPending = false;
      active.stopReason = "disconnect";
      active.controller.abort();
      const normalized = normalizeApiError(error);
      updateMessage(active.assistantMessageId, (message) => ({
        ...message,
        delivery: "disconnected",
        status: "failed",
      }));
      setSendError({
        code: normalized.code,
        message: normalized.message,
        recoverable: true,
        requestId: normalized.requestId,
      });
      setLastRequest({ body: active.body, outcome: "uncertain" });
      setPhase("disconnected");
      if (activeRef.current === active) activeRef.current = null;
    };

    const settleTerminal = async (status: ChatRequestStatus) => {
      if (activeRef.current !== active) return;
      if (status.state === "cancelled") {
        confirmCancelled();
        return;
      }
      if (status.state === "completed") {
        active.cancelPending = false;
        active.stopReason = "cancel";
        active.controller.abort();
        activeRef.current = null;
        setSendError(null);
        setLastRequest(null);
        await reloadHistory();
        if (instanceRef.current === instanceName) setPhase("completed");
        return;
      }
      if (status.state === "failed") {
        active.cancelPending = false;
        active.stopReason = "cancel";
        active.controller.abort();
        activeRef.current = null;
        const error = status.error
          ? new ButlyApiError(status.error.code, status.error.message, {
              details: status.error.details,
              requestId: status.error.request_id,
              recoverable: status.retryable,
            })
          : normalizeApiError(active.pendingStreamError);
        updateMessage(active.assistantMessageId, (message) => ({
          ...message,
          delivery: "failed",
          status: "failed",
        }));
        setSendError(sessionError(error));
        setLastRequest(
          status.retryable
            ? { body: active.body, outcome: "uncertain" }
            : null,
        );
        setPhase("failed");
      }
    };

    const reconcileLostStream = async (initial: ChatRequestStatus) => {
      let status = initial;
      for (let attempt = 0; attempt < 20; attempt += 1) {
        if (status.state !== "running" && status.state !== "finalizing") break;
        if (activeRef.current !== active) return;
        status = await transport.getChatRequest(active.requestId as string);
        if (status.state === "running" || status.state === "finalizing") {
          await new Promise((resolve) => setTimeout(resolve, 250));
        }
      }
      if (status.state === "running" || status.state === "finalizing") {
        throw new ButlyApiError("request_status_timeout", "Generation status timed out.", {
          recoverable: true,
        });
      }
      await settleTerminal(status);
    };

    let reconciliationStarted = false;
    active.reconcileAfterStreamLoss = () => {
      if (reconciliationStarted || !active.cancelStatus) return;
      reconciliationStarted = true;
      void reconcileLostStream(active.cancelStatus).catch(markUncertain);
    };

    void transport
      .cancelChat(active.requestId)
      .then(async (status) => {
        if (activeRef.current !== active) return;
        active.cancelPending = false;
        if (status.state === "cancelled") {
          confirmCancelled();
          return;
        }
        if (status.state === "completed" || status.state === "failed") {
          await settleTerminal(status);
          return;
        }
        active.cancelStatus = status;
        if (active.pendingStreamError) {
          active.reconcileAfterStreamLoss?.();
          return;
        }
        setPhase(status.state === "running" ? "streaming" : "finalizing");
      })
      .catch((error: unknown) => {
        markUncertain(error);
      });
  }, [instanceName, reloadHistory, transport, updateMessage]);

  const retry = useCallback(async () => {
    if (!lastRequest) return;
    const body = buildRetryRequest(lastRequest);
    const preparedAttachments = (lastRequest.body.attachments ?? []).map(
      (input): PreparedAttachment => ({
        input,
        sizeBytes: 0,
        previewUrl: `data:${input.mime_type};base64,${input.data_base64}`,
      }),
    );
    await execute(
      body,
      preparedAttachments,
      lastRequest.body.client_request_id ?? undefined,
    );
  }, [execute, lastRequest]);

  useEffect(() => {
    if (connectionPhase !== "disconnected") return;
    const active = activeRef.current;
    if (!active) return;
    active.stopReason = "disconnect";
    active.controller.abort();
    updateMessage(active.assistantMessageId, (message) => ({
      ...message,
      delivery: "disconnected",
      status: "failed",
    }));
    setLastRequest({ body: active.body, outcome: "uncertain" });
    setPhase("disconnected");
    activeRef.current = null;
  }, [connectionPhase, updateMessage]);

  const busy = useMemo(
    () =>
      phase === "submitting" ||
      phase === "waiting_metadata" ||
      phase === "streaming" ||
      phase === "finalizing",
    [phase],
  );
  const canRetry =
    (lastRequest?.outcome === "cancelled" && phase === "cancelled") ||
    (lastRequest?.outcome === "uncertain" &&
      (phase === "failed" || phase === "disconnected" || phase === "idle"));

  return {
    messages,
    phase,
    historyError,
    sendError,
    lastInteractionAt,
    debug,
    busy,
    canRetry,
    send,
    cancel,
    retry,
    reloadHistory,
  };
}

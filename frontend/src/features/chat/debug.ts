import type { ChatDebugView } from "./types";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function stringOrNull(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function stringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

function numberMap(value: unknown): Record<string, number> {
  if (!isRecord(value)) return {};
  return Object.fromEntries(
    Object.entries(value).filter(
      (entry): entry is [string, number] =>
        typeof entry[1] === "number" && Number.isFinite(entry[1]),
    ),
  );
}

export function extractChatDebug(payload: unknown): ChatDebugView | null {
  if (!isRecord(payload)) return null;
  const debug = isRecord(payload.debug) ? payload.debug : payload;
  const gatekeeper = isRecord(debug.gatekeeper)
    ? debug.gatekeeper
    : typeof debug.tier === "string" || typeof debug.need === "string"
      ? debug
      : null;
  const rag = isRecord(debug.rag) ? debug.rag : null;
  if (!gatekeeper && !rag) return null;

  return {
    gatekeeper: {
      tier: stringOrNull(gatekeeper?.tier),
      need: stringOrNull(gatekeeper?.need),
      searchTargets: stringList(gatekeeper?.search_targets),
      scores: numberMap(gatekeeper?.scores),
      fallbackReason: stringOrNull(gatekeeper?.fallback_reason),
      memoryProbeStatus: stringOrNull(gatekeeper?.memory_probe_status),
    },
    rag: {
      enabled: rag?.enabled === true,
      candidateCount:
        typeof rag?.candidate_count === "number" ? rag.candidate_count : 0,
      injectedCount:
        typeof rag?.injected_count === "number" ? rag.injected_count : 0,
      activeNodes: stringList(rag?.active_nodes),
    },
  };
}

export function mergeChatDebug(
  previous: ChatDebugView | null,
  next: ChatDebugView | null,
): ChatDebugView | null {
  if (!previous) return next;
  if (!next) return previous;
  return {
    gatekeeper: {
      tier: next.gatekeeper.tier ?? previous.gatekeeper.tier,
      need: next.gatekeeper.need ?? previous.gatekeeper.need,
      searchTargets:
        next.gatekeeper.searchTargets.length > 0
          ? next.gatekeeper.searchTargets
          : previous.gatekeeper.searchTargets,
      scores:
        Object.keys(next.gatekeeper.scores).length > 0
          ? next.gatekeeper.scores
          : previous.gatekeeper.scores,
      fallbackReason:
        next.gatekeeper.fallbackReason ?? previous.gatekeeper.fallbackReason,
      memoryProbeStatus:
        next.gatekeeper.memoryProbeStatus ?? previous.gatekeeper.memoryProbeStatus,
    },
    rag: next.rag,
  };
}

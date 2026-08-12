import type {
  AttachmentInput,
  AttachmentSummary,
  ChatRequest,
  CitationSource,
  Message,
} from "../../api/generated";

export type ChatPhase =
  | "idle"
  | "loading_history"
  | "submitting"
  | "waiting_metadata"
  | "streaming"
  | "finalizing"
  | "completed"
  | "failed"
  | "disconnected"
  | "cancelled";

export type MessageDelivery =
  | "completed"
  | "sending"
  | "streaming"
  | "failed"
  | "cancelled"
  | "disconnected";

export type ChatMessageView = Message & {
  delivery: MessageDelivery;
  clientRequestId?: string;
  requestId?: string;
  attachmentPreviews?: string[];
};

export interface PreparedAttachment {
  input: AttachmentInput;
  sizeBytes: number;
  previewUrl: string;
}

export interface GatekeeperDebug {
  tier: string | null;
  need: string | null;
  searchTargets: string[];
  scores: Record<string, number>;
  fallbackReason: string | null;
  memoryProbeStatus: string | null;
}

export interface RagDebug {
  enabled: boolean;
  candidateCount: number;
  injectedCount: number;
  activeNodes: string[];
}

export interface ChatDebugView {
  gatekeeper: GatekeeperDebug;
  rag: RagDebug;
}

export type RetryOutcome = "completed" | "uncertain" | "cancelled" | "terminal_failure";

export type ChatRequestWithDebug = ChatRequest;

export interface LastChatRequest {
  body: ChatRequestWithDebug;
  outcome: RetryOutcome;
}

export interface SendChatInput {
  text: string;
  attachments: PreparedAttachment[];
  useRag: boolean;
  useGoogleSearch: boolean;
  useWebSearch: boolean;
  includeDebug: boolean;
}

export interface ChatSessionError {
  code: string;
  message: string;
  recoverable: boolean;
  requestId: string | null;
}

export function attachmentSummary(attachment: PreparedAttachment): AttachmentSummary {
  return {
    kind: "image",
    mime_type: attachment.input.mime_type,
    name: attachment.input.name,
    size_bytes: attachment.sizeBytes,
  };
}

export function completedMessage(message: Message): ChatMessageView {
  let delivery: MessageDelivery = "completed";
  if (message.status === "failed") delivery = "failed";
  if (message.status === "cancelled") delivery = "cancelled";
  return { ...message, delivery };
}

export function safeSources(value: CitationSource[] | undefined): CitationSource[] {
  return value ?? [];
}

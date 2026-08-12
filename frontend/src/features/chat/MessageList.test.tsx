import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { MessageList } from "./MessageList";
import type { ChatMessageView } from "./types";

describe("MessageList accessibility", () => {
  it("keeps streamed text out of the live region and announces only status", () => {
    render(
      <MessageList
        loading={false}
        phase="streaming"
        messages={[
          {
            id: "assistant-stream",
            role: "assistant",
            text: "partial response",
            attachments: [],
            sources: [],
            delivery: "streaming",
          },
        ]}
      />,
    );

    expect(screen.getByRole("region", { name: "チャット" })).toHaveAttribute(
      "aria-live",
      "off",
    );
    expect(screen.getByRole("status")).toHaveTextContent("生成中");
  });

  it("shows persisted attachment metadata when image bytes are not in history", () => {
    render(
      <MessageList
        loading={false}
        phase="idle"
        messages={[
          {
            id: "saved-user",
            role: "user",
            text: "この画像を見て",
            attachments: [
              {
                kind: "image",
                mime_type: "image/png",
                name: "memory.png",
                size_bytes: 1536,
              },
            ],
            sources: [],
            delivery: "completed",
          },
        ]}
      />,
    );

    expect(screen.getByText(/memory\.png/)).toHaveTextContent("memory.png · 1.5 KB");
  });
});

describe("MessageList retry affordance", () => {
  const userMessage: ChatMessageView = {
    id: "user-1",
    role: "user",
    text: "届いてる？",
    attachments: [],
    sources: [],
    delivery: "completed",
  };
  const failedAssistant: ChatMessageView = {
    id: "assistant-1",
    role: "assistant",
    text: "",
    attachments: [],
    sources: [],
    delivery: "failed",
  };
  const failedExchange: ChatMessageView[] = [userMessage, failedAssistant];

  it("offers retry on the failed message so the text being re-sent stays visible", async () => {
    const onRetry = vi.fn();
    render(
      <MessageList
        loading={false}
        phase="failed"
        messages={failedExchange}
        canRetry
        onRetry={onRetry}
      />,
    );

    // 送り直す文面が同じ画面に残っていることが、この配置の目的。
    expect(screen.getByText("届いてる？")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /再送/ }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("hides retry when the session cannot retry", () => {
    render(
      <MessageList
        loading={false}
        phase="failed"
        messages={failedExchange}
        canRetry={false}
        onRetry={vi.fn()}
      />,
    );

    expect(screen.queryByRole("button", { name: /再送/ })).not.toBeInTheDocument();
  });

  it("keeps retry on the latest failure only", () => {
    render(
      <MessageList
        loading={false}
        phase="streaming"
        messages={[
          ...failedExchange,
          {
            id: "assistant-2",
            role: "assistant",
            text: "こんばんは",
            attachments: [],
            sources: [],
            delivery: "streaming",
          },
        ]}
        canRetry
        onRetry={vi.fn()}
      />,
    );

    expect(screen.queryByRole("button", { name: /再送/ })).not.toBeInTheDocument();
  });

  it("also offers retry when the stream was lost mid-flight", () => {
    render(
      <MessageList
        loading={false}
        phase="disconnected"
        messages={[userMessage, { ...failedAssistant, delivery: "disconnected" }]}
        canRetry
        onRetry={vi.fn()}
      />,
    );

    expect(screen.getByRole("button", { name: /再送/ })).toBeInTheDocument();
  });
});

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { MessageList } from "./MessageList";

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

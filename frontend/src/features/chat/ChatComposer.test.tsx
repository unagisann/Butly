import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { CapabilitiesResponse } from "../../api/generated";
import { ChatComposer } from "./ChatComposer";

const CAPABILITIES: CapabilitiesResponse = {
  attachments: {
    max_count: 3,
    max_size_bytes: 20 * 1024 * 1024,
    allowed_mime_types: ["image/png"],
  },
  chat: { available: true },
  chat_debug: { available: false, reason: "developer_mode_disabled" },
  streaming: { available: true, mode: "incremental" },
  vision: { available: false },
  native_google_search: { available: false },
  generic_web_search: { available: false },
};

function renderComposer(overrides: Partial<CapabilitiesResponse> = {}, canCancel = true) {
  render(
    <ChatComposer
      capabilities={{ ...CAPABILITIES, ...overrides }}
      disabled={false}
      busy
      canCancel={canCancel}
      debugEnabled={false}
      debugAvailable={overrides.chat_debug?.available ?? false}
      onDebugEnabledChange={vi.fn()}
      onSend={vi.fn()}
      onCancel={vi.fn()}
    />,
  );
}

describe("ChatComposer capability gates", () => {
  it("hides developer debug and provider controls when unavailable", () => {
    renderComposer();

    expect(screen.queryByText("デバッグを表示")).not.toBeInTheDocument();
    expect(screen.queryByText("Google 検索")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "画像を添付" })).toBeDisabled();
  });

  it("disables cancellation while the request is finalizing", () => {
    renderComposer({}, false);

    expect(screen.getByRole("button", { name: "保存しています…" })).toBeDisabled();
  });
});

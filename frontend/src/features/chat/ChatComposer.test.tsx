import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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

describe("ChatComposer paste-to-attach", () => {
  function pasteImage(name = "shot.png", type = "image/png") {
    const file = new File([new Uint8Array([1, 2, 3])], name, { type });
    const event = {
      clipboardData: {
        items: [{ kind: "file", type, getAsFile: () => file }],
      },
    };
    fireEvent.paste(screen.getByRole("textbox"), event);
  }

  it("attaches an image pasted into the composer", async () => {
    renderComposer({ vision: { available: true } }, true);

    pasteImage();

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /shot\.png/ }),
      ).toBeInTheDocument(),
    );
  });

  it("names clipboard screenshots that arrive without a file name", async () => {
    renderComposer({ vision: { available: true } }, true);

    pasteImage("");

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /pasted-image\.png/ }),
      ).toBeInTheDocument(),
    );
  });

  it("ignores pasted images when the model cannot see them", async () => {
    renderComposer({ vision: { available: false } }, true);

    pasteImage();

    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByRole("button", { name: /shot\.png/ })).toBeNull();
  });

  it("leaves plain text pastes to the browser", () => {
    renderComposer({ vision: { available: true } }, true);

    const event = {
      clipboardData: { items: [{ kind: "string", type: "text/plain" }] },
    };
    // preventDefault されないこと = 通常の貼り付けが生きていること
    expect(fireEvent.paste(screen.getByRole("textbox"), event)).toBe(true);
  });
});

import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type {
  ChatRequestStatus,
  PreflightResponse,
} from "../api/generated";
import type { ApiTransport } from "../api/transport";
import type { LifecycleBridge } from "../lifecycle/bridge";
import type { BackendState, ConnectionInfo } from "../lifecycle/types";
import { App } from "./App";

const PREFLIGHT: PreflightResponse = {
  status: "ready",
  checked_at: "2026-08-12T00:00:00Z",
  connections: [],
  embedding: { configured: true, reachable: true, status: "ready" },
};

function status(state: ChatRequestStatus["state"]): ChatRequestStatus {
  return {
    request_id: "request-1",
    state,
    created_at: "2026-08-12T00:00:00Z",
  };
}

function fakeTransport(): ApiTransport {
  return {
    ping: vi.fn(async () => ({ ready: true, checks: [] })),
    listInstances: vi.fn(async () => []),
    getMessages: vi.fn(async () => ({ items: [] })),
    getCapabilities: vi.fn(async () => ({
      attachments: {
        max_count: 3,
        max_size_bytes: 20 * 1024 * 1024,
        allowed_mime_types: ["image/png"],
      },
      chat: { available: true },
      chat_debug: { available: false },
      streaming: { available: true, mode: "incremental" as const },
      vision: { available: false },
      native_google_search: { available: false },
      generic_web_search: { available: false },
    })),
    getPreflight: vi.fn(async () => PREFLIGHT),
    getChatRequest: vi.fn(async () => status("running")),
    streamChat: vi.fn(async () => ({
      event: "done" as const,
      request_id: "request-1",
      data: { full_text: "" },
    })),
    cancelChat: vi.fn(async () => status("cancelled")),
  };
}

class FakeBridge implements LifecycleBridge {
  handler: ((state: BackendState) => void) | null = null;
  state: BackendState = { phase: "starting" };
  connection: ConnectionInfo | null = null;
  restartBackend = vi.fn(async () => {});

  async getBackendState(): Promise<BackendState> {
    return this.state;
  }

  async getConnectionInfo(): Promise<ConnectionInfo | null> {
    return this.connection;
  }

  async onBackendState(
    handler: (state: BackendState) => void,
  ): Promise<() => void> {
    this.handler = handler;
    return () => {
      this.handler = null;
    };
  }

  async emit(state: BackendState): Promise<void> {
    this.state = state;
    await act(async () => {
      this.handler?.(state);
    });
  }
}

async function renderApp(
  bridge: FakeBridge,
  transport: ApiTransport = fakeTransport(),
) {
  await act(async () => {
    render(<App bridge={bridge} transportFactory={() => transport} />);
  });
}

describe("App backend lifecycle and API connectivity", () => {
  beforeEach(() => localStorage.clear());

  it("shows starting state without restart button", async () => {
    const bridge = new FakeBridge();
    await renderApp(bridge);

    expect(screen.getByTestId("backend-status")).toHaveAttribute(
      "data-phase",
      "starting",
    );
    expect(screen.queryByTestId("restart-button")).not.toBeInTheDocument();
  });

  it("enters the workspace only after both Tauri and the API are ready", async () => {
    const bridge = new FakeBridge();
    bridge.connection = {
      baseUrl: "http://127.0.0.1:43210",
      token: "test-token",
    };
    const transport = fakeTransport();
    await renderApp(bridge, transport);

    await bridge.emit({
      phase: "ready",
      port: 43210,
      backendVersion: "0.1.0",
      apiVersion: "v1",
    });

    await waitFor(() =>
      expect(screen.getByTestId("backend-status")).toHaveAttribute(
        "data-api-phase",
        "connected",
      ),
    );
    expect(
      screen.getByRole("heading", {
        level: 1,
        name: "インスタンスがありません",
      }),
    ).toBeInTheDocument();
    expect(transport.ping).toHaveBeenCalledTimes(1);
  });

  it("shows API reconnecting separately and reconnects on demand", async () => {
    const bridge = new FakeBridge();
    bridge.connection = {
      baseUrl: "http://127.0.0.1:43210",
      token: "test-token",
    };
    const transport = fakeTransport();
    vi.mocked(transport.ping)
      .mockRejectedValueOnce(new Error("connection refused"))
      .mockResolvedValue({ ready: true, checks: [] });
    await renderApp(bridge, transport);

    await bridge.emit({
      phase: "ready",
      port: 43210,
      backendVersion: "0.1.0",
      apiVersion: "v1",
    });
    await waitFor(() =>
      expect(screen.getByTestId("backend-status")).toHaveAttribute(
        "data-api-phase",
        "reconnecting",
      ),
    );
    expect(screen.getByTestId("backend-status")).toHaveAttribute("data-phase", "ready");

    await userEvent.click(screen.getByRole("button", { name: "今すぐ再接続" }));
    await waitFor(() =>
      expect(screen.getByTestId("backend-status")).toHaveAttribute(
        "data-api-phase",
        "connected",
      ),
    );
  });

  it("shows restart on crash and invokes the lifecycle bridge", async () => {
    const bridge = new FakeBridge();
    await renderApp(bridge);
    await bridge.emit({ phase: "crashed", detail: "exit_code=1" });

    expect(screen.getByTestId("backend-detail")).toHaveTextContent("exit_code=1");
    await userEvent.click(screen.getByTestId("restart-button"));
    expect(bridge.restartBackend).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("backend-status")).toHaveAttribute(
      "data-phase",
      "starting",
    );
  });

  it("shows version mismatch and allows a restart", async () => {
    const bridge = new FakeBridge();
    await renderApp(bridge);
    await bridge.emit({
      phase: "version_mismatch",
      detail: "expected v1, got v9",
    });

    expect(screen.getByTestId("backend-status")).toHaveAttribute(
      "data-phase",
      "version_mismatch",
    );
    expect(screen.getByTestId("restart-button")).toBeInTheDocument();
  });

  it("switches the complete interface catalog between Japanese and English", async () => {
    const bridge = new FakeBridge();
    await renderApp(bridge);
    expect(document.documentElement.lang).toBe("ja");

    await userEvent.click(
      screen.getByRole("button", { name: "表示言語を英語に切り替える" }),
    );
    expect(screen.getByText("Conversations connected to your memories")).toBeInTheDocument();
    expect(document.documentElement.lang).toBe("en");
  });
});

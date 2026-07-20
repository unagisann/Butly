import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { getHealth, getReadiness } from "../api/generated";
import type { LifecycleBridge } from "../lifecycle/bridge";
import type { BackendState, ConnectionInfo } from "../lifecycle/types";
import { App } from "./App";

vi.mock("../api/generated", () => ({
  getHealth: vi.fn(async () => ({
    data: { backend_version: "0.1.0", api_version: "v1", status: "ok" },
  })),
  getReadiness: vi.fn(async () => ({
    data: { ready: true, checks: [] },
  })),
}));

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

async function renderApp(bridge: FakeBridge) {
  await act(async () => {
    render(<App bridge={bridge} />);
  });
}

describe("App shell backend states", () => {
  it("shows starting state without restart button", async () => {
    const bridge = new FakeBridge();
    await renderApp(bridge);

    const status = screen.getByTestId("backend-status");
    expect(status).toHaveAttribute("data-phase", "starting");
    expect(screen.queryByTestId("restart-button")).not.toBeInTheDocument();
  });

  it("shows connected state and versions when ready", async () => {
    const bridge = new FakeBridge();
    bridge.connection = {
      baseUrl: "http://127.0.0.1:43210",
      token: "test-token",
    };
    await renderApp(bridge);

    await bridge.emit({
      phase: "ready",
      port: 43210,
      backendVersion: "0.1.0",
      apiVersion: "v1",
    });

    expect(screen.getByTestId("backend-status")).toHaveAttribute(
      "data-phase",
      "ready",
    );
    const versions = screen.getByTestId("backend-versions");
    expect(versions).toHaveTextContent("0.1.0");
    expect(versions).toHaveTextContent("v1");
    expect(screen.queryByTestId("restart-button")).not.toBeInTheDocument();
    await waitFor(() => {
      expect(getHealth).toHaveBeenCalledTimes(1);
      expect(getReadiness).toHaveBeenCalledTimes(1);
    });
  });

  it("shows restart button on crash and restarts via bridge", async () => {
    const bridge = new FakeBridge();
    await renderApp(bridge);

    await bridge.emit({ phase: "crashed", detail: "exit_code=1" });

    expect(screen.getByTestId("backend-status")).toHaveAttribute(
      "data-phase",
      "crashed",
    );
    expect(screen.getByTestId("backend-detail")).toHaveTextContent(
      "exit_code=1",
    );

    const button = screen.getByTestId("restart-button");
    await userEvent.click(button);

    expect(bridge.restartBackend).toHaveBeenCalledTimes(1);
    // restart 直後は UI 上も starting に戻る
    expect(screen.getByTestId("backend-status")).toHaveAttribute(
      "data-phase",
      "starting",
    );
  });

  it("shows version mismatch state with restart button", async () => {
    const bridge = new FakeBridge();
    await renderApp(bridge);

    await bridge.emit({
      phase: "version_mismatch",
      detail: "expected backend 0.1.0 / api v1, got 9.9.9 / v9",
    });

    expect(screen.getByTestId("backend-status")).toHaveAttribute(
      "data-phase",
      "version_mismatch",
    );
    expect(screen.getByTestId("restart-button")).toBeInTheDocument();
  });

  it("transitions crashed -> ready clears restart button", async () => {
    const bridge = new FakeBridge();
    await renderApp(bridge);

    await bridge.emit({ phase: "crashed" });
    expect(screen.getByTestId("restart-button")).toBeInTheDocument();

    await bridge.emit({
      phase: "ready",
      port: 43210,
      backendVersion: "0.1.0",
      apiVersion: "v1",
    });
    expect(screen.queryByTestId("restart-button")).not.toBeInTheDocument();
  });
});

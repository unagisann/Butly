import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ApiTransport } from "../api/transport";
import type { LifecycleBridge } from "../lifecycle/bridge";
import { useApiConnection } from "./useApiConnection";

describe("useApiConnection", () => {
  afterEach(() => vi.useRealTimers());

  it("times out a readiness probe that never settles", async () => {
    vi.useFakeTimers();
    const bridge: LifecycleBridge = {
      getBackendState: vi.fn(async () => ({ phase: "ready" as const })),
      getConnectionInfo: vi.fn(async () => ({
        baseUrl: "http://127.0.0.1:43210",
        token: "token",
      })),
      restartBackend: vi.fn(async () => {}),
      onBackendState: vi.fn(async () => () => {}),
    };
    const transport = {
      ping: vi.fn(() => new Promise<never>(() => {})),
    } as unknown as ApiTransport;
    const transportFactory = vi.fn(() => transport);
    const { result, unmount } = renderHook(() =>
      useApiConnection(
        { phase: "ready", port: 43210 },
        bridge,
        transportFactory,
      ),
    );

    await act(async () => Promise.resolve());
    await act(async () => vi.advanceTimersByTimeAsync(3_000));
    expect(result.current.phase).toBe("reconnecting");
    expect(result.current.detail).toBe("api_probe_timeout");
    unmount();
  });
});

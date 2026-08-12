import { afterEach, describe, expect, it, vi } from "vitest";

import { createLifecycleBridge } from "./bridge";

function healthResponse(): Response {
  return new Response(
    JSON.stringify({ status: "ok", backend_version: "0.1.0", api_version: "v1" }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("createLifecycleBridge (dev browser)", () => {
  it("probes the sidecar through the dev server's same-origin /api proxy", async () => {
    const fetchMock = vi.fn().mockResolvedValue(healthResponse());
    vi.stubGlobal("fetch", fetchMock);

    const bridge = createLifecycleBridge();

    await expect(bridge.getBackendState()).resolves.toEqual({
      phase: "ready",
      backendVersion: "0.1.0",
      apiVersion: "v1",
      detail: "dev_browser",
    });
    expect(fetchMock).toHaveBeenCalledWith(
      `${window.location.origin}/api/v1/health`,
      expect.anything(),
    );
  });

  it("hands the transport a same-origin base URL and no token", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(healthResponse()));

    // token を持たないのは意図。dev browser 経路は認証オフの loopback sidecar 専用。
    await expect(createLifecycleBridge().getConnectionInfo()).resolves.toEqual({
      baseUrl: window.location.origin,
      token: null,
    });
  });

  it("reports a missing proxy separately from a broken backend", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("", { status: 404 })),
    );

    await expect(createLifecycleBridge().getBackendState()).resolves.toEqual({
      phase: "unavailable",
      detail: "dev_backend_not_configured",
    });
  });

  it("keeps other HTTP failures distinguishable", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("", { status: 503 })),
    );

    await expect(createLifecycleBridge().getBackendState()).resolves.toEqual({
      phase: "unavailable",
      detail: "dev_health_http_503",
    });
  });

  it("reports unavailable instead of throwing when the sidecar is down", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValue(new TypeError("Failed to fetch")),
    );

    await expect(createLifecycleBridge().getBackendState()).resolves.toEqual({
      phase: "unavailable",
      detail: "dev_health_failed_TypeError",
    });
  });

  it("re-probes and notifies subscribers on restart", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response("", { status: 503 }))
      .mockImplementation(async () => healthResponse());
    vi.stubGlobal("fetch", fetchMock);

    const bridge = createLifecycleBridge();
    const states: string[] = [];
    const unlisten = await bridge.onBackendState((state) => states.push(state.phase));

    await expect(bridge.getBackendState()).resolves.toEqual({
      phase: "unavailable",
      detail: "dev_health_http_503",
    });
    await bridge.restartBackend();
    expect(states).toEqual(["ready"]);

    unlisten();
    await bridge.restartBackend();
    expect(states).toEqual(["ready"]);
  });
});

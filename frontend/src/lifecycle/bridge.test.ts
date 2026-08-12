import { afterEach, describe, expect, it, vi } from "vitest";

import { createLifecycleBridge } from "./bridge";

const DEV_URL_ENV = "VITE_BUTLY_DEV_BACKEND_URL";

function healthResponse(): Response {
  return new Response(
    JSON.stringify({ status: "ok", backend_version: "0.1.0", api_version: "v1" }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );
}

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe("createLifecycleBridge", () => {
  it("falls back to NullBridge when neither Tauri nor dev URL is present", async () => {
    vi.stubEnv(DEV_URL_ENV, "");
    const bridge = createLifecycleBridge();

    await expect(bridge.getBackendState()).resolves.toEqual({
      phase: "unavailable",
      detail: "not_running_in_tauri",
    });
    await expect(bridge.getConnectionInfo()).resolves.toBeNull();
  });

  it("connects to the manually started sidecar when a dev URL is set", async () => {
    vi.stubEnv(DEV_URL_ENV, "http://localhost:8000/");
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
      "http://localhost:8000/api/v1/health",
      expect.anything(),
    );
    // dev browser 経路は token を持たない（VITE_* は bundle に inline されるため）
    await expect(bridge.getConnectionInfo()).resolves.toEqual({
      baseUrl: "http://localhost:8000",
      token: null,
    });
  });

  it("reports unavailable instead of throwing when the dev sidecar is down", async () => {
    vi.stubEnv(DEV_URL_ENV, "http://localhost:8000");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValue(new TypeError("Failed to fetch")),
    );

    const bridge = createLifecycleBridge();

    await expect(bridge.getBackendState()).resolves.toEqual({
      phase: "unavailable",
      detail: "dev_health_failed_TypeError",
    });
  });

  it("re-probes and notifies subscribers on restart", async () => {
    vi.stubEnv(DEV_URL_ENV, "http://localhost:8000");
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

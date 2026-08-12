import { useCallback, useEffect, useReducer, useState } from "react";

import type { ApiTransport, ApiTransportFactory } from "../api/transport";
import type { LifecycleBridge } from "../lifecycle/bridge";
import type { BackendState } from "../lifecycle/types";

export type ApiConnectionPhase =
  | "idle"
  | "connecting"
  | "connected"
  | "reconnecting"
  | "disconnected";

export interface ApiConnectionState {
  phase: ApiConnectionPhase;
  transport: ApiTransport | null;
  detail: string | null;
  reconnectCount: number;
  connectionRevision: number;
}

const INITIAL_STATE: ApiConnectionState = {
  phase: "idle",
  transport: null,
  detail: null,
  reconnectCount: 0,
  connectionRevision: 0,
};

interface UseApiConnectionResult extends ApiConnectionState {
  retryNow: () => void;
}

const HEALTHY_POLL_MS = 5_000;
const MAX_BACKOFF_MS = 10_000;
const PROBE_TIMEOUT_MS = 3_000;

async function pingWithTimeout(
  transport: ApiTransport,
  disposalSignal: AbortSignal,
): Promise<void> {
  const probeController = new AbortController();
  let timeout: ReturnType<typeof setTimeout> | null = null;
  const abortProbe = () => probeController.abort(disposalSignal.reason);
  disposalSignal.addEventListener("abort", abortProbe, { once: true });

  const timeoutPromise = new Promise<never>((_, reject) => {
    timeout = setTimeout(() => {
      probeController.abort(new DOMException("API probe timed out.", "TimeoutError"));
      reject(new Error("api_probe_timeout"));
    }, PROBE_TIMEOUT_MS);
  });
  let rejectDisposed: (() => void) | null = null;
  const disposalPromise = new Promise<never>((_, reject) => {
    if (disposalSignal.aborted) {
      reject(disposalSignal.reason);
      return;
    }
    rejectDisposed = () =>
      reject(disposalSignal.reason ?? new DOMException("Aborted", "AbortError"));
    disposalSignal.addEventListener("abort", rejectDisposed, { once: true });
  });

  try {
    const readiness = await Promise.race([
      transport.ping(probeController.signal),
      timeoutPromise,
      disposalPromise,
    ]);
    if (!readiness.ready) throw new Error("backend_not_ready");
  } finally {
    if (timeout) clearTimeout(timeout);
    disposalSignal.removeEventListener("abort", abortProbe);
    if (rejectDisposed) disposalSignal.removeEventListener("abort", rejectDisposed);
  }
}

export function useApiConnection(
  lifecycleState: BackendState,
  bridge: LifecycleBridge,
  transportFactory: ApiTransportFactory,
): UseApiConnectionResult {
  const [state, setState] = useState<ApiConnectionState>(INITIAL_STATE);
  const [retryToken, retryNow] = useReducer((value: number) => value + 1, 0);

  useEffect(() => {
    if (lifecycleState.phase !== "ready") {
      setState(INITIAL_STATE);
      return;
    }

    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let activeTransport: ApiTransport | null = null;
    let consecutiveFailures = 0;
    let wasConnected = false;
    const controller = new AbortController();

    setState((current) => ({
      ...current,
      phase: current.transport ? "reconnecting" : "connecting",
      detail: null,
    }));

    const schedule = (delay: number) => {
      timer = setTimeout(() => {
        void probe();
      }, delay);
    };

    const probe = async () => {
      try {
        if (!activeTransport) {
          const info = await bridge.getConnectionInfo();
          if (!info) throw new Error("connection_info_unavailable");
          activeTransport = transportFactory(info);
        }
        await pingWithTimeout(activeTransport, controller.signal);
        if (disposed) return;

        const revisionIncrement = wasConnected ? 0 : 1;
        wasConnected = true;
        consecutiveFailures = 0;
        setState((current) => ({
          phase: "connected",
          transport: activeTransport,
          detail: null,
          reconnectCount: current.reconnectCount,
          connectionRevision: current.connectionRevision + revisionIncrement,
        }));
        schedule(HEALTHY_POLL_MS);
      } catch (error) {
        if (disposed || controller.signal.aborted) return;
        consecutiveFailures += 1;
        const delay = Math.min(1_000 * 2 ** (consecutiveFailures - 1), MAX_BACKOFF_MS);
        setState((current) => ({
          ...current,
          phase: consecutiveFailures >= 3 ? "disconnected" : "reconnecting",
          transport: activeTransport ?? current.transport,
          detail: error instanceof Error ? error.message : String(error),
          reconnectCount: current.reconnectCount + 1,
        }));
        wasConnected = false;
        schedule(delay);
      }
    };

    void probe();

    return () => {
      disposed = true;
      controller.abort();
      if (timer) clearTimeout(timer);
    };
  }, [
    bridge,
    lifecycleState.apiVersion,
    lifecycleState.backendVersion,
    lifecycleState.phase,
    lifecycleState.port,
    retryToken,
    transportFactory,
  ]);

  const retry = useCallback(() => {
    setState((current) => ({
      ...current,
      phase: current.transport ? "reconnecting" : "connecting",
      detail: null,
    }));
    retryNow();
  }, []);

  return { ...state, retryNow: retry };
}

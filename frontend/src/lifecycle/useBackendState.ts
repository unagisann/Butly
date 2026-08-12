import { useCallback, useEffect, useState } from "react";

import type { LifecycleBridge } from "./bridge";
import type { BackendState } from "./types";

export interface BackendStateHook {
  state: BackendState;
  restart: () => Promise<void>;
}

const INITIAL_STATE: BackendState = { phase: "starting" };

export function useBackendState(bridge: LifecycleBridge): BackendStateHook {
  const [state, setState] = useState<BackendState>(INITIAL_STATE);

  useEffect(() => {
    let unlisten: (() => void) | null = null;
    let disposed = false;

    (async () => {
      // event 購読を先に張ってから現在値を取り、初期状態の取りこぼしを防ぐ
      unlisten = await bridge.onBackendState((next) => {
        if (!disposed) setState(next);
      });
      const current = await bridge.getBackendState();
      if (!disposed) setState(current);
    })().catch((err: unknown) => {
      if (!disposed) {
        setState({
          phase: "unavailable",
          detail: err instanceof Error ? err.message : String(err),
        });
      }
    });

    return () => {
      disposed = true;
      if (unlisten) unlisten();
    };
  }, [bridge]);

  const restart = useCallback(async () => {
    setState({ phase: "starting" });
    try {
      await bridge.restartBackend();
    } catch (error) {
      setState({
        phase: "unavailable",
        detail: error instanceof Error ? error.message : String(error),
      });
    }
  }, [bridge]);

  return { state, restart };
}

import { useCallback, useEffect, useReducer, useState } from "react";

import type { CapabilitiesResponse } from "../../api/generated";
import { isAbortError, normalizeApiError } from "../../api/errors";
import type { ApiTransport } from "../../api/transport";

export function useCapabilities(
  transport: ApiTransport,
  connectionRevision: number,
): {
  capabilities: CapabilitiesResponse | null;
  error: string | null;
  loading: boolean;
  refresh: () => void;
} {
  const [capabilities, setCapabilities] = useState<CapabilitiesResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshToken, bumpRefresh] = useReducer((value: number) => value + 1, 0);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    transport
      .getCapabilities(controller.signal)
      .then((next) => {
        if (!controller.signal.aborted) setCapabilities(next);
      })
      .catch((cause: unknown) => {
        if (!isAbortError(cause)) setError(normalizeApiError(cause).message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [connectionRevision, refreshToken, transport]);

  const refresh = useCallback(() => bumpRefresh(), []);
  return { capabilities, error, loading, refresh };
}

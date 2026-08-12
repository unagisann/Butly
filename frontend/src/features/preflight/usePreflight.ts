import { useCallback, useEffect, useReducer, useState } from "react";

import { isAbortError, normalizeApiError } from "../../api/errors";
import type { PreflightResponse } from "../../api/generated";
import type { ApiTransport } from "../../api/transport";

interface UsePreflightOptions {
  transport: ApiTransport;
  connectionRevision: number;
}

export interface PreflightState {
  report: PreflightResponse | null;
  loading: boolean;
  error: string | null;
  refresh: () => void;
}

export function usePreflight({
  transport,
  connectionRevision,
}: UsePreflightOptions): PreflightState {
  const [report, setReport] = useState<PreflightResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshToken, bumpRefresh] = useReducer((value: number) => value + 1, 0);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    transport
      .getPreflight(controller.signal, refreshToken > 0)
      .then((nextReport) => {
        if (controller.signal.aborted) return;
        setReport(nextReport);
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
  return { report, loading, error, refresh };
}

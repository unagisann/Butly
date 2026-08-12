import { useCallback, useEffect, useReducer, useState } from "react";

import type { InstanceSummary } from "../../api/generated";
import type { ApiTransport } from "../../api/transport";
import { isAbortError, normalizeApiError } from "../../api/errors";

interface UseInstancesOptions {
  transport: ApiTransport;
  connectionRevision: number;
}

export interface InstancesState {
  items: InstanceSummary[];
  selectedName: string | null;
  loading: boolean;
  error: string | null;
  select: (name: string) => void;
  refresh: () => void;
}

export function useInstances({
  transport,
  connectionRevision,
}: UseInstancesOptions): InstancesState {
  const [items, setItems] = useState<InstanceSummary[]>([]);
  const [selectedName, setSelectedName] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshToken, refresh] = useReducer((value: number) => value + 1, 0);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    transport
      .listInstances(controller.signal)
      .then((nextItems) => {
        if (controller.signal.aborted) return;
        setItems(nextItems);
        setSelectedName((current) => {
          if (current && nextItems.some((item) => item.name === current)) return current;
          return nextItems[0]?.name ?? null;
        });
      })
      .catch((cause: unknown) => {
        if (!isAbortError(cause)) setError(normalizeApiError(cause).message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [connectionRevision, refreshToken, transport]);

  const select = useCallback((name: string) => setSelectedName(name), []);
  return { items, selectedName, loading, error, select, refresh };
}

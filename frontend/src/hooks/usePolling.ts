import { useCallback, useEffect, useRef, useState } from 'react';
import { isAbortError } from '../api/client';

export type Interval<T> = number | null | ((data: T | undefined) => number | null);

export type PollingState<T> = {
  data: T | undefined;
  error: string | null;
  /** true only until the first response arrives */
  loading: boolean;
  /** true while any request (including background ones) is in flight */
  refreshing: boolean;
  refresh: () => void;
};

/**
 * Load `fetcher` now and then keep it fresh.
 *
 * - The next poll is scheduled only after the previous request finished, so requests never overlap.
 * - Polling pauses while the tab is hidden and refreshes immediately when it becomes visible again.
 * - `interval` may be a function of the latest data (e.g. poll fast only while jobs are active); null = no auto refresh.
 * - Changing `deps` (page, filter, id...) aborts the running request, and a stale response can never overwrite newer state.
 * - Unmounting clears the timer, aborts the request and stops all state updates.
 */
export function usePolling<T>(fetcher: (signal: AbortSignal) => Promise<T>, interval: Interval<T>, deps: readonly unknown[]): PollingState<T> {
  const [data, setData] = useState<T | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const fetcherRef = useRef(fetcher);
  const intervalRef = useRef(interval);
  const dataRef = useRef<T | undefined>(undefined);
  const refreshRef = useRef<() => void>(() => undefined);
  fetcherRef.current = fetcher;
  intervalRef.current = interval;

  useEffect(() => {
    let stopped = false;
    let inflight = false;
    let queued = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const controller = new AbortController();

    const clear = () => { if (timer !== undefined) { clearTimeout(timer); timer = undefined; } };
    const schedule = () => {
      clear();
      if (stopped || document.hidden) return;
      const current = intervalRef.current;
      const ms = typeof current === 'function' ? current(dataRef.current) : current;
      if (ms !== null && ms !== undefined) timer = setTimeout(() => { timer = undefined; void tick(); }, ms);
    };
    const tick = async () => {
      if (stopped) return;
      if (inflight) { queued = true; return; } // coalesce: run once more right after the current request
      inflight = true;
      setRefreshing(true);
      try {
        const value = await fetcherRef.current(controller.signal);
        if (!stopped) { dataRef.current = value; setData(value); setError(null); }
      } catch (caught) {
        if (!stopped && !isAbortError(caught)) setError(caught instanceof Error ? caught.message : String(caught));
      } finally {
        inflight = false;
        if (!stopped) {
          setLoading(false);
          setRefreshing(false);
          if (queued) { queued = false; void tick(); } else schedule();
        }
      }
    };
    const onVisibility = () => {
      if (document.hidden) clear();
      else if (!inflight) void tick();
    };

    refreshRef.current = () => { clear(); void tick(); };
    document.addEventListener('visibilitychange', onVisibility);
    void tick();
    return () => {
      stopped = true;
      clear();
      controller.abort();
      document.removeEventListener('visibilitychange', onVisibility);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  const refresh = useCallback(() => refreshRef.current(), []);
  return { data, error, loading, refreshing, refresh };
}

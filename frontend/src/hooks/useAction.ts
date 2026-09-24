import { useCallback, useEffect, useRef, useState } from 'react';

/**
 * Runs one async action at a time (a second click while busy is ignored, so a double click cannot
 * enqueue/retry twice) and exposes `busyKey` for disabling the right button and `error` for display.
 */
export function useAction() {
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const busy = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const run = useCallback(async (key: string, action: () => Promise<unknown>, success?: string): Promise<boolean> => {
    if (busy.current) return false;
    busy.current = true;
    setBusyKey(key);
    setError(null);
    setNotice(null);
    try {
      await action();
      if (mounted.current && success) setNotice(success);
      return true;
    } catch (caught) {
      if (mounted.current) setError(caught instanceof Error ? caught.message : String(caught));
      return false;
    } finally {
      busy.current = false;
      if (mounted.current) setBusyKey(null);
    }
  }, []);

  return {
    run,
    busyKey,
    busy: busyKey !== null,
    error,
    notice,
    clear: () => {
      setError(null);
      setNotice(null);
    },
  };
}

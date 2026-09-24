import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { usePolling } from './usePolling';

const setHidden = (hidden: boolean) => {
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
  document.dispatchEvent(new Event('visibilitychange'));
};

const flush = () =>
  act(async () => {
    await Promise.resolve();
  });
const advance = (ms: number) =>
  act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });

beforeEach(() => {
  vi.useFakeTimers();
  setHidden(false);
});
afterEach(() => {
  vi.useRealTimers();
  setHidden(false);
});

describe('usePolling', () => {
  it('loads immediately, then polls after each completed request', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce('a').mockResolvedValueOnce('b').mockResolvedValue('c');
    const { result } = renderHook(() => usePolling(fetcher, 1000, []));
    expect(result.current.loading).toBe(true);
    await flush();
    expect(result.current.data).toBe('a');
    expect(result.current.loading).toBe(false);
    await advance(1000);
    expect(result.current.data).toBe('b');
    await advance(1000);
    expect(fetcher).toHaveBeenCalledTimes(3);
  });

  it('never overlaps requests: the next poll waits for the slow one', async () => {
    let resolve: (value: string) => void = () => undefined;
    const fetcher = vi.fn(
      () =>
        new Promise<string>((done) => {
          resolve = done;
        }),
    );
    renderHook(() => usePolling(fetcher, 1000, []));
    await advance(10_000);
    expect(fetcher).toHaveBeenCalledTimes(1); // still waiting on the first response, no piled-up requests
    await act(async () => {
      resolve('x');
    });
    await advance(1000);
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it('pauses while the tab is hidden and refreshes right away when it comes back', async () => {
    const fetcher = vi.fn().mockResolvedValue('v');
    renderHook(() => usePolling(fetcher, 1000, []));
    await flush();
    expect(fetcher).toHaveBeenCalledTimes(1);
    act(() => setHidden(true));
    await advance(10_000);
    expect(fetcher).toHaveBeenCalledTimes(1);
    act(() => setHidden(false));
    await flush();
    expect(fetcher).toHaveBeenCalledTimes(2);
    await advance(1000);
    expect(fetcher).toHaveBeenCalledTimes(3);
  });

  it('stops everything on unmount: no timers, aborted request, no state update', async () => {
    let signal: AbortSignal | undefined;
    let resolve: (value: string) => void = () => undefined;
    const fetcher = vi.fn((s: AbortSignal) => {
      signal = s;
      return new Promise<string>((done) => {
        resolve = done;
      });
    });
    const { result, unmount } = renderHook(() => usePolling(fetcher, 1000, []));
    unmount();
    expect(signal?.aborted).toBe(true);
    await act(async () => {
      resolve('late');
    });
    expect(result.current.data).toBeUndefined();
    await advance(10_000);
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
    const listeners = vi.spyOn(document, 'removeEventListener');
    unmount();
    listeners.mockRestore();
  });

  it('unmount while idle clears the pending timer and the visibility listener', async () => {
    const fetcher = vi.fn().mockResolvedValue('v');
    const add = vi.spyOn(document, 'addEventListener');
    const remove = vi.spyOn(document, 'removeEventListener');
    const { unmount } = renderHook(() => usePolling(fetcher, 1000, []));
    await flush();
    expect(vi.getTimerCount()).toBe(1);
    unmount();
    expect(vi.getTimerCount()).toBe(0);
    expect(remove.mock.calls.filter(([type]) => type === 'visibilitychange').length).toBe(add.mock.calls.filter(([type]) => type === 'visibilitychange').length);
  });

  it('supports adaptive intervals and null (no auto refresh)', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce({ active: true }).mockResolvedValueOnce({ active: false }).mockResolvedValue({ active: false });
    renderHook(() => usePolling(fetcher, (data: { active: boolean } | undefined) => (data?.active ? 1000 : null), []));
    await flush();
    await advance(1000);
    expect(fetcher).toHaveBeenCalledTimes(2);
    await advance(60_000);
    expect(fetcher).toHaveBeenCalledTimes(2); // went idle: polling stopped
  });

  it('a stale response from previous deps never overwrites newer data', async () => {
    const resolvers: Record<string, (value: string) => void> = {};
    const fetcher = vi.fn(
      (_signal: AbortSignal, page: number) =>
        new Promise<string>((done) => {
          resolvers[`p${page}`] = done;
        }),
    );
    const { result, rerender } = renderHook(({ page }) => usePolling((signal) => fetcher(signal, page), null, [page]), { initialProps: { page: 1 } });
    rerender({ page: 2 });
    await act(async () => {
      resolvers.p2('page 2');
    });
    await act(async () => {
      resolvers.p1('page 1 (stale)');
    });
    expect(result.current.data).toBe('page 2');
  });

  it('manual refresh while a request is running runs exactly one follow-up', async () => {
    const resolvers: ((value: string) => void)[] = [];
    const fetcher = vi.fn(
      () =>
        new Promise<string>((done) => {
          resolvers.push(done);
        }),
    );
    const { result } = renderHook(() => usePolling(fetcher, null, []));
    act(() => {
      result.current.refresh();
      result.current.refresh();
      result.current.refresh();
    });
    expect(fetcher).toHaveBeenCalledTimes(1);
    await act(async () => {
      resolvers[0]('first');
    });
    expect(fetcher).toHaveBeenCalledTimes(2);
    await act(async () => {
      resolvers[1]('second');
    });
    expect(result.current.data).toBe('second');
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it('reports errors without stopping the polling loop', async () => {
    const fetcher = vi.fn().mockRejectedValueOnce(new Error('boom')).mockResolvedValue('ok');
    const { result } = renderHook(() => usePolling(fetcher, 1000, []));
    await flush();
    expect(result.current.error).toBe('boom');
    await advance(1000);
    expect(result.current.data).toBe('ok');
    expect(result.current.error).toBeNull();
  });
});

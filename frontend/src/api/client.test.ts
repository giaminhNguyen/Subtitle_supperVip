import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, api, formatDetail, query } from './client';

const respond = (body: unknown, init: ResponseInit = {}) => new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' }, ...init });
const mockFetch = (response: Response | (() => Response)) => vi.spyOn(globalThis, 'fetch').mockImplementation(async () => (typeof response === 'function' ? response() : response));

afterEach(() => vi.restoreAllMocks());

describe('api client', () => {
  it('reads the total from X-Total-Count and sends limit/offset/filter', async () => {
    const spy = mockFetch(respond([{ id: 'j1' }], { headers: { 'X-Total-Count': '120', 'Content-Type': 'application/json' } }));
    const page = await api.jobs({ limit: 50, offset: 100, status: 'failed' });
    expect(page).toEqual({ items: [{ id: 'j1' }], total: 120 });
    expect(String(spy.mock.calls[0][0])).toMatch(/\/jobs\?limit=50&offset=100&status=failed$/);
  });

  it('falls back to the item count when the total header is missing', async () => {
    mockFetch(respond([1, 2, 3]));
    expect((await api.logs({ limit: 10, offset: 0 })).total).toBe(3);
  });

  it('omits empty filters from the query string', () => {
    expect(query({ limit: 5, status: '', offset: 0, q: undefined })).toBe('?limit=5&offset=0');
    expect(query({})).toBe('');
  });

  it('turns API errors into ApiError with the server message', async () => {
    mockFetch(respond({ detail: 'Đã có job đang hoạt động' }, { status: 409 }));
    await expect(api.jobAction('j1', 'retry')).rejects.toMatchObject({ name: 'ApiError', status: 409, message: 'Đã có job đang hoạt động' });
  });

  it('formats validation error lists and unknown bodies', () => {
    expect(formatDetail([{ msg: 'field required' }, { msg: 'too short' }], 422)).toBe('field required; too short');
    expect(formatDetail(undefined, 500)).toBe('Lỗi 500');
  });

  it('explains network failures in plain language', async () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('Failed to fetch'));
    const error = await api.dashboard().catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).message).toMatch(/start-app\.bat/);
  });

  it('lets aborts through untouched so callers can ignore them', async () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new DOMException('aborted', 'AbortError'));
    await expect(api.dashboard()).rejects.toMatchObject({ name: 'AbortError' });
  });

  it('health() returns the 503 body instead of throwing, and a critical stub when unreachable', async () => {
    mockFetch(respond({ ok: false, status: 'critical', api: 'ok', database: 'error' }, { status: 503 }));
    expect((await api.health()).status).toBe('critical');
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('down'));
    expect(await api.health()).toMatchObject({ ok: false, status: 'critical', api: 'unreachable' });
  });

  it('never sends the API key anywhere except the save call', async () => {
    const spy = mockFetch(() => respond({ configured: true }));
    await api.saveApiKey('AIzaSy-secret-key');
    expect(spy.mock.calls[0][1]?.method).toBe('PUT');
    await api.youtubeConfig();
    expect(JSON.stringify(spy.mock.calls[1])).not.toContain('secret');
  });
});

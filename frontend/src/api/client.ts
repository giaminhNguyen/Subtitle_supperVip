import type { Channel, ChannelDetail, ChannelSettings, DashboardStats, Diagnostics, Health, Job, JobAction, JobLog, JobStatus, Page, PageQuery, Video, VideoStatus, YouTubeConfig } from '../types';

export const API_BASE: string = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000/api';
const HEALTH_URL = `${API_BASE.replace(/\/api\/?$/, '')}/health`;

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

/** FastAPI returns `detail` as a string or (validation errors) a list of {msg}. */
export function formatDetail(detail: unknown, status: number): string {
  if (typeof detail === 'string' && detail) return detail;
  if (Array.isArray(detail)) {
    const messages = detail.map((item) => (typeof item === 'object' && item && 'msg' in item ? String((item as { msg: unknown }).msg) : '')).filter(Boolean);
    if (messages.length) return messages.join('; ');
  }
  return `Lỗi ${status}`;
}

export const isAbortError = (error: unknown): boolean => error instanceof DOMException && error.name === 'AbortError';

type Options = { method?: string; body?: unknown; signal?: AbortSignal };

async function send(url: string, { method, body, signal }: Options = {}): Promise<Response> {
  let response: Response;
  try {
    response = await fetch(url, { method, signal, headers: { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch (error) {
    if (isAbortError(error)) throw error;
    throw new ApiError('Không kết nối được tới máy chủ. Hãy kiểm tra start-app.bat đang chạy.', 0);
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new ApiError(formatDetail(payload?.detail, response.status), response.status);
  }
  return response;
}

async function json<T>(path: string, options?: Options): Promise<T> {
  return (await send(`${API_BASE}${path}`, options)).json() as Promise<T>;
}

async function page<T>(path: string, options?: Options): Promise<Page<T>> {
  const response = await send(`${API_BASE}${path}`, options);
  const items = (await response.json()) as T[];
  const total = Number(response.headers.get('X-Total-Count'));
  return { items, total: Number.isFinite(total) && response.headers.has('X-Total-Count') ? total : items.length };
}

export function query(params: Record<string, string | number | undefined | null>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) if (value !== undefined && value !== null && value !== '') search.set(key, String(value));
  const text = search.toString();
  return text ? `?${text}` : '';
}

export const api = {
  dashboard: (signal?: AbortSignal) => json<DashboardStats>('/dashboard', { signal }),
  channels: (signal?: AbortSignal) => json<Channel[]>('/channels', { signal }),
  channel: (id: string, signal?: AbortSignal) => json<ChannelDetail>(`/channels/${id}`, { signal }),
  addChannel: (url: string) => json<Channel>('/channels', { method: 'POST', body: { url } }),
  updateSettings: (id: string, settings: ChannelSettings) => json<ChannelSettings>(`/channels/${id}/settings`, { method: 'PUT', body: settings }),
  scan: (id: string, mode: 'all' | 'retryable') => json<{ job_id: string }>(`/channels/${id}/scan`, { method: 'POST', body: { mode } }),
  syncNew: (id: string) => json<{ job_id: string }>(`/channels/${id}/sync`, { method: 'POST' }),
  videos: (id: string, params: PageQuery & { status?: VideoStatus | '' }, signal?: AbortSignal) => page<Video>(`/channels/${id}/videos${query(params)}`, { signal }),
  downloadVideo: (id: string, force: boolean) => json<{ job_id: string }>(`/videos/${id}/download${query({ force: force ? 'true' : undefined })}`, { method: 'POST' }),
  youtubeConfig: (signal?: AbortSignal) => json<YouTubeConfig>('/config/youtube', { signal }),
  saveApiKey: (apiKey: string) => json<YouTubeConfig>('/config/youtube', { method: 'PUT', body: { api_key: apiKey } }),
  jobs: (params: PageQuery & { status?: JobStatus | '' }, signal?: AbortSignal) => page<Job>(`/jobs${query(params)}`, { signal }),
  jobAction: (id: string, action: JobAction) => json<Job>(`/jobs/${id}/${action}`, { method: 'POST' }),
  logs: (params: PageQuery, signal?: AbortSignal) => page<JobLog>(`/logs${query(params)}`, { signal }),
  diagnostics: (signal?: AbortSignal) => json<Diagnostics>('/diagnostics', { signal }),
  /** /health answers 503 with a JSON body when critical; that body is still the answer we want to show. */
  health: async (signal?: AbortSignal): Promise<Health> => {
    let response: Response;
    try { response = await fetch(HEALTH_URL, { signal }); } catch (error) {
      if (isAbortError(error)) throw error;
      return { ok: false, status: 'critical', api: 'unreachable', database: 'unknown' };
    }
    return (await response.json().catch(() => ({ ok: false, status: 'critical', api: 'error', database: 'unknown' }))) as Health;
  },
};

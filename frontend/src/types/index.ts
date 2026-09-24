export type Channel = { id: string; title: string; url: string; avatar_url?: string | null; video_count: number; added_at: string };

export type ChannelSettings = {
  preferred_languages: string[];
  subtitle_preference: 'any' | 'manual' | 'auto';
  allow_translation: boolean;
  export_formats: string[];
  sync_interval_hours: number;
};

export type ChannelDetail = Channel & { youtube_channel_id: string; last_scanned_at?: string | null; next_sync_at?: string | null; settings: ChannelSettings };

export type Subtitle = { id: string; language: string; language_code: string; format: string; file_path: string };

export type VideoStatus = 'pending' | 'queued' | 'processing' | 'completed' | 'no_subtitle' | 'language_unavailable' | 'failed' | 'blocked' | 'skipped';
export const VIDEO_STATUSES: VideoStatus[] = ['pending', 'queued', 'processing', 'completed', 'no_subtitle', 'language_unavailable', 'failed', 'blocked', 'skipped'];

export type Video = {
  id: string; title: string; youtube_video_id: string; published_at?: string | null; video_type: string; status: VideoStatus;
  retry_count: number; last_error?: string | null; subtitle_path?: string | null; subtitles: Subtitle[];
};

export type JobStatus = 'queued' | 'processing' | 'paused' | 'completed' | 'failed' | 'cancelled';
export const JOB_STATUSES: JobStatus[] = ['queued', 'processing', 'paused', 'completed', 'failed', 'cancelled'];
export type JobAction = 'pause' | 'resume' | 'cancel' | 'retry';

export type Job = {
  id: string; kind: string; status: JobStatus; outcome?: string | null; attempts: number; max_attempts: number;
  error?: string | null; created_at: string; sync_run_id?: string | null; video_id?: string | null; channel_id?: string | null;
};

export type JobLog = { id: string; job_id?: string | null; level: string; message: string; created_at: string };

export type DashboardStats = { channels: number; videos: number; completed: number; failed: number; no_subtitle: number; running_jobs: number };
export type YouTubeConfig = { configured: boolean };

export type Page<T> = { items: T[]; total: number };
export type PageQuery = { limit: number; offset: number };

export type WorkerSummary = { status: 'running' | 'offline' | 'absent' | 'unknown'; active: number; stale: number; busy: number; last_heartbeat: string | null };

export type Health = {
  ok: boolean; status: 'ok' | 'degraded' | 'critical'; api: string; database: string; storage?: string;
  worker?: WorkerSummary; youtube_api_key_configured?: boolean;
};

export type SyncRunSummary = { id: string; channel_id: string; mode: string; status: string; started_at: string; queued: number; successful: number; failed: number };

export type Diagnostics = {
  app: { version: string; python: string; uptime_seconds: number };
  database: { status: string; engine: string; revision?: string | null; journal_mode?: string; size_bytes?: number | null };
  storage: { status: string; free_bytes: number | null; total_bytes: number | null };
  runtime_logs: string | null;
  queue?: Record<JobStatus, number>;
  worker?: WorkerSummary;
  youtube_api_key_configured?: boolean;
  active_sync_runs?: SyncRunSummary[];
  last_successful_sync?: string | null;
};

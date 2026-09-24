import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, api } from '../api/client';
import { Pagination } from '../components/Pagination';
import type { Job } from '../types';
import { DiagnosticsPage, formatBytes, formatUptime } from './DiagnosticsPage';
import { JobsPage } from './JobsPage';

const job = (overrides: Partial<Job>): Job => ({ id: 'j1', kind: 'download', status: 'failed', attempts: 5, max_attempts: 5, created_at: '2026-01-01T00:00:00', error: 'boom', outcome: 'failed', ...overrides });

afterEach(() => vi.restoreAllMocks());

describe('JobsPage', () => {
  it('retry is sent once even on a double click, then the list refreshes', async () => {
    vi.spyOn(api, 'jobs').mockResolvedValue({ items: [job({})], total: 1 });
    vi.spyOn(api, 'logs').mockResolvedValue({ items: [], total: 0 });
    let finish: (value: Job) => void = () => undefined;
    const action = vi.spyOn(api, 'jobAction').mockImplementation(() => new Promise<Job>((done) => { finish = done; }));
    render(<JobsPage back={() => undefined} />);
    const button = await screen.findByRole('button', { name: 'Retry' });
    const user = userEvent.setup();
    await user.dblClick(button);
    expect(action).toHaveBeenCalledTimes(1);
    expect(action).toHaveBeenCalledWith('j1', 'retry');
    expect(screen.getByRole('button', { name: '…' })).toBeDisabled();
    finish(job({ status: 'queued' }));
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Đã đưa job vào hàng đợi lại.'));
  });

  it('shows the server refusal when a retry conflicts and keeps the button usable afterwards', async () => {
    vi.spyOn(api, 'jobs').mockResolvedValue({ items: [job({})], total: 1 });
    vi.spyOn(api, 'logs').mockResolvedValue({ items: [], total: 0 });
    vi.spyOn(api, 'jobAction').mockRejectedValue(new ApiError('Đã có job đang hoạt động cho mục này', 409));
    render(<JobsPage back={() => undefined} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Retry' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Đã có job đang hoạt động');
    expect(screen.getByRole('button', { name: 'Retry' })).toBeEnabled();
  });

  it('offers pause/cancel only for queued jobs and nothing for processing ones', async () => {
    vi.spyOn(api, 'jobs').mockResolvedValue({ items: [job({ id: 'q', status: 'queued', error: null }), job({ id: 'p', status: 'processing', error: null })], total: 2 });
    vi.spyOn(api, 'logs').mockResolvedValue({ items: [], total: 0 });
    render(<JobsPage back={() => undefined} />);
    expect(await screen.findByRole('button', { name: 'Pause' })).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: /Pause|Hủy|Resume|Retry/ })).toHaveLength(2);
  });

  it('loads the next page when paging', async () => {
    const jobs = vi.spyOn(api, 'jobs').mockImplementation(async ({ offset }) => ({ items: [job({ id: `job-${offset}`, status: 'completed', error: null })], total: 120 }));
    vi.spyOn(api, 'logs').mockResolvedValue({ items: [], total: 0 });
    render(<JobsPage back={() => undefined} />);
    await screen.findByText('1–50 / 120');
    await userEvent.click(screen.getAllByRole('button', { name: 'Sau →' })[0]);
    await waitFor(() => expect(jobs).toHaveBeenLastCalledWith(expect.objectContaining({ offset: 50, limit: 50 }), expect.anything()));
    await screen.findByText('51–100 / 120');
  });
});

describe('Pagination', () => {
  it('is hidden for a single page and disables the edges', async () => {
    const onChange = vi.fn();
    const { container, rerender } = render(<Pagination offset={0} limit={50} total={10} onChange={onChange} />);
    expect(container).toBeEmptyDOMElement();
    rerender(<Pagination offset={0} limit={50} total={120} onChange={onChange} />);
    expect(screen.getByRole('button', { name: '← Trước' })).toBeDisabled();
    await userEvent.click(screen.getByRole('button', { name: 'Sau →' }));
    expect(onChange).toHaveBeenCalledWith(50);
    rerender(<Pagination offset={100} limit={50} total={120} onChange={onChange} />);
    expect(screen.getByRole('button', { name: 'Sau →' })).toBeDisabled();
    expect(screen.getByText('101–120 / 120')).toBeInTheDocument();
  });
});

describe('DiagnosticsPage', () => {
  it('shows worker offline guidance and formats sizes', async () => {
    vi.spyOn(api, 'diagnostics').mockResolvedValue({
      app: { version: '0.1.0', python: '3.13', uptime_seconds: 3700 },
      database: { status: 'ok', engine: 'sqlite', revision: '0004', journal_mode: 'wal', size_bytes: 2048 },
      storage: { status: 'ok', free_bytes: 5 * 1024 ** 3, total_bytes: 10 * 1024 ** 3 }, runtime_logs: '.runtime/logs',
      queue: { queued: 3, processing: 0, paused: 0, completed: 9, failed: 1, cancelled: 0 },
      worker: { status: 'offline', active: 0, stale: 1, busy: 0, last_heartbeat: null }, youtube_api_key_configured: true, active_sync_runs: [], last_successful_sync: null,
    });
    render(<DiagnosticsPage back={() => undefined} />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Worker không hoạt động');
    expect(screen.getByText(/trống 5\.0 GB \/ 10\.0 GB/)).toBeInTheDocument();
    expect(screen.getByText(/revision 0004/)).toBeInTheDocument();
  });

  it('formats helpers', () => {
    expect(formatBytes(null)).toBe('–'); expect(formatBytes(1536)).toBe('1.5 KB'); expect(formatUptime(45)).toBe('0m 45s'); expect(formatUptime(3700)).toBe('1h 1m');
  });
});

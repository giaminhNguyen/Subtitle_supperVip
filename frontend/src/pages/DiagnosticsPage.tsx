import { api } from '../api/client';
import { Badge } from '../components/Badge';
import { ErrorNotice } from '../components/Notice';
import { usePolling } from '../hooks/usePolling';
import { JOB_STATUSES } from '../types';

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return '–';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

export function formatUptime(seconds: number): string {
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return days ? `${days}d ${hours}h` : hours ? `${hours}h ${minutes}m` : `${minutes}m ${seconds % 60}s`;
}

// The API sends naive UTC timestamps; append Z so the browser shows them in local time.
const when = (iso?: string | null) => (iso ? new Date(/[zZ]|[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`).toLocaleString() : '–');

export function DiagnosticsPage({ back }: { back: () => void }) {
  const result = usePolling((signal) => api.diagnostics(signal), 10000, []);
  const data = result.data;
  const worker = data?.worker;

  return (
    <main>
      <button className="back" onClick={back}>Dashboard</button>
      <header><div><p className="eyebrow">HEALTH</p><h1>Chẩn đoán hệ thống</h1></div><button className="secondary" onClick={result.refresh} disabled={result.refreshing}>{result.refreshing ? 'Đang tải…' : 'Refresh'}</button></header>
      <ErrorNotice message={result.error} />
      {result.loading && <p>Đang tải…</p>}
      {data && (
        <>
          <section className="metrics diag">
            <div className="metric"><small>Database</small><strong><Badge status={data.database.status === 'ok' ? 'completed' : 'failed'} /></strong><small>revision {data.database.revision ?? '–'} · {data.database.journal_mode ?? ''} · {formatBytes(data.database.size_bytes)}</small></div>
            <div className="metric"><small>Worker</small><strong><Badge status={worker?.status === 'running' ? 'completed' : 'failed'} /></strong><small>{worker ? `${worker.active} hoạt động · ${worker.stale} stale · ${worker.busy} đang bận` : '–'}</small></div>
            <div className="metric"><small>Lưu trữ</small><strong><Badge status={data.storage.status === 'ok' ? 'completed' : 'failed'} /></strong><small>trống {formatBytes(data.storage.free_bytes)} / {formatBytes(data.storage.total_bytes)}</small></div>
            <div className="metric"><small>YouTube API key</small><strong><Badge status={data.youtube_api_key_configured ? 'completed' : 'failed'} /></strong><small>{data.youtube_api_key_configured ? 'đã cấu hình' : 'chưa cấu hình'}</small></div>
          </section>
          {worker && worker.status !== 'running' && <p className="error" role="alert">Worker không hoạt động: job sẽ không được xử lý. Hãy chạy lại start-app.bat và xem log worker{data.runtime_logs ? ` trong ${data.runtime_logs}` : ''}.</p>}
          <section className="settings">
            <h2>Hàng đợi</h2>
            <div className="queue-counts">{data.queue ? JOB_STATUSES.map((status) => <span key={status}><Badge status={status} /> {data.queue?.[status] ?? 0}</span>) : '–'}</div>
          </section>
          <section className="settings">
            <h2>SyncRun đang chạy</h2>
            {data.active_sync_runs?.length ? (
              <div className="table-wrap"><table><thead><tr><th>Mode</th><th>Trạng thái</th><th>Bắt đầu</th><th>Queued</th><th>Thành công</th><th>Lỗi</th></tr></thead><tbody>
                {data.active_sync_runs.map((run) => <tr key={run.id}><td>{run.mode}</td><td>{run.status}</td><td>{when(run.started_at)}</td><td>{run.queued}</td><td>{run.successful}</td><td>{run.failed}</td></tr>)}
              </tbody></table></div>
            ) : <p className="empty">Không có lần đồng bộ nào đang chạy.</p>}
          </section>
          <section className="settings">
            <h2>Thông tin</h2>
            <p>Sync thành công gần nhất: <b>{when(data.last_successful_sync)}</b></p>
            <p>Worker heartbeat gần nhất: <b>{when(worker?.last_heartbeat)}</b></p>
            <p>Phiên bản {data.app.version} · Python {data.app.python} · chạy được {formatUptime(data.app.uptime_seconds)}</p>
          </section>
        </>
      )}
    </main>
  );
}

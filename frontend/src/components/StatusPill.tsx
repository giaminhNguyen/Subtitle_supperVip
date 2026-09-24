import { api } from '../api/client';
import { usePolling } from '../hooks/usePolling';
import type { Health } from '../types';

const LABEL: Record<Health['status'], string> = { ok: 'Hệ thống ổn', degraded: 'Cần chú ý', critical: 'Lỗi nghiêm trọng' };

/** Header pill fed by /health (cheap, polled every 15s while the tab is visible). */
export function StatusPill({ onOpen }: { onOpen: () => void }) {
  const { data } = usePolling((signal) => api.health(signal), 15000, []);
  const status = data?.status ?? 'degraded';
  const worker = data?.worker?.status;
  const title = data ? `API: ${data.api} · DB: ${data.database} · worker: ${worker ?? '?'}` : 'Đang kiểm tra…';
  return (
    <button className={`api status-${status}`} title={title} onClick={onOpen}>
      {data ? LABEL[status] : 'Đang kiểm tra…'}
      {worker && worker !== 'running' ? ' · worker offline' : ''}
    </button>
  );
}

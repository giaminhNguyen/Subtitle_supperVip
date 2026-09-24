import { useState } from 'react';
import { api } from '../api/client';
import { ApiKeySetup } from '../components/ApiKeySetup';
import { ErrorNotice } from '../components/Notice';
import { StatusPill } from '../components/StatusPill';
import { useAction } from '../hooks/useAction';
import { usePolling } from '../hooks/usePolling';
import type { Channel, DashboardStats } from '../types';

const METRICS: [string, keyof DashboardStats][] = [
  ['Kênh', 'channels'],
  ['Tổng video', 'videos'],
  ['Đã có sub', 'completed'],
  ['Lỗi / Blocked', 'failed'],
  ['Không có sub', 'no_subtitle'],
  ['Job đang chờ', 'running_jobs'],
];

export function ChannelsPage({ onSelect, onOpenDiagnostics }: { onSelect: (channel: Channel) => void; onOpenDiagnostics: () => void }) {
  const [url, setUrl] = useState('');
  const action = useAction();
  const overview = usePolling(
    async (signal) => {
      const [stats, channels, youtube] = await Promise.all([api.dashboard(signal), api.channels(signal), api.youtubeConfig(signal)]);
      return { stats, channels, youtube };
    },
    (data) => (data && data.stats.running_jobs > 0 ? 5000 : 20000),
    [],
  );

  const add = async (event: React.FormEvent) => {
    event.preventDefault();
    if (await action.run('add', () => api.addChannel(url))) {
      setUrl('');
      overview.refresh();
    }
  };
  const { stats, channels, youtube } = overview.data ?? {};

  return (
    <main>
      <header>
        <div>
          <p className="eyebrow">QUẢN LÝ CAPTION</p>
          <h1>YouTube Subtitle Manager</h1>
        </div>
        <StatusPill onOpen={onOpenDiagnostics} />
      </header>
      <ApiKeySetup configured={youtube?.configured ?? false} onSaved={overview.refresh} />
      <section className="metrics">
        {METRICS.map(([label, key]) => (
          <div className="metric" key={key}>
            <small>{label}</small>
            <strong>{stats?.[key] ?? '-'}</strong>
          </div>
        ))}
      </section>
      <section className="add">
        <div>
          <h2>Thêm kênh YouTube</h2>
          <p>Dán URL dạng @handle, /channel/ID, /user/ hoặc custom URL.</p>
        </div>
        <form onSubmit={add}>
          <input value={url} onChange={(event) => setUrl(event.target.value)} placeholder="https://www.youtube.com/@channelname" required />
          <button disabled={action.busy}>{action.busy ? 'Đang thêm…' : 'Thêm kênh'}</button>
        </form>
        <ErrorNotice message={action.error} />
      </section>
      <section>
        <div className="section-title">
          <h2>Kênh đang quản lý</h2>
          <button className="secondary" onClick={overview.refresh} disabled={overview.refreshing}>
            {overview.refreshing ? 'Đang tải…' : 'Làm mới'}
          </button>
        </div>
        <ErrorNotice message={overview.error} />
        <div className="channels">
          {overview.loading && <div className="empty">Đang tải…</div>}
          {channels?.map((channel) => (
            <button className="channel" onClick={() => onSelect(channel)} key={channel.id}>
              {channel.avatar_url ? <img src={channel.avatar_url} alt="" /> : <i />}
              <span>
                <b>{channel.title}</b>
                <small>
                  {channel.video_count} video · thêm {new Date(channel.added_at).toLocaleDateString('vi-VN')}
                </small>
              </span>
              <em>Chi tiết →</em>
            </button>
          ))}
          {!overview.loading && channels?.length === 0 && <div className="empty">Chưa có kênh nào. Hãy thêm URL để bắt đầu.</div>}
        </div>
      </section>
    </main>
  );
}

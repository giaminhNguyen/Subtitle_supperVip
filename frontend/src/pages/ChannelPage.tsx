import { useState } from 'react';
import { api } from '../api/client';
import { Badge } from '../components/Badge';
import { ErrorNotice, InfoNotice } from '../components/Notice';
import { Pagination } from '../components/Pagination';
import { useAction } from '../hooks/useAction';
import { usePolling } from '../hooks/usePolling';
import { VIDEO_STATUSES, type Channel, type VideoStatus } from '../types';

const PAGE_SIZE = 50;
const EXPORT_FORMATS = ['srt', 'vtt', 'txt', 'json', 'csv'];
const IN_PROGRESS: VideoStatus[] = ['queued', 'processing'];

export function ChannelPage({ channel, back }: { channel: Channel; back: () => void }) {
  const [status, setStatus] = useState<VideoStatus | ''>('');
  const [offset, setOffset] = useState(0);
  const action = useAction();
  const detail = usePolling((signal) => api.channel(channel.id, signal), null, [channel.id]);
  const videos = usePolling(
    (signal) => api.videos(channel.id, { status, limit: PAGE_SIZE, offset }, signal),
    (data) => (data?.items.some((video) => IN_PROGRESS.includes(video.status)) ? 4000 : 30000),
    [channel.id, status, offset],
  );

  const queue = (key: string, request: () => Promise<unknown>) => action.run(key, request, 'Đã thêm vào hàng đợi.').then((ok) => { if (ok) videos.refresh(); });
  const saveSettings = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    void action.run('settings', () => api.updateSettings(channel.id, {
      preferred_languages: String(form.get('languages')).split(',').map((value) => value.trim()).filter(Boolean),
      subtitle_preference: form.get('kind') as 'any' | 'manual' | 'auto',
      allow_translation: form.get('translate') === 'on',
      export_formats: form.getAll('format').map(String),
      sync_interval_hours: Number(form.get('interval')),
    }), 'Đã lưu cấu hình.');
  };

  if (detail.loading) return <main>Đang tải…</main>;
  if (!detail.data) return <main><button className="back" onClick={back}>← Tất cả kênh</button><ErrorNotice message={detail.error ?? 'Không tải được kênh'} /></main>;
  const info = detail.data;
  const settings = info.settings;
  const page = videos.data;

  return (
    <main>
      <button className="back" onClick={back}>← Tất cả kênh</button>
      <header className="channel-head">{info.avatar_url && <img src={info.avatar_url} alt="" />}<div><p className="eyebrow">CHANNEL</p><h1>{info.title}</h1><a href={info.url} target="_blank" rel="noreferrer">{info.youtube_channel_id}</a></div></header>
      <div className="actions">
        <button disabled={action.busy} onClick={() => void queue('scan-all', () => api.scan(channel.id, 'all'))}>Quét toàn bộ</button>
        <button disabled={action.busy} onClick={() => void queue('sync', () => api.syncNew(channel.id))}>Đồng bộ video mới</button>
        <button className="secondary" disabled={action.busy} onClick={() => void queue('scan-retry', () => api.scan(channel.id, 'retryable'))}>Xử lý lỗi / thiếu sub</button>
      </div>
      <InfoNotice message={action.notice} />
      <ErrorNotice message={action.error} />
      <section className="settings">
        <h2>Cấu hình subtitle</h2>
        <form onSubmit={saveSettings}>
          <label>Ưu tiên ngôn ngữ<input name="languages" defaultValue={settings.preferred_languages.join(', ')} /></label>
          <label>Loại sub<select name="kind" defaultValue={settings.subtitle_preference}><option value="any">Bất kỳ</option><option value="manual">Thủ công</option><option value="auto">Tự động</option></select></label>
          <label>Đồng bộ mỗi (giờ)<input name="interval" type="number" min="1" max="720" defaultValue={settings.sync_interval_hours} /></label>
          <label className="check"><input name="translate" type="checkbox" defaultChecked={settings.allow_translation} /> Cho phép dịch YouTube</label>
          <fieldset><legend>Xuất</legend>{EXPORT_FORMATS.map((format) => <label className="check" key={format}><input type="checkbox" name="format" value={format} defaultChecked={settings.export_formats.includes(format)} />{format.toUpperCase()}</label>)}</fieldset>
          <button disabled={action.busy}>Lưu cấu hình</button>
        </form>
      </section>
      <section>
        <div className="section-title">
          <h2>Video ({page?.total ?? '…'})</h2>
          <select aria-label="Lọc trạng thái video" value={status} onChange={(event) => { setStatus(event.target.value as VideoStatus | ''); setOffset(0); }}><option value="">Mọi trạng thái</option>{VIDEO_STATUSES.map((value) => <option key={value}>{value}</option>)}</select>
        </div>
        <ErrorNotice message={videos.error} />
        <div className="table-wrap">
          <table>
            <thead><tr><th>Video</th><th>Ngày</th><th>Loại</th><th>Trạng thái</th><th>Retry / lỗi</th><th /></tr></thead>
            <tbody>
              {page?.items.map((video) => (
                <tr key={video.id}>
                  <td><b>{video.title}</b><small>{video.youtube_video_id}</small></td>
                  <td>{video.published_at && new Date(video.published_at).toLocaleDateString('vi-VN')}</td>
                  <td>{video.video_type}</td>
                  <td><Badge status={video.status} /></td>
                  <td>{video.retry_count}{video.last_error && <small className="error">{video.last_error}</small>}</td>
                  <td><button className="secondary" disabled={action.busy || IN_PROGRESS.includes(video.status)} onClick={() => void queue(`dl-${video.id}`, () => api.downloadVideo(video.id, true))}>Tải lại</button></td>
                </tr>
              ))}
              {page && page.items.length === 0 && <tr><td colSpan={6} className="empty">Không có video nào.</td></tr>}
            </tbody>
          </table>
        </div>
        <Pagination offset={offset} limit={PAGE_SIZE} total={page?.total ?? 0} disabled={videos.refreshing} onChange={setOffset} />
      </section>
    </main>
  );
}

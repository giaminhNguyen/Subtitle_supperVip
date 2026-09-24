import { useState } from 'react';
import { api } from '../api/client';
import { useAction } from '../hooks/useAction';
import { ErrorNotice, InfoNotice } from './Notice';

export function ApiKeySetup({ configured, onSaved }: { configured: boolean; onSaved: () => void }) {
  const [apiKey, setApiKey] = useState('');
  const action = useAction();
  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    const ok = await action.run('save', () => api.saveApiKey(apiKey), 'Đã lưu API key. Các job mới sẽ dùng key này.');
    if (ok) {
      setApiKey('');
      onSaved();
    }
  };
  return (
    <section className="settings api-key-settings">
      <h2>{configured ? 'Thay YouTube API key' : 'Cấu hình YouTube API key'}</h2>
      <p>Key được lưu cục bộ trong database của ứng dụng và không hiển thị lại sau khi lưu.</p>
      <form onSubmit={save}>
        <label>
          API key
          <input type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} minLength={10} autoComplete="off" placeholder="AIza..." required />
        </label>
        <button disabled={action.busy}>{action.busy ? 'Đang lưu…' : 'Lưu API key'}</button>
      </form>
      <InfoNotice message={action.notice} />
      <ErrorNotice message={action.error} />
    </section>
  );
}

import { useState } from 'react';
import { ChannelPage } from './pages/ChannelPage';
import { ChannelsPage } from './pages/ChannelsPage';
import { DiagnosticsPage } from './pages/DiagnosticsPage';
import { JobsPage } from './pages/JobsPage';
import type { Channel } from './types';

type Route = { name: 'channels' } | { name: 'channel'; channel: Channel } | { name: 'jobs' } | { name: 'diagnostics' };

export function App() {
  const [route, setRoute] = useState<Route>({ name: 'channels' });
  const home = () => setRoute({ name: 'channels' });
  return (
    <>
      {route.name !== 'jobs' && <button className="secondary jobs-button" onClick={() => setRoute({ name: 'jobs' })}>Jobs & logs</button>}
      {route.name === 'jobs' && <JobsPage back={home} />}
      {route.name === 'diagnostics' && <DiagnosticsPage back={home} />}
      {route.name === 'channel' && <ChannelPage channel={route.channel} back={home} />}
      {route.name === 'channels' && <ChannelsPage onSelect={(channel) => setRoute({ name: 'channel', channel })} onOpenDiagnostics={() => setRoute({ name: 'diagnostics' })} />}
    </>
  );
}

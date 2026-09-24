import { useState } from 'react';
import { api } from '../api/client';
import { Badge } from '../components/Badge';
import { ErrorNotice, InfoNotice } from '../components/Notice';
import { Pagination } from '../components/Pagination';
import { useAction } from '../hooks/useAction';
import { usePolling } from '../hooks/usePolling';
import { JOB_STATUSES, type Job, type JobAction, type JobStatus } from '../types';

const JOB_PAGE = 50;
const LOG_PAGE = 50;
const ACTIVE: JobStatus[] = ['queued', 'processing'];
const ACTIONS: Record<JobStatus, { action: JobAction; label: string; secondary?: boolean }[]> = {
  queued: [
    { action: 'pause', label: 'Pause', secondary: true },
    { action: 'cancel', label: 'Hủy', secondary: true },
  ],
  paused: [
    { action: 'resume', label: 'Resume' },
    { action: 'cancel', label: 'Hủy', secondary: true },
  ],
  failed: [{ action: 'retry', label: 'Retry' }],
  processing: [],
  completed: [],
  cancelled: [],
};

export function JobsPage({ back }: { back: () => void }) {
  const [status, setStatus] = useState<JobStatus | ''>('');
  const [jobOffset, setJobOffset] = useState(0);
  const [logOffset, setLogOffset] = useState(0);
  const action = useAction();
  const jobs = usePolling(
    (signal) => api.jobs({ status, limit: JOB_PAGE, offset: jobOffset }, signal),
    (data) => (data?.items.some((job) => ACTIVE.includes(job.status)) ? 3000 : 15000),
    [status, jobOffset],
  );
  const logs = usePolling((signal) => api.logs({ limit: LOG_PAGE, offset: logOffset }, signal), 10000, [logOffset]);

  const control = (job: Job, verb: JobAction) =>
    action.run(`${job.id}:${verb}`, () => api.jobAction(job.id, verb), verb === 'retry' ? 'Đã đưa job vào hàng đợi lại.' : undefined).then(() => jobs.refresh());
  const jobPage = jobs.data;
  const logPage = logs.data;

  return (
    <main>
      <button className="back" onClick={back}>
        Dashboard
      </button>
      <header>
        <div>
          <p className="eyebrow">QUEUE</p>
          <h1>Jobs & logs</h1>
        </div>
        <div className="row">
          <select
            aria-label="Lọc trạng thái job"
            value={status}
            onChange={(event) => {
              setStatus(event.target.value as JobStatus | '');
              setJobOffset(0);
            }}
          >
            <option value="">Mọi trạng thái</option>
            {JOB_STATUSES.map((value) => (
              <option key={value}>{value}</option>
            ))}
          </select>
          <button
            className="secondary"
            onClick={() => {
              jobs.refresh();
              logs.refresh();
            }}
            disabled={jobs.refreshing}
          >
            {jobs.refreshing ? 'Đang tải…' : 'Refresh'}
          </button>
        </div>
      </header>
      <ErrorNotice message={action.error ?? jobs.error} />
      <InfoNotice message={action.notice} />
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Type</th>
              <th>Status</th>
              <th>Kết quả</th>
              <th>Attempts</th>
              <th>Error</th>
              <th>Tạo lúc</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody>
            {jobPage?.items.map((job) => (
              <tr key={job.id}>
                <td>{job.kind}</td>
                <td>
                  <Badge status={job.status} />
                </td>
                <td>{job.outcome ? <Badge status={job.outcome} /> : '–'}</td>
                <td>
                  {job.attempts}/{job.max_attempts}
                </td>
                <td className="error">{job.error}</td>
                <td>{new Date(job.created_at).toLocaleString()}</td>
                <td>
                  {ACTIONS[job.status].map(({ action: verb, label, secondary }) => (
                    <button key={verb} className={secondary ? 'secondary' : undefined} disabled={action.busy} onClick={() => void control(job, verb)}>
                      {action.busyKey === `${job.id}:${verb}` ? '…' : label}
                    </button>
                  ))}
                </td>
              </tr>
            ))}
            {jobs.loading && (
              <tr>
                <td colSpan={7} className="empty">
                  Đang tải…
                </td>
              </tr>
            )}
            {jobPage && jobPage.items.length === 0 && (
              <tr>
                <td colSpan={7} className="empty">
                  Không có job nào.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <Pagination offset={jobOffset} limit={JOB_PAGE} total={jobPage?.total ?? 0} disabled={jobs.refreshing} onChange={setJobOffset} />
      <section className="settings">
        <h2>Recent logs</h2>
        <ErrorNotice message={logs.error} />
        {logPage?.items.map((log) => (
          <p key={log.id}>
            <small>
              {new Date(log.created_at).toLocaleString()} · {log.level}
            </small>
            <br />
            {log.message}
          </p>
        ))}
        {logPage && logPage.items.length === 0 && <p className="empty">Chưa có log.</p>}
        <Pagination offset={logOffset} limit={LOG_PAGE} total={logPage?.total ?? 0} disabled={logs.refreshing} onChange={setLogOffset} />
      </section>
    </main>
  );
}

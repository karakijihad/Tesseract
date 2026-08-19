// Worker detail modal — full record projection for a single worker.
//
// Opened by clicking any WorkersPane row. Renders prompt, summary,
// status timeline, identity (pid / pane / invocation / worktree), error
// surface, transcript link, artifacts. Mirrors AgendaDetailModal's
// shell so the visual language stays consistent.

import React from 'react';
import { useAutonomyStore } from '../../stores/autonomy';
import type { WorkerDetail } from '../../lib/api';
import { CloseButton } from '../../components/common/CloseButton';
import { Modal } from '../../components/common/Modal';

function _fmtIso(iso: string | null | undefined): string {
  if (!iso) return '—';
  const parsed = Date.parse(iso);
  if (Number.isNaN(parsed)) return iso;
  const d = new Date(parsed);
  return d.toISOString().replace('T', ' ').slice(0, 19) + 'Z';
}

function _fmtDuration(seconds: number | null | undefined): string {
  if (!seconds || seconds < 1) return '—';
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

function _fmtUsage(w: WorkerDetail): string {
  if (w.billing === 'subscription') {
    return 'subscription · flat-rate plan, no per-call usage';
  }
  const total = w.tokens_in + w.tokens_out;
  const cost = w.cost_usd > 0 ? `$${w.cost_usd.toFixed(4)}` : '—';
  return `${w.billing} · ${total} tok (${w.tokens_in}/${w.tokens_out}) · ${cost}`;
}

export function WorkerDetailModal(): React.ReactElement | null {
  const selectedId = useAutonomyStore((s) => s.selectedWorkerId);
  const worker = useAutonomyStore((s) => s.workerDetail);
  const status = useAutonomyStore((s) => s.workerDetailStatus);
  const error = useAutonomyStore((s) => s.workerDetailError);
  const close = useAutonomyStore((s) => s.closeWorkerDetail);

  if (selectedId == null) return null;

  return (
    <Modal
      onClose={close}
      ariaLabel="worker detail"
      ariaLabelledBy="worker-modal-title"
      className="autonomy-modal"
      testId="worker-detail-modal"
    >
        <div className="autonomy-modal__head">
          <div className="autonomy-modal__title" id="worker-modal-title">
            {worker ? (
              <>
                <div className="autonomy-row__head">
                  <span className={`autonomy-chip autonomy-chip--${worker.status}`}>
                    {worker.status}
                  </span>
                  <span className="autonomy-chip autonomy-chip--kind">{worker.kind}</span>
                  {worker.role && (
                    <span className="autonomy-chip autonomy-chip--source">{worker.role}</span>
                  )}
                  <span className={`autonomy-chip autonomy-chip--risk-${worker.risk_class}`}>
                    {worker.risk_class}
                  </span>
                </div>
                <div className="t-meta t-mono" style={{ marginTop: 4 }}>{worker.id}</div>
              </>
            ) : (
              <div className="t-meta t-mono">{selectedId}</div>
            )}
          </div>
          <CloseButton onClick={close} ariaLabel="close detail" />
        </div>

        {status === 'loading' && (
          <div className="autonomy-modal__section">
            <p className="t-meta">Loading worker…</p>
          </div>
        )}

        {status === 'error' && (
          <div className="autonomy-modal__section">
            <p className="t-meta">Failed to load: {error}</p>
          </div>
        )}

        {worker && (
          <>
            <div className="autonomy-modal__section">
              <div className="autonomy-modal__section-title">Prompt</div>
              <div style={{ whiteSpace: 'pre-wrap' }}>{worker.prompt || '—'}</div>
            </div>

            {worker.summary && (
              <div className="autonomy-modal__section">
                <div className="autonomy-modal__section-title">Summary</div>
                <div style={{ whiteSpace: 'pre-wrap' }}>{worker.summary}</div>
              </div>
            )}

            {(worker.error_class || worker.error_message) && (
              <div className="autonomy-modal__section">
                <div className="autonomy-modal__section-title">Error</div>
                {worker.error_class && (
                  <div className="t-mono">{worker.error_class}</div>
                )}
                {worker.error_message && (
                  <div className="t-meta" style={{ whiteSpace: 'pre-wrap' }}>
                    {worker.error_message}
                  </div>
                )}
              </div>
            )}

            <div className="autonomy-modal__section">
              <div className="autonomy-modal__section-title">Identity</div>
              <div className="autonomy-modal__score">
                <div className="autonomy-modal__score-key">agenda</div>
                <div className="autonomy-modal__score-val t-mono">{worker.agenda_item_id}</div>
                {worker.parent_worker_id && (
                  <>
                    <div className="autonomy-modal__score-key">parent</div>
                    <div className="autonomy-modal__score-val t-mono">{worker.parent_worker_id}</div>
                  </>
                )}
                <div className="autonomy-modal__score-key">created</div>
                <div className="autonomy-modal__score-val">{_fmtIso(worker.created_at)}</div>
                <div className="autonomy-modal__score-key">updated</div>
                <div className="autonomy-modal__score-val">{_fmtIso(worker.updated_at)}</div>
                <div className="autonomy-modal__score-key">duration</div>
                <div className="autonomy-modal__score-val">{_fmtDuration(worker.duration_seconds)}</div>
                <div className="autonomy-modal__score-key">usage</div>
                <div className="autonomy-modal__score-val">{_fmtUsage(worker)}</div>
                <div className="autonomy-modal__score-key">retries</div>
                <div className="autonomy-modal__score-val">{worker.retry_count}</div>
                {worker.exit_code != null && (
                  <>
                    <div className="autonomy-modal__score-key">exit</div>
                    <div className="autonomy-modal__score-val">{worker.exit_code}</div>
                  </>
                )}
                {worker.pid != null && (
                  <>
                    <div className="autonomy-modal__score-key">pid</div>
                    <div className="autonomy-modal__score-val">{worker.pid}</div>
                  </>
                )}
                {worker.pane_id && (
                  <>
                    <div className="autonomy-modal__score-key">pane</div>
                    <div className="autonomy-modal__score-val t-mono">{worker.pane_id}</div>
                  </>
                )}
                {worker.worktree_path && (
                  <>
                    <div className="autonomy-modal__score-key">worktree</div>
                    <div className="autonomy-modal__score-val t-mono">{worker.worktree_path}</div>
                  </>
                )}
                {worker.transcript_path && (
                  <>
                    <div className="autonomy-modal__score-key">transcript</div>
                    <div className="autonomy-modal__score-val t-mono">{worker.transcript_path}</div>
                  </>
                )}
                {worker.cli_invocation && worker.cli_invocation.length > 0 && (
                  <>
                    <div className="autonomy-modal__score-key">invocation</div>
                    <div className="autonomy-modal__score-val t-mono">
                      {worker.cli_invocation.join(' ')}
                    </div>
                  </>
                )}
              </div>
            </div>

            {worker.artifacts.length > 0 && (
              <div className="autonomy-modal__section">
                <div className="autonomy-modal__section-title">Artifacts</div>
                <ul className="autonomy-list">
                  {worker.artifacts.map((a, i) => (
                    <li key={`${a.path}-${i}`} className="autonomy-row">
                      <div className="autonomy-row__goal t-mono">{a.path}</div>
                      <div className="t-meta">
                        {a.kind}
                        {a.size_bytes != null ? ` · ${a.size_bytes} bytes` : ''}
                      </div>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {worker.status_history.length > 0 && (
              <div className="autonomy-modal__section">
                <div className="autonomy-modal__section-title">Timeline</div>
                <ul className="autonomy-list">
                  {worker.status_history.slice().reverse().map((t, i) => (
                    <li key={`${t.at}-${i}`} className="autonomy-row">
                      <div className="autonomy-row__head">
                        <span className="autonomy-chip autonomy-chip--source">
                          {t.from_status} → {t.to_status}
                        </span>
                        <span className="t-meta">{_fmtIso(t.at)}</span>
                      </div>
                      {t.reason && (
                        <div className="autonomy-row__rationale t-meta">{t.reason}</div>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </>
        )}
    </Modal>
  );
}

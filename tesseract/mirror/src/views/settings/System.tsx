import { useEffect, useState } from 'react';

import { Hint } from '../../components/ui/Hint';
import { isTauri } from '../../lib/endpoints';
import { fetchSystem, type CapabilitySnapshot } from '../../lib/api';
import type { Divergence } from '../../lib/update';
import { useUpdateStore } from '../../stores/update';

function fmtMaybe(v: string | null | undefined, suffix = ''): string {
  if (v == null || v === '') return 'unknown';
  return suffix ? `${v}${suffix}` : v;
}

function fmtNum(v: number | null | undefined, suffix = ''): string {
  if (v == null) return 'unknown';
  return suffix ? `${v}${suffix}` : String(v);
}

// Full detail for the inline banner: what diverged and the (capped) names.
function describeDivergence(d: Divergence): string {
  const parts: string[] = [];
  if (d.ahead > 0) {
    parts.push(
      `${d.ahead} local commit${d.ahead === 1 ? '' : 's'} (${d.ahead_summaries.join(', ')})`,
    );
  }
  if (d.dirty_total > 0) {
    const more = d.dirty_total - d.dirty.length;
    parts.push(
      `${d.dirty_total} uncommitted change${d.dirty_total === 1 ? '' : 's'} (${d.dirty.join(', ')}${more > 0 ? `, +${more} more` : ''})`,
    );
  }
  return parts.join('; ');
}

// Short summary for the discard-confirmation dialog - counts only, the
// banner above it already spelled out the names.
function divergenceSummary(d: Divergence): string {
  const parts: string[] = [];
  if (d.ahead > 0) parts.push(`${d.ahead} local commit${d.ahead === 1 ? '' : 's'}`);
  if (d.dirty_total > 0) {
    parts.push(`${d.dirty_total} uncommitted change${d.dirty_total === 1 ? '' : 's'}`);
  }
  return parts.join(' and ');
}

// Task 13 - version line + manual "Check now" + error text. Tauri-only:
// there is no update.rs IPC bridge in a browser dev session, so the whole
// block is hidden there rather than showing a control that can never work.
//
// Divergence (task-14): repo::fast_forward refuses to update when the
// checked-out branch has local commits and/or uncommitted changes that
// origin/main does not know about. Rather than let the operator only
// discover this from a failed Apply, update_check reports it up front so
// this row can warn and offer the explicit "discard & update" path
// (update_force_apply) - never triggered automatically.
function UpdateRow() {
  const version = useUpdateStore((s) => s.version);
  const behind = useUpdateStore((s) => s.behind);
  const divergence = useUpdateStore((s) => s.divergence);
  const checking = useUpdateStore((s) => s.checking);
  const applying = useUpdateStore((s) => s.applying);
  const error = useUpdateStore((s) => s.error);
  const check = useUpdateStore((s) => s.check);
  const forceApply = useUpdateStore((s) => s.forceApply);
  const [confirmingDiscard, setConfirmingDiscard] = useState(false);

  useEffect(() => {
    void check();
    // Mount-only: App.tsx already owns the launch check + 6h interval - this
    // just makes sure the Settings view shows current state on open (e.g. an
    // operator jumping straight to Settings before the launch check lands,
    // or reopening the panel later in a long session).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (!isTauri()) return null;

  return (
    <>
      <div className="system-row">
        <span className="system-label t-meta">version</span>
        <span className="system-value">
          <span className="system-version">TESSERACT {fmtMaybe(version)}</span>
          {behind > 0 && !applying && (
            <span className="system-update-note t-meta">
              {' '}
              — {behind} commit{behind === 1 ? '' : 's'} behind (apply from the update chip in the top HUD)
            </span>
          )}
          {applying && (
            <span className="system-update-note t-meta">
              {' '}
              — applying update, TARS will restart shortly and be briefly unavailable…
            </span>
          )}
          <button
            type="button"
            className="system-redetect"
            onClick={() => void check()}
            disabled={checking || applying}
          >
            {checking ? 'checking…' : 'check for updates'}
          </button>
          {error && <div className="settings-error">{error}</div>}
        </span>
      </div>
      {divergence && !applying && (
        <div className="system-row">
          <span className="system-label t-meta">divergence</span>
          <span className="system-value">
            <span className="system-update-note t-meta">
              local history diverged from origin/main — {describeDivergence(divergence)}
            </span>{' '}
            <button
              type="button"
              className="system-redetect"
              onClick={() => setConfirmingDiscard(true)}
            >
              discard local changes & update
            </button>
          </span>
        </div>
      )}
      {confirmingDiscard && divergence && (
        <div
          className="mode-modal"
          role="dialog"
          aria-modal="true"
          aria-labelledby="update-discard-title"
        >
          <div className="mode-modal__card">
            <h4 id="update-discard-title" className="mode-modal__title">
              Discard local changes?
            </h4>
            <p className="mode-modal__body">
              This will permanently discard {divergenceSummary(divergence)} and reset to
              origin/main, then update TESSERACT to the latest version. Untracked files on disk
              are left in place.
            </p>
            <div className="mode-modal__actions">
              <button
                type="button"
                className="mode-modal__btn"
                onClick={() => setConfirmingDiscard(false)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="mode-modal__btn mode-modal__btn--primary"
                onClick={() => {
                  setConfirmingDiscard(false);
                  void forceApply();
                }}
              >
                Discard &amp; update
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

export function SystemSection() {
  const [snap, setSnap] = useState<CapabilitySnapshot | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchSystem(false)
      .then(setSnap)
      .catch((err) => setError(err instanceof Error ? err.message : String(err)));
  }, []);

  const refresh = async () => {
    setRefreshing(true);
    setError(null);
    try {
      const fresh = await fetchSystem(true);
      setSnap(fresh);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setRefreshing(false);
    }
  };

  if (!snap) {
    return (
      <section className="settings-section">
        <h3 className="settings-section__title">System</h3>
        <div className="t-meta">{error ?? '(loading…)'}</div>
      </section>
    );
  }

  const gpuLabel =
    snap.gpu.vendor === 'unknown'
      ? 'no GPU detected'
      : `${snap.gpu.vendor}${snap.gpu.name ? ' · ' + snap.gpu.name : ''}${snap.gpu.memory_mb ? ` (${(snap.gpu.memory_mb / 1024).toFixed(1)} GB)` : ''}${snap.gpu.cuda ? ' · CUDA' : ''}`;

  return (
    <section className="settings-section">
      <h3 className="settings-section__title">System</h3>
      <div className="settings-hint t-meta">
        Capability snapshot from{' '}
        <code>tesseract/scripts/check_dependencies.py</code>. Phase 17 reuses
        this as a pre-flight gate for the bootstrap installer.
      </div>
      {error && <div className="settings-error">{error}</div>}
      <div className="system-grid">
        <UpdateRow />
        <div className="system-row">
          <span className="system-label t-meta">platform</span>
          <span className="system-value">
            {snap.platform.system} {snap.platform.release} ({snap.platform.machine})
          </span>
        </div>
        <div className="system-row">
          <span className="system-label t-meta">python</span>
          <span className="system-value">{fmtMaybe(snap.python_version)}</span>
        </div>
        <div className="system-row">
          <span className="system-label t-meta">node</span>
          <span className="system-value">{fmtMaybe(snap.node_version)}</span>
        </div>
        <div className="system-row">
          <span className="system-label t-meta">pnpm</span>
          <span className="system-value">{fmtMaybe(snap.pnpm_version)}</span>
        </div>
        <div className="system-row">
          <Hint label="GPU detection prefers pynvml (NVIDIA), falls back to platform-specific tooling.">
            <span className="system-label t-meta">gpu</span>
          </Hint>
          <span className="system-value">{gpuLabel}</span>
        </div>
        <div className="system-row">
          <span className="system-label t-meta">ram</span>
          <span className="system-value">{fmtNum(snap.ram_total_gb, ' GB')}</span>
        </div>
        <div className="system-row">
          <Hint label="Free space on the drive holding the tesseract repo.">
            <span className="system-label t-meta">disk free</span>
          </Hint>
          <span className="system-value">{fmtNum(snap.disk_free_gb, ' GB')}</span>
        </div>
        <div className="system-row">
          <Hint label="Count of input devices reported by sounddevice (null when sounddevice not installed).">
            <span className="system-label t-meta">mic devices</span>
          </Hint>
          <span className="system-value">{fmtNum(snap.mic_devices)}</span>
        </div>
      </div>
      <div className="system-actions">
        <button
          type="button"
          className="system-redetect"
          onClick={refresh}
          disabled={refreshing}
        >
          {refreshing ? 're-detecting…' : 're-detect'}
        </button>
      </div>
    </section>
  );
}
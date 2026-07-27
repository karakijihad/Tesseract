import { useEffect, useMemo, useState } from 'react';

import { Hint } from '../components/ui/Hint';
import { sendCommand } from '../lib/commands';
import { deleteScheduleJob, fetchScheduleRoles } from '../lib/api';
import { useScheduleStore, type RunFlash } from '../stores/schedule';
import { useToastStore } from '../stores/toasts';
import type { ScheduleJob } from '../lib/types';
import { CadencePicker } from './schedule/CadencePicker';
import { AddJobForm } from './schedule/AddJobForm';
import { AlarmsPanel } from './schedule/AlarmsPanel';

const RUN_FLASH_MS = 4500;

function formatTs(ts: string | null): string {
  if (!ts) return '—';
  try {
    return new Date(ts).toLocaleTimeString([], {
      hour12: false,
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    });
  } catch {
    return '—';
  }
}

function formatDuration(ms: number): string {
  if (!ms) return '—';
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

interface RowProps {
  job: ScheduleJob;
  flash: RunFlash | undefined;
  now: number;
  roles: string[];
}

function JobRow({ job, flash, now, roles }: RowProps) {
  const [editingCadence, setEditingCadence] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const fetchJobs = useScheduleStore((s) => s.fetchJobs);
  const running = flash && !flash.finished_at && (now - flash.started_at) < RUN_FLASH_MS;
  const lastOk = flash?.finished_at && flash.ok !== undefined
    ? flash.ok
    : job.runtime?.last_result?.ok;

  const enabled = job.enabled && (job.runtime?.enabled ?? true);
  const circuitBroken = job.runtime?.circuit_broken ?? false;
  const cadenceShown = job.runtime?.cadence ?? job.cadence;
  const lastFiredAt = formatTs(job.runtime?.last_fired_at ?? null);
  const lastDetail = flash?.detail ?? job.runtime?.last_result?.detail ?? '';
  const lastDuration = formatDuration(flash?.finished_at
    ? (flash.finished_at - flash.started_at)
    : (job.runtime?.last_result?.duration_ms ?? 0));
  const lastPayload = job.runtime?.last_result?.payload;
  const hasPayload = !!lastPayload && Object.keys(lastPayload).length > 0;

  const toggleEnabled = () => {
    const cmd = enabled ? '/schedule-disable' : '/schedule-enable';
    sendCommand(cmd, ` ${job.name}`);
  };

  const runNow = () => {
    sendCommand('/schedule-run-now', ` ${job.name}`);
  };

  const commitCadence = (next: string) => {
    setEditingCadence(false);
    if (!next || next === cadenceShown) return;
    sendCommand('/schedule-set-cadence', ` ${job.name} ${next}`);
  };

  const removeJob = async () => {
    setRemoving(true);
    try {
      await deleteScheduleJob(job.name);
      useToastStore.getState().push(`Removed '${job.name}'`);
      await fetchJobs();
    } catch (err) {
      useToastStore.getState().push(
        `Remove failed: ${err instanceof Error ? err.message : String(err)}`,
        'error',
      );
    } finally {
      setRemoving(false);
      setConfirmRemove(false);
    }
  };

  return (
    <div
      className={`schedule-row${running ? ' is-running' : ''}${circuitBroken ? ' is-broken' : ''}${!enabled ? ' is-off' : ''}`}
      data-job={job.name}
    >
      <div className="schedule-row-head">
        <Hint label={enabled ? 'Enabled — click to disable' : 'Disabled — click to enable'}>
          <button
            type="button"
            className={`schedule-toggle${enabled ? ' is-on' : ''}`}
            onClick={toggleEnabled}
            aria-pressed={enabled}
            aria-label={enabled ? `Disable ${job.name}` : `Enable ${job.name}`}
          >
            <span className="schedule-toggle-dot" />
          </button>
        </Hint>
        <span className="schedule-name">{job.name}</span>
        {circuitBroken && (
          <Hint label={`Circuit broken after ${job.runtime?.consecutive_failures ?? 0} consecutive failures — re-enable to reset`}>
            <span className="schedule-cb-badge">CB</span>
          </Hint>
        )}
        {running && <span className="schedule-run-indicator" aria-label="Running" />}
        <Hint label="Fire this job right now (off-schedule)">
          <button
            type="button"
            className="schedule-run-btn"
            onClick={runNow}
            disabled={!!running}
            aria-label={`Run ${job.name} now`}
          >
            run now
          </button>
        </Hint>
        <Hint label="Remove this job (persists to schedule.yaml)">
          <button
            type="button"
            className="schedule-remove-btn"
            onClick={() => setConfirmRemove(true)}
            disabled={removing}
            aria-label={`Remove ${job.name}`}
          >
            ×
          </button>
        </Hint>
      </div>
      <div className="schedule-row-meta">
        <Hint label="Click to edit cadence (opens picker)">
          <button
            type="button"
            className="schedule-cadence-pill"
            onClick={() => setEditingCadence(true)}
          >
            {cadenceShown}
          </button>
        </Hint>
        <Hint label={job.handler} maxWidth={360}>
          <span className="schedule-handler">
            {job.handler.split('.').slice(-1)[0]}
          </span>
        </Hint>
        <span className="schedule-on-failure" data-mode={job.on_failure}>
          on_failure: {job.on_failure}
        </span>
        {job.runtime?.uses_llm && (
          <ModelRoleDropdown job={job} roles={roles} />
        )}
      </div>
      {editingCadence && (
        <CadencePicker
          jobName={job.name}
          value={cadenceShown}
          onCommit={commitCadence}
          onCancel={() => setEditingCadence(false)}
        />
      )}
      {confirmRemove && (
        <div className="schedule-confirm-remove" role="alertdialog">
          <span className="schedule-confirm-text">
            Remove '{job.name}' permanently?
          </span>
          <button
            type="button"
            className="schedule-confirm-yes"
            onClick={removeJob}
            disabled={removing}
          >
            {removing ? 'removing…' : 'remove'}
          </button>
          <button
            type="button"
            className="schedule-confirm-no"
            onClick={() => setConfirmRemove(false)}
            disabled={removing}
          >
            cancel
          </button>
        </div>
      )}
      <div className="schedule-row-last">
        <span className="schedule-last-label">last:</span>
        <span className="schedule-last-ts">{lastFiredAt}</span>
        <span className={`schedule-last-status ${lastOk === true ? 'ok' : lastOk === false ? 'fail' : ''}`}>
          {lastOk === undefined ? '—' : lastOk ? 'ok' : 'fail'}
        </span>
        <span className="schedule-last-duration">{lastDuration}</span>
        {lastDetail && (
          <Hint label={lastDetail} maxWidth={420}>
            <span className="schedule-last-detail">{lastDetail}</span>
          </Hint>
        )}
        {hasPayload && (
          <Hint label={expanded ? 'Hide last-run payload' : 'Show last-run payload'}>
            <button
              type="button"
              className="schedule-expand-btn"
              onClick={() => setExpanded((x) => !x)}
              aria-expanded={expanded}
              aria-label={expanded ? `Collapse ${job.name} payload` : `Expand ${job.name} payload`}
            >
              {expanded ? '▾' : '▸'}
            </button>
          </Hint>
        )}
      </div>
      {expanded && hasPayload && (
        <pre className="schedule-row-payload">{JSON.stringify(lastPayload, null, 2)}</pre>
      )}
    </div>
  );
}

function ModelRoleDropdown({ job, roles }: { job: ScheduleJob; roles: string[] }) {
  // The runtime payload carries `default_model_role` (the handler's
  // built-in fallback) and `effective_model_role` (what actually fires:
  // override OR default). We want the <select> to reflect the *override*
  // — empty string == "use default" — so flipping back to default is
  // straightforward. The default is shown as the placeholder option label
  // so the operator can see what they'd revert to.
  const override = job.model_role ?? job.runtime?.model_role ?? '';
  const fallback = job.runtime?.default_model_role ?? null;
  const options = useMemo(() => {
    const set = new Set<string>(roles);
    if (override) set.add(override);
    if (fallback) set.add(fallback);
    return Array.from(set).sort();
  }, [roles, override, fallback]);

  const onChange = (e: React.ChangeEvent<HTMLSelectElement>) => {
    const next = e.target.value;
    const arg = next === '' ? '-' : next;
    sendCommand('/schedule-set-role', ` ${job.name} ${arg}`);
  };

  const label = override
    ? `LLM role override (default: ${fallback ?? '—'})`
    : `LLM role — using default${fallback ? ` (${fallback})` : ''}`;

  return (
    <Hint label={label} maxWidth={320}>
      <select
        className="schedule-model-role"
        value={override}
        onChange={onChange}
        aria-label={`Model role for ${job.name}`}
      >
        <option value="">
          {fallback ? `default (${fallback})` : 'default'}
        </option>
        {options.map((role) => (
          <option key={role} value={role}>{role}</option>
        ))}
      </select>
    </Hint>
  );
}

export function ScheduleView() {
  const jobs = useScheduleStore((s) => s.jobs);
  const runFlashes = useScheduleStore((s) => s.runFlashes);
  const loading = useScheduleStore((s) => s.loading);
  const lastError = useScheduleStore((s) => s.lastError);
  const fetchJobs = useScheduleStore((s) => s.fetchJobs);
  const [now, setNow] = useState(() => Date.now());
  const [adding, setAdding] = useState(false);
  const [roles, setRoles] = useState<string[]>([]);

  useEffect(() => {
    fetchJobs();
  }, [fetchJobs]);

  useEffect(() => {
    let cancelled = false;
    fetchScheduleRoles()
      .then((res) => { if (!cancelled) setRoles(res.roles ?? []); })
      .catch(() => { /* dropdown still works with the per-job override + default seeded in */ });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    const hasPending = Object.values(runFlashes).some((f) => !f.finished_at || (Date.now() - f.started_at) < RUN_FLASH_MS);
    if (!hasPending) return;
    const id = window.setInterval(() => setNow(Date.now()), 500);
    return () => window.clearInterval(id);
  }, [runFlashes]);

  const enabledCount = useMemo(() => jobs.filter((j) => j.enabled).length, [jobs]);

  return (
    <div className="schedule-view-wrap">
      <AlarmsPanel />
      <div className="schedule-view">
      <div className="schedule-header">
        <span className="schedule-header-title">Schedule</span>
        <span className="schedule-header-meta">
          {enabledCount} enabled · {jobs.length} total
        </span>
        <div className="schedule-header-actions">
          <Hint label="Add a new scheduled job (persists to schedule.yaml)" position="bottom">
            <button
              type="button"
              onClick={() => setAdding((a) => !a)}
              className="schedule-header-btn"
              aria-expanded={adding}
            >
              {adding ? '× cancel' : '+ add'}
            </button>
          </Hint>
          <Hint label="Re-fetch job list + runtime state from the scheduler" position="bottom">
            <button
              type="button"
              onClick={() => fetchJobs()}
              className="schedule-header-btn"
            >
              refresh
            </button>
          </Hint>
        </div>
      </div>
      {adding && <AddJobForm onClose={() => setAdding(false)} />}
      {lastError && <div className="schedule-error">{lastError}</div>}
      <div className="schedule-list">
        {jobs.length === 0 && !loading && (
          <div className="schedule-empty">No jobs — scheduler may be offline.</div>
        )}
        {jobs.map((job) => (
          <JobRow key={job.name} job={job} flash={runFlashes[job.name]} now={now} roles={roles} />
        ))}
      </div>
      </div>
    </div>
  );
}

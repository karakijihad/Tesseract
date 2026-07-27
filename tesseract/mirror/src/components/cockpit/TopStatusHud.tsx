// Slice 2 — floating top-centre status pill for the cockpit. A glass HUD
// carrying TARS's identity at a glance (name · mode · model · state · last
// reflected), so the operator reads status without the right rail expanded.
// Mounted by CockpitStage (SC-1) — always-on in the immersive cockpit.
//
// HUD runs-surface fix (change-set A): the permanent activity segment +
// ActivityMap toggle live here now (replacing the bottom ActivityPill, which
// rendered nothing when idle and was the only `hydrate()` call site). The
// segment is always rendered — "0 running" dimmed when idle, live-accented
// when work is in flight — so the operator always has a running-work
// indicator, not just when something happens to be active.

import { useEffect, useState } from 'react';

import { useIdentityStore } from '../../stores/identity';
import { useEntityStore } from '../../stores/entity';
import { useSoulStore } from '../../stores/soul';
import { useActivityStore } from '../../stores/activity';
import { useUpdateStore } from '../../stores/update';
import { isTauri } from '../../lib/endpoints';
import { formatRelative } from '../../lib/time';
import { ActivityMap } from '../../cockpit/ActivityMap';

export function TopStatusHud() {
  const name = useIdentityStore((s) => s.name);
  const securityMode = useIdentityStore((s) => s.securityMode);
  const modelName = useIdentityStore((s) => s.modelName);
  const state = useEntityStore((s) => s.state);
  const lastReflectedAt = useSoulStore((s) => s.lastReflectedAt);
  const hydrateActivity = useActivityStore((s) => s.hydrate);
  const running = useActivityStore((s) => s.runningCount());
  const [mapOpen, setMapOpen] = useState(false);

  const updateBehind = useUpdateStore((s) => s.behind);
  const updateApplying = useUpdateStore((s) => s.applying);
  const applyUpdate = useUpdateStore((s) => s.apply);
  const showUpdateChip = isTauri() && updateBehind > 0;

  useEffect(() => {
    void hydrateActivity();
  }, [hydrateActivity]);

  const ledClass =
    state === 'error' ? 'is-bad' : state === 'idle' ? 'is-ok' : 'is-active';

  return (
    <>
      <div className="top-status-hud">
        <span className={`top-status-hud__led ${ledClass}`} aria-hidden="true" />
        <span className="top-status-hud__name">{name || 'TARS'}</span>
        {securityMode ? (
          <span className="top-status-hud__mode">{securityMode}</span>
        ) : null}
        {modelName ? <span className="top-status-hud__model t-meta">{modelName}</span> : null}
        <span className="top-status-hud__sep" aria-hidden="true" />
        <span className="top-status-hud__state t-meta">{state}</span>
        <button
          type="button"
          className={`top-status-hud__activity${running > 0 ? ' is-live' : ''}`}
          aria-expanded={mapOpen}
          aria-label={`${running} running — toggle activity map`}
          onClick={() => setMapOpen((v) => !v)}
        >
          <span className="top-status-hud__activity-glyph" aria-hidden="true">◉</span>
          <span className="top-status-hud__activity-count t-meta">{running} running</span>
        </button>
        <span className="top-status-hud__reflected t-meta">
          reflected {formatRelative(lastReflectedAt)}
        </span>
        {showUpdateChip && (
          <button
            type="button"
            className="top-status-hud__update"
            disabled={updateApplying}
            aria-label={
              updateApplying
                ? 'Applying update — TARS will restart shortly'
                : `Update available, ${updateBehind} commit${updateBehind === 1 ? '' : 's'} behind — click to apply`
            }
            onClick={() => void applyUpdate()}
          >
            {updateApplying ? 'restarting…' : `update · ${updateBehind}`}
          </button>
        )}
      </div>
      {mapOpen && <ActivityMap onClose={() => setMapOpen(false)} />}
    </>
  );
}

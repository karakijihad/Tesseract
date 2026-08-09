// Slice 2 — floating top-centre status pill for the cockpit. A glass HUD
// carrying the assistant's identity at a glance (name · mode · model · state · last
// reflected), so the operator reads status without the right rail expanded.
// Mounted by CockpitStage (SC-1) — always-on in the immersive cockpit.
//
// HUD runs-surface fix (change-set A): the permanent activity segment +
// ActivityMap toggle live here now (replacing the bottom ActivityPill, which
// rendered nothing when idle and was the only `hydrate()` call site). The
// segment is always rendered — "0 running" dimmed when idle, live-accented
// when work is in flight — so the operator always has a running-work
// indicator, not just when something happens to be active.

import { useEffect, useState } from "react";

import { useIdentityStore } from "../../stores/identity";
import { useEntityStore } from "../../stores/entity";
import { useSoulStore } from "../../stores/soul";
import { useActivityStore } from "../../stores/activity";
import { needsManualRestart, useUpdateStore } from "../../stores/update";
import { isTauri } from "../../lib/endpoints";
import { formatRelative } from "../../lib/time";
import { ActivityMap } from "../../cockpit/ActivityMap";

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
  const updateError = useUpdateStore((s) => s.error);
  const updateErrorSource = useUpdateStore((s) => s.errorSource);
  const applyUpdate = useUpdateStore((s) => s.apply);
  const exeAvailable = useUpdateStore((s) => s.exeAvailable);
  const exeVersion = useUpdateStore((s) => s.exeVersion);
  const exeApplying = useUpdateStore((s) => s.exeApplying);
  const exeApply = useUpdateStore((s) => s.exeApply);

  // Apply is HUD-only (Settings only offers "check now"), so an apply
  // failure — including the worst case, the app's respawn itself failing —
  // must be visible right here, not just in a Settings panel the user has
  // no reason to open. A stale background-check network blip stays
  // Settings-only (errorSource === 'check'); only a failed apply attempt
  // pre-empts the normal "update available" pill.
  const updateFailed =
    isTauri() &&
    !updateApplying &&
    updateErrorSource === "apply" &&
    !!updateError;
  const manualRestart =
    updateFailed && updateError ? needsManualRestart(updateError) : false;
  // Shell self-update chip: a newer installer exists. Clicking is the
  // consent — download, verify, restart into the new version.
  const showExeChip = isTauri() && !updateFailed && exeAvailable;
  // One chip at a time (operator request 2026-07-30 — two yellow chips read
  // as duplicate actions). The exe chip wins when both are pending (the
  // rarer, bigger update — shell + bundled frontend); the git chip
  // resurfaces on the post-relaunch check if the app tree is still behind.
  const showUpdateChip =
    isTauri() && !updateFailed && updateBehind > 0 && !showExeChip;

  useEffect(() => {
    void hydrateActivity();
  }, [hydrateActivity]);

  const ledClass =
    state === "error" ? "is-bad" : state === "idle" ? "is-ok" : "is-active";

  return (
    <>
      <div className="top-status-hud">
        <span
          className={`top-status-hud__led ${ledClass}`}
          aria-hidden="true"
        />
        <span className="top-status-hud__name">{name}</span>
        {securityMode ? (
          <span className="top-status-hud__mode">{securityMode}</span>
        ) : null}
        {modelName ? (
          <span className="top-status-hud__model t-meta">{modelName}</span>
        ) : null}
        <span className="top-status-hud__sep" aria-hidden="true" />
        <span className="top-status-hud__state t-meta">{state}</span>
        <button
          type="button"
          className={`top-status-hud__activity${running > 0 ? " is-live" : ""}`}
          aria-expanded={mapOpen}
          aria-label={`${running} running — toggle activity map`}
          onClick={() => setMapOpen((v) => !v)}
        >
          <span className="top-status-hud__activity-glyph" aria-hidden="true">
            ◉
          </span>
          <span className="top-status-hud__activity-count t-meta">
            {running} running
          </span>
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
                ? "Applying update — TESSERACT will restart shortly"
                : `Update available, ${updateBehind} commit${updateBehind === 1 ? "" : "s"} behind — click to apply`
            }
            onClick={() => void applyUpdate()}
          >
            {updateApplying ? "restarting…" : `update · ${updateBehind}`}
          </button>
        )}
        {showExeChip && (
          <button
            type="button"
            className="top-status-hud__update"
            disabled={exeApplying}
            aria-label={
              exeApplying
                ? "Downloading the new version — TESSERACT will restart itself"
                : `TESSERACT ${exeVersion} is available — click to download and restart`
            }
            title={
              exeApplying
                ? "Downloading the new version — TESSERACT will restart itself"
                : `TESSERACT ${exeVersion} is available — click to download and restart`
            }
            onClick={() => void exeApply()}
          >
            {exeApplying ? "downloading…" : `new version · ${exeVersion}`}
          </button>
        )}
        {updateFailed && (
          <button
            type="button"
            className={`top-status-hud__update top-status-hud__update--failed${manualRestart ? " is-manual" : ""}`}
            aria-label={updateError ?? "update failed"}
            title={updateError ?? "update failed"}
            onClick={() => void applyUpdate()}
          >
            {manualRestart
              ? "update failed — restart TESSERACT"
              : "update failed — retry"}
          </button>
        )}
      </div>
      {mapOpen && <ActivityMap onClose={() => setMapOpen(false)} />}
    </>
  );
}

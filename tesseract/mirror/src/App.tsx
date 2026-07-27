import { useEffect, useState } from 'react';
import { useWebSocket } from './hooks/useWebSocket';
import { fetchCostState } from './lib/api';
import { isTauri } from './lib/endpoints';
import { useUIStore } from './stores/ui';
import { useConversationStore } from './stores/conversation';
import { useChannelsStore } from './stores/channels';
import { useWebSocketStore } from './stores/websocket';
import { useCostStore } from './stores/cost';
import { useSurfacesStore } from './stores/surfaces';
import { useUpdateStore } from './stores/update';
import { usePanelStore } from './cockpit/panelStore';
import { loadSlashCommands } from './lib/slashCommands';
import { installViewSnapshotWatcher } from './lib/viewSnapshot';
import { CockpitStage } from './cockpit/CockpitStage';
import { RegionCapture } from './cockpit/RegionCapture';
import { GlobalCanvas } from './components/layout/GlobalCanvas';
import { MotionTestPanel } from './components/debug/MotionTestPanel';
import { SessionDrawer } from './components/sessions/SessionDrawer';
import { ToastStack } from './components/ui/ToastStack';
import { ResetConfirmDialog } from './components/chat/ResetConfirmDialog';

function msUntilNextLocalMidnight(now = new Date()): number {
  const next = new Date(now);
  next.setHours(24, 0, 0, 0);
  return Math.max(1_000, next.getTime() - now.getTime());
}

// Self-update poll cadence (task 13). update_check does a real network
// fetch against the GitHub remote, so this isn't a cheap local read — the
// operator pushes updates rarely (not multiple times a day), and the
// launch check plus the manual "Check now" control in Settings already
// cover the moments an operator actually cares about catching one same-day.
// 6h keeps a long-running session from going more than half a day without
// noticing an update exists, without polling GitHub needlessly.
const UPDATE_CHECK_INTERVAL_MS = 6 * 60 * 60 * 1000;

// SC-1 — the tab-switching ViewRouter + grid CockpitShell are replaced by the
// fixed spatial CockpitStage: the orb (GlobalCanvas) is the permanent
// centerpiece, framed by rails + top/bottom HUDs, and clicking a tab summons a
// whole view as a movable glass panel over the orb (panel manager wired in
// SC-2). The orb keeps its exact prior rendering — full mode, clamped above the
// `.cockpit-hud`, immersive `data-view="tars"` atmosphere.
// Spec: Docs/Plan/tars-cockpit/design/2026-06-17-spatial-cockpit-spec.md.
function App() {
  useWebSocket();

  useEffect(() => {
    let cancelled = false;
    let timer: number | null = null;

    const refreshCostState = async () => {
      try {
        const snapshot = await fetchCostState();
        if (cancelled) return;
        useCostStore.getState().applySnapshot({
          timestamp: new Date().toISOString(),
          data: snapshot,
        });
      } catch {
        // Keep the last known totals on screen; the next websocket snapshot
        // or midnight tick will reconcile if this fetch blips.
      }
    };

    const armNextRefresh = () => {
      timer = window.setTimeout(async () => {
        await refreshCostState();
        if (!cancelled) armNextRefresh();
      }, msUntilNextLocalMidnight());
    };

    void refreshCostState();
    armNextRefresh();

    return () => {
      cancelled = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, []);

  // Self-update: launch check + periodic re-check. Tauri-only — a browser
  // dev session has no update.rs IPC bridge to call.
  useEffect(() => {
    if (!isTauri()) return;
    const check = useUpdateStore.getState().check;
    void check();
    const id = window.setInterval(() => void check(), UPDATE_CHECK_INTERVAL_MS);
    return () => window.clearInterval(id);
  }, []);

  // Hydrate the slash palette once on mount. Failures are logged but
  // non-fatal — chat still works, autocomplete just stays empty.
  useEffect(() => {
    void loadSlashCommands();
    installViewSnapshotWatcher();
  }, []);
  const [showMotionPanel, setShowMotionPanel] = useState(() => {
    if (typeof window === 'undefined') return false;
    return new URLSearchParams(window.location.search).has('debug');
  });

  useEffect(() => {
    if (!import.meta.env.DEV) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey && e.shiftKey && (e.key === 'D' || e.key === 'd')) {
        e.preventDefault();
        setShowMotionPanel((v) => !v);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  useEffect(() => {
    if (!import.meta.env.DEV) return;
    const w = window as typeof window & {
      __tesseractTestStores?: {
        conversation: typeof useConversationStore;
        ui: typeof useUIStore;
        channels: typeof useChannelsStore;
        websocket: typeof useWebSocketStore;
        surfaces: typeof useSurfacesStore;
        panel: typeof usePanelStore;
        update: typeof useUpdateStore;
      };
    };
    w.__tesseractTestStores = {
      conversation: useConversationStore,
      ui: useUIStore,
      channels: useChannelsStore,
      websocket: useWebSocketStore,
      surfaces: useSurfacesStore,
      // P5 e2e — the cockpit mounts ChatView as a glass panel (viewRegistry),
      // so the multi-chat e2e opens it via panel.openPanel('chat').
      panel: usePanelStore,
      // Task 13 — lets Playwright/manual dev verification drive the update
      // chip/Settings row without a real Tauri IPC bridge.
      update: useUpdateStore,
    };
    return () => {
      delete w.__tesseractTestStores;
    };
  }, []);

  return (
    <>
      <CockpitStage />
      <RegionCapture />
      <GlobalCanvas />
      <SessionDrawer />
      <ToastStack />
      <ResetConfirmDialog />
      {import.meta.env.DEV && showMotionPanel && <MotionTestPanel />}
    </>
  );
}

export default App;

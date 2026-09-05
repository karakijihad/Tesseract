// SC-2 — maps a cockpit tab `kind` to the REAL view component (unchanged from
// SC-0) that the panel manager summons into a GlassPanel. `orb` is absent on
// purpose: it is the orb home (the bare GlobalCanvas), not a panel — reached by
// closing the active panel, not by opening one (see panelStore.closePanel /
// resetAll). There is no dedicated "the assistant" tab; the orb is always on-canvas.

import type { ReactNode } from 'react';

import type { PanelKind } from './panelStore';
import { usePanelStore } from './panelStore';
import { LeftPanel } from '../components/cockpit/left/LeftPanel';
import { RightPanel } from '../components/cockpit/RightPanel';
import { AutonomyView } from '../views/AutonomyView';
import { PulseView } from '../views/PulseView';
import { ChatView } from '../views/ChatView';
import { TerminalView } from '../views/TerminalView';
import { ChannelsView } from '../views/ChannelsView';
import { IdentityView } from '../views/IdentityView';
import { ConscienceView } from '../views/ConscienceView';
import { WorkspaceView } from '../views/WorkspaceView';
import { GraphView } from '../views/GraphView';
import { SettingsView } from '../views/SettingsView';

export const VIEW_REGISTRY: Record<PanelKind, () => ReactNode> = {
  autonomy: () => <AutonomyView />,
  pulse: () => <PulseView />,
  chat: () => <ChatView />,
  terminal: () => <TerminalView />,
  channels: () => <ChannelsView />,
  identity: () => <IdentityView />,
  conscience: () => <ConscienceView />,
  workspace: () => <WorkspaceView />,
  // The way back is the map's own, not the panel chrome's. Closing a panel
  // leaves the operator on the orb, which is not where they came from: the
  // map's home is the Atlas room and it opens from there.
  graph: () => (
    <GraphView
      onBack={() => usePanelStore.getState().openPanel('autonomy')}
    />
  ),
  settings: () => <SettingsView />,
  // SC-3 — the rails are panels too (Kernel left, right rail — id kept as
  // 'lifeline' for panel-store type stability — hosts Breakers/Observer).
  kernel: () => <LeftPanel />,
  lifeline: () => <RightPanel />,
};

export { VIEW_LABELS } from './viewLabels';

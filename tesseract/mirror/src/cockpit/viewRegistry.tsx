// SC-2 — maps a cockpit tab `kind` to the REAL view component (unchanged from
// SC-0) that the panel manager summons into a GlassPanel. `orb` is absent on
// purpose: it is the orb home (the bare GlobalCanvas), not a panel — reached by
// closing the active panel, not by opening one (see panelStore.closePanel /
// resetAll). There is no dedicated "the assistant" tab; the orb is always on-canvas.

import type { ReactNode } from 'react';

import type { PanelKind } from './panelStore';
import { LeftPanel } from '../components/cockpit/left/LeftPanel';
import { RightPanel } from '../components/cockpit/RightPanel';
import { AutonomyView } from '../views/AutonomyView';
import { PulseView } from '../views/PulseView';
import { ChatView } from '../views/ChatView';
import { TerminalView } from '../views/TerminalView';
import { ScheduleView } from '../views/ScheduleView';
import { AgentsView } from '../views/AgentsView';
import { ChannelsView } from '../views/ChannelsView';
import { IdentityView } from '../views/IdentityView';
import { ConscienceView } from '../views/ConscienceView';
import { WorkspaceView } from '../views/WorkspaceView';
import { SettingsView } from '../views/SettingsView';

export const VIEW_REGISTRY: Record<PanelKind, () => ReactNode> = {
  autonomy: () => <AutonomyView />,
  pulse: () => <PulseView />,
  chat: () => <ChatView />,
  terminal: () => <TerminalView />,
  schedule: () => <ScheduleView />,
  agents: () => <AgentsView />,
  channels: () => <ChannelsView />,
  identity: () => <IdentityView />,
  conscience: () => <ConscienceView />,
  workspace: () => <WorkspaceView />,
  settings: () => <SettingsView />,
  // SC-3 — the rails are panels too (Kernel left, right rail — id kept as
  // 'lifeline' for panel-store type stability — hosts Breakers/Observer).
  kernel: () => <LeftPanel />,
  lifeline: () => <RightPanel />,
};

export const VIEW_LABELS: Record<PanelKind, string> = {
  autonomy: 'Autonomy',
  pulse: 'Pulse',
  chat: 'Chat',
  terminal: 'Terminal',
  schedule: 'Schedule',
  agents: 'Agents',
  channels: 'Channels',
  identity: 'Identity',
  conscience: 'Conscience',
  workspace: 'Workspace',
  settings: 'Settings',
  kernel: 'Kernel',
  lifeline: 'Monitor',
};

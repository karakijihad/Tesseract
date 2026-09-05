// What each cockpit tab is called, kept apart from `viewRegistry` on purpose.
// The registry holds the view COMPONENTS, so importing it pulls in every view
// in the app; anything that only needs a name (the dock's chips) would drag the
// whole tree behind it. `viewRegistry` re-exports this, so existing callers are
// unaffected.

import type { PanelKind } from './panelStore';

export const VIEW_LABELS: Record<PanelKind, string> = {
  autonomy: 'Autonomy',
  pulse: 'Pulse',
  chat: 'Chat',
  terminal: 'Terminal',
  channels: 'Channels',
  identity: 'Identity',
  conscience: 'Conscience',
  workspace: 'Workspace',
  graph: 'The map',
  settings: 'Settings',
  kernel: 'Kernel',
  lifeline: 'Monitor',
};

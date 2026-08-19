import { useEffect, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useUIStore, type View } from "../../stores/ui";
import { isRailKind, usePanelStore } from "../../cockpit/panelStore";
import { useWorkspaceStore } from "../../stores/workspace";
import { useHudDockStore } from "../../stores/hudDock";
import { Hint } from "../ui/Hint";
import { ChatHudGroup } from "./hud/ChatHudGroup";
import { HudChatInput } from "./hud/HudChatInput";
import { EdgeTab } from '../common/EdgeTab';

interface TabDef {
  id: View;
  icon: ReactNode;
  label: string;
}

// Inline SVGs (1em, stroke: currentColor) replace the prior ◎ / ✦
// placeholder Unicode glyphs which rendered inconsistently across OSes.
// Terminal (❯) keeps Unicode — it reads cleanly everywhere.
const PulseIcon = () => (
  <svg
    viewBox="0 0 24 12"
    width="1em"
    height="0.5em"
    stroke="currentColor"
    fill="none"
    strokeWidth={1.5}
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <polyline points="0,6 4,6 6,1 8,11 10,6 14,6 16,3 18,9 20,6 24,6" />
  </svg>
);

const ChatIcon = () => (
  <svg
    viewBox="0 0 20 20"
    width="1em"
    height="1em"
    stroke="currentColor"
    fill="none"
    strokeWidth={1.5}
    strokeLinejoin="round"
  >
    <path d="M2 2h16a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H6l-4 4V3a1 1 0 0 1 1-1z" />
    <circle cx="7" cy="8" r="1" fill="currentColor" stroke="none" />
    <circle cx="13" cy="8" r="1" fill="currentColor" stroke="none" />
  </svg>
);

// Schedule tab — clock face. Stroke-only so it picks up currentColor.
const ScheduleIcon = () => (
  <svg
    viewBox="0 0 20 20"
    width="1em"
    height="1em"
    stroke="currentColor"
    fill="none"
    strokeWidth={1.5}
    strokeLinecap="round"
  >
    <circle cx="10" cy="10" r="7" />
    <path d="M10 6 V10 L13 12" />
  </svg>
);

// Conscience tab — balance-scale glyph reads as "weighed / judged";
// stroke-only so it picks up currentColor like the other tab icons.
const ConscienceIcon = () => (
  <svg
    viewBox="0 0 20 20"
    width="1em"
    height="1em"
    stroke="currentColor"
    fill="none"
    strokeWidth={1.3}
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <path d="M10 3 V17" />
    <path d="M4 17 H16" />
    <path d="M3 8 L6 13 L9 8" />
    <path d="M11 8 L14 13 L17 8" />
    <path d="M3 8 H17" />
  </svg>
);

// Identity tab — concentric circles + radiating arcs evoke an inner core
// surrounded by aura. Stroke-only so it picks up currentColor like the
// other tab icons.
const IdentityIcon = () => (
  <svg
    viewBox="0 0 20 20"
    width="1em"
    height="1em"
    stroke="currentColor"
    fill="none"
    strokeWidth={1.3}
    strokeLinecap="round"
  >
    <circle cx="10" cy="10" r="2.5" />
    <circle cx="10" cy="10" r="6" opacity="0.55" />
    <path
      d="M10 1.5 V3.5 M10 16.5 V18.5 M1.5 10 H3.5 M16.5 10 H18.5"
      opacity="0.7"
    />
  </svg>
);

const AgentsIcon = () => (
  <svg
    viewBox="0 0 20 20"
    width="1em"
    height="1em"
    stroke="currentColor"
    fill="none"
    strokeWidth={1.4}
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <circle cx="10" cy="7" r="3" />
    <path d="M3.5 17 C3.5 13.5 6.5 11.5 10 11.5 C13.5 11.5 16.5 13.5 16.5 17" />
  </svg>
);

// Channels tab — two overlapping speech bubbles. Reads as "messaging
// channels" (Telegram + future WhatsApp / Signal adapters) without the
// proportional ambiguity of a single vertical-mast silhouette.
const ChannelsIcon = () => (
  <svg
    viewBox="0 0 20 20"
    width="1em"
    height="1em"
    stroke="currentColor"
    fill="none"
    strokeWidth={1.4}
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <path d="M3 4 H12 V11 H7 L4.5 13.5 V11 H3 Z" />
    <path d="M9 8 H17 V14 H14.5 L12.5 16 V14 H9" opacity="0.75" />
  </svg>
);

// Workspace tab — inbox/tray icon. Stroke-only so it picks up
// currentColor like the other tab icons.
const WorkspaceIcon = () => (
  <svg
    viewBox="0 0 20 20"
    width="1em"
    height="1em"
    stroke="currentColor"
    fill="none"
    strokeWidth={1.4}
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <path d="M3 5 H17 V14 H3 Z" />
    <path d="M3 11 H7 L8.5 12.5 H11.5 L13 11 H17" />
  </svg>
);

// Settings tab — proper 8-tooth notched mechanical gear. Stroke-only so it
// picks up currentColor; teeth are small rounded rectangles ringing the
// pitch circle, with a center hub.
const SettingsIcon = () => (
  <svg
    viewBox="0 0 20 20"
    width="1em"
    height="1em"
    stroke="currentColor"
    fill="none"
    strokeWidth={1.3}
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    {[0, 45, 90, 135, 180, 225, 270, 315].map((angle) => (
      <rect
        key={angle}
        x={9.3}
        y={1.2}
        width={1.4}
        height={2.4}
        rx={0.3}
        transform={`rotate(${angle} 10 10)`}
      />
    ))}
    <circle cx="10" cy="10" r="6" />
    <circle cx="10" cy="10" r="2" />
  </svg>
);

const TABS: TabDef[] = [
  { id: "autonomy", icon: "⚡", label: "Autonomy" },
  { id: "pulse", icon: <PulseIcon />, label: "Pulse" },
  { id: "chat", icon: <ChatIcon />, label: "Chat" },
  { id: "terminal", icon: "❯", label: "Terminal" },
  { id: "schedule", icon: <ScheduleIcon />, label: "Schedule" },
  { id: "agents", icon: <AgentsIcon />, label: "Agents" },
  { id: "channels", icon: <ChannelsIcon />, label: "Channels" },
  { id: "identity", icon: <IdentityIcon />, label: "Identity" },
  { id: "conscience", icon: <ConscienceIcon />, label: "Conscience" },
  { id: "workspace", icon: <WorkspaceIcon />, label: "Workspace" },
  { id: "settings", icon: <SettingsIcon />, label: "Settings" },
];

function badgeText(n: number): string {
  return n > 9 ? "9+" : String(n);
}

// Every tab, always on the bar (operator, 2026-08-13). They used to live in a
// section stack behind one icon, which meant two clicks and a guess to reach a
// view; eleven icon buttons cost ~310px, which the bar has.
function ViewTabs() {
  const view = useUIStore((s) => s.view);
  // SC-2 — a tab summons its whole view as a glass panel (openPanel also drives
  // `setView`, preserving the terminal-fit / chat-collapse / snapshot
  // subscribers). `orb` routes to the home view (resetAll) inside openPanel.
  const openPanel = usePanelStore((s) => s.openPanel);
  const fetchInbox = useWorkspaceStore((s) => s.fetchInbox);
  const fetchSeen = useWorkspaceStore((s) => s.fetchSeen);
  const unreadCount = useWorkspaceStore((s) => s.unreadCount());

  // One-shot load on mount so the badge is correct before the operator
  // ever opens the Workspace tab.
  useEffect(() => {
    fetchInbox();
    fetchSeen();
  }, [fetchInbox, fetchSeen]);

  return (
    <div className="hud-tabs" role="group" aria-label="Views">
      {TABS.map((t) => {
        const badge =
          t.id === "workspace" && unreadCount > 0
            ? { count: unreadCount, label: "pending" }
            : null;
        return (
          <Hint key={t.id} label={t.label} maxWidth={140}>
            <button
              type="button"
              className={`hud-tab${view === t.id ? " is-active" : ""}`}
              onClick={() => openPanel(t.id)}
              aria-label={
                badge ? `${t.label} (${badge.count} ${badge.label})` : t.label
              }
              aria-current={view === t.id ? "page" : undefined}
            >
              <span className="hud-tab-icon" aria-hidden="true">
                {t.icon}
              </span>
              {badge && (
                <span className="hud-tab-badge" aria-hidden="true">
                  {badgeText(badge.count)}
                </span>
              )}
            </button>
          </Hint>
        );
      })}
    </div>
  );
}

// Close every open view at once, back to the orb. `resetAll` is what the orb
// tab already does, so this is the same act reachable without hunting for it
// (operator, 2026-08-14) — pinned panels survive, which is what pinning means.
function CloseAllButton() {
  const closeAllViews = usePanelStore((s) => s.closeAllViews);
  const openCount = usePanelStore(
    (s) => s.panels.filter((p) => p.open && !isRailKind(p.kind)).length,
  );
  // Always present, disabled when there is nothing to close: a control that
  // comes and goes moves the two beside it every time a panel opens.
  return (
    <Hint
      label={
        openCount === 0
          ? 'No views open'
          : `Close all ${openCount} open view${openCount === 1 ? '' : 's'}`
      }
      maxWidth={160}
    >
      {/* Not a `CloseButton`: this closes no surface of its own, it is a bulk
          command in the HUD's icon row beside the collapse chevron and the
          sessions drawer, and it carries that row's 32px tab footprint. It
          renders the app's one close CHARACTER all the same. */}
      <button
        type="button"
        className="hud-tab hud-close-all"
        onClick={closeAllViews}
        disabled={openCount === 0}
        aria-label="Close all open views"
      >
        ×
      </button>
    </Hint>
  );
}

// Single-arrow collapse (operator spec): the chevron tucks the whole bar; the
// portalled edge tab brings it back.
function CollapseButton() {
  const setTucked = useHudDockStore((s) => s.setTucked);
  return (
    <Hint label="Hide HUD" maxWidth={120}>
      <button
        type="button"
        className="hud-tab hud-collapse"
        onClick={() => setTucked(true)}
        aria-label="Hide the bottom HUD"
      >
        ▾
      </button>
    </Hint>
  );
}

function DockRestoreTab() {
  const tucked = useHudDockStore((s) => s.tucked);
  const setTucked = useHudDockStore((s) => s.setTucked);
  if (!tucked) return null;
  return createPortal(
    <EdgeTab side="bottom" onClick={() => setTucked(false)} ariaLabel="Show the bottom HUD">
      ▴
    </EdgeTab>,
    document.body,
  );
}

// Centred: [every view tab] [mic + sessions + tokens] [chat input] [▾].
//
// What used to be here and is not: the stage-controls stack (summoned panes,
// orb and caption toggles, the two rail letters), the observer group, and the
// chat model chip. The panes menu listed windows you can already see; orb and
// captions moved under the assistant's name in the top HUD, where the thing
// they toggle lives; the rails carry their own edge tabs now; the observer has
// its own Monitor tab; and the model was already named in the top HUD, two
// inches away.
export function BottomHud() {
  const tucked = useHudDockStore((s) => s.tucked);
  return (
    <>
      <div className={`cockpit-hud-inner${tucked ? " is-tucked" : ""}`}>
        <ViewTabs />
        <span className="hud-sep" aria-hidden="true" />
        <ChatHudGroup />
        <HudChatInput />
        <CloseAllButton />
        <CollapseButton />
      </div>
      <DockRestoreTab />
    </>
  );
}

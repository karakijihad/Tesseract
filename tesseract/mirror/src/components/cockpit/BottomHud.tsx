import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { createPortal } from 'react-dom';
import { useUIStore, type View } from '../../stores/ui';
import { usePanelStore, isRailKind } from '../../cockpit/panelStore';
import { VIEW_LABELS } from '../../cockpit/viewRegistry';
import { useCaptionsStore } from '../../stores/captions';
import { useOrbVisibilityStore } from '../../stores/orbVisibility';
import { useWorkspaceStore } from '../../stores/workspace';
import { spawnTrio } from '../../canvas/triorenderer';
import { Hint } from '../ui/Hint';
import { ChatHudGroup } from './hud/ChatHudGroup';
import { HudChatInput } from './hud/HudChatInput';
import { ObserverHudGroup } from './hud/ObserverHudGroup';
// StatusLine removed from the HUD — entity state already renders in the
// right-panel header next to the TARS name, no point duplicating it.

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

// Soul tab — concentric circles + radiating arcs evoke an inner core
// surrounded by aura. Stroke-only so it picks up currentColor like the
// other tab icons.
const SoulIcon = () => (
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
    <path d="M10 1.5 V3.5 M10 16.5 V18.5 M1.5 10 H3.5 M16.5 10 H18.5" opacity="0.7" />
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
  { id: 'autonomy',   icon: '⚡',               label: 'Autonomy' },
  { id: 'pulse',      icon: <PulseIcon />,      label: 'Pulse' },
  { id: 'chat',       icon: <ChatIcon />,       label: 'Chat' },
  { id: 'terminal',   icon: '❯',                label: 'Terminal' },
  { id: 'schedule',   icon: <ScheduleIcon />,   label: 'Schedule' },
  { id: 'agents',     icon: <AgentsIcon />,     label: 'Agents' },
  { id: 'channels',   icon: <ChannelsIcon />,   label: 'Channels' },
  { id: 'soul',       icon: <SoulIcon />,       label: 'Soul' },
  { id: 'conscience', icon: <ConscienceIcon />, label: 'Conscience' },
  { id: 'workspace',  icon: <WorkspaceIcon />,  label: 'Workspace' },
  { id: 'settings',   icon: <SettingsIcon />,   label: 'Settings' },
];

function _badgeText(n: number): string {
  return n > 9 ? '9+' : String(n);
}

function TabsZone() {
  const view = useUIStore(s => s.view);
  // SC-2 — a tab summons its whole view as a glass panel (openPanel also drives
  // `setView`, preserving the terminal-fit / chat-collapse / snapshot
  // subscribers). `tars` routes to the orb home (resetAll) inside openPanel.
  const openPanel = usePanelStore(s => s.openPanel);
  const fetchInbox = useWorkspaceStore(s => s.fetchInbox);
  const fetchSeen = useWorkspaceStore(s => s.fetchSeen);
  const unreadCount = useWorkspaceStore(s => s.unreadCount());

  // One-shot load on mount so the badge is correct before the operator
  // ever opens the Workspace tab.
  useEffect(() => {
    fetchInbox();
    fetchSeen();
  }, [fetchInbox, fetchSeen]);

  const badgeFor = (id: View): { count: number; label: string } | null => {
    if (id === 'workspace' && unreadCount > 0) return { count: unreadCount, label: 'pending' };
    return null;
  };

  return (
    <nav className="hud-tabs" aria-label="Cockpit tabs">
      {TABS.map((t) => {
        const badge = badgeFor(t.id);
        return (
          <Hint key={t.id} label={t.label} position="top" maxWidth={120}>
            <button
              type="button"
              className={`hud-tab${view === t.id ? ' is-active' : ''}`}
              onClick={() => openPanel(t.id)}
              aria-label={
                badge ? `${t.label} (${badge.count} ${badge.label})` : t.label
              }
              aria-current={view === t.id ? 'page' : undefined}
            >
              <span className="hud-tab-icon" aria-hidden="true">{t.icon}</span>
              {badge && (
                <span className="hud-tab-badge" aria-hidden="true">
                  {_badgeText(badge.count)}
                </span>
              )}
            </button>
          </Hint>
        );
      })}
    </nav>
  );
}

// SC-3 — [K][L] toggles hide/show the Kernel / right (id: lifeline, hosts
// Breakers/Observer) rail panels; active reflects the rail's open state (a
// rail closed by its × un-presses the toggle).
function RailToggles() {
  const toggleRail = usePanelStore((s) => s.toggleRail);
  const kernelOpen = usePanelStore((s) => s.panels.find((p) => p.id === 'kernel')?.open ?? false);
  const lifelineOpen = usePanelStore((s) => s.panels.find((p) => p.id === 'lifeline')?.open ?? false);
  const rails: { id: 'kernel' | 'lifeline'; key: string; label: string; open: boolean }[] = [
    { id: 'kernel', key: 'K', label: 'Kernel', open: kernelOpen },
    { id: 'lifeline', key: 'L', label: 'Monitor', open: lifelineOpen },
  ];
  return (
    <div className="hud-rail-toggles" aria-label="Rail toggles">
      {rails.map((r) => (
        <Hint key={r.id} label={`${r.open ? 'Hide' : 'Show'} ${r.label}`} position="top" maxWidth={120}>
          <button
            type="button"
            className={`hud-tab hud-rail-toggle${r.open ? ' is-active' : ''}`}
            onClick={() => toggleRail(r.id)}
            aria-label={`${r.open ? 'Hide' : 'Show'} ${r.label} rail`}
            aria-pressed={r.open}
          >
            {r.key}
          </button>
        </Hint>
      ))}
    </div>
  );
}

// "Summoned panes" — a dropdown listing every open view panel (a window list)
// so the operator can jump to, restore (un-minimize), or close any of them even
// when one is buried, minimized off-stage, or maximized over the rest.
const SUMMONED_GAP = 8;
const SUMMONED_PAD = 8;

function SummonedPanes() {
  const panels = usePanelStore((s) => s.panels);
  const focus = usePanelStore((s) => s.focus);
  const toggleMinimize = usePanelStore((s) => s.toggleMinimize);
  const closePanel = usePanelStore((s) => s.closePanel);
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLUListElement>(null);
  const [coords, setCoords] = useState<{ top: number; left: number } | null>(null);

  const summoned = panels.filter((p) => p.open && !isRailKind(p.kind));

  // The HUD bar clips its overflow (`.cockpit-hud { overflow: hidden }`), so the
  // menu must portal to <body> and position with `fixed` coords — otherwise it
  // pops up outside the 48px bar and is clipped (mirrors Hint.tsx).
  const reposition = useCallback(() => {
    const trigger = ref.current;
    const menu = menuRef.current;
    if (!trigger || !menu) return;
    const tRect = trigger.getBoundingClientRect();
    const mRect = menu.getBoundingClientRect();
    const vw = window.innerWidth;
    let top = tRect.top - mRect.height - SUMMONED_GAP; // open upward
    if (top < SUMMONED_PAD) top = tRect.bottom + SUMMONED_GAP; // flip down if no room
    let left = tRect.left;
    left = Math.max(SUMMONED_PAD, Math.min(left, vw - mRect.width - SUMMONED_PAD));
    setCoords({ top, left });
  }, []);

  // Close the menu on any outside click (the menu is portalled, so test it too).
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (ref.current?.contains(t) || menuRef.current?.contains(t)) return;
      setOpen(false);
    };
    window.addEventListener('mousedown', onDown);
    return () => window.removeEventListener('mousedown', onDown);
  }, [open]);

  useLayoutEffect(() => {
    if (open) reposition();
    else setCoords(null);
  }, [open, summoned.length, reposition]);

  useEffect(() => {
    if (!open) return;
    const handler = () => reposition();
    window.addEventListener('scroll', handler, true);
    window.addEventListener('resize', handler);
    return () => {
      window.removeEventListener('scroll', handler, true);
      window.removeEventListener('resize', handler);
    };
  }, [open, reposition]);

  const jumpTo = (id: (typeof summoned)[number]['id'], minimized: boolean) => {
    // A minimized panel restores (toggleMinimize raises + re-activates); an
    // already-visible one just comes to the front.
    if (minimized) toggleMinimize(id);
    else focus(id);
    setOpen(false);
  };

  return (
    <div className="hud-panes" ref={ref}>
      <Hint label="Summoned panes" position="top" maxWidth={120}>
        <button
          type="button"
          className={`hud-tab hud-panes__btn${open ? ' is-active' : ''}`}
          onClick={() => setOpen((v) => !v)}
          aria-label={`Summoned panes (${summoned.length} open)`}
          aria-expanded={open}
          disabled={summoned.length === 0}
        >
          <span aria-hidden="true">▤</span>
          {summoned.length > 0 && <span className="hud-tab-badge" aria-hidden="true">{summoned.length}</span>}
        </button>
      </Hint>
      {open && summoned.length > 0 &&
        createPortal(
          <ul
            ref={menuRef}
            className="hud-panes__menu"
            role="menu"
            style={{
              top: coords?.top ?? -9999,
              left: coords?.left ?? -9999,
              opacity: coords ? 1 : 0,
            }}
          >
            {summoned.map((p) => (
              <li key={p.id} className="hud-panes__row" role="none">
                <button
                  type="button"
                  role="menuitem"
                  className="hud-panes__jump"
                  onClick={() => jumpTo(p.id, p.minimized)}
                >
                  <span className="hud-panes__label">{VIEW_LABELS[p.kind]}</span>
                  {p.minimized && <span className="hud-panes__tag t-meta">minimized</span>}
                </button>
                <button
                  type="button"
                  className="hud-panes__close"
                  aria-label={`Close ${VIEW_LABELS[p.kind]}`}
                  onClick={() => closePanel(p.id)}
                >
                  ×
                </button>
              </li>
            ))}
          </ul>,
          document.body,
        )}
    </div>
  );
}

// Two side-by-side panes — reads as the two lane cards the trio lays out.
const TrioIcon = () => (
  <svg
    viewBox="0 0 20 20"
    width="1em"
    height="1em"
    stroke="currentColor"
    fill="none"
    strokeWidth={1.4}
    strokeLinejoin="round"
  >
    <rect x="2.5" y="5" width="6" height="10" rx="1" />
    <rect x="11.5" y="5" width="6" height="10" rx="1" />
  </svg>
);

// Manual "load the trio lanes" action — ensures the named lanes (coder/claude +
// auditor/codex) and lays out their canvas cards. Same path as the typed
// `spawn trio` command, surfaced as a no-typing button. spawnTrio is idempotent
// and in-flight-deduped per view, so repeated clicks are safe.
function TrioButton() {
  const [busy, setBusy] = useState(false);
  const load = useCallback(() => {
    setBusy(true);
    void spawnTrio('tars').finally(() => setBusy(false));
  }, []);
  return (
    <Hint label="Load trio lanes" position="top" maxWidth={140}>
      <button
        type="button"
        className="hud-tab hud-rail-toggle"
        onClick={load}
        aria-label="Load trio lanes (coder + auditor)"
        disabled={busy}
      >
        <span className="hud-tab-icon" aria-hidden="true"><TrioIcon /></span>
      </button>
    </Hint>
  );
}

// Orb hide/show — the tab that used to park the cockpit on the bare orb is
// gone (the orb is always on-canvas now), so this is the operator's control
// for hiding it instead.
function OrbToggle() {
  const visible = useOrbVisibilityStore((s) => s.visible);
  const toggle = useOrbVisibilityStore((s) => s.toggle);
  return (
    <Hint label={`${visible ? 'Hide' : 'Show'} orb`} position="top" maxWidth={140}>
      <button
        type="button"
        className={`hud-tab hud-rail-toggle${visible ? ' is-active' : ''}`}
        onClick={toggle}
        aria-label={`${visible ? 'Hide' : 'Show'} the TARS orb`}
        aria-pressed={visible}
      >
        ◉
      </button>
    </Hint>
  );
}

// Ambient orb-captions on/off (TARS's latest line faded under the orb).
function CaptionsToggle() {
  const enabled = useCaptionsStore((s) => s.enabled);
  const toggle = useCaptionsStore((s) => s.toggle);
  return (
    <Hint label={`${enabled ? 'Hide' : 'Show'} orb captions`} position="top" maxWidth={140}>
      <button
        type="button"
        className={`hud-tab hud-rail-toggle${enabled ? ' is-active' : ''}`}
        onClick={toggle}
        aria-label={`${enabled ? 'Hide' : 'Show'} ambient TARS captions`}
        aria-pressed={enabled}
      >
        CC
      </button>
    </Hint>
  );
}

// Layout: [K][L] | tabs | chat-group (mic + sessions + stats) | observer-group | ←spacer→
// Entity state lives in the right-panel header — keeping it here too
// would duplicate the same `idle` / `thinking` / `error` text in two
// surfaces 80px apart.
export function BottomHud() {
  return (
    <div className="cockpit-hud-inner">
      <RailToggles />
      <span className="hud-sep" aria-hidden="true" />
      <SummonedPanes />
      <TrioButton />
      <OrbToggle />
      <CaptionsToggle />
      <span className="hud-sep" aria-hidden="true" />
      <TabsZone />
      <span className="hud-sep" aria-hidden="true" />
      <ChatHudGroup />
      <span className="hud-sep" aria-hidden="true" />
      <ObserverHudGroup />
      <div className="hud-spacer" aria-hidden="true" />
      <HudChatInput />
    </div>
  );
}

import { useCostStore } from '../../../stores/cost';
import { Hint } from '../../ui/Hint';
import { formatUsd, colorBand } from '../../../lib/money';

interface CostChipProps {
  role: 'chat_brain' | 'observer_agent';
  shortLabel: 'chat' | 'obs';
}

export function CostChip({ role, shortLabel }: CostChipProps) {
  const perRole = useCostStore((s) => s.perRole[role]);
  const globalState = useCostStore((s) => s.globalState);
  const overageUnlocked = useCostStore((s) => s.overageUnlocked);

  if (!perRole || !globalState) {
    return (
      <Hint
        label={`${shortLabel === 'chat' ? 'Chat' : 'Observer'} cost — no billed turns yet today`}
        position="top"
        maxWidth={240}
      >
        <span className="hud-stats hud-stats--empty" aria-label="No cost yet">
          <span className="hud-stats-text">{shortLabel} —</span>
          <span className="hud-stats-bar hud-stats-bar--empty" aria-hidden="true" />
        </span>
      </Hint>
    );
  }

  // Whichever ceiling is tighter (role sub-cap OR global remaining) governs
  // the colored meter — the operator sees "how close to blocked am I" at a
  // glance, not two independent gauges.
  const roleCap = perRole.role_cap_usd;
  const roleRatio = roleCap && roleCap > 0 ? perRole.role_total_usd / roleCap : 0;
  const globalRatio = globalState.cap_usd > 0 ? globalState.spent_usd / globalState.cap_usd : 0;
  const ratio = Math.min(Math.max(roleRatio, globalRatio), 1);
  // Cost UX overhaul — distinct visual for "operator approved continuing
  // past 100%". The chip is over-cap but NOT blocked; render in a red
  // overage state so the extra spend is loud and obvious.
  const roleScopeKey = `role:${role}`;
  const roleOver = roleCap !== null && perRole.role_total_usd > roleCap && overageUnlocked.includes(roleScopeKey);
  const globalOver = globalState.spent_usd > globalState.cap_usd && overageUnlocked.includes('global');
  const isOverage = roleOver || globalOver;
  const band = globalState.blocked ? 'bad' : colorBand(ratio);

  const roleName = role === 'chat_brain' ? 'Chat' : 'Observer';
  const roleCapText = roleCap !== null ? formatUsd(roleCap) : '—';
  const hint =
    `${roleName} ${formatUsd(perRole.role_total_usd)} / ${roleCapText} · ` +
    `global ${formatUsd(globalState.spent_usd)} / ${formatUsd(globalState.cap_usd)}` +
    (isOverage ? ' · OVERAGE (you approved)' : globalState.blocked ? ' · BLOCKED' : globalState.warning ? ' · warning' : '');

  return (
    <Hint label={hint} position="top" maxWidth={320}>
      <span
        className={`hud-stats hud-stats--${band}${isOverage ? ' is-overage' : ''}`}
        aria-label={hint}
      >
        <span className="hud-stats-text">
          {shortLabel} {formatUsd(perRole.role_total_usd)}
        </span>
        <span className="hud-stats-bar" aria-hidden="true">
          <span
            className="hud-stats-fill hud-stats-fill--chat"
            style={{ width: `${Math.round(ratio * 100)}%` }}
          />
        </span>
      </span>
    </Hint>
  );
}

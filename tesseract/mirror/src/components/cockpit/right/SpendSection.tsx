import { useCostStore } from '../../../stores/cost';
import { formatUsd, colorBand } from '../../../lib/money';

// The summary of everything that spends: chat, the observer, delegated
// agents, subagents and the autonomy kernel. The ledger has always rolled
// this at local midnight and broadcast it; no surface showed it.
//
// Grouped rather than listed per role — fifteen rows is a ledger, not a
// summary. Anything unmapped falls into `other` rather than vanishing, so a
// new billable role can never go missing from the total's breakdown.
const GROUPS: { key: string; label: string; roles?: string[]; prefix?: string }[] = [
  { key: 'chat', label: 'chat', roles: ['chat_brain'] },
  { key: 'observer', label: 'observer', roles: ['observer_agent'] },
  {
    key: 'agents',
    label: 'agents',
    roles: ['agents_default', 'claude_cli', 'codex_cli', 'coder', 'auditor'],
  },
  { key: 'subagents', label: 'subagents', roles: ['subagents_default'] },
  {
    key: 'autonomy',
    label: 'autonomy',
    prefix: 'autonomy_',
    roles: ['feedback_consolidator'],
  },
  { key: 'speech', label: 'speech', roles: ['voice_tts'] },
  { key: 'listening', label: 'listening', roles: ['voice_stt', 'audio_transcribe'] },
  { key: 'images', label: 'images', roles: ['image_generator'] },
];

function groupFor(role: string): string {
  for (const g of GROUPS) {
    if (g.roles?.includes(role)) return g.key;
    if (g.prefix && role.startsWith(g.prefix)) return g.key;
  }
  return 'other';
}

export function SpendSection() {
  const globalState = useCostStore((s) => s.globalState);
  const perRole = useCostStore((s) => s.perRole);
  const overageUnlocked = useCostStore((s) => s.overageUnlocked);

  const totals = new Map<string, number>();
  for (const [role, entry] of Object.entries(perRole)) {
    const key = groupFor(role);
    totals.set(key, (totals.get(key) ?? 0) + (entry?.role_total_usd ?? 0));
  }
  const rows = [
    ...GROUPS.map((g) => ({ label: g.label, spent: totals.get(g.key) ?? 0 })),
    { label: 'other', spent: totals.get('other') ?? 0 },
  ].filter((r) => r.spent > 0);

  const ratio =
    globalState && globalState.cap_usd > 0
      ? Math.min(globalState.spent_usd / globalState.cap_usd, 1)
      : 0;
  const isOverage =
    globalState !== null &&
    globalState.spent_usd > globalState.cap_usd &&
    overageUnlocked.includes('global');
  const band = globalState?.blocked ? 'bad' : colorBand(ratio);

  return (
    <section className="right-section">
      {!globalState ? (
          <div className="t-caption right-section-empty">nothing billed yet</div>
        ) : (
          <>
            <div
              className={`spend-total spend-total--${band}${isOverage ? ' is-overage' : ''}`}
            >
              <span className="spend-total-figure">
                {formatUsd(globalState.spent_usd)}
              </span>
              <span className="t-meta spend-total-cap">
                of {formatUsd(globalState.cap_usd)}
              </span>
            </div>
            <div className="spend-bar" aria-hidden="true">
              <span
                className="spend-bar-fill"
                style={{ width: `${Math.round(ratio * 100)}%` }}
              />
            </div>
            {rows.length > 0 && (
              <ul className="right-section-list spend-breakdown">
                {rows.map((r) => (
                  <li key={r.label}>
                    <span className="t-meta">{r.label}</span>
                    <span className="t-caption spend-row-figure">
                      {formatUsd(r.spent)}
                    </span>
                  </li>
                ))}
              </ul>
            )}
            {/* Overage outranks blocked, as it does on the HUD chip: the
                backend sets `blocked` from spent >= cap whether or not the
                operator approved continuing, so checking it first would
                report an approved overage as a refusal. */}
            <div className="t-meta spend-date">
              since midnight
              {isOverage
                ? ' · over cap (you approved)'
                : globalState.blocked
                  ? ' · blocked'
                  : globalState.warning
                    ? ' · warning'
                    : ''}
            </div>
        </>
      )}
    </section>
  );
}

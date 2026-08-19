import { useSessionStore } from "../../../stores/session";
import { colorBand } from "../../../lib/money";
import { sendCommand } from "../../../lib/commands";
import { Hint } from "../../ui/Hint";
import { Chip } from "../../common/Chip";

function formatTokens(n: number): string {
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return `${n}`;
}

interface StatsChipProps {
  /** Hint direction — 'right' when rendered inside a folded HUD section
   *  stack (2026-07-31 review finding: a top hint covers the stack item
   *  above in the tight vertical layout). */
  hintPosition?: "top" | "right";
}

export function StatsChip({ hintPosition = "top" }: StatsChipProps) {
  const stats = useSessionStore((s) => s.latestStats);

  if (!stats) {
    return (
      <Hint
        label="No stats yet — click to request"
        position={hintPosition}
        maxWidth={240}
      >
        <Chip
          className="hud-stats"
          onClick={() => sendCommand("/stats")}
          ariaLabel="No stats yet"
        >
          <span className="hud-stats-text">Turns 0 · —</span>
          <span
            className="hud-stats-bar hud-stats-bar--empty"
            aria-hidden="true"
          />
        </Chip>
      </Hint>
    );
  }

  const threshold = stats.compact_threshold_tokens;
  const totalRatio = threshold > 0 ? Math.min(stats.tokens / threshold, 1) : 0;
  const systemRatio =
    threshold > 0 ? Math.min(stats.system_tokens / threshold, 1) : 0;
  const conversationRatio = Math.max(totalRatio - systemRatio, 0);
  const band = colorBand(totalRatio);
  const label =
    `Turns ${stats.turns} · ${formatTokens(stats.tokens)} of ${formatTokens(threshold)} ` +
    `(${formatTokens(stats.system_tokens)} manifest · ${formatTokens(stats.tokens - stats.system_tokens)} chat)`;

  return (
    <Hint label={label} position={hintPosition} maxWidth={320}>
      <Chip
        className={`hud-stats hud-stats--${band}`}
        tone={band === "ok" ? "default" : band}
        onClick={() => sendCommand("/stats")}
        ariaLabel={label}
      >
        <span className="hud-stats-text">
          Turns {stats.turns} · {formatTokens(stats.tokens)}
        </span>
        <span className="hud-stats-bar" aria-hidden="true">
          <span
            className="hud-stats-fill hud-stats-fill--system"
            style={{ width: `${Math.round(systemRatio * 100)}%` }}
          />
          <span
            className="hud-stats-fill hud-stats-fill--chat"
            style={{ width: `${Math.round(conversationRatio * 100)}%` }}
          />
        </span>
      </Chip>
    </Hint>
  );
}

import { useConversationStore } from "../../../stores/conversation";
import { useSessionStore } from "../../../stores/session";
import { foldCeiling, foldableTokens } from "../../../lib/types";
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
  const latest = useSessionStore((s) => s.latestStats);
  const latestChatId = useSessionStore((s) => s.latestStatsChatId);
  const activeChatId = useConversationStore((s) => s.activeChatId);
  // Stats are per conversation. Every focus change asks the backend for the
  // new chat's numbers, but until they land these belong to the chat that was
  // left, and drawing them here labelled them as this one's.
  const stats = latestChatId === activeChatId ? latest : null;

  if (!stats) {
    return (
      <Hint
        label="No stats yet. Click to request them."
        position={hintPosition}
        maxWidth={240}
      >
        <Chip
          className="hud-stats"
          onClick={() => sendCommand("/stats")}
          ariaLabel="No stats yet"
        >
          <span className="hud-stats-text">Turns 0 · {'—'}</span>
          <span
            className="hud-stats-bar hud-stats-bar--empty"
            aria-hidden="true"
          />
        </Chip>
      </Hint>
    );
  }

  const threshold = foldCeiling(stats);
  // The slice the ceiling governs, not the whole payload. Dividing `tokens` by
  // it drew the bar over-full by the size of the system prompt and the turn's
  // late half, and disagreed with what `context_read` reports for the same
  // conversation.
  const measured = foldableTokens(stats);
  const totalRatio = threshold > 0 ? Math.min(measured / threshold, 1) : 0;
  const band = colorBand(totalRatio);
  const label =
    `Turns ${stats.turns} · ${formatTokens(measured)} of ${formatTokens(threshold)} ` +
    `(${formatTokens(stats.system_tokens)} manifest, which the ceiling already excludes)`;

  return (
    <Hint label={label} position={hintPosition} maxWidth={320}>
      <Chip
        className={`hud-stats hud-stats--${band}`}
        tone={band === "ok" ? "default" : band}
        onClick={() => sendCommand("/stats")}
        ariaLabel={label}
      >
        <span className="hud-stats-text">
          Turns {stats.turns} · {formatTokens(measured)}
        </span>
        {/* One fill. The bar used to draw the manifest as a share of this
            ceiling and the conversation as the rest, but the ceiling already
            has the manifest taken out of it (`fold_trigger_tokens` is the
            ratio's share of the window MINUS the system prompt), so the two
            segments were fractions of different things. The manifest's size
            stays in the hint, where `context_report.render` also keeps it. */}
        <span className="hud-stats-bar" aria-hidden="true">
          <span
            className="hud-stats-fill hud-stats-fill--chat"
            style={{ width: `${Math.round(totalRatio * 100)}%` }}
          />
        </span>
      </Chip>
    </Hint>
  );
}

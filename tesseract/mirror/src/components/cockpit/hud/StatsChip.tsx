import { useConversationStore } from "../../../stores/conversation";
import { useSessionStore } from "../../../stores/session";
import { boundaryCeiling, conversationTokens, type SessionStatsData } from "../../../lib/types";
import { colorBand } from "../../../lib/money";
import { sendCommand } from "../../../lib/commands";
import { Hint } from "../../ui/Hint";
import { Chip } from "../../common/Chip";
import { DataTable } from "../../common/DataTable";

function formatTokens(n: number): string {
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return `${n}`;
}

//: The app's no-value mark, for a figure an older backend has not sent yet.
//  Never a zero: a zero here would read as "the tool list costs nothing"
//  rather than "this was not measured".
const NOT_MEASURED = "—";

function formatMeasured(n: number | undefined): string {
  return n === undefined || n === null ? NOT_MEASURED : formatTokens(n);
}

/** The two figures every other reading here is built from: the true
 * whole-request total, and the late-turn block hiding inside `rest_tokens`.
 *
 * `rest_tokens` (`context_report.py`'s "everything else") is the late block
 * PLUS the conversation, because `_conversation_split` excludes the late
 * block from `conversation_tokens` but the assembled prompt this measures
 * still carries it: the clock, the memory capsule, this turn's directives.
 * `payload_tokens + conversation_tokens` therefore under counts the real
 * request by exactly that block's size, which every request pays. The true
 * total is `payload_tokens + rest_tokens`, and the late block on its own is
 * what is left of `rest_tokens` once the narrow conversation figure is taken
 * out.
 *
 * `undefined` for either figure means an older backend has not sent the
 * field yet, not a real zero, so both stay unknown rather than guessing.
 */
function payloadParts(stats: SessionStatsData) {
  const total =
    stats.payload_tokens === undefined || stats.rest_tokens === undefined
      ? undefined
      : stats.payload_tokens + stats.rest_tokens;
  const lateBlock =
    stats.rest_tokens === undefined || stats.conversation_tokens === undefined
      ? undefined
      : Math.max(0, stats.rest_tokens - stats.conversation_tokens);
  return { total, lateBlock };
}

/** The two rows built from what the provider actually reported, each falling
 * back to the structural estimate (`total` above) until its own call has
 * happened. Kept apart from `payloadHint`'s breakdown rows on purpose: those
 * are one moment's estimate of the NEXT request and always sum to `total`,
 * while these come from actual past requests and can disagree with that
 * estimate and with each other, so folding them into the same sum would
 * claim an addition that is not true.
 *
 * `coldStart` is this conversation's first call since it began, or since a
 * reset or a consolidation boundary last cleared it, and stays fixed once it
 * lands. `latest` is whichever call is newest and moves every turn. Neither
 * is a real row until SOME number exists to show, which is only when the
 * fallback estimate itself is known.
 */
function measuredRequestRows(stats: SessionStatsData, total: number | undefined) {
  const coldStart = stats.first_request_tokens;
  const latest = stats.last_request_tokens;
  return [
    {
      key: "cold_start",
      cells: [
        `payload at the start of this conversation, ${coldStart !== undefined ? "measured" : "estimate"}`,
        formatMeasured(coldStart ?? total),
      ],
    },
    {
      key: "latest_request",
      cells: [
        latest !== undefined ? "the last request, measured" : "the next request, estimate",
        formatMeasured(latest ?? total),
      ],
    },
  ];
}

/** The parts of what a request sends, as one sentence and a row per part.
 *
 * `payload_tokens` (head plus the tool list) is the same word and the same
 * reading the Conscience room's Payload section already shows, so the two
 * cannot drift the way "manifest" and "Payload" once did. `undefined` fields
 * are an older backend that has not picked up the reading yet, not a real
 * zero, so they show the no-value mark rather than pretending to know. The
 * late-block row only appears when it is a real, distinct part of the
 * request: rows then add up top to bottom, payload plus conversation plus
 * the late block equals the total.
 */
function payloadHint(stats: SessionStatsData, conversation: number) {
  const { total, lateBlock } = payloadParts(stats);
  const rows = [
    {
      key: "head",
      cells: ["instructions and memory", formatMeasured(stats.system_tokens)],
    },
    { key: "tools", cells: ["tool list", formatMeasured(stats.tools_tokens)] },
    { key: "payload", cells: ["payload", formatMeasured(stats.payload_tokens)] },
    { key: "conversation", cells: ["conversation", formatTokens(conversation)] },
    ...(lateBlock !== undefined && lateBlock > 0
      ? [
          {
            key: "late",
            cells: [
              "clock, memory summary and directives this turn",
              formatTokens(lateBlock),
            ],
          },
        ]
      : []),
    { key: "total", cells: ["next request in total", formatMeasured(total)] },
    ...measuredRequestRows(stats, total),
  ];
  return (
    <>
      <p className="hud-stats-hint-lead">
        These are the parts of what every request sends: payload is the
        instructions, memory and tool list that go out before the
        conversation, which comes on top of it. The last two rows come from
        what the provider actually reported for real requests, not this
        estimate, so they do not always match it or each other.
      </p>
      <DataTable
        label="What this request sends, in parts"
        columns={[
          { label: "part", width: "minmax(0, 1fr)" },
          { label: "tokens", width: "auto" },
        ]}
        rows={rows}
      />
    </>
  );
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

  const threshold = boundaryCeiling(stats);
  // The slice the ceiling governs, not the whole payload. Dividing `tokens` by
  // it drew the bar over-full by the size of the system prompt and the turn's
  // late half, and disagreed with what `context_read` reports for the same
  // conversation.
  const measured = conversationTokens(stats);
  const totalRatio = threshold > 0 ? Math.min(measured / threshold, 1) : 0;
  const band = colorBand(totalRatio);
  // The full breakdown a sighted reader gets on hover: one sentence plus a
  // row per part, in `payloadHint` below. `ariaLabel` carries the same
  // numbers as plain text, because the chip's `aria-label` is what assistive
  // tech reads instead of its contents, and a rich node in the hint is not
  // guaranteed to read cleanly there on its own.
  const { total, lateBlock } = payloadParts(stats);
  const lateBlockPart =
    lateBlock !== undefined && lateBlock > 0
      ? `, plus a clock, memory summary and directives block of ${formatTokens(lateBlock)} this turn`
      : "";
  const measuredRows = measuredRequestRows(stats, total);
  const measuredSummary = measuredRows.length
    ? " " + measuredRows.map((row) => `${row.cells[0]} ${row.cells[1]}`).join(", ") + "."
    : "";
  const ariaSummary =
    `Turns ${stats.turns} · ${formatTokens(measured)} of ${formatTokens(threshold)}. ` +
    `Payload is instructions, memory and the tool list sent before the ` +
    `conversation, which comes on top of it: instructions and memory ` +
    `${formatMeasured(stats.system_tokens)}, tool list ${formatMeasured(stats.tools_tokens)}, ` +
    `payload ${formatMeasured(stats.payload_tokens)}, conversation ${formatTokens(measured)}` +
    `${lateBlockPart}, next request in total ${formatMeasured(total)}.` +
    measuredSummary;

  return (
    <Hint label={payloadHint(stats, measured)} position={hintPosition} maxWidth={320}>
      <Chip
        className={`hud-stats hud-stats--${band}`}
        tone={band === "ok" ? "default" : band}
        onClick={() => sendCommand("/stats")}
        ariaLabel={ariaSummary}
      >
        <span className="hud-stats-text">
          Turns {stats.turns} · {formatTokens(measured)}
        </span>
        {/* One fill. The bar used to draw the head as a share of this
            ceiling and the conversation as the rest, but the ceiling already
            has the head taken out of it (`boundary_trigger_tokens` is the
            ratio's share of the window MINUS the head), so the two
            segments were fractions of different things. The head's size
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

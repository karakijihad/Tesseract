/** The arithmetic behind the boundary bar, kept out of the component.
 *
 * Every number here starts from something the runtime measured and sent, not
 * from a rule restated on this side. A control that works out its own floor
 * draws a different one from the runtime's the first turn that runs heavy, and
 * then it is offering a threshold the runtime will refuse.
 */

/** What the API sends, narrowed to what the bar reads. */
export interface CompactionFacts {
  window: number;
  /** Where a fresh install sits. Read from the sealed factory copy, not from
   *  the operator's, which the pane overwrites on every save. */
  defaultRatio: number | null;
  /** The system prompt as the boundary decision measured it. Sent on every
   *  turn and nothing can shrink it, so it belongs in the reserved part
   *  rather than in the part the operator is choosing. Absent until a session
   *  has run a turn. */
  systemTokens: number | null;
  /** What the route will accept, so this file does not keep its own copy. */
  ratioFloor: number | null;
  ratioCeiling: number | null;
  /** What the tool list costs, priced the same structural way the head is
   *  (`request_size.measure_tools`). Sent on every turn beside the head, so
   *  it belongs in the reserved part too. Optional and defaults to zero: a
   *  caller with nothing measured yet (Settings' draft preview, an older
   *  backend) reserves nothing extra rather than guessing. */
  toolsTokens?: number | null;
  /** How big the conversation is right now. `null` for Settings' draft
   *  preview, which has no open conversation to measure; a real figure draws
   *  where the conversation currently sits on the bar. */
  conversationTokens?: number | null;
  /** The runtime's own boundary figure for an open conversation
   *  (`boundary_trigger_tokens`, falling back to `compact_threshold_tokens`),
   *  already priced the way `chat.py::_boundary_trigger_tokens` prices it.
   *  When given, the bar draws THIS instead of re-deriving the ratio's share
   *  of the window a second time: a live reading already has the runtime's
   *  own number. Settings' draft preview has none, because it is asking what
   *  a ratio nothing has committed yet would mean. */
  measuredCeilingTokens?: number | null;
}

export type VerdictTone = "ok" | "warn" | "bad";

export interface CompactionBar {
  /** False before any turn has run, when there is nothing measured to draw. */
  measured: boolean;
  /** The reserved share: the head plus the tool list, neither of which any
   *  boundary can shrink. */
  systemRatio: number;
  /** The conversation's share, as the RUNTIME grants it. Not `ratio` less the
   *  reserved share: below twice the reserved share the runtime holds the
   *  boundary at its floor, and a band drawn off the raw setting would put
   *  the edge left of where the conversation is actually wrapped up. */
  yoursRatio: number;
  /** Where the conversation sits right now, as a share of the window. `null`
   *  when nothing live was given (Settings' draft preview). */
  usedRatio: number | null;
  /** Where the handle stops. */
  minRatio: number;
  maxRatio: number;
  defaultRatio: number | null;
  tokens: number;
  /** What is left for conversation before the boundary falls.
   *
   *  Mirrors `chat.py::_boundary_trigger_tokens` rather than restating the
   *  subtraction: the runtime FLOORS the trigger at the instructions' own
   *  size, so between one and two instructions' worth of setting the bar was
   *  reporting less room than the runtime grants, and the warn line below was
   *  telling the operator to raise a setting the runtime had already floored. */
  roomTokens: number;
  verdict: { tone: VerdictTone; text: string };
}

/** Used only when the API sends no bounds of its own. */
export const RATIO_FLOOR = 0.1;
export const RATIO_CEILING = 0.95;

const clamp = (n: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, n));

const fmt = (n: number) => Math.round(n).toLocaleString("en-US");

const pct = (n: number) => `${Math.round(n * 100)}%`;

/**
 * Work out the bar for a draft threshold.
 *
 * There used to be three reserved bands here: a head anchor, a verbatim tail,
 * and breathing room on top of them, all sized so a summarising fold could
 * land clear of the line it had just crossed. There is no fold. A boundary
 * clears the conversation, so the only thing it cannot shrink is the
 * instructions, and everything above them is the operator's to set.
 */
export function measureBar(
  facts: CompactionFacts,
  ratio: number,
): CompactionBar {
  const window = Math.max(0, facts.window);
  const floor = facts.ratioFloor ?? RATIO_FLOOR;
  const ceilingRatio = facts.ratioCeiling ?? RATIO_CEILING;
  const tokens = Math.round(ratio * window);
  const systemTokens = facts.systemTokens ?? 0;
  // The tool list rides beside the head on every turn, so it is reserved the
  // same way. Zero by default: a caller with nothing measured (Settings'
  // draft preview, an older backend) reserves nothing extra rather than
  // guessing.
  const reserved = systemTokens + (facts.toolsTokens ?? 0);
  const systemRatio = reserved && window ? reserved / window : 0;

  // The room the RUNTIME grants, not from the raw setting. Below twice the
  // reserved share the runtime holds the boundary at its floor, so a bar
  // drawn off `ratio` alone puts the edge left of where the conversation is
  // actually wrapped up and disagrees with the sentence printed under it.
  //
  // A live reading's `measuredCeilingTokens` IS the room already, not
  // reserved-plus-room: `chat.py::_boundary_trigger_tokens` has the reserved
  // share taken out of it before it is ever sent (`should_compact` compares
  // it directly against `conversation_tokens`, which itself excludes the
  // reserved share, and `context_report.render` divides the conversation by
  // it the same way), so subtracting `reserved` from it here a second time
  // shrank the room by that much twice. A draft preview has no such figure
  // and derives the room the same way the runtime does, because nothing has
  // been committed for the runtime to report yet.
  const room =
    facts.measuredCeilingTokens != null
      ? Math.max(0, facts.measuredCeilingTokens)
      : roomFor(tokens, reserved, window);
  const yoursRatio = window ? room / window : Math.max(0, ratio - systemRatio);
  const usedRatio =
    facts.conversationTokens != null && window
      ? clamp(facts.conversationTokens / window, 0, 1)
      : null;

  return {
    measured: Boolean(window && facts.systemTokens != null),
    systemRatio,
    yoursRatio,
    usedRatio,
    minRatio: clamp(systemRatio, floor, ceilingRatio),
    maxRatio: ceilingRatio,
    defaultRatio: facts.defaultRatio,
    tokens,
    roomTokens: room,
    // The SAME room just computed, not a third figure re-derived from
    // `ratio * window`: the segment width, the legend value and the verdict
    // text all have to agree on what "room" means for the same facts.
    verdict: verdictFor(tokens, reserved, systemRatio, room),
  };
}

/** The runtime's own trigger, in one place on this side too.
 *
 *  `chat.py::_boundary_trigger_tokens` is
 *  `max(window*ratio - system, min(system, window - system))`.
 *
 *  Both halves of that floor are derived. Below the payload's fixed cost,
 *  clearing frees less than half of what is sent, so it is not worth a model
 *  turn, and without a floor the subtraction goes negative under a manifest
 *  larger than the setting and consolidates an empty conversation every turn.
 *  And the floor may not exceed what can physically be in the conversation,
 *  which is the window less the prompt: past half the window a floor at the
 *  prompt's own size is a line the conversation can never reach. */
function roomFor(
  tokens: number,
  reserved: number,
  window: number,
): number {
  return Math.max(
    tokens - reserved,
    Math.min(reserved, Math.max(0, window - reserved)),
  );
}

function verdictFor(
  tokens: number,
  reserved: number,
  systemRatio: number,
  room: number,
): { tone: VerdictTone; text: string } {
  // Below this the setting decides nothing: the runtime holds the boundary at
  // the floor instead, so moving the handle inside this range changes no
  // behaviour and saying otherwise would be a control that lies.
  if (tokens < 2 * reserved) {
    return {
      tone: "bad",
      text:
        `The reserved part fills ${pct(systemRatio)} of the window, so this ` +
        `setting is doing nothing: the runtime waits until the conversation ` +
        `is as big as that before it wraps up, at about ` +
        `${fmt(room)} tokens. Raise it past ${pct(2 * systemRatio)} to have ` +
        `it decide.`,
    };
  }
  return {
    tone: "ok",
    text:
      `About ${fmt(room)} tokens of conversation before this one is wrapped ` +
      `up and a fresh one carries the work on.`,
  };
}

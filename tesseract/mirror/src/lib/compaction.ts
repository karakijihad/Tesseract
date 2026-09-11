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
}

export type VerdictTone = "ok" | "warn" | "bad";

export interface CompactionBar {
  /** False before any turn has run, when there is nothing measured to draw. */
  measured: boolean;
  /** The system prompt's share, which no boundary can shrink. */
  systemRatio: number;
  /** The conversation's share, as the RUNTIME grants it. Not `ratio` less the
   *  instructions: below twice the instructions the runtime holds the
   *  boundary at its floor, and a band drawn off the raw setting would put
   *  the edge left of where the conversation is actually wrapped up. */
  yoursRatio: number;
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
  const systemRatio = systemTokens && window ? systemTokens / window : 0;

  // The bands are drawn from the room the RUNTIME grants, not from the raw
  // setting. Below twice the instructions the runtime holds the boundary at
  // its floor, so a bar drawn off `ratio` alone puts the edge left of where
  // the conversation is actually wrapped up and disagrees with the sentence
  // printed under it.
  const room = roomFor(tokens, systemTokens, window);
  const yoursRatio = window ? room / window : Math.max(0, ratio - systemRatio);

  return {
    measured: Boolean(window && facts.systemTokens != null),
    systemRatio,
    yoursRatio,
    minRatio: clamp(systemRatio, floor, ceilingRatio),
    maxRatio: ceilingRatio,
    defaultRatio: facts.defaultRatio,
    tokens,
    roomTokens: room,
    verdict: verdictFor(tokens, systemTokens, systemRatio, window),
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
  systemTokens: number,
  window: number,
): number {
  return Math.max(
    tokens - systemTokens,
    Math.min(systemTokens, Math.max(0, window - systemTokens)),
  );
}

function verdictFor(
  tokens: number,
  systemTokens: number,
  systemRatio: number,
  window: number,
): { tone: VerdictTone; text: string } {
  // The handle cannot sit below what is already spoken for, so it stops at the
  // instructions and the setting behind it is the one the runtime raises.
  // Saying nothing here would leave a handle that visibly disagrees with the
  // percentage beside it and no reason given.
  const room = roomFor(tokens, systemTokens, window);
  // Below this the setting decides nothing: the runtime holds the boundary at
  // the floor instead, so moving the handle inside this range changes no
  // behaviour and saying otherwise would be a control that lies.
  if (tokens < 2 * systemTokens) {
    return {
      tone: "bad",
      text:
        `The instructions fill ${pct(systemRatio)} of the window, so this ` +
        `setting is doing nothing: the runtime waits until the conversation ` +
        `is as big as the instructions and wraps up then, at about ` +
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

/** The arithmetic behind the compaction bar, kept out of the component.
 *
 * Every number here starts from something the runtime measured and sent, not
 * from a rule restated on this side. A control that works out its own floor
 * draws a different one from the runtime's the first turn that runs heavy, and
 * then it is offering a threshold the runtime will refuse.
 */

/** What the API sends, narrowed to what the bar reads. */
export interface CompactionFacts {
  window: number;
  headroom: number | null;
  comfortable: number | null;
  /** Where a fresh install sits. Read from the sealed factory copy, not from
   *  the operator's, which the pane overwrites on every save. */
  defaultRatio: number | null;
  /** Absent until a session has run a turn. */
  anchorTokens: number | null;
  tailTokens: number | null;
  tailTurns: number | null;
  /** The system prompt as the fold decision measured it. Unfoldable like the
   *  anchor and the tail, so it belongs in the reserved part rather than in
   *  the part the operator is choosing. */
  systemTokens: number | null;
  /** What the route will accept, so this file does not keep its own copy. */
  ratioFloor: number | null;
  ratioCeiling: number | null;
}

export type VerdictTone = "ok" | "warn" | "bad";

export interface CompactionBar {
  /** False before any turn has run, when there is no floor to draw. */
  measured: boolean;
  /** The system prompt's share, which no fold can shrink. */
  systemRatio: number;
  /** What a fold always leaves behind: the head anchor and the kept turns. */
  keptRatio: number;
  keptTokens: number;
  /** The margin on top of it, so a fold lands clear of the line rather than
   *  exactly on it. Drawn separately because it is the one reserved part the
   *  operator can shrink without losing any conversation. */
  marginRatio: number;
  /** kept plus margin, which is where the handle stops. */
  lockedTokens: number;
  lockedRatio: number;
  /** Everything above the reserved parts, up to where the handle sits. */
  yoursRatio: number;
  /** Where the handle stops. */
  minRatio: number;
  maxRatio: number;
  recommendedRatio: number | null;
  defaultRatio: number | null;
  tokens: number;
  /** What is left for conversation once the reserved parts are taken out.
   *  Clamped here rather than at each caller: the bar has a state where the
   *  reserved parts exceed the setting, and it has a verdict for it, so a
   *  legend row reading a negative number is the same sum done twice. */
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
 * Work out the bar for a draft threshold and a draft number of turns.
 *
 * The turns half is an estimate, and deliberately so: the tail's real size is
 * known only after a fold runs, so the recommendation would sit still while
 * the operator moved the control that changes it. The estimate is the measured
 * average turn multiplied out, clamped by the same ceiling the runtime clamps
 * with, so it can promise nothing the runtime would not do.
 */
export function measureBar(
  facts: CompactionFacts,
  ratio: number,
  turns: number,
): CompactionBar {
  const window = Math.max(0, facts.window);
  const headroom = facts.headroom ?? 1.2;
  const floor = facts.ratioFloor ?? RATIO_FLOOR;
  const ceilingRatio = facts.ratioCeiling ?? RATIO_CEILING;
  const tokens = Math.round(ratio * window);
  const systemRatio =
    facts.systemTokens && window ? facts.systemTokens / window : 0;

  if (!window || facts.anchorTokens == null || facts.tailTokens == null) {
    return {
      measured: false,
      systemRatio,
      keptRatio: 0,
      keptTokens: 0,
      marginRatio: 0,
      lockedTokens: 0,
      lockedRatio: 0,
      yoursRatio: Math.max(0, ratio - systemRatio),
      minRatio: floor,
      maxRatio: ceilingRatio,
      recommendedRatio: null,
      defaultRatio: facts.defaultRatio,
      tokens,
      roomTokens: Math.max(0, tokens - (facts.systemTokens ?? 0)),
      verdict: verdictFor(
        null, ratio, tokens, 0, facts.systemTokens ?? 0, turns, systemRatio,
      ),
    };
  }

  const avgTurn =
    facts.tailTurns && facts.tailTurns > 0
      ? facts.tailTokens / facts.tailTurns
      : facts.tailTokens;
  // The runtime's own ceiling on the tail, so a draft turn count cannot
  // reserve more than a fold would actually be allowed to keep. The system
  // prompt is deliberately not subtracted here, exactly as it is not in
  // `chat.py::_tail_ceiling_tokens` — taking it off both this and the trigger
  // collapses the tail to nothing whenever the prompt is large.
  const ceiling = Math.max(0, (window * ratio) / headroom - facts.anchorTokens);
  const tail = Math.min(avgTurn * Math.max(1, turns), ceiling);
  const keptTokens = facts.anchorTokens + tail;
  const lockedTokens = keptTokens * headroom;
  const lockedRatio = window ? lockedTokens / window : 0;
  const keptRatio = window ? keptTokens / window : 0;
  // The system prompt is in here for the same reason it is in `minRatio`:
  // the runtime's configured arm is `window * ratio - system_tokens`, so a
  // recommendation that leaves it out advises a threshold with less room than
  // it claims.
  const recommended = facts.comfortable
    ? ((facts.systemTokens ?? 0) + facts.anchorTokens + tail) *
      facts.comfortable /
      window
    : null;

  return {
    measured: true,
    systemRatio,
    keptRatio,
    keptTokens,
    marginRatio: Math.max(0, lockedRatio - keptRatio),
    lockedTokens,
    lockedRatio,
    yoursRatio: Math.max(0, ratio - systemRatio - lockedRatio),
    minRatio: clamp(systemRatio + lockedRatio, floor, ceilingRatio),
    maxRatio: ceilingRatio,
    recommendedRatio: recommended,
    defaultRatio: facts.defaultRatio,
    tokens,
    roomTokens: Math.max(
      0, tokens - lockedTokens - (facts.systemTokens ?? 0),
    ),
    verdict: verdictFor(
      recommended,
      ratio,
      tokens,
      lockedTokens,
      facts.systemTokens ?? 0,
      turns,
      systemRatio + lockedRatio,
    ),
  };
}

function verdictFor(
  recommended: number | null,
  ratio: number,
  tokens: number,
  lockedTokens: number,
  systemTokens: number,
  turns: number,
  reservedRatio = 0,
): { tone: VerdictTone; text: string } {
  // The handle cannot sit below what is already spoken for, so it stops at the
  // reserved edge and the setting behind it is the one the runtime raises.
  // Saying nothing here would leave a handle that visibly disagrees with the
  // percentage beside it and no reason given.
  if (reservedRatio > ratio) {
    return {
      tone: "bad",
      text:
        `The instructions and the turns a fold keeps already fill ` +
        `${pct(reservedRatio)} of the window, which is more than this setting ` +
        `allows. The runtime folds at ${pct(reservedRatio)} instead. Raise it ` +
        `past that, or keep fewer turns.`,
    };
  }
  if (recommended != null && ratio < recommended) {
    return {
      tone: "warn",
      text:
        `There is little room above what a fold has to keep, so folds come ` +
        `often and most turns carry a summary rather than the conversation. ` +
        `${pct(recommended)} leaves twice the room.`,
    };
  }
  const room = Math.max(0, tokens - lockedTokens - systemTokens);
  return {
    tone: "ok",
    text:
      `About ${fmt(room)} tokens of conversation before a fold, and ` +
      `${turns} ${turns === 1 ? "turn" : "turns"} kept word for word after it.`,
  };
}

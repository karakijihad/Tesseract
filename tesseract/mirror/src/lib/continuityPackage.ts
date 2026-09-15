// The continuity package `brain/continuity.py::render` writes, read back by
// its own field labels rather than by which runtime mark carried it.
//
// A live boundary note arrives as a `session_note` stamped `mark: "boundary"`.
// The same text, once a turn has run against it or once the conversation was
// left in front of it, comes back out of history stamped `_runtime: "boundary"`
// (the reset-and-cut-off notices), `"continuity"` (a reset that carried the
// package with nothing to answer it yet) or `"carry_on"` (a continue's own
// first message). Three marks for one piece of content is exactly the kind of
// difference the operator should never see, so this reads the TEXT and not the
// mark: whatever carried it, the same headings parse into the same card.
//
// The headings are `continuity.py::_FIELDS`, in the order a person would ask
// them. A block this does not recognise is not an error — the same function
// also renders the plain sentences (`RESET_NOTICE`, `CUT_OFF_NOTICE`, the
// handoff ask) through this exact channel, and those have no headings at all.
// `parseContinuityPackage` answers `null` for those, and the caller falls back
// to plain prose.

export interface ContinuityPackage {
  objective: string;
  phase: string;
  completed: string[];
  remaining: string[];
  nextAction: string;
  openQuestions: string[];
  blockedBy: string;
  workRoot: string;
  artifacts: string[];
}

type ListField = "completed" | "remaining" | "openQuestions" | "artifacts";
type StringField = "objective" | "phase" | "nextAction" | "blockedBy" | "workRoot";

const LIST_HEADINGS: Record<string, ListField> = {
  Done: "completed",
  Remaining: "remaining",
  "Open questions": "openQuestions",
  Artifacts: "artifacts",
};

const STRING_HEADINGS: Record<string, StringField> = {
  Objective: "objective",
  Phase: "phase",
  Next: "nextAction",
  "Blocked by": "blockedBy",
  "Working from": "workRoot",
};

function emptyPackage(): ContinuityPackage {
  return {
    objective: "",
    phase: "",
    completed: [],
    remaining: [],
    nextAction: "",
    openQuestions: [],
    blockedBy: "",
    workRoot: "",
    artifacts: [],
  };
}

/**
 * Parse a continuity package out of plain text, or `null` when the text
 * carries none of the headings `continuity.py` emits.
 *
 * Blocks are separated the way `render()` joins them: a blank line. A list
 * field's block opens with a bare `Heading:` line and every line after it is
 * a `- item` bullet; a string field's block is one line, `Heading: value`.
 * Anything else (the lead sentence, a plain notice with no headings at all)
 * is not a parse failure, it is simply not a heading, and is skipped.
 */
export function parseContinuityPackage(text: string): ContinuityPackage | null {
  const blocks = text
    .split(/\n{2,}/)
    .map(block => block.trim())
    .filter(Boolean);
  const out = emptyPackage();
  let found = false;

  for (const block of blocks) {
    const lines = block.split("\n");
    const headingLine = lines[0];
    const colon = headingLine.indexOf(":");
    if (colon === -1) continue;
    const heading = headingLine.slice(0, colon).trim();
    const inline = headingLine.slice(colon + 1).trim();

    const listField = LIST_HEADINGS[heading];
    if (listField) {
      found = true;
      out[listField] = lines
        .slice(1)
        .map(line => line.trim())
        .filter(line => line.startsWith("- "))
        .map(line => line.slice(2).trim())
        .filter(Boolean);
      continue;
    }

    const stringField = STRING_HEADINGS[heading];
    if (stringField) {
      found = true;
      out[stringField] = inline;
    }
  }

  return found ? out : null;
}

/** Whether a parsed package actually says anything. Mirrors
 *  `checkpoints.Checkpoint.is_empty` (inverted): a package with every field
 *  blank is not one worth drawing as a card. */
export function hasContinuityContent(pkg: ContinuityPackage): boolean {
  return Boolean(
    pkg.objective ||
      pkg.phase ||
      pkg.completed.length ||
      pkg.remaining.length ||
      pkg.nextAction ||
      pkg.openQuestions.length ||
      pkg.blockedBy ||
      pkg.workRoot ||
      pkg.artifacts.length,
  );
}

/** The one line a collapsed card shows: what this was for, or the next
 *  clearest thing the boundary said. */
export function continuitySummary(pkg: ContinuityPackage): string {
  return pkg.objective || pkg.phase || pkg.nextAction || "See what carried on";
}

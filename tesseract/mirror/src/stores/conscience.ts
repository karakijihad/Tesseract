import { create } from 'zustand';
import { BACKEND_BASE } from '../lib/endpoints';

export type SignalStatus = 'ok' | 'warn' | 'bad';

export interface SignalResult {
  name: string;
  value: number;
  status: SignalStatus;
  warn: number;
  bad: number;
  detail: string;
}

export interface DriftReport {
  timestamp: string;
  window_hours: number;
  signals: SignalResult[];
  /** How many signals sit in each band. Named `summary` on reports written
   *  before the runtime gave every record one shape, where `summary` is the
   *  sentence instead. `driftCounts` reads either. */
  counts?: { ok: number; warn: number; bad: number };
  summary?: { ok: number; warn: number; bad: number } | string;
}

const NO_COUNTS = { ok: 0, warn: 0, bad: 0 };

/** The band tally, whichever name the report carries it under. */
export function driftCounts(
  report: DriftReport | null | undefined,
): { ok: number; warn: number; bad: number } {
  if (!report) return NO_COUNTS;
  if (report.counts) return report.counts;
  return typeof report.summary === 'object' && report.summary
    ? report.summary
    : NO_COUNTS;
}

/** `YYYY-MM-DD`, inclusive both ends. `null` on either means "let the backend
 *  choose" — which it does by opening on the last 30 days. */
export interface DriftRange {
  from: string | null;
  to: string | null;
}

/** One tool's usage inside one window. `sessions` is the rank, never `calls`:
 *  a loop calling one tool four hundred times is one session's worth of
 *  evidence. `calls: 0` is a real row — the registry is joined in, so a tool
 *  nobody has touched appears as a zero rather than going missing. */
/** One revision of a playbook over the window. `loads` is how often it was
 *  read; `succeeded` and `failed` are how the turns that read it ended, which
 *  is the half the tool ledger cannot answer. `unjoined` is neither: a load
 *  with no closed turn around it is not a success and not a failure, and
 *  folding it into either would let a run of open turns read as a procedure
 *  that stopped working. `ungraded` is the same refusal one step further in:
 *  the turn closed its task on the assistant's own sentence, so its outcome is
 *  the model grading itself. `trouble` is failures and corrections over loads,
 *  and null when no turn graded this revision at all, whether because nothing
 *  read it or because nothing that read it could be graded: a ratio over
 *  nothing is not a reading, and zero over nothing is not a clean record. */
export interface PlaybookRevisionRow {
  version: string;
  loads: number;
  succeeded: number;
  failed: number;
  unjoined: number;
  ungraded: number;
  corrections: number;
  retries: number;
  trouble: number | null;
  /** How many distinct turns the two figures below are summed over, so the
   *  sample is visible rather than inferred from `loads`. */
  turns: number;
  /** The tool calls those turns made and what they cost. These belong to the
   *  TURN, not to the playbook: a turn does the work the playbook describes
   *  and everything else it was for, and nothing on disk separates them. Show
   *  them only with that said. */
  turn_calls: number;
  /** null when the cost was not measured, which is not the same as nothing
   *  spent. Render it as unmeasured, never as $0.0000. */
  turn_cost_usd: number | null;
}

export interface PlaybookUsageRow {
  playbook: string;
  description: string;
  version: string;
  status: string;
  /** Whether it arrives on every turn with its steps, or costs a
   *  `playbook_search` first. The dial this panel is read to set. */
  carried: boolean;
  revisions: PlaybookRevisionRow[];
  loads: number;
  succeeded: number;
  failed: number;
  unjoined: number;
  ungraded: number;
  corrections: number;
  retries: number;
  turns: number;
  turn_calls: number;
  /** null when the cost was not measured, which is not the same as nothing
   *  spent. Render it as unmeasured, never as $0.0000. */
  turn_cost_usd: number | null;
  /** The revision this one replaced, or "" when there has never been one.
   *  A playbook is rewritten because it kept being corrected, and this is
   *  what says whether the rewrite worked. */
  previous_version: string;
  previous_trouble: number | null;
  /** Four answers, because a strict comparison has three outcomes and "no
   *  data" is a fourth. "unknown" means undecided, NOT unchanged: a fresh
   *  revision has barely been read. "same" is a rewrite that measured
   *  identically, which was reported as an improvement while this was a
   *  boolean. */
  comparison: 'better' | 'worse' | 'same' | 'unknown';
}

export interface PlaybookUsageResponse {
  days: number;
  playbooks: PlaybookUsageRow[];
  carried_count: number;
  total: number;
  /** The file the carried list lives in, so the panel can say where to edit. */
  path: string;
  /** Why the calls and cost figures are the turn's. Read off the backend so
   *  the panel and the chat answer cannot come to word it differently. */
  turn_scope_note: string;
}

export interface ToolUsageRow {
  tool: string;
  sessions: number;
  calls: number;
}

export interface ToolUsageWindow {
  days: number;
  tools: ToolUsageRow[];
}

/** One tool, and whether it rides every turn. `group` and `summary` are the
 *  tool class's own strings, so the panel cannot become a third opinion about
 *  what a tool does. `locked` is the floor: `tool_search` is how everything
 *  off the list is reached, so it cannot be taken off. */
export interface WorkingSetRow {
  tool: string;
  group: string;
  group_label: string;
  summary: string;
  core: boolean;
  calls_30d: number;
  locked: boolean;
  /** `shipped` for tools TESSERACT comes with, `custom` for ones written into
   *  the operator's own tools folder. Promotion writes to a different file for
   *  each, which the endpoint handles; the panel only shows which is which. */
  origin?: 'shipped' | 'custom';
}

export interface WorkingSetResponse {
  /** Where custom promotions are written; shipped ones go to `path`. */
  custom_path?: string;
  tools: WorkingSetRow[];
  /** Every taxonomy group, in the order the assistant is shown them. */
  groups: { slug: string; label: string }[];
  core_count: number;
  total: number;
  locked: string[];
  /** The file the names live in, so the panel can name it. */
  path: string;
}

/** The ledger as a tool-by-day grid. `counts` is positionally aligned to
 *  `dates`, including the empty days, so a quiet day is a gap rather than a
 *  shift. */
export interface ToolHeatmap {
  dates: string[];
  tools: { tool: string; counts: number[] }[];
}

/** What the runtime did on a day, or over a span of them.
 *
 *  Every figure here is read back from the records a turn writes for itself.
 *  Nothing is computed in this file and nothing should be: the same reader
 *  answers `day_read` on a channel, and a second arithmetic over the same
 *  rows is a second answer waiting to disagree with the first.
 */
export interface DayCall {
  at: string;
  tool: string;
  outcome: string;
  reason: string;
  ms: number;
  turn: string;
  door: string;
  /** What the call left behind, when it left anything. `null` covers three
   *  different cases and the OUTCOME tells them apart: the tool cannot leave
   *  marks, it had nothing to leave this time, or it owed one and did not
   *  produce it, which is `unverified`. */
  receipt: { kind: string; id: string; locator: string } | null;
}

export interface DayTool {
  tool: string;
  calls: number;
  byOutcome: Record<string, number>;
  receipts: number;
}

export interface DayTurn {
  turn: string;
  at: string;
  entry: string;
  label: string;
  /** The door in words. Never `entry`, which is a slug: a person cannot
   *  resolve `channel:telegram` and the backend already answers this. */
  door: string;
  tools: string[];
  outcome: string;
  taskId: string;
  taskOutcome: string;
  taskVerifiedBy: string;
}

export interface DayReport {
  days: string[];
  turns: DayTurn[];
  calls: DayCall[];
  tools: DayTool[];
  /** Every call that was not a clean success, in the order it happened.
   *  `unverified` is in here: a band showing only failures would be silent
   *  about the calls nobody can check. */
  trouble: DayCall[];
  byOutcome: Record<string, number>;
  byDoor: { entry: string; name: string; turns: number }[];
  turnCount: number;
  callCount: number;
  /** Records this reader could not parse. Rendered rather than dropped:
   *  "nothing happened" and "this could not tell you" are different answers. */
  unreadable: number;
  /** The day the report is anchored on, which is the newest day with records
   *  when the caller named none. `null` when nothing has ever been recorded,
   *  and the panel says that rather than drawing today as a quiet day. */
  anchor: string | null;
  /** Which days the picker may reach, so an arrow goes inert rather than
   *  offering a day that draws empty. */
  available: string[];
  maxSpanDays: number;
}

/** One block of the system prompt, as the assembly built it. Every one of
 *  them is sent — there is no budget and nothing is dropped. */
export interface PayloadSection {
  name: string;
  /** A filename for an inlined document, a short noun phrase otherwise. */
  label: string;
  /** One plain sentence saying what the block is. */
  description: string;
  group: 'instructions' | 'tools' | 'memory';
  chars: number;
  /** An estimate at `chars_per_token` — the one the runtime compacts by. */
  tokens: number;
  /** The workspace file this section inlines whole, if it is one. */
  document: string | null;
  /** Cockpit view showing where this came from, or null when no surface
   *  exists for it yet — the panel renders those plain rather than as a link
   *  that goes nowhere. */
  panel: string | null;
  hint: string | null;
}

/** How much of the memory store rides the turn versus how much only
 *  `memory_search` can reach. Resident is not the same as retrievable. */
export interface PayloadResidency {
  topic_hubs: number;
  topic_hubs_inlined: number;
  source_rollups: number;
  source_rollups_inlined: number;
  daily_captures: number;
}

export interface PayloadGroup {
  name: 'instructions' | 'tools' | 'memory';
  chars: number;
  tokens: number;
}

/** One surface's turn, composed. Measured by assembling, so these are the
 *  figures the turn would send, not an estimate of them. */
export interface PayloadReading {
  surface: string;
  total_chars: number;
  total_tokens: number;
  prose_chars: number;
  schema_chars: number;
  core_tools: number;
  groups: PayloadGroup[];
  sections: PayloadSection[];
  residency: PayloadResidency;
}

/** Every surface in one response — the difference between them is the reading,
 *  and two round trips would only add a way for them to disagree. */
export interface PayloadResponse {
  readings: PayloadReading[];
  chars_per_token: number;
}

/** One turn's model calls, as far as the prompt cache is concerned.
 *
 *  `hit_rate` is `null`, never 0, when no call in the turn reported a cached
 *  count. A provider that said nothing and a provider that said zero are
 *  different facts, and only the second one is a miss. */
export interface CacheTurn {
  turn_id: string;
  started_at: string;
  local_date: string;
  role: string;
  model: string;
  calls: number;
  reported_calls: number;
  input_tokens: number;
  cached_tokens: number;
  uncached_tokens: number;
  written_tokens: number;
  cost_usd: number;
  hit_rate: number | null;
}

export interface CacheSummary {
  turns: number;
  calls: number;
  reported_calls: number;
  unreported_calls: number;
  input_tokens: number;
  cached_tokens: number;
  uncached_tokens: number;
  written_tokens: number;
  cost_usd: number;
  hit_rate: number | null;
}

/** `ledger: false` means no ledger file exists yet, which is not the same as
 *  a window with no hits and must not be drawn as one. */
export interface CacheResponse {
  days: number;
  summary: CacheSummary | null;
  /** The newest turns in the window, newest first. A turn that cached well is
   *  at the bottom of `turns` by construction, so this is the only half that
   *  can answer what the turn you just took did. */
  latest: CacheTurn[];
  /** The same window ranked by what each turn re-read, worst first. */
  turns: CacheTurn[];
  ledger: boolean;
}

interface ConscienceState {
  report: DriftReport | null;
  history: DriftReport[];
  /** Every date with a report on disk, oldest first. The picker is bounded by
   *  this rather than by a calendar: only these days can return anything. */
  available: string[];
  /** What the backend actually resolved the window to, which is what the
   *  inputs show — asking for an open-ended range and displaying blanks would
   *  leave the operator unable to tell what they are looking at. */
  range: DriftRange;
  loading: boolean;
  error: string | null;
  fetchDrift: (range?: DriftRange) => Promise<void>;

  /** Usage per window, in the order the backend returned them. */
  usage: ToolUsageWindow[];
  /** False when the backend had no registry to join — the panel says so
   *  rather than showing a roster it cannot vouch for. */
  usageRoster: boolean;
  usageTotal: number;
  usageLoading: boolean;
  usageError: string | null;
  fetchToolUsage: () => Promise<void>;

  payload: PayloadResponse | null;
  payloadLoading: boolean;
  payloadError: string | null;
  fetchPayload: () => Promise<void>;

  cache: CacheResponse | null;
  cacheLoading: boolean;
  cacheError: string | null;
  fetchCache: (days?: number) => Promise<void>;

  workingSet: WorkingSetResponse | null;
  workingSetLoading: boolean;
  workingSetError: string | null;
  /** The tool whose switch is mid-flight, so only that one goes inert. */
  workingSetSaving: string | null;
  fetchWorkingSet: () => Promise<void>;
  /** Carry a tool every turn, or stop. Live on the next turn — the backend
   *  re-marks the registry rather than waiting for the config watcher. */
  setToolCore: (tool: string, core: boolean) => Promise<void>;

  heatmap: ToolHeatmap | null;
  heatmapDays: number;
  heatmapLoading: boolean;
  heatmapError: string | null;
  fetchHeatmap: (days?: number) => Promise<void>;

  playbookUsage: PlaybookUsageResponse | null;
  playbookUsageLoading: boolean;
  playbookUsageError: string | null;
  fetchPlaybookUsage: () => Promise<void>;

  day: DayReport | null;
  /** `YYYY-MM-DD`, or null for whichever day the backend anchors on.
   *  Held here rather than in the view so a refresh keeps the day the
   *  operator was reading. */
  dayOn: string | null;
  /** The far end of a span, or null for a single day. */
  dayThrough: string | null;
  dayLoading: boolean;
  dayError: string | null;
  fetchDay: (on?: string | null, through?: string | null) => Promise<void>;
}

export const useConscienceStore = create<ConscienceState>((set) => ({
  report: null,
  history: [],
  available: [],
  range: { from: null, to: null },
  loading: false,
  error: null,

  usage: [],
  usageRoster: false,
  usageTotal: 0,
  usageLoading: false,
  usageError: null,

  payload: null,
  payloadLoading: false,
  payloadError: null,

  cache: null,
  cacheLoading: false,
  cacheError: null,

  workingSet: null,
  workingSetLoading: false,
  workingSetError: null,
  workingSetSaving: null,

  heatmap: null,
  heatmapDays: 30,
  heatmapLoading: false,
  heatmapError: null,

  playbookUsage: null,
  playbookUsageLoading: false,
  playbookUsageError: null,

  day: null,
  dayOn: null,
  dayThrough: null,
  dayLoading: false,
  dayError: null,

  fetchWorkingSet: async () => {
    set({ workingSetLoading: true, workingSetError: null });
    try {
      const res = await fetch(`${BACKEND_BASE}/api/conscience/working-set`);
      const data = await res.json();
      if (!res.ok) throw new Error(data?.error ?? `HTTP ${res.status}`);
      set({ workingSet: data as WorkingSetResponse, workingSetLoading: false });
    } catch (err) {
      set({
        workingSetError: err instanceof Error ? err.message : String(err),
        workingSetLoading: false,
      });
    }
  },

  setToolCore: async (tool, core) => {
    set({ workingSetSaving: tool, workingSetError: null });
    try {
      const res = await fetch(`${BACKEND_BASE}/api/conscience/working-set`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tool, core }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data?.error ?? `HTTP ${res.status}`);
      // The row and the count come from what the backend applied, not from
      // what was asked for: the floor can refuse, and a switch that moved
      // anyway would be reporting a change the file does not hold.
      set((state) =>
        state.workingSet
          ? {
              workingSet: {
                ...state.workingSet,
                core_count: data.core_count ?? state.workingSet.core_count,
                tools: state.workingSet.tools.map((row) =>
                  row.tool === data.tool ? { ...row, core: Boolean(data.core) } : row,
                ),
              },
              workingSetSaving: null,
            }
          : { workingSetSaving: null },
      );
    } catch (err) {
      set({
        workingSetError: err instanceof Error ? err.message : String(err),
        workingSetSaving: null,
      });
    }
  },

  fetchPlaybookUsage: async () => {
    set({ playbookUsageLoading: true, playbookUsageError: null });
    try {
      const res = await fetch(`${BACKEND_BASE}/api/conscience/playbook-usage`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as PlaybookUsageResponse;
      set({ playbookUsage: data, playbookUsageLoading: false });
    } catch (err) {
      set({
        playbookUsageError: err instanceof Error ? err.message : String(err),
        playbookUsageLoading: false,
      });
    }
  },

  fetchDay: async (on, through) => {
    // `undefined` means "keep what is showing"; `null` means "clear it". The
    // two are different asks and collapsing them made the range toggle
    // unable to turn itself off.
    const held = useConscienceStore.getState();
    const nextOn = on === undefined ? held.dayOn : on;
    const nextThrough = through === undefined ? held.dayThrough : through;
    set({
      dayLoading: true,
      dayError: null,
      dayOn: nextOn,
      dayThrough: nextThrough,
    });
    const query = new URLSearchParams();
    if (nextOn) query.set('on', nextOn);
    if (nextThrough) query.set('through', nextThrough);
    try {
      const res = await fetch(
        `${BACKEND_BASE}/api/conscience/day?${query.toString()}`,
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as DayReport;
      // The backend chose the day when nobody named one, and the picker has
      // to show what is actually being drawn.
      set({ day: data, dayOn: data.anchor ?? nextOn, dayLoading: false });
    } catch (err) {
      set({
        dayError: err instanceof Error ? err.message : String(err),
        dayLoading: false,
      });
    }
  },

  fetchHeatmap: async (days) => {
    const window = days ?? useConscienceStore.getState().heatmapDays;
    set({ heatmapLoading: true, heatmapError: null, heatmapDays: window });
    try {
      const res = await fetch(
        `${BACKEND_BASE}/api/conscience/tool-heatmap?days=${window}`,
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as ToolHeatmap;
      set({ heatmap: data, heatmapLoading: false });
    } catch (err) {
      set({
        heatmapError: err instanceof Error ? err.message : String(err),
        heatmapLoading: false,
      });
    }
  },

  fetchPayload: async () => {
    set({ payloadLoading: true, payloadError: null });
    try {
      const res = await fetch(`${BACKEND_BASE}/api/conscience/payload`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      set({ payload: (await res.json()) as PayloadResponse, payloadLoading: false });
    } catch (err) {
      set({
        payloadError: err instanceof Error ? err.message : String(err),
        payloadLoading: false,
      });
    }
  },

  fetchCache: async (days) => {
    set({ cacheLoading: true, cacheError: null });
    try {
      const window = days ? `?days=${days}` : '';
      const res = await fetch(`${BACKEND_BASE}/api/conscience/cache${window}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      set({ cache: (await res.json()) as CacheResponse, cacheLoading: false });
    } catch (err) {
      set({
        cacheError: err instanceof Error ? err.message : String(err),
        cacheLoading: false,
      });
    }
  },

  fetchToolUsage: async () => {
    set({ usageLoading: true, usageError: null });
    try {
      const res = await fetch(`${BACKEND_BASE}/api/conscience/tool-usage`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as {
        windows: ToolUsageWindow[];
        roster: boolean;
        total_tools: number;
      };
      set({
        usage: data.windows ?? [],
        usageRoster: Boolean(data.roster),
        usageTotal: data.total_tools ?? 0,
        usageLoading: false,
      });
    } catch (err) {
      set({
        usageError: err instanceof Error ? err.message : String(err),
        usageLoading: false,
      });
    }
  },

  fetchDrift: async (range) => {
    set({ loading: true, error: null });
    try {
      const params = new URLSearchParams();
      if (range?.from) params.set('from', range.from);
      if (range?.to) params.set('to', range.to);
      const query = params.toString();
      const res = await fetch(
        `${BACKEND_BASE}/api/conscience/drift${query ? `?${query}` : ''}`,
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as {
        report: DriftReport | null;
        history: DriftReport[];
        available?: string[];
        from?: string;
        to?: string;
      };
      set({
        report: data.report ?? null,
        history: data.history ?? [],
        available: data.available ?? [],
        range: { from: data.from ?? null, to: data.to ?? null },
        loading: false,
      });
    } catch (err) {
      set({
        error: err instanceof Error ? err.message : String(err),
        loading: false,
      });
    }
  },
}));

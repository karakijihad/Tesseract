import { Select } from "../../components/common/Select";
import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react';
import { useEntityName } from '../../hooks/useEntityName';
import {
  useWorkspaceStore,
  type EventKind,
  type WorkspaceEvent,
} from '../../stores/workspace';
import { CommentThread } from './CommentThread';
import { EventDetailBody } from './EventDetailBody';
import { NewThreadButton } from './NewThreadButton';
import { VoiceComposer } from './VoiceComposer';
import { Hint } from '../../components/ui/Hint';
import { Button } from '../../components/common/Button';
import { Checkbox } from '../../components/common/Checkbox';
import { Disclosure } from '../../components/common/Disclosure';
import { Input } from '../../components/common/Input';
import { Note } from '../../components/common/Note';

export const KIND_LABEL: Record<EventKind, string> = {
  feedback_proposal: 'Proposal',
  feedback_sweep: 'Sweep',
  agent_approval: 'Approval',
  skill_approval: 'Skill',
  skill_refinement: 'Skill fix',
  soul_proposal: 'Soul',
  change_proposal: 'Change',
  mission_reflection_proposal: 'Mission Reflection',
  reflection_proposal: 'Reflection',
  nudge: 'Nudge',
  agent_post: 'Note',
  operator_post: 'You',
  daily_brief: 'Brief',
  yaml_change_proposal: 'Catalog',
  kb_merge_conflict: 'KB conflict',
  clarification: 'Question',
};

// Kinds where Approve/Reject map to a real backend effect — the operator's
// decision changes system state. Other kinds are informational (the action
// already happened or there's nothing to gate); they render Resolve only.
const ACTIONABLE_KINDS = new Set<EventKind>([
  'change_proposal',
  'feedback_proposal',
  'feedback_sweep',
  'soul_proposal',
  'agent_approval',
  'skill_approval',
  'skill_refinement',
  'mission_reflection_proposal',
  'yaml_change_proposal',
]);

const KIND_ORIGIN_LABEL: Record<string, string> = {
  provider_model_added: 'Model added',
  provider_pricing_changed: 'Pricing changed',
  provider_context_changed: 'Context window changed',
  provider_model_deprecated: 'Model deprecated',
  role_model_remapped: 'Role remapped',
};

export type InboxFilter =
  | 'all'
  | 'proposals'
  | 'approvals'
  | 'nudges'
  | 'missed'
  | 'history';

export const INBOX_FILTERS: Array<{ key: InboxFilter; label: string }> = [
  { key: 'all', label: 'All' },
  { key: 'proposals', label: 'Proposals' },
  { key: 'approvals', label: 'Approvals' },
  { key: 'nudges', label: 'Nudges' },
  { key: 'missed', label: 'Missed' },
  { key: 'history', label: 'History' },
];


const STATUS_LABEL: Record<string, string> = {
  approved: '✓ Approved',
  rejected: '✗ Rejected',
  resolved: '◯ Resolved',
  applied: '✓ Applied',
  deleted: '🗑 Deleted',
  pending: 'Pending',
};

const PAGE_SIZES = [10, 20, 50, 100];

function fmtTs(ts: string): string {
  try {
    return new Date(ts).toLocaleString([], {
      hour12: false,
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch {
    return ts;
  }
}

function filterEvents(
  events: WorkspaceEvent[],
  filter: InboxFilter,
  lastSeen: string | undefined,
): WorkspaceEvent[] {
  switch (filter) {
    case 'proposals':
      return events.filter(
        (e) =>
          e.kind === 'feedback_proposal' ||
          e.kind === 'feedback_sweep' ||
          e.kind === 'soul_proposal' ||
          e.kind === 'change_proposal' ||
          e.kind === 'mission_reflection_proposal' ||
          e.kind === 'yaml_change_proposal',
      );
    case 'approvals':
      return events.filter((e) =>
        ['agent_approval', 'skill_approval', 'skill_refinement'].includes(e.kind),
      );
    case 'nudges':
      return events.filter(
        (e) =>
          e.kind === 'nudge' ||
          e.kind === 'agent_post' ||
          e.kind === 'reflection_proposal' ||
          e.kind === 'daily_brief',
      );
    case 'missed':
      if (!lastSeen) return events;
      return events.filter((e) => e.ts > lastSeen);
    case 'history':
      return events;
    default:
      return events;
  }
}

function dateFilter(events: WorkspaceEvent[], from: string, to: string): WorkspaceEvent[] {
  if (!from && !to) return events;
  // Parse `YYYY-MM-DD` from <input type="date"> as LOCAL time, not UTC, so a
  // "from = today" filter doesn't drop events created earlier today in the
  // operator's tz. End-of-day on `to` for inclusive range.
  const fromMs = from ? new Date(`${from}T00:00:00`).getTime() : Number.NEGATIVE_INFINITY;
  const toMs = to ? new Date(`${to}T23:59:59.999`).getTime() : Number.POSITIVE_INFINITY;
  return events.filter((e) => {
    const t = Date.parse(e.ts);
    return Number.isFinite(t) && t >= fromMs && t <= toMs;
  });
}

export interface InboxPanelProps {
  /** Which slice the rail has open. */
  filter?: InboxFilter;
  /** The event kinds ticked in the rail — null means every kind. */
  kinds?: Set<EventKind> | null;
  /** Reports the kinds present in the current slice, so the rail can list
   *  them with counts. The rail cannot know them: they come out of the
   *  fetched events. */
  onKindsAvailable?: (counts: Map<EventKind, number>) => void;
}

export function InboxPanel({
  filter: filterProp,
  kinds,
  onKindsAvailable,
}: InboxPanelProps = {}) {
  const entityName = useEntityName();
  const events = useWorkspaceStore((s) => s.events);
  const history = useWorkspaceStore((s) => s.history);
  const seen = useWorkspaceStore((s) => s.seen);
  const loading = useWorkspaceStore((s) => s.loading);
  const lastError = useWorkspaceStore((s) => s.lastError);
  const fetchInbox = useWorkspaceStore((s) => s.fetchInbox);
  const fetchHistory = useWorkspaceStore((s) => s.fetchHistory);
  const fetchSeen = useWorkspaceStore((s) => s.fetchSeen);
  const markSeen = useWorkspaceStore((s) => s.markPanelSeen);
  const decide = useWorkspaceStore((s) => s.decide);
  const attentionCount = useWorkspaceStore((s) => s.attentionCount);

  const filter = filterProp ?? 'all';
  const [fromDate, setFromDate] = useState<string>('');
  const [toDate, setToDate] = useState<string>('');
  const [pageSize, setPageSize] = useState<number>(20);
  const [page, setPage] = useState<number>(1);
  const [openId, setOpenId] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkBusy, setBulkBusy] = useState(false);
  const selectAllRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    fetchInbox();
    fetchSeen();
  }, [fetchInbox, fetchSeen]);

  useEffect(() => {
    markSeen('inbox');
  }, [markSeen]);

  // History pane lazy-loads on first switch and refetches when reopened —
  // separate from the live inbox so its `?status=all` snapshot never
  // contaminates pending-only counts (badge, attention timer).
  useEffect(() => {
    if (filter === 'history') void fetchHistory();
  }, [filter, fetchHistory]);

  const sourceEvents = filter === 'history' ? history : events;
  const filtered = useMemo(
    () => filterEvents(sourceEvents, filter, seen.inbox),
    [sourceEvents, filter, seen.inbox],
  );

  // Per-kind counts derived from the group-filtered list so the chips
  // reflect "what's available under the current group", not the whole
  // store. Empty kinds are dropped — no point rendering a chip for a
  // type the operator has nothing of right now.
  const kindCounts = useMemo<Map<EventKind, number>>(() => {
    const counts = new Map<EventKind, number>();
    for (const e of filtered) {
      counts.set(e.kind, (counts.get(e.kind) ?? 0) + 1);
    }
    return counts;
  }, [filtered]);

  const kindFiltered = useMemo(
    () => (kinds ? filtered.filter((e) => kinds.has(e.kind)) : filtered),
    [filtered, kinds],
  );

  const ranged = useMemo(
    () => dateFilter(kindFiltered, fromDate, toDate),
    [kindFiltered, fromDate, toDate],
  );

  // The rail lists the kinds present in this slice, which only the fetched
  // events know. Reported up rather than derived twice.
  useEffect(() => {
    onKindsAvailable?.(kindCounts);
  }, [kindCounts, onKindsAvailable]);

  const totalPages = Math.max(1, Math.ceil(ranged.length / pageSize));
  const safePage = Math.min(page, totalPages);
  const paginated = useMemo(
    () => ranged.slice((safePage - 1) * pageSize, safePage * pageSize),
    [ranged, safePage, pageSize],
  );

  const attention = attentionCount();

  // Selection is scoped to the PAGE, deliberately. Ticking a header box that
  // silently also selects the 90 rows on the next four pages is how a bulk
  // delete becomes a surprise; what you can see is what you have selected.
  // The count beside the verbs says exactly how many each one will touch.
  const pageIds = useMemo(() => paginated.map((e) => e.event_id), [paginated]);

  // Selecting a row and then paging away should not leave a decision armed
  // against something off-screen.
  useEffect(() => {
    setSelected((prev) => {
      if (prev.size === 0) return prev;
      const onPage = new Set(pageIds);
      const next = new Set([...prev].filter((id) => onPage.has(id)));
      return next.size === prev.size ? prev : next;
    });
  }, [pageIds]);

  const allOnPageSelected = pageIds.length > 0 && pageIds.every((id) => selected.has(id));
  const someOnPageSelected = pageIds.some((id) => selected.has(id));

  // `indeterminate` is a DOM property with no React prop, so the shared
  // Checkbox forwards a ref for exactly this.
  useEffect(() => {
    if (selectAllRef.current) {
      selectAllRef.current.indeterminate = someOnPageSelected && !allOnPageSelected;
    }
  }, [someOnPageSelected, allOnPageSelected]);

  const toggleOne = (id: string, next: boolean) =>
    setSelected((prev) => {
      const s = new Set(prev);
      if (next) s.add(id);
      else s.delete(id);
      return s;
    });

  const toggleAllOnPage = (next: boolean) =>
    setSelected(next ? new Set(pageIds) : new Set());

  const selectedEvents = useMemo(
    () => paginated.filter((e) => selected.has(e.event_id)),
    [paginated, selected],
  );

  // A verb only counts the rows it is actually legal for, so the label says
  // what will happen rather than how many boxes are ticked. Approve/Reject
  // reach only the kinds where the operator's decision changes system state;
  // Resolve is for the rest; Delete is anything not already deleted.
  const eligible = useMemo(() => {
    // Approve and Reject are the same gate — one decision, two answers — so
    // the set is computed once. Written twice, the two counts could never
    // differ and a later change to one would silently not reach the other.
    const decidable = selectedEvents.filter(
      (e) => e.status === 'pending' && ACTIONABLE_KINDS.has(e.kind),
    );
    return {
      approve: decidable,
      reject: decidable,
      resolve: selectedEvents.filter(
        (e) => e.status === 'pending' && !ACTIONABLE_KINDS.has(e.kind),
      ),
      delete: selectedEvents.filter((e) => e.status !== 'deleted'),
    };
  }, [selectedEvents]);

  // One request per event, all in flight together — the backend takes a
  // PER-EVENT decision lock, so different events were built to be decided
  // concurrently. `allSettled` because one failure must not abandon the other
  // forty-nine; whatever failed stays selected and the error surfaces from the
  // store as it does for a single decision.
  //
  // Failure is read from `decide`'s RETURN, not from a rejected promise. The
  // store catches every non-404 itself and resolves anyway — deliberately, so
  // a single-row caller need not catch — so `allSettled` sees success for a
  // 5xx and every attempted row was being deselected regardless of outcome.
  // A rejection is still treated as a failure: it is the shape an unexpected
  // throw would take, and defaulting that to "worked" is the mistake here.
  const runBulk = async (
    decision: 'approve' | 'reject' | 'resolve' | 'delete',
    reason?: string,
  ) => {
    const targets = eligible[decision];
    if (targets.length === 0 || bulkBusy) return;
    setBulkBusy(true);
    try {
      const results = await Promise.allSettled(
        targets.map((e) => decide(e.event_id, decision, reason)),
      );
      const failed = new Set(
        targets
          .filter((_, i) => {
            const r = results[i];
            return r.status === 'rejected' || r.value === false;
          })
          .map((e) => e.event_id),
      );
      // What survives: anything this verb did not touch, plus what it tried
      // and could not do. Filtering on `failed` alone also dropped the rows
      // that were selected but not ELIGIBLE — so on a mixed page, resolving
      // the informational rows silently deselected the actionable ones beside
      // them and the following Approve acted on nothing.
      const attempted = new Set(targets.map((e) => e.event_id));
      setSelected(
        (prev) =>
          new Set([...prev].filter((id) => !attempted.has(id) || failed.has(id))),
      );
      if (openId && !failed.has(openId) && targets.some((e) => e.event_id === openId)) {
        setOpenId(null);
      }
    } finally {
      setBulkBusy(false);
    }
  };

  const onDecide = async (
    event_id: string,
    decision: 'approve' | 'reject' | 'resolve' | 'delete',
    reason?: string,
  ) => {
    setBusyId(event_id);
    try {
      // Only close the row if the decision actually landed. `decide` reports
      // failure by returning false — collapsing the details on a 5xx hid the
      // actions the operator needs in order to retry, on the row the store had
      // deliberately kept for exactly that.
      const settled = await decide(event_id, decision, reason);
      if (settled && openId === event_id) setOpenId(null);
    } finally {
      setBusyId(null);
    }
  };


  return (
    <div className="workspace-view">
      <div className="workspace-composer-row">
        <NewThreadButton buttonLabel="New" />
        <VoiceComposer />
      </div>

      <div className="workspace-view-controls">
        <label className="workspace-control workspace-control--select-all">
          <Checkbox
            checked={allOnPageSelected}
            onChange={toggleAllOnPage}
            disabled={pageIds.length === 0 || bulkBusy}
            inputRef={selectAllRef}
            ariaLabel="select every row on this page"
          />
          <span className="t-meta">
            {selected.size > 0 ? `${selected.size} selected` : 'Select all'}
          </span>
        </label>
        <label className="workspace-control">
          <span className="t-meta">From</span>
          <Input
            type="date"
            value={fromDate}
            max={toDate || undefined}
            ariaLabel="Events from"
            onChange={(next) => { setFromDate(next); setPage(1); }}
          />
        </label>
        <label className="workspace-control">
          <span className="t-meta">To</span>
          <Input
            type="date"
            value={toDate}
            min={fromDate || undefined}
            ariaLabel="Events to"
            onChange={(next) => { setToDate(next); setPage(1); }}
          />
        </label>
        {(fromDate || toDate) && (
          <Button onClick={() => { setFromDate(''); setToDate(''); setPage(1); }}>
            Clear dates
          </Button>
        )}
        <label className="workspace-control">
          <span className="t-meta">Per page</span>
          <Select
            value={String(pageSize)}
            options={PAGE_SIZES.map((n) => ({ value: String(n), label: String(n) }))}
            onChange={(v) => { setPageSize(Number(v)); setPage(1); }}
            ariaLabel="Rows per page"
          />
        </label>
        <span className="workspace-control-count t-meta">
          {ranged.length} of {filtered.length} shown
        </span>
      </div>

      {selected.size > 0 && (
        <div className="workspace-bulk-bar" role="group" aria-label="Actions for selected rows">
          <span className="t-meta workspace-bulk-bar__count">
            {selected.size} selected on this page
          </span>
          <Hint label="Approve every selected row whose kind has a real backend effect">
            <Button
              tone="good"
              onClick={() => void runBulk('approve')}
              disabled={bulkBusy || eligible.approve.length === 0}
            >
              Approve {eligible.approve.length}
            </Button>
          </Hint>
          <Hint label="Resolve every selected informational row — flips status without re-running an approval gate">
            <Button
              onClick={() => void runBulk('resolve')}
              disabled={bulkBusy || eligible.resolve.length === 0}
            >
              Resolve {eligible.resolve.length}
            </Button>
          </Hint>
          <Button
            tone="danger"
            onClick={() => {
              const n = eligible.reject.length;
              if (n === 0) return;
              const r = window.prompt(
                `Reason for rejecting ${n} item${n === 1 ? '' : 's'} (optional — ${entityName} sees it):`,
                '',
              );
              if (r === null) return;
              void runBulk('reject', r.trim() || undefined);
            }}
            disabled={bulkBusy || eligible.reject.length === 0}
          >
            Reject {eligible.reject.length}
          </Button>
          <Button
            tone="danger"
            onClick={() => {
              const n = eligible.delete.length;
              if (n === 0) return;
              // The one verb here with no undo, and the only one that asks.
              if (!window.confirm(`Delete ${n} item${n === 1 ? '' : 's'}? This cannot be undone.`)) {
                return;
              }
              void runBulk('delete');
            }}
            disabled={bulkBusy || eligible.delete.length === 0}
          >
            Delete {eligible.delete.length}
          </Button>
          <Button onClick={() => setSelected(new Set())} disabled={bulkBusy}>
            Clear
          </Button>
          {bulkBusy && <span className="t-meta">working…</span>}
        </div>
      )}

      {attention > 0 && (
        <Note tone="warn" className="workspace-attention-banner">
          {attention} item{attention === 1 ? '' : 's'} waiting &gt; 24h
        </Note>
      )}

      {lastError && (
        <Note tone="bad" className="workspace-error">{lastError}</Note>
      )}

      <div className="workspace-view-body">
        {paginated.length === 0 && !loading ? (
          <div className="workspace-empty t-caption">
            {filter === 'all' && !fromDate && !toDate
              ? 'Inbox is clear.'
              : 'Nothing matches these filters.'}
          </div>
        ) : (
          paginated.map((ev) => {
            const isOpen = openId === ev.event_id;
            const unread = seen.inbox ? ev.ts > seen.inbox : true;
            const toggle = () => setOpenId(isOpen ? null : ev.event_id);
            const triggerId = `we-trigger-${ev.event_id}`;
            const detailsId = `we-details-${ev.event_id}`;
            const onTriggerKeyDown = (e: KeyboardEvent<HTMLButtonElement>) => {
              if (e.key === 'Escape' && isOpen) {
                e.preventDefault();
                setOpenId(null);
              }
            };
            // Approve/Reject only fires for kinds where the operator's
            // decision changes system state (change_proposal applies the
            // file write, etc). Informational kinds — operator_post,
            // agent_post, nudge, session reflection — get a single Resolve
            // verb that flips status without re-running an approval gate.
            const isActionable = ACTIONABLE_KINDS.has(ev.kind);
            // CR-5 — agent_post events whose payload carries a `channel` were
            // sourced by the channel gate. These are RECORDS: the operator
            // already answered on the channel the request came through, which
            // is the only surface that reaches them when they are away from
            // this screen. The inbox says what happened; it does not decide.
            const gatePayload =
              ev.kind === 'agent_post' &&
              typeof ev.payload === 'object' &&
              ev.payload !== null &&
              typeof (ev.payload as { channel?: unknown }).channel === 'string'
                ? (ev.payload as {
                    channel: string;
                    chat_id?: string;
                    tool?: string;
                    decision?: string;
                  })
                : null;
            return (
              <article
                key={ev.event_id}
                className={`workspace-event${unread ? ' is-unread' : ''}${isOpen ? ' is-open' : ''}${selected.has(ev.event_id) ? ' is-selected' : ''}`}
              >
                {/* Outside the trigger button, not inside it — a checkbox
                    nested in a button is not reachable as its own control. */}
                <div className="workspace-event-select">
                  <Checkbox
                    checked={selected.has(ev.event_id)}
                    onChange={(next) => toggleOne(ev.event_id, next)}
                    disabled={bulkBusy}
                    ariaLabel={`select ${ev.title}`}
                  />
                </div>
                <Disclosure
                  variant="row"
                  id={triggerId}
                  className="workspace-event-trigger"
                  open={isOpen}
                  ariaControls={detailsId}
                  onToggle={toggle}
                  onKeyDown={onTriggerKeyDown}
                >
                  <div className="workspace-event-meta">
                    <span className="t-meta workspace-event-kind">{KIND_LABEL[ev.kind] ?? ev.kind}</span>
                    <span className="workspace-event-source t-meta">{ev.source}</span>
                    <span className="workspace-event-ts t-meta">{fmtTs(ev.ts)}</span>
                    <span className="workspace-event-meta-right">
                      {ev.comments.length > 0 && (
                        <span className="workspace-event-comments t-meta" aria-label={`${ev.comments.length} comments`}>
                          {ev.comments.length} comment{ev.comments.length === 1 ? '' : 's'}
                        </span>
                      )}
                      {ev.priority >= 8 && (
                        <span className="workspace-event-prio" aria-label={`priority ${ev.priority}`}>
                          P{ev.priority}
                        </span>
                      )}
                      {ev.status !== 'pending' && (
                        <span className={`workspace-event-status workspace-event-status--${ev.status}`}>
                          {STATUS_LABEL[ev.status] ?? ev.status}
                        </span>
                      )}
                    </span>
                  </div>
                  <div className="workspace-event-title">{ev.title}</div>
                  <div className="workspace-event-summary">{ev.summary}</div>
                  {ev.kind === 'yaml_change_proposal' && (() => {
                    const p = ev.payload as {
                      kind_origin?: string;
                      yaml_path?: string;
                      diff?: string;
                    };
                    const originLabel = p.kind_origin
                      ? KIND_ORIGIN_LABEL[p.kind_origin] ?? p.kind_origin
                      : null;
                    return (
                      <div className="workspace-event-yaml-meta">
                        {originLabel && (
                          <span className="workspace-event-chip">{originLabel}</span>
                        )}
                        {p.yaml_path && (
                          <span className="t-meta workspace-event-path">{p.yaml_path}</span>
                        )}
                      </div>
                    );
                  })()}
                  {ev.kind === 'agent_approval' && (() => {
                    const p = ev.payload as {
                      model_role?: string;
                      proposer?: string;
                    };
                    return (
                      <div className="workspace-event-agent-meta">
                        {p.model_role && (
                          <span className="workspace-event-chip">{p.model_role}</span>
                        )}
                        {p.proposer && (
                          <span className="t-meta">proposed by {p.proposer}</span>
                        )}
                      </div>
                    );
                  })()}
                  {ev.kind === 'skill_approval' && (() => {
                    const p = ev.payload as {
                      proposer?: string;
                    };
                    return (
                      <div className="workspace-event-agent-meta">
                        {p.proposer && (
                          <span className="t-meta">proposed by {p.proposer}</span>
                        )}
                      </div>
                    );
                  })()}
                  {ev.kind === 'skill_refinement' && (() => {
                    const p = ev.payload as {
                      stats?: { total?: number; negative?: number };
                    };
                    return (
                      <div className="workspace-event-agent-meta">
                        {p.stats && (
                          <span className="workspace-event-chip">{p.stats.negative}/{p.stats.total} failed</span>
                        )}
                      </div>
                    );
                  })()}
                </Disclosure>
                <div className="workspace-event-actions">
                  {ev.status === 'pending' && (
                    isActionable ? (
                      <>
                        <Hint label={ev.kind === 'agent_approval'
                            ? 'Move the agent from pending/ into the active set'
                            : undefined}>
                          <Button
                            tone="good"
                            onClick={() => onDecide(ev.event_id, 'approve')}
                            disabled={busyId === ev.event_id}
                          >
                            {ev.kind === 'agent_approval' || ev.kind === 'skill_approval'
                              ? 'Promote'
                              : ev.kind === 'skill_refinement'
                              ? 'Apply'
                              : 'Approve'}
                          </Button>
                        </Hint>
                        <Button
                          tone="danger"
                          onClick={() => {
                            if (ev.kind === 'agent_approval' || ev.kind === 'skill_approval' || ev.kind === 'skill_refinement') {
                              // Same minimal surface as the channel gate:
                              // one prompt, optional text. The reason is
                              // archived beside the rejected agent and
                              // delivered to the agent on its next turn.
                              const r = window.prompt(
                                `Reason for rejection (optional — ${entityName} sees it):`,
                                '',
                              );
                              if (r === null) return; // cancelled
                              onDecide(ev.event_id, 'reject', r.trim() || undefined);
                              return;
                            }
                            onDecide(ev.event_id, 'reject');
                          }}
                          disabled={busyId === ev.event_id}
                        >
                          Reject
                        </Button>
                      </>
                    ) : gatePayload ? (
                      <span className="t-meta">
                        {gatePayload.decision === 'approved' ? 'Approved' : 'Refused'}
                        {' on '}
                        {gatePayload.channel}
                      </span>
                    ) : (
                      <Button
                        onClick={() => onDecide(ev.event_id, 'resolve')}
                        disabled={busyId === ev.event_id}
                      >
                        Resolve
                      </Button>
                    )
                  )}
                  {ev.status !== 'deleted' && (
                    <Button
                      tone="danger"
                      onClick={() => onDecide(ev.event_id, 'delete')}
                      disabled={busyId === ev.event_id}
                    >
                      Delete
                    </Button>
                  )}
                </div>
                {isOpen && (
                  <div
                    id={detailsId}
                    role="region"
                    aria-labelledby={triggerId}
                    className="workspace-event-expanded"
                  >
                    <EventDetailBody event={ev} />
                    <CommentThread event_id={ev.event_id} comments={ev.comments} />
                  </div>
                )}
              </article>
            );
          })
        )}
      </div>

      {ranged.length > pageSize && (
        <div className="workspace-pagination">
          <Button
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            disabled={safePage === 1}
          >
            Prev
          </Button>
          <span className="t-meta">Page {safePage} of {totalPages}</span>
          <Button
            onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
            disabled={safePage === totalPages}
          >
            Next
          </Button>
        </div>
      )}
    </div>
  );
}

import { useEffect, useMemo, useState, type KeyboardEvent } from 'react';
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

const KIND_LABEL: Record<EventKind, string> = {
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

const FILTERS: Array<{ id: 'all' | 'proposals' | 'approvals' | 'nudges' | 'missed' | 'history'; label: string }> = [
  { id: 'all', label: 'All' },
  { id: 'proposals', label: 'Proposals' },
  { id: 'approvals', label: 'Approvals' },
  { id: 'nudges', label: 'Nudges' },
  { id: 'missed', label: 'Missed' },
  { id: 'history', label: 'History' },
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
  filter: typeof FILTERS[number]['id'],
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

export function InboxPanel() {
  const entityName = useEntityName();
  const events = useWorkspaceStore((s) => s.events);
  const history = useWorkspaceStore((s) => s.history);
  const seen = useWorkspaceStore((s) => s.seen);
  const loading = useWorkspaceStore((s) => s.loading);
  const loadingHistory = useWorkspaceStore((s) => s.loadingHistory);
  const lastError = useWorkspaceStore((s) => s.lastError);
  const fetchInbox = useWorkspaceStore((s) => s.fetchInbox);
  const fetchHistory = useWorkspaceStore((s) => s.fetchHistory);
  const fetchSeen = useWorkspaceStore((s) => s.fetchSeen);
  const markSeen = useWorkspaceStore((s) => s.markPanelSeen);
  const decide = useWorkspaceStore((s) => s.decide);
  const channelGate = useWorkspaceStore((s) => s.channelGate);
  const attentionCount = useWorkspaceStore((s) => s.attentionCount);

  const [filter, setFilter] = useState<typeof FILTERS[number]['id']>('all');
  const [kindFilter, setKindFilter] = useState<EventKind | null>(null);
  const [fromDate, setFromDate] = useState<string>('');
  const [toDate, setToDate] = useState<string>('');
  const [pageSize, setPageSize] = useState<number>(20);
  const [page, setPage] = useState<number>(1);
  const [openId, setOpenId] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

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
    () => (kindFilter ? filtered.filter((e) => e.kind === kindFilter) : filtered),
    [filtered, kindFilter],
  );

  const ranged = useMemo(
    () => dateFilter(kindFiltered, fromDate, toDate),
    [kindFiltered, fromDate, toDate],
  );

  // Reset stale per-kind selection when it leaves the visible set (group
  // changed, history filter swapped, store refresh). Without this the
  // chip stays "selected" but matches zero events — looks broken.
  useEffect(() => {
    if (kindFilter && !kindCounts.has(kindFilter)) {
      setKindFilter(null);
    }
  }, [kindFilter, kindCounts]);

  const totalPages = Math.max(1, Math.ceil(ranged.length / pageSize));
  const safePage = Math.min(page, totalPages);
  const paginated = useMemo(
    () => ranged.slice((safePage - 1) * pageSize, safePage * pageSize),
    [ranged, safePage, pageSize],
  );

  const attention = attentionCount();
  const pendingCount = events.length;

  const onDecide = async (
    event_id: string,
    decision: 'approve' | 'reject' | 'resolve' | 'delete',
    reason?: string,
  ) => {
    setBusyId(event_id);
    try {
      await decide(event_id, decision, reason);
      if (openId === event_id) setOpenId(null);
    } finally {
      setBusyId(null);
    }
  };

  const onChannelGate = async (
    event_id: string,
    action: 'approve_next_turn' | 'reject_and_message',
  ) => {
    let reply: string | undefined;
    if (action === 'reject_and_message') {
      // Operator can either accept the templated decline (empty input)
      // or type a custom one-line reply. ``prompt`` is the smallest
      // viable surface; a richer composer can replace this when CR-5
      // metrics ask for it.
      const custom = window.prompt(
        'Reply to the remote user (leave blank for the default decline template):',
        '',
      );
      if (custom === null) return; // cancelled
      reply = custom.trim() || undefined;
    }
    setBusyId(event_id);
    try {
      await channelGate(event_id, action, reply);
      if (openId === event_id) setOpenId(null);
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="workspace-view">
      <header className="workspace-view-head">
        <div className="workspace-view-title-row">
          <h1 className="t-head workspace-view-title">Workspace</h1>
          <span className="t-meta workspace-view-meta">
            {pendingCount === 0
              ? 'Inbox clear'
              : `${pendingCount} pending${attention ? ` · ${attention} waiting >24h` : ''}`}
          </span>
        </div>
        <button
          type="button"
          className="workspace-view-refresh"
          onClick={() => {
            if (filter === 'history') {
              fetchHistory();
            } else {
              fetchInbox();
            }
            fetchSeen();
          }}
          disabled={loading || loadingHistory}
          aria-label="Refresh inbox"
        >
          {loading || loadingHistory ? '…' : 'refresh'}
        </button>
      </header>

      <div className="workspace-composer-row">
        <NewThreadButton buttonLabel="New" />
        <VoiceComposer />
      </div>

      <div className="workspace-view-filters" role="tablist">
        {FILTERS.map((f) => (
          <button
            key={f.id}
            type="button"
            role="tab"
            aria-selected={filter === f.id}
            className={`workspace-filter${filter === f.id ? ' is-active' : ''}`}
            onClick={() => { setFilter(f.id); setKindFilter(null); setPage(1); }}
          >
            {f.label}
          </button>
        ))}
      </div>

      {kindCounts.size > 1 && (
        <div className="workspace-view-kind-chips" role="tablist" aria-label="Filter by event kind">
          <button
            type="button"
            role="tab"
            aria-selected={kindFilter === null}
            className={`workspace-filter${kindFilter === null ? ' is-active' : ''}`}
            onClick={() => { setKindFilter(null); setPage(1); }}
          >
            All <span className="workspace-filter-count">{filtered.length}</span>
          </button>
          {Array.from(kindCounts.entries())
            .sort((a, b) => b[1] - a[1])
            .map(([kind, count]) => (
              <button
                key={kind}
                type="button"
                role="tab"
                aria-selected={kindFilter === kind}
                className={`workspace-filter${kindFilter === kind ? ' is-active' : ''}`}
                onClick={() => { setKindFilter(kind); setPage(1); }}
              >
                {KIND_LABEL[kind] ?? kind}{' '}
                <span className="workspace-filter-count">{count}</span>
              </button>
            ))}
        </div>
      )}

      <div className="workspace-view-controls">
        <label className="workspace-control">
          <span className="t-meta">From</span>
          <input
            className="workspace-control-date"
            type="date"
            value={fromDate}
            max={toDate || undefined}
            onChange={(e) => { setFromDate(e.target.value); setPage(1); }}
          />
        </label>
        <label className="workspace-control">
          <span className="t-meta">To</span>
          <input
            className="workspace-control-date"
            type="date"
            value={toDate}
            min={fromDate || undefined}
            onChange={(e) => { setToDate(e.target.value); setPage(1); }}
          />
        </label>
        {(fromDate || toDate) && (
          <button
            type="button"
            className="workspace-control-clear"
            onClick={() => { setFromDate(''); setToDate(''); setPage(1); }}
          >
            Clear dates
          </button>
        )}
        <label className="workspace-control">
          <span className="t-meta">Per page</span>
          <select
            className="workspace-control-select"
            value={pageSize}
            onChange={(e) => { setPageSize(Number(e.target.value)); setPage(1); }}
          >
            {PAGE_SIZES.map((n) => (
              <option key={n} value={n}>{n}</option>
            ))}
          </select>
        </label>
        <span className="workspace-control-count t-meta">
          {ranged.length} of {filtered.length} shown
        </span>
      </div>

      {attention > 0 && (
        <div className="workspace-attention-banner t-caption">
          {attention} item{attention === 1 ? '' : 's'} waiting &gt; 24h
        </div>
      )}

      {lastError && <div className="workspace-error t-caption">{lastError}</div>}

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
            // sourced by the channel gate. Surface "Approve next turn" +
            // "Reject & message user" instead of the generic Resolve verb so
            // the operator can drive the round-trip from one place.
            const gatePayload =
              ev.kind === 'agent_post' &&
              typeof ev.payload === 'object' &&
              ev.payload !== null &&
              typeof (ev.payload as { channel?: unknown }).channel === 'string'
                ? (ev.payload as {
                    channel: string;
                    chat_id?: string;
                    tool?: string;
                  })
                : null;
            return (
              <article
                key={ev.event_id}
                className={`workspace-event${unread ? ' is-unread' : ''}${isOpen ? ' is-open' : ''}`}
              >
                <button
                  type="button"
                  id={triggerId}
                  className="workspace-event-trigger"
                  aria-expanded={isOpen}
                  aria-controls={detailsId}
                  onClick={toggle}
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
                </button>
                <div className="workspace-event-actions">
                  {ev.status === 'pending' && (
                    isActionable ? (
                      <>
                        <button
                          type="button"
                          className="workspace-action workspace-action--approve"
                          onClick={() => onDecide(ev.event_id, 'approve')}
                          disabled={busyId === ev.event_id}
                          title={ev.kind === 'agent_approval'
                            ? 'Move the agent from pending/ into the active set'
                            : undefined}
                        >
                          {ev.kind === 'agent_approval' || ev.kind === 'skill_approval'
                            ? 'Promote'
                            : ev.kind === 'skill_refinement'
                            ? 'Apply'
                            : 'Approve'}
                        </button>
                        <button
                          type="button"
                          className="workspace-action workspace-action--reject"
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
                        </button>
                      </>
                    ) : gatePayload ? (
                      <>
                        <button
                          type="button"
                          className="workspace-action workspace-action--approve"
                          onClick={() => onChannelGate(ev.event_id, 'approve_next_turn')}
                          disabled={busyId === ev.event_id}
                          title={`Allow ${gatePayload.tool ?? 'this tool'} on the next channel turn`}
                        >
                          Approve next turn
                        </button>
                        <button
                          type="button"
                          className="workspace-action workspace-action--reject"
                          onClick={() => onChannelGate(ev.event_id, 'reject_and_message')}
                          disabled={busyId === ev.event_id}
                          title="Send a templated decline reply to the remote user"
                        >
                          Reject & message user
                        </button>
                      </>
                    ) : (
                      <button
                        type="button"
                        className="workspace-action workspace-action--resolve"
                        onClick={() => onDecide(ev.event_id, 'resolve')}
                        disabled={busyId === ev.event_id}
                      >
                        Resolve
                      </button>
                    )
                  )}
                  {ev.status !== 'deleted' && (
                    <button
                      type="button"
                      className="workspace-action workspace-action--delete"
                      onClick={() => onDecide(ev.event_id, 'delete')}
                      disabled={busyId === ev.event_id}
                    >
                      Delete
                    </button>
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
          <button
            type="button"
            className="workspace-pagination-btn"
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            disabled={safePage === 1}
          >
            Prev
          </button>
          <span className="t-meta">Page {safePage} of {totalPages}</span>
          <button
            type="button"
            className="workspace-pagination-btn"
            onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
            disabled={safePage === totalPages}
          >
            Next
          </button>
        </div>
      )}
    </div>
  );
}

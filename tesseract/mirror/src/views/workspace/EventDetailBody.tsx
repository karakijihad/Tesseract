import type { WorkspaceEvent } from '../../stores/workspace';
import { linkifyText } from '../../lib/linkify';
import { ChatMarkdown } from '../../components/chat/ChatMarkdown';
import { DailyBriefBody } from './DailyBriefBody';
import { PathPill } from './PathPill';

interface Props {
  event: WorkspaceEvent;
}

function asString(v: unknown): string | null {
  if (typeof v === 'string') return v;
  return null;
}

function asNumber(v: unknown): number | null {
  if (typeof v === 'number' && Number.isFinite(v)) return v;
  return null;
}

function asStringList(v: unknown): string[] | null {
  if (!Array.isArray(v)) return null;
  return v.map((x) => (typeof x === 'string' ? x : JSON.stringify(x)));
}

function DiffBlock({ diff }: { diff: string }) {
  const lines = diff.split('\n');
  return (
    <pre className="workspace-diff" aria-label="Unified diff">
      {lines.map((line, i) => {
        let cls = 'workspace-diff-line';
        if (line.startsWith('+++') || line.startsWith('---')) cls += ' is-meta';
        else if (line.startsWith('@@')) cls += ' is-hunk';
        else if (line.startsWith('+')) cls += ' is-add';
        else if (line.startsWith('-')) cls += ' is-del';
        return (
          <span key={i} className={cls}>
            {line || ' '}
            {'\n'}
          </span>
        );
      })}
    </pre>
  );
}

function ChangeProposalBody({ payload }: { payload: Record<string, unknown> }) {
  const label = asString(payload.label) ?? asString(payload.target_path) ?? 'unknown';
  const action = asString(payload.action) ?? 'unknown';
  const target = asString(payload.target_path) ?? '';
  const diff = asString(payload.diff);
  const bytesBefore = asNumber(payload.bytes_before);
  const bytesAfter = asNumber(payload.bytes_after);
  const section = asString(payload.section);
  return (
    <div className="workspace-detail-body">
      <dl className="workspace-detail-dl">
        <dt className="t-meta">target</dt>
        <dd>
          {label}
          {target && (
            <>
              {' '}
              <PathPill path={target} />
            </>
          )}
        </dd>
        <dt className="t-meta">action</dt>
        <dd>
          {action}
          {section && <span className="t-meta"> · §{section}</span>}
        </dd>
        {bytesBefore !== null && bytesAfter !== null && (
          <>
            <dt className="t-meta">size</dt>
            <dd className="t-meta">
              {bytesBefore} → {bytesAfter} bytes
            </dd>
          </>
        )}
      </dl>
      {diff ? <DiffBlock diff={diff} /> : <p className="t-meta">No diff available.</p>}
    </div>
  );
}

function SoulProposalBody({ payload }: { payload: Record<string, unknown> }) {
  const bullet = asString(payload.bullet) ?? '';
  const action = asString(payload.action) ?? 'propose_soul_growth';
  const supporting = asStringList(payload.supporting_ids) ?? [];
  return (
    <div className="workspace-detail-body">
      <dl className="workspace-detail-dl">
        <dt className="t-meta">bullet</dt>
        <dd>{bullet}</dd>
        <dt className="t-meta">action</dt>
        <dd>{action}</dd>
        {supporting.length > 0 && (
          <>
            <dt className="t-meta">supporting</dt>
            <dd className="t-meta">{supporting.join(', ')}</dd>
          </>
        )}
      </dl>
    </div>
  );
}

interface ReflectionSave {
  tool?: unknown;
  title?: unknown;
  snippet?: unknown;
  status?: unknown;
  memory_id?: unknown;
  path?: unknown;
  target_path?: unknown;
  event_id?: unknown;
}

function SessionReflectionBody({ payload }: { payload: Record<string, unknown> }) {
  const savesRaw = Array.isArray(payload.saves) ? (payload.saves as ReflectionSave[]) : [];
  const reason = asString(payload.reason) ?? '';
  const label = asString(payload.label) ?? '';
  const errorType = asString(payload.error_type);
  const count = asNumber(payload.saves_count) ?? savesRaw.length;
  if (errorType) {
    return (
      <div className="workspace-detail-body">
        <dl className="workspace-detail-dl">
          <dt className="t-meta">error</dt>
          <dd>{errorType}</dd>
          {reason && (
            <>
              <dt className="t-meta">reason</dt>
              <dd className="t-meta">{reason}</dd>
            </>
          )}
        </dl>
      </div>
    );
  }
  return (
    <div className="workspace-detail-body">
      <dl className="workspace-detail-dl">
        <dt className="t-meta">writes</dt>
        <dd>{count}</dd>
        {label && (
          <>
            <dt className="t-meta">trigger</dt>
            <dd className="t-meta">{label}</dd>
          </>
        )}
        {reason && reason !== label && (
          <>
            <dt className="t-meta">reason</dt>
            <dd className="t-meta">{reason}</dd>
          </>
        )}
      </dl>
      {savesRaw.length > 0 ? (
        <ul className="workspace-detail-list">
          {savesRaw.map((s, i) => {
            const tool = asString(s.tool) ?? '?';
            const title = asString(s.title) ?? '(no title)';
            const snippet = asString(s.snippet) ?? '';
            const status = asString(s.status) ?? '';
            const memoryId = asString(s.memory_id) ?? '';
            const path = asString(s.path) ?? asString(s.target_path) ?? '';
            return (
              <li key={i} className="workspace-detail-list-item">
                <div>
                  <span className="t-meta">[{tool}]</span> <strong>{title}</strong>
                  {status && status !== 'saved' && status !== 'completed' && (
                    <span className="workspace-detail-status t-meta"> · {status}</span>
                  )}
                </div>
                {snippet && <div className="workspace-detail-snippet t-meta">{snippet}</div>}
                {(path || memoryId) && (
                  <div className="workspace-detail-meta-row">
                    {path && <PathPill path={path} />}
                    {memoryId && (
                      <span className="workspace-detail-id t-meta">id: {memoryId}</span>
                    )}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="t-meta">Nothing load-bearing to save.</p>
      )}
    </div>
  );
}

interface MissionMemoryEntry {
  id?: unknown;
  path?: unknown;
  title?: unknown;
  content?: unknown;
}

function ReflectionProposalBody({
  payload,
  fallbackSummary,
}: {
  payload: Record<string, unknown>;
  fallbackSummary: string;
}) {
  const missionId = asString(payload.mission_id) ?? '';
  // Old events (pre-2026-05-10) don't have `summary_md` in the payload —
  // the only carrier was `event.summary`, which is the truncated card
  // preview. Better to show the truncated text than nothing while old
  // mission_reflection_proposals are still in the inbox.
  const summaryMd = asString(payload.summary_md) ?? fallbackSummary ?? '';
  const memSaves = asNumber(payload.memory_saves_count);
  const driftNotes = asNumber(payload.drift_notes_count);
  const agentImps = asNumber(payload.agent_improvements_count);
  const memoryItems = Array.isArray(payload.memory_saves)
    ? (payload.memory_saves as MissionMemoryEntry[])
    : [];
  const driftItems = asStringList(payload.drift_notes) ?? [];
  const agentImpItems = asStringList(payload.agent_improvements) ?? [];
  return (
    <div className="workspace-detail-body">
      {summaryMd && (
        <>
          <h4 className="workspace-detail-section-head t-meta">Reflection</h4>
          <pre className="workspace-detail-summary-md">{summaryMd}</pre>
        </>
      )}
      <dl className="workspace-detail-dl">
        <dt className="t-meta">mission</dt>
        <dd>{missionId || <span className="t-meta">—</span>}</dd>
        {memSaves !== null && (
          <>
            <dt className="t-meta">memory saves</dt>
            <dd>{memSaves}</dd>
          </>
        )}
        {driftNotes !== null && (
          <>
            <dt className="t-meta">drift notes</dt>
            <dd>{driftNotes}</dd>
          </>
        )}
        {agentImps !== null && (
          <>
            <dt className="t-meta">agent improvements</dt>
            <dd>{agentImps}</dd>
          </>
        )}
      </dl>
      {memoryItems.length > 0 && (
        <>
          <h4 className="workspace-detail-section-head t-meta">Memory writes on approve</h4>
          <ul className="workspace-detail-list">
            {memoryItems.map((m, i) => {
              const id = asString(m.id) ?? `#${i}`;
              const path = asString(m.path) ?? '';
              const title = asString(m.title) ?? '(no title)';
              const content = asString(m.content) ?? '';
              return (
                <li key={id} className="workspace-detail-list-item">
                  <div>
                    <strong>{title}</strong>
                  </div>
                  {content && <div className="workspace-detail-snippet t-meta"><ChatMarkdown>{content}</ChatMarkdown></div>}
                  {path && (
                    <div className="workspace-detail-meta-row">
                      <PathPill path={path} />
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        </>
      )}
      {driftItems.length > 0 && (
        <>
          <h4 className="workspace-detail-section-head t-meta">Drift notes</h4>
          <ul className="workspace-detail-list">
            {driftItems.map((note, i) => (
              <li key={i} className="workspace-detail-list-item">{note}</li>
            ))}
          </ul>
        </>
      )}
      {agentImpItems.length > 0 && (
        <>
          <h4 className="workspace-detail-section-head t-meta">Agent improvements</h4>
          <ul className="workspace-detail-list">
            {agentImpItems.map((note, i) => (
              <li key={i} className="workspace-detail-list-item">{note}</li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

function NudgeBody({ payload }: { payload: Record<string, unknown> }) {
  const promoted = asStringList(payload.promoted) ?? [];
  if (promoted.length === 0) {
    return <p className="t-meta workspace-detail-body">No promoted memories.</p>;
  }
  return (
    <div className="workspace-detail-body">
      <ul className="workspace-detail-list">
        {promoted.map((id) => (
          <li key={id} className="workspace-detail-list-item">{id}</li>
        ))}
      </ul>
    </div>
  );
}

function FeedbackProposalBody({ payload }: { payload: Record<string, unknown> }) {
  const action = asString(payload.action) ?? '';
  const keep = asString(payload.keep);
  const absorb = asStringList(payload.absorb);
  const memoryId = asString(payload.memory_id);
  return (
    <div className="workspace-detail-body">
      <dl className="workspace-detail-dl">
        <dt className="t-meta">action</dt>
        <dd>{action}</dd>
        {keep && (
          <>
            <dt className="t-meta">keep</dt>
            <dd>{keep}</dd>
          </>
        )}
        {absorb && absorb.length > 0 && (
          <>
            <dt className="t-meta">absorb</dt>
            <dd className="t-meta">{absorb.join(', ')}</dd>
          </>
        )}
        {memoryId && (
          <>
            <dt className="t-meta">memory_id</dt>
            <dd>{memoryId}</dd>
          </>
        )}
      </dl>
    </div>
  );
}

function OperatorPostBody({ payload }: { payload: Record<string, unknown> }) {
  const body = asString(payload.body) ?? '';
  const source = asString(payload.source) ?? 'unknown';
  return (
    <div className="workspace-detail-body">
      <dl className="workspace-detail-dl">
        <dt className="t-meta">source</dt>
        <dd>{source}</dd>
      </dl>
      {body ? (
        <pre className="workspace-detail-raw-pre">{linkifyText(body)}</pre>
      ) : (
        <p className="t-meta">Empty post.</p>
      )}
    </div>
  );
}

function AgentApprovalBody({ payload }: { payload: Record<string, unknown> }) {
  const name = asString(payload.name) ?? '';
  const modelRole = asString(payload.model_role) ?? '';
  const proposer = asString(payload.proposer) ?? '';
  const rationale = asString(payload.rationale) ?? '';
  const rendered = asString(payload.rendered_markdown) ?? '';
  return (
    <div className="workspace-detail-body">
      <dl className="workspace-detail-dl">
        <dt className="t-meta">agent</dt>
        <dd>{name || <span className="t-meta">—</span>}</dd>
        {modelRole && (
          <>
            <dt className="t-meta">model role</dt>
            <dd>{modelRole}</dd>
          </>
        )}
        {proposer && (
          <>
            <dt className="t-meta">proposed by</dt>
            <dd>{proposer}</dd>
          </>
        )}
        {rationale && (
          <>
            <dt className="t-meta">rationale</dt>
            <dd>{rationale}</dd>
          </>
        )}
      </dl>
      {rendered ? (
        <>
          <h4 className="workspace-detail-section-head t-meta">Agent card</h4>
          <pre className="workspace-event-agent-pre">{rendered}</pre>
        </>
      ) : (
        <p className="t-meta">No rendered card in this proposal.</p>
      )}
    </div>
  );
}

function SkillApprovalBody({ payload }: { payload: Record<string, unknown> }) {
  const name = asString(payload.name) ?? '';
  const description = asString(payload.description) ?? '';
  const proposer = asString(payload.proposer) ?? '';
  const rationale = asString(payload.rationale) ?? '';
  const rendered = asString(payload.rendered_markdown) ?? '';
  return (
    <div className="workspace-detail-body">
      <dl className="workspace-detail-dl">
        <dt className="t-meta">skill</dt>
        <dd>{name || <span className="t-meta">—</span>}</dd>
        {description && (
          <>
            <dt className="t-meta">description</dt>
            <dd>{description}</dd>
          </>
        )}
        {proposer && (
          <>
            <dt className="t-meta">proposed by</dt>
            <dd>{proposer}</dd>
          </>
        )}
        {rationale && (
          <>
            <dt className="t-meta">rationale</dt>
            <dd>{rationale}</dd>
          </>
        )}
      </dl>
      {rendered ? (
        <>
          <h4 className="workspace-detail-section-head t-meta">SKILL.md</h4>
          <pre className="workspace-event-agent-pre">{rendered}</pre>
        </>
      ) : (
        <p className="t-meta">No rendered card in this proposal.</p>
      )}
    </div>
  );
}

function SkillRefinementBody({ payload }: { payload: Record<string, unknown> }) {
  const name = asString(payload.name) ?? '';
  const stats = payload.stats as { total?: number; negative?: number } | undefined;
  const total = asNumber(stats?.total) ?? 0;
  const negative = asNumber(stats?.negative) ?? 0;
  const proposedMarkdown = asString(payload.proposed_markdown) ?? '';
  const currentMarkdown = asString(payload.current_markdown) ?? '';
  return (
    <div className="workspace-detail-body">
      <dl className="workspace-detail-dl">
        <dt className="t-meta">skill</dt>
        <dd>{name || <span className="t-meta">—</span>}</dd>
        <dt className="t-meta">stats</dt>
        <dd>{negative}/{total} loads ended in error/correction</dd>
      </dl>
      {proposedMarkdown ? (
        <>
          <h4 className="workspace-detail-section-head t-meta">Proposed SKILL.md</h4>
          <pre className="workspace-event-agent-pre">{proposedMarkdown}</pre>
        </>
      ) : (
        <p className="t-meta">No automated proposal — refine manually.</p>
      )}
      {currentMarkdown && (
        <>
          <h4 className="workspace-detail-section-head t-meta">Current SKILL.md</h4>
          <pre className="workspace-event-agent-pre">{currentMarkdown}</pre>
        </>
      )}
    </div>
  );
}

function RawBody({ payload }: { payload: Record<string, unknown> }) {
  if (Object.keys(payload).length === 0) {
    return <p className="t-meta workspace-detail-body">No payload.</p>;
  }
  return (
    <details className="workspace-detail-raw">
      <summary className="t-meta">Raw payload</summary>
      <pre className="workspace-detail-raw-pre">
        {JSON.stringify(payload, null, 2)}
      </pre>
    </details>
  );
}

export function EventDetailBody({ event }: Props) {
  const payload = event.payload || {};
  switch (event.kind) {
    case 'change_proposal':
      return <ChangeProposalBody payload={payload} />;
    case 'soul_proposal':
      return <SoulProposalBody payload={payload} />;
    case 'reflection_proposal':
      return <SessionReflectionBody payload={payload} />;
    case 'mission_reflection_proposal':
      return (
        <ReflectionProposalBody payload={payload} fallbackSummary={event.summary} />
      );
    case 'nudge':
      return <NudgeBody payload={payload} />;
    case 'feedback_proposal':
    case 'feedback_sweep':
      return <FeedbackProposalBody payload={payload} />;
    case 'operator_post':
      return <OperatorPostBody payload={payload} />;
    case 'agent_approval':
      return <AgentApprovalBody payload={payload} />;
    case 'skill_approval':
      return <SkillApprovalBody payload={payload} />;
    case 'skill_refinement':
      return <SkillRefinementBody payload={payload} />;
    case 'daily_brief':
      return <DailyBriefBody payload={payload} />;
    default:
      return <RawBody payload={payload} />;
  }
}

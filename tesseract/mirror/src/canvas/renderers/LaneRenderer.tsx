// CV-1 — Surface Protocol `lane` renderer. Renders a live Claude/Codex
// controller lane on the canvas: typed event stream + status header +
// follow-up input. The lane authority is the controller daemon; this card
// `attach`es on mount (brain-restart recovery) then polls `lane.read`.
//
// Closing the card does NOT close the lane (separation of concerns per the
// lane contract) — the lane survives, only its canvas representation is
// dismissed.

import { useEffect, useRef, useState } from 'react';

import { useLanesStore, type LaneEvent } from '../../stores/lanes';
import { useSurfacesStore } from '../../stores/surfaces';
import type { RendererProps } from './index';

const POLL_MS = 1500;

export function LaneRenderer({ descriptor }: RendererProps) {
  const props = descriptor.props ?? {};
  const laneId = String(props.lane_id ?? '');
  const name = String(props.name ?? descriptor.title ?? laneId);
  const kind = String(props.kind ?? '');
  const model = String(props.model ?? '');

  const attach = useLanesStore((s) => s.attach);
  const poll = useLanesStore((s) => s.poll);
  const send = useLanesStore((s) => s.send);
  const clearLane = useLanesStore((s) => s.clear);
  const closeLane = useLanesStore((s) => s.close);
  const lane = useLanesStore((s) => s.byLane[laneId]);
  const gone = useLanesStore((s) => s.byLane[laneId]?.gone ?? false);

  const resizeSurface = useSurfacesStore((s) => s.resizeSurface);
  const renameSurface = useSurfacesStore((s) => s.renameSurface);
  const closeSurface = useSurfacesStore((s) => s.closeSurface);

  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const [editing, setEditing] = useState(false);
  const [titleDraft, setTitleDraft] = useState('');
  const [confirmDelete, setConfirmDelete] = useState(false);
  // Expanded is DERIVED from the (persisted) descriptor size, so it
  // self-heals across remounts — local boolean state would desync from the
  // store. priorSizeRef snapshots the pre-expand size to restore on collapse.
  const expanded = descriptor.size.w >= 760 && descriptor.size.h >= 560;
  const priorSizeRef = useRef<{ w: number; h: number } | null>(null);
  const confirmTimerRef = useRef<number | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(
    () => () => {
      if (confirmTimerRef.current !== null) window.clearTimeout(confirmTimerRef.current);
    },
    [],
  );

  useEffect(() => {
    if (!laneId || gone) return; // a gone lane no longer exists — stop polling (no 502 storm)
    void attach(laneId);
    const t = window.setInterval(() => void poll(laneId), POLL_MS);
    return () => window.clearInterval(t);
  }, [laneId, gone, attach, poll]);

  // A gone lane with no transcript is a stale card that outlived its lane (e.g.
  // a card persisted across a restart the lane didn't) — dismiss it so it stops
  // cluttering the canvas. Cards WITH history stay so the operator can still
  // read the final transcript; polling has already stopped either way.
  useEffect(() => {
    if (gone && (lane?.events.length ?? 0) === 0) {
      closeSurface(descriptor.view, descriptor.id);
    }
  }, [gone, lane?.events.length, closeSurface, descriptor.view, descriptor.id]);

  // Keep the transcript pinned to the newest event.
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lane?.events.length]);

  const onSend = async () => {
    const msg = draft.trim();
    if (!msg || sending) return;
    setSending(true);
    const ok = await send(laneId, msg);
    setSending(false);
    if (ok) {
      setDraft('');
      void poll(laneId);
    }
  };

  const toggleExpand = () => {
    if (expanded) {
      // Restore the snapshot, or a sane default if it was lost to a remount.
      resizeSurface(descriptor.view, descriptor.id, priorSizeRef.current ?? { w: 440, h: 420 });
    } else {
      priorSizeRef.current = { w: descriptor.size.w, h: descriptor.size.h };
      resizeSurface(descriptor.view, descriptor.id, { w: 760, h: 560 });
    }
  };

  const saveTitle = () => {
    const t = titleDraft.trim();
    if (t) renameSurface(descriptor.view, descriptor.id, t);
    setEditing(false);
  };

  const onDelete = async () => {
    if (!confirmDelete) {
      setConfirmDelete(true);
      if (confirmTimerRef.current !== null) window.clearTimeout(confirmTimerRef.current);
      confirmTimerRef.current = window.setTimeout(() => setConfirmDelete(false), 3000);
      return;
    }
    if (confirmTimerRef.current !== null) window.clearTimeout(confirmTimerRef.current);
    await closeLane(laneId); // terminate the lane (CLI)
    closeSurface(descriptor.view, descriptor.id); // dismiss the card
  };

  if (!laneId) {
    return <div className="lane-card lane-card--empty t-meta">no lane bound</div>;
  }

  const status = lane?.status;
  const busy = status?.busy ?? false;

  return (
    <div className="lane-card" data-lane-kind={kind}>
      <header className="lane-card__head">
        <span className={`lane-card__dot lane-card__dot--${kind}`} aria-hidden="true" />
        {editing ? (
          <input
            className="lane-card__title-edit"
            value={titleDraft}
            autoFocus
            onChange={(e) => setTitleDraft(e.target.value)}
            onBlur={saveTitle}
            onKeyDown={(e) => {
              if (e.key === 'Enter') saveTitle();
              if (e.key === 'Escape') setEditing(false);
            }}
          />
        ) : (
          <span className="lane-card__name">{name}</span>
        )}
        <span className="lane-card__model t-meta">{model}</span>
        <span className={`lane-card__state ${busy ? 'is-busy' : 'is-idle'}`}>
          {lane?.offline ? 'offline' : busy ? 'working…' : 'idle'}
        </span>
        {status && status.queue_depth > 0 ? (
          <span className="lane-card__queue t-meta">+{status.queue_depth} queued</span>
        ) : null}
        <div className="lane-card__actions">
          <button type="button" title="Edit name" aria-label="Edit name"
            onClick={() => { setTitleDraft(descriptor.title ?? name); setEditing(true); }}>✎</button>
          <button type="button" title="Clear transcript" aria-label="Clear transcript"
            onClick={() => clearLane(laneId)}>⌫</button>
          <button type="button" title={expanded ? 'Collapse' : 'Expand'} aria-label="Expand"
            onClick={toggleExpand}>{expanded ? '⤡' : '⤢'}</button>
          <button type="button"
            className={`lane-card__delete${confirmDelete ? ' is-armed' : ''}`}
            title={confirmDelete ? 'Confirm — terminate lane' : 'Delete lane'}
            aria-label="Delete lane"
            onClick={() => void onDelete()}>{confirmDelete ? 'confirm' : '🗑'}</button>
        </div>
      </header>
      {lane?.reattachedAt ? (
        <div className="lane-card__reattach t-meta">
          ● re-attached after brain restart {fmtTime(lane.reattachedAt)}
        </div>
      ) : null}
      <div className="lane-card__stream" ref={scrollRef}>
        {(lane?.events ?? []).map((e, i) => (
          <LaneEventRow key={`${e.cursor ?? i}`} event={e} />
        ))}
        {(lane?.events?.length ?? 0) === 0 ? (
          <div className="lane-card__empty t-meta">no activity yet</div>
        ) : null}
      </div>
      <div className="lane-card__input">
        <textarea
          className="lane-card__draft"
          placeholder={gone ? 'lane closed — no longer reachable' : `Message ${name}…`}
          value={draft}
          rows={2}
          disabled={gone}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              void onSend();
            }
          }}
        />
        <button
          type="button"
          className="lane-card__send"
          disabled={gone || sending || !draft.trim()}
          onClick={() => void onSend()}
        >
          {sending ? '…' : 'Send'}
        </button>
      </div>
    </div>
  );
}

export function LaneEventRow({ event }: { event: LaneEvent }) {
  const p = event.payload ?? {};
  switch (event.kind) {
    case 'assistant_text':
    case 'assistant_text_partial':
      return <div className="lane-ev lane-ev--text">{String(p.text ?? '')}</div>;
    case 'tool_use':
      return (
        <div className="lane-ev lane-ev--tool">
          <span className="lane-ev__tag">tool</span> {String(p.name ?? 'tool')}
          {p.input ? <code className="lane-ev__io">{compact(p.input)}</code> : null}
        </div>
      );
    case 'tool_result':
      return (
        <div className="lane-ev lane-ev--result">
          <span className="lane-ev__tag">result</span>
          <code className="lane-ev__io">{compact(p.output)}</code>
        </div>
      );
    case 'permission_request':
      return (
        <div className="lane-ev lane-ev--ask">
          <span className="lane-ev__tag">ASK</span> {String(p.tool ?? 'permission')}
        </div>
      );
    case 'turn_started': {
      // The turn's prompt is the operator's sent message — render it as a
      // sent bubble so the lane shows the conversation, not just replies.
      const msg = String(p.message ?? '');
      return msg ? <div className="lane-ev lane-ev--sent">{msg}</div> : null;
    }
    case 'turn_ended':
      return <div className="lane-ev lane-ev--turn t-meta">— turn complete —</div>;
    case 'error':
      return <div className="lane-ev lane-ev--error">error: {String(p.message ?? p.error ?? '')}</div>;
    case 'closed':
      return <div className="lane-ev lane-ev--closed t-meta">lane closed</div>;
    case 'status_change':
      return null; // chrome-only transition; reflected in the header
    default:
      return <div className="lane-ev lane-ev--unknown t-meta">{event.kind}</div>;
  }
}

function compact(value: unknown): string {
  try {
    const s = typeof value === 'string' ? value : JSON.stringify(value);
    return s.length > 240 ? `${s.slice(0, 240)}…` : s;
  } catch {
    return String(value);
  }
}

function fmtTime(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  } catch {
    return '';
  }
}

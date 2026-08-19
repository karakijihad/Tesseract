// Y-3 / D-6 — delegate transcript as a Surface Protocol card. Replaces the
// chat-overlay SpawnDrawer: clicking a running spawn chip now spawns one of
// these cards on the cockpit canvas (see canvas/delegateTranscript.ts).
//
// The handle is `descriptor.props.call_id` — the id the backend stamps on
// every cli_start / cli_output / cli_end envelope and that
// `useConversationStore.cliStreams` keys its transcript map on. The card
// reads the prompt (outer ToolCall.input), the live stdout stream, and the
// final result (outer ToolResult.output). Card chrome (title, close, drag,
// resize) is owned by the SurfaceCard wrapper.

import { useEffect, useMemo, useRef, useState } from 'react';

import { useConversationStore } from '../../stores/conversation';
import type { ToolCall, ToolResult } from '../../lib/types';
import type { RendererProps } from './index';
import { Hint } from '../../components/ui/Hint';
import { Disclosure } from '../../components/common/Disclosure';

const VISIBLE_ENTRIES = 200;
const PROMPT_PREVIEW_CHARS = 220;

type Entry =
  | { kind: 'tool_call'; name: string; args: string }
  | { kind: 'tool_output'; text: string }
  | { kind: 'assistant'; text: string };

const TOOL_CALL_RE = /^●\s+([A-Za-z_][\w.]*)\s*\((.*)\)\s*$/;
const TOOL_OUTPUT_RE = /^\s*⎿\s+(.+)$/;

function parseTranscript(lines: string[]): Entry[] {
  const raw = lines.join('').split('\n');
  const out: Entry[] = [];
  let proseBuf: string[] = [];
  const flushProse = () => {
    const joined = proseBuf.join('\n').replace(/\n{3,}/g, '\n\n').trim();
    if (joined) out.push({ kind: 'assistant', text: joined });
    proseBuf = [];
  };
  for (const line of raw) {
    const callMatch = line.match(TOOL_CALL_RE);
    if (callMatch) {
      flushProse();
      out.push({ kind: 'tool_call', name: callMatch[1], args: callMatch[2] });
      continue;
    }
    const outMatch = line.match(TOOL_OUTPUT_RE);
    if (outMatch) {
      flushProse();
      out.push({ kind: 'tool_output', text: outMatch[1] });
      continue;
    }
    proseBuf.push(line);
  }
  flushProse();
  return out;
}

function formatElapsed(startedAt: number): string {
  const sec = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
  if (sec < 60) return `${sec}s`;
  const min = Math.floor(sec / 60);
  return `${min}m ${sec % 60}s`;
}

function findToolCall(
  callId: string,
  current: ToolCall[],
  history: { toolCalls?: ToolCall[] }[],
): ToolCall | undefined {
  for (const tc of current) if (tc.call_id === callId) return tc;
  for (const msg of history) {
    const hit = msg.toolCalls?.find((c) => c.call_id === callId);
    if (hit) return hit;
  }
  return undefined;
}

function findToolResult(
  callId: string,
  current: ToolResult[],
  history: { toolResults?: ToolResult[] }[],
): ToolResult | undefined {
  for (const tr of current) if (tr.call_id === callId) return tr;
  for (const msg of history) {
    const hit = msg.toolResults?.find((r) => r.call_id === callId);
    if (hit) return hit;
  }
  return undefined;
}

function extractPrompt(input: Record<string, unknown> | undefined): string | null {
  if (!input) return null;
  const preferred = ['task', 'prompt', 'instructions', 'message'];
  for (const key of preferred) {
    const v = input[key];
    if (typeof v === 'string' && v.trim()) return v;
  }
  for (const [, v] of Object.entries(input)) {
    if (typeof v === 'string' && v.length > 40) return v;
  }
  return null;
}

function metaChips(input: Record<string, unknown> | undefined): { key: string; value: string }[] {
  if (!input) return [];
  const skipKeys = new Set(['task', 'prompt', 'instructions', 'message']);
  const out: { key: string; value: string }[] = [];
  for (const [k, v] of Object.entries(input)) {
    if (skipKeys.has(k) || v === null || v === undefined) continue;
    if (typeof v === 'string') {
      if (!v) continue;
      out.push({ key: k, value: v.length > 60 ? v.slice(0, 60) + '…' : v });
    } else if (typeof v === 'number' || typeof v === 'boolean') {
      out.push({ key: k, value: String(v) });
    }
  }
  return out;
}

export function DelegateTranscriptRenderer({ descriptor }: RendererProps) {
  const callId = String(descriptor.props?.call_id ?? '');
  const stream = useConversationStore((s) => (callId ? s.getActiveSlice()?.cliStreams.get(callId) : undefined));
  const isBackground = useConversationStore((s) =>
    callId ? (s.getActiveSlice()?.backgroundCalls.has(callId) ?? false) : false,
  );
  const toolCall = useConversationStore((s) => {
    const slice = s.getActiveSlice();
    return callId && slice ? findToolCall(callId, slice.currentToolCalls, slice.messages) : undefined;
  });
  const toolResult = useConversationStore((s) => {
    const slice = s.getActiveSlice();
    return callId && slice ? findToolResult(callId, slice.currentToolResults, slice.messages) : undefined;
  });

  const bodyRef = useRef<HTMLDivElement | null>(null);
  const [, setTick] = useState(0);
  const [promptExpanded, setPromptExpanded] = useState(false);

  const finished = stream?.exit_code !== undefined;
  const lineCount = stream?.lines.length ?? 0;
  const startedAt = stream?.started_at;

  const entries = useMemo(
    () => parseTranscript(stream?.lines ?? []),
    [stream?.lines, stream?.exit_code],
  );

  useEffect(() => {
    const el = bodyRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lineCount, finished, callId]);

  useEffect(() => {
    if (!callId || finished) return;
    const id = window.setInterval(() => setTick((t) => t + 1), 1000);
    return () => window.clearInterval(id);
  }, [callId, finished]);

  if (!callId) {
    return <div className="spawn-card-empty t-meta">No call_id bound to this card.</div>;
  }

  const prompt = extractPrompt(toolCall?.input);
  const chips = metaChips(toolCall?.input);
  const promptIsLong = prompt !== null && prompt.length > PROMPT_PREVIEW_CHARS;
  const visibleEntries = entries.slice(-VISIBLE_ENTRIES);
  const elidedEntries = entries.length - visibleEntries.length;
  const hasStream = (stream?.lines.length ?? 0) > 0;
  const resultText = toolResult?.output ?? '';
  const resultIsError = toolResult?.is_error === true;

  return (
    <div className={`spawn-card${isBackground ? ' is-background' : ''}`}>
      <div className="spawn-drawer-meta t-meta">
        <span>
          {finished
            ? resultIsError || (stream?.exit_code ?? 0) !== 0
              ? `exit ${stream?.exit_code} · error`
              : `exit ${stream?.exit_code}`
            : 'running…'}
        </span>
        {startedAt && (
          <Hint label={new Date(startedAt).toLocaleString()}>
            <span>{formatElapsed(startedAt)}</span>
          </Hint>
        )}
        <span>
          {lineCount} line{lineCount === 1 ? '' : 's'}
        </span>
        <Hint label={callId}>
          <span className="spawn-drawer-callid">
            {callId.slice(0, 8)}
          </span>
        </Hint>
      </div>
      <div ref={bodyRef} className="spawn-drawer-body">
        {prompt !== null && (
          <section className="spawn-drawer-section">
            <div className="spawn-drawer-section-label">Prompt</div>
            <div className="spawn-drawer-prompt">
              {promptIsLong && !promptExpanded
                ? prompt.slice(0, PROMPT_PREVIEW_CHARS).trimEnd() + '…'
                : prompt}
            </div>
            {promptIsLong && (
              <Disclosure
                open={promptExpanded}
                onToggle={() => setPromptExpanded((v) => !v)}
                className="spawn-drawer-expand"
              >
                {promptExpanded ? 'collapse' : 'show full prompt'}
              </Disclosure>
            )}
            {chips.length > 0 && (
              <div className="spawn-drawer-chips">
                {chips.map((c) => (
                  <Hint label={c.value}>
                    <span key={c.key} className="spawn-drawer-chip">
                      <span className="spawn-drawer-chip-key">{c.key}</span>
                      <span className="spawn-drawer-chip-val">{c.value}</span>
                    </span>
                  </Hint>
                ))}
              </div>
            )}
          </section>
        )}

        <section className="spawn-drawer-section">
          <div className="spawn-drawer-section-label">
            {finished ? 'Transcript' : 'Transcript · live'}
          </div>
          {hasStream ? (
            <div className="spawn-drawer-entries">
              {elidedEntries > 0 && (
                <div className="spawn-drawer-elided t-meta">
                  … {elidedEntries} earlier entr{elidedEntries === 1 ? 'y' : 'ies'} elided …
                </div>
              )}
              {visibleEntries.map((entry, i) => (
                <TranscriptEntry key={i} entry={entry} />
              ))}
              {!finished && (
                <div className="spawn-drawer-cursor" aria-hidden>
                  ▍
                </div>
              )}
            </div>
          ) : (
            <div className="spawn-drawer-empty-inline t-meta">
              {finished ? '(no output)' : 'waiting for first chunk…'}
            </div>
          )}
        </section>

        {finished &&
          resultText &&
          resultText.trim() !==
            entries
              .filter((e) => e.kind === 'assistant')
              .map((e) => (e as { text: string }).text)
              .join('\n\n')
              .trim() && (
            <section className="spawn-drawer-section">
              <div className="spawn-drawer-section-label">
                {resultIsError ? 'Result · error' : 'Result'}
              </div>
              <pre className={`spawn-drawer-result${resultIsError ? ' is-error' : ''}`}>
                {resultText}
              </pre>
            </section>
          )}

        {!stream && !toolCall && (
          <div className="spawn-drawer-empty t-meta">Transcript not available for this call.</div>
        )}
      </div>
    </div>
  );
}

function TranscriptEntry({ entry }: { entry: Entry }) {
  if (entry.kind === 'tool_call') {
    return (
      <div className="spawn-drawer-entry is-tool-call">
        <span className="spawn-drawer-entry-glyph" aria-hidden>
          ●
        </span>
        <span className="spawn-drawer-entry-tool">{entry.name}</span>
        <span className="spawn-drawer-entry-args">({entry.args})</span>
      </div>
    );
  }
  if (entry.kind === 'tool_output') {
    return (
      <div className="spawn-drawer-entry is-tool-output">
        <span className="spawn-drawer-entry-glyph" aria-hidden>
          ⎿
        </span>
        <span className="spawn-drawer-entry-text">{entry.text}</span>
      </div>
    );
  }
  return (
    <div className="spawn-drawer-entry is-assistant">
      <pre className="spawn-drawer-entry-prose">{entry.text}</pre>
    </div>
  );
}

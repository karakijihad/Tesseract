import type { AssistantStreamSegment, ToolCall, ToolResult } from './types';
import { Markdown } from '../components/common/Markdown';
import { Note } from '../components/common/Note';
import { ToolCallPill } from '../components/chat/ToolCallPill';
import { splitIntentLines } from './intentSplit';

export function renderIntent(text: string, key: string) {
  const lines = splitIntentLines(text);
  if (!lines.length) return null;
  return (
    <div className="assistant-status-strip" key={key}>
      {lines.map((line, idx) => <div key={`${key}-${idx}`}>{line}</div>)}
    </div>
  );
}

/** `streaming` marks text whose closing delimiter may not have arrived yet, so
 * the renderer trims a dangling `**` instead of printing it. Only ever true for
 * the segment currently being appended to — an earlier, finished segment is
 * complete text, and trimming its odd `*` would corrupt a multiplication. */
export function renderAnswer(text: string, key: string, streaming = false) {
  if (!text) return null;
  return (
    <div className="bubble-md" key={key}>
      <Markdown streaming={streaming}>{text}</Markdown>
    </div>
  );
}

export function renderSegment(
  segment: AssistantStreamSegment,
  idx: number,
  calls: ToolCall[],
  results: ToolResult[],
  streaming = false,
) {
  if (segment.kind === 'intent') return renderIntent(segment.text, `intent-${idx}`);
  // `spoken` is deliberately not rendered: it is the audio track for the
  // `answer` that follows it, and showing both prints the reply twice.
  // OrbCaptions is where it surfaces.
  if (segment.kind === 'spoken') return null;
  if (segment.kind === 'answer') return renderAnswer(segment.text, `answer-${idx}`, streaming);
  if (segment.kind === 'system_note')
    return (
      <Note tone="warn" key={`note-${idx}`}>
        {segment.text}
      </Note>
    );
  if (segment.kind === 'tool_call' && segment.call_id) {
    const call =
      calls.find(c => c.call_id === segment.call_id) ??
      { call_id: segment.call_id, name: segment.name ?? '', input: {} };
    const result = results.find(r => r.call_id === segment.call_id);
    return <ToolCallPill key={`pill-${segment.call_id}-${idx}`} call={call} result={result} />;
  }
  return null;
}

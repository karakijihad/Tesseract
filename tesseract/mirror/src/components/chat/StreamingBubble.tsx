import {
  useConversationStore,
  EMPTY_SEGMENTS,
  EMPTY_TOOL_CALLS,
  EMPTY_TOOL_RESULTS,
} from '../../stores/conversation';
import { ToolCallPill } from './ToolCallPill';
import { renderSegment, renderIntent, renderAnswer } from '../../lib/segmentRender';

export function StreamingBubble() {
  const streamingMessageId = useConversationStore(s => s.getActiveSlice()?.streamingMessageId ?? null);
  const streamingText = useConversationStore(s => s.getActiveSlice()?.streamingText ?? '');
  const streamingStatusText = useConversationStore(s => s.getActiveSlice()?.streamingStatusText ?? '');
  const streamingSegments = useConversationStore(s => s.getActiveSlice()?.streamingSegments ?? EMPTY_SEGMENTS);
  const currentToolCalls = useConversationStore(s => s.getActiveSlice()?.currentToolCalls ?? EMPTY_TOOL_CALLS);
  const currentToolResults = useConversationStore(s => s.getActiveSlice()?.currentToolResults ?? EMPTY_TOOL_RESULTS);

  if (streamingMessageId === null) return null;

  const hasInterleaved = streamingSegments.some(s => s.kind === 'tool_call');

  // Phase 2 (revised 2026-05-11): the standalone queued-message badge
  // was redundant with the operator's own user bubble — `sendUserMessage`
  // already marks mid-turn messages as `status: 'queued'` and
  // MessageBubble renders the dashed border + "queued" pill. Flip
  // happens via the existing `beginTurn` path AND via
  // `stream_user_inject` (Phase 2 inject at next tool boundary).
  return (
    <div className="message-row assistant is-streaming">
      <div className="streaming-bubble">
        {/* Only the last segment is still being appended to; the ones before it
            are finished text and must render exactly as they will after the
            turn ends. */}
        {streamingSegments.length > 0
          ? streamingSegments.map((segment, idx) =>
              renderSegment(segment, idx, currentToolCalls, currentToolResults,
                idx === streamingSegments.length - 1))
          : (
            <>
              {streamingStatusText && renderIntent(streamingStatusText, 'status-fallback')}
              {streamingText && renderAnswer(streamingText, 'answer-fallback', true)}
            </>
          )}
        {!hasInterleaved && currentToolCalls.map((tc, idx) => (
          <ToolCallPill
            key={`trail-${tc.call_id || tc.name}-${idx}`}
            call={tc}
            result={currentToolResults.find(r => r.call_id === tc.call_id)}
          />
        ))}
      </div>
    </div>
  );
}

import { useEffect, useMemo, useRef, useState } from 'react';
import { useVirtualizer } from '@tanstack/react-virtual';
import {
  useConversationStore,
  EMPTY_MESSAGES,
  EMPTY_APPROVALS,
} from '../stores/conversation';
import { MessageBubble } from '../components/chat/MessageBubble';
import { StreamingBubble } from '../components/chat/StreamingBubble';
import { ApprovalCard } from '../components/chat/ApprovalCard';
import { CostOverageCard } from '../components/chat/CostOverageCard';
import { ChatInput } from '../components/chat/ChatInput';
import { ChatManager } from '../components/chat/ChatManager';
import { CommandTips } from '../components/chat/CommandTips';
import { DisconnectedChip } from '../components/chat/DisconnectedChip';
import { TodosCard } from '../components/chat/TodosCard';
import { ActivityTaskbar } from '../components/chat/ActivityTaskbar';
import { CostChip } from '../components/cockpit/hud/CostChip';
import { VoiceCostChip } from '../components/cockpit/hud/VoiceCostChip';
import { useCostStore } from '../stores/cost';
import { useTasksStore } from '../stores/tasks';

const SCROLL_LOCK_THRESHOLD_PX = 40;
// `.chat-scroll` spaces in-flow rows with `gap: 14px` (chat.css). The
// virtualized rows are position:absolute and never receive that flex gap, so
// the spacing is baked into each measured row's padding-bottom instead —
// `measureElement` includes padding, so `translateY` stacks rows 14px apart.
const ROW_GAP_PX = 14;
const ROW_ESTIMATE_PX = 96;

export function ChatView() {
  const messages = useConversationStore(s => s.getActiveSlice()?.messages ?? EMPTY_MESSAGES);
  const pendingApprovals = useConversationStore(s => s.getActiveSlice()?.pendingApprovals ?? EMPTY_APPROVALS);
  const pendingOverageAsks = useCostStore(s => s.pendingOverageAsks);
  const activeChatId = useConversationStore(s => s.activeChatId);
  const isStreaming = useConversationStore(s => s.getActiveSlice()?.isStreaming ?? false);
  const streamingTextLen = useConversationStore(s => s.getActiveSlice()?.streamingText.length ?? 0);
  const streamingStatusTextLen = useConversationStore(s => s.getActiveSlice()?.streamingStatusText.length ?? 0);
  const currentToolCallsLen = useConversationStore(s => s.getActiveSlice()?.currentToolCalls.length ?? 0);
  const currentToolResultsLen = useConversationStore(s => s.getActiveSlice()?.currentToolResults.length ?? 0);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [userScrolledUp, setUserScrolledUp] = useState(false);

  // P5 — each chat opens pinned to its own latest message. Without this, a
  // scroll-lock set in the chat we left would carry over and suppress the
  // scroll-to-bottom for the chat we switched to.
  useEffect(() => {
    setUserScrolledUp(false);
  }, [activeChatId]);

  const { lastAssistantCompleteIdx, previousUserByIdx, hasQueued } = useMemo(() => {
    let lastIdx = -1;
    const prevUser = new Map<number, number>();
    let lastUserIdx = -1;
    let queued = false;
    for (let i = 0; i < messages.length; i += 1) {
      const m = messages[i];
      if (m.status === 'queued') queued = true;
      if (m.role === 'assistant') {
        prevUser.set(i, lastUserIdx);
        if (m.status === 'complete') lastIdx = i;
      } else if (m.role === 'user' && m.status !== 'interrupted') {
        lastUserIdx = i;
      }
    }
    return { lastAssistantCompleteIdx: lastIdx, previousUserByIdx: prevUser, hasQueued: queued };
  }, [messages]);

  // The virtualized list is the (unbounded) non-queued history; the small
  // trailing set of queued bubbles renders normally after it. Each row keeps
  // its ORIGINAL index so renderBubble's previous-user / last-complete
  // bookkeeping stays correct.
  const visibleRows = useMemo(
    () => messages.map((m, idx) => ({ m, idx })).filter(({ m }) => m.status !== 'queued'),
    [messages],
  );
  const queuedRows = useMemo(
    () => messages.map((m, idx) => ({ m, idx })).filter(({ m }) => m.status === 'queued'),
    [messages],
  );

  const rowVirtualizer = useVirtualizer({
    count: visibleRows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_ESTIMATE_PX,
    overscan: 8,
    getItemKey: (index) => visibleRows[index].m.id,
  });

  // Pin to the bottom while the operator hasn't scrolled up. With virtualized
  // dynamic row heights a single scroll can't reach the true bottom of a
  // freshly-loaded (still-unmeasured) history — rows measure incrementally as
  // they scroll into view — so re-assert scroll-to-bottom across animation
  // frames until the scroll height stabilizes: instant for live streaming
  // (prior rows already measured), ~0.5s for a resumed session. Bounded by a
  // frame budget so it can never spin.
  useEffect(() => {
    if (userScrolledUp) return;
    let raf = 0;
    let frames = 0;
    let lastHeight = -1;
    const settle = () => {
      const el = scrollRef.current;
      if (!el) return;
      el.scrollTop = el.scrollHeight;
      frames += 1;
      const h = el.scrollHeight;
      if (h !== lastHeight && frames < 90) {
        lastHeight = h;
        raf = requestAnimationFrame(settle);
      }
    };
    raf = requestAnimationFrame(settle);
    return () => cancelAnimationFrame(raf);
  }, [
    activeChatId,
    messages.length,
    isStreaming,
    pendingApprovals.length,
    streamingTextLen,
    streamingStatusTextLen,
    currentToolCallsLen,
    currentToolResultsLen,
    userScrolledUp,
  ]);

  const handleScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.clientHeight - el.scrollTop;
    setUserScrolledUp(distanceFromBottom > SCROLL_LOCK_THRESHOLD_PX);
  };

  const jumpToLatest = () => {
    // Clearing the scroll-lock re-runs the pin effect above, which settles to
    // the true bottom even through unmeasured virtual rows.
    setUserScrolledUp(false);
  };

  const isEmpty = messages.length === 0 && !isStreaming && pendingApprovals.length === 0;
  const todoCount = useTasksStore(s => s.items.length);

  const renderBubble = (m: typeof messages[number], idx: number) => {
    const prevIdx = previousUserByIdx.get(idx);
    const previousUser = prevIdx !== undefined && prevIdx >= 0 ? messages[prevIdx] : undefined;
    return (
      <MessageBubble
        key={m.id}
        message={m}
        isLastAssistantComplete={idx === lastAssistantCompleteIdx}
        previousUser={previousUser}
        hasQueuedMessage={hasQueued}
      />
    );
  };

  const virtualRows = rowVirtualizer.getVirtualItems();

  return (
    <div className="chat-view">
      <ChatManager />
      <div className="chat-scroll" ref={scrollRef} onScroll={handleScroll}>
        {isEmpty ? (
          <div className="chat-empty-state">
            <div className="chat-empty-headline">Start a conversation</div>
            <CommandTips />
          </div>
        ) : (
          <>
            {/* Virtualized history — only viewport rows (+overscan) hit the DOM. */}
            <div
              style={{
                height: `${rowVirtualizer.getTotalSize()}px`,
                width: '100%',
                position: 'relative',
                flexShrink: 0,
              }}
            >
              {virtualRows.map((vi) => {
                const row = visibleRows[vi.index];
                return (
                  <div
                    key={vi.key}
                    data-index={vi.index}
                    ref={rowVirtualizer.measureElement}
                    style={{
                      position: 'absolute',
                      top: 0,
                      left: 0,
                      width: '100%',
                      transform: `translateY(${vi.start}px)`,
                      // 14px inter-row spacing replaces the flex `gap` that
                      // absolute rows can't receive. The LAST row gets 0 so the
                      // parent's flex gap (to StreamingBubble / cards) isn't
                      // doubled into 28px at the history↔trailing seam.
                      paddingBottom: vi.index === visibleRows.length - 1 ? 0 : ROW_GAP_PX,
                    }}
                  >
                    {renderBubble(row.m, row.idx)}
                  </div>
                );
              })}
            </div>
            {isStreaming && <StreamingBubble />}
            {queuedRows.map(({ m, idx }) => renderBubble(m, idx))}
            {pendingOverageAsks.map((ask) => (
              <CostOverageCard key={ask.call_id} ask={ask} />
            ))}
            {pendingApprovals.map((a, idx) => (
              <ApprovalCard key={a.call_id} approval={a} isPrimary={idx === 0} />
            ))}
          </>
        )}
      </div>
      {userScrolledUp && (
        <button
          type="button"
          className="scroll-to-bottom-btn"
          onClick={jumpToLatest}
          aria-label="Jump to latest"
        >
          ↓ Jump to latest
        </button>
      )}
      <ActivityTaskbar />
      {todoCount > 0 && (
        <div className="chat-todos-strip">
          <TodosCard />
        </div>
      )}
      <DisconnectedChip />
      <div className="chat-cost-strip" role="group" aria-label="Chat cost today">
        <CostChip role="chat_brain" shortLabel="chat" />
        <VoiceCostChip />
      </div>
      <ChatInput variant="inline" />
    </div>
  );
}

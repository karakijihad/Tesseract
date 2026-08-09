// Ambient orb captions — the assistant's latest line as a fading subtitle under the orb,
// so you see the conversation without opening the Chat tab. Voice-first: it
// reads like the assistant speaking on screen. Live during a turn (streamingText), then
// the finalized reply; fades out after a hold once the turn ends. agent-only —
// the operator's own messages are not echoed. Dismissable (HUD toggle).

import { useEffect, useRef, useState } from 'react';

import type { AssistantStreamSegment, ChatMessage } from '../lib/types';
import { useCaptionsStore } from '../stores/captions';
import { useConversationStore } from '../stores/conversation';

// How long the last line lingers after a turn ends before fading out.
const HOLD_MS = 7000;
// Subtitle tail cap — show the newest words of a long answer, not its head.
const MAX_CHARS = 220;

interface LineSource {
  isStreaming: boolean;
  streamingText: string;
  streamingSegments?: AssistantStreamSegment[];
  messages: ChatMessage[];
}

/** Concatenated `spoken` text from a segment timeline, or '' when the reply
 * carried no spoken form. */
function spokenFrom(segments: AssistantStreamSegment[] | undefined): string {
  if (!segments) return '';
  return segments
    .filter(seg => seg.kind === 'spoken')
    .map(seg => seg.text)
    .join('')
    .trim();
}

/** The current the assistant line to caption: the live reply while streaming, else the
 * most recent finalized assistant message. Empty when the assistant hasn't spoken or no
 * chat slice is active yet.
 *
 * A reply carrying a `<spoken>` block captions THAT, not the answer — spoken
 * is what the operator actually hears, and captions that disagree with the
 * audio are worse than no captions. Replies without one caption the answer,
 * exactly as before. */
export function currentAgentLine(s: LineSource | null): string {
  if (!s) return '';
  if (s.isStreaming) {
    const spoken = spokenFrom(s.streamingSegments);
    if (spoken) return spoken;
    if (s.streamingText.trim()) return s.streamingText.trim();
  }
  for (let i = s.messages.length - 1; i >= 0; i--) {
    const m = s.messages[i];
    if (m.role !== 'assistant') continue;
    const spoken = spokenFrom(m.segments);
    if (spoken) return spoken;
    if (m.content.trim()) return m.content.trim();
  }
  return '';
}

/** Keep the newest words visible for long lines (subtitle tail), breaking on a
 * word boundary so the leading ellipsis doesn't slice a word. */
export function tail(line: string): string {
  if (line.length <= MAX_CHARS) return line;
  const cut = line.slice(line.length - MAX_CHARS);
  const space = cut.indexOf(' ');
  // Drop the leading partial word (or a leading space when the window opens
  // right on a boundary); only keep the raw cut when there's no space at all.
  return '…' + (space >= 0 ? cut.slice(space + 1) : cut);
}

export function OrbCaptions() {
  const enabled = useCaptionsStore((s) => s.enabled);
  const line = useConversationStore((s) => currentAgentLine(s.getActiveSlice()));
  const isStreaming = useConversationStore((s) => s.getActiveSlice()?.isStreaming ?? false);
  const [visible, setVisible] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  useEffect(() => {
    if (timer.current) clearTimeout(timer.current);
    if (!enabled || !line) {
      setVisible(false);
      return;
    }
    setVisible(true);
    // Hold visible while the assistant is still speaking; fade out a while after the
    // turn settles.
    if (!isStreaming) {
      timer.current = setTimeout(() => setVisible(false), HOLD_MS);
    }
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, [enabled, line, isStreaming]);

  if (!enabled || !line) return null;
  return (
    <div
      className={`orb-captions${visible ? ' is-visible' : ''}`}
      // Stay silent while the answer streams in (per-delta announcements would
      // flood a screen reader); announce once the turn settles.
      aria-live={isStreaming ? 'off' : 'polite'}
      aria-hidden={visible ? undefined : true}
    >
      <span className="orb-captions__text">{tail(line)}</span>
    </div>
  );
}

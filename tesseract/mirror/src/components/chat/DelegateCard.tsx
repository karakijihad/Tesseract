import { useEffect, useRef } from 'react';
import { useConversationStore } from '../../stores/conversation';
import { useEntityName } from '../../hooks/useEntityName';
import { Hint } from '../ui/Hint';

interface Props {
  call_id: string;
}

const VISIBLE_LINES = 50;

export function DelegateCard({ call_id }: Props) {
  const entityName = useEntityName();
  const stream = useConversationStore(state => state.getActiveSlice()?.cliStreams.get(call_id));
  const isBackground = useConversationStore(state => state.getActiveSlice()?.backgroundCalls.has(call_id) ?? false);
  const outputRef = useRef<HTMLPreElement | null>(null);
  const lineCount = stream?.lines.length ?? 0;

  useEffect(() => {
    const el = outputRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lineCount, stream?.exit_code]);

  if (!stream) return null;

  const visible = stream.lines.slice(-VISIBLE_LINES);
  const trimmed = stream.lines.length - visible.length;
  const finished = stream.exit_code !== undefined;

  return (
    <div className={`delegate-card${isBackground ? ' is-background' : ''}`}>
      <div className="delegate-card-header">
        <span className="delegate-card-tool">{stream.tool}</span>
        {isBackground && (
          <Hint label={`Dispatched in background — ${entityName} can keep working in parallel`}>
            <span className="delegate-card-bg-badge">
              ↻ background
            </span>
          </Hint>
        )}
        <span className="delegate-card-state">
          {finished ? `exit: ${stream.exit_code}` : 'running…'}
        </span>
      </div>
      <pre ref={outputRef} className="delegate-card-output">
        {trimmed > 0 ? `… ${trimmed} earlier line(s) elided …\n` : ''}
        {visible.join('')}
      </pre>
    </div>
  );
}

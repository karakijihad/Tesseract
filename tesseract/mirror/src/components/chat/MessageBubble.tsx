import { memo, useEffect, useRef, useState } from 'react';
import type {
  ChatAttachment,
  ChatMessage,
  MessageStats,
} from '../../lib/types';
import { useConversationStore } from '../../stores/conversation';
import { useIdentityStore } from '../../stores/identity';
import { ENTITY_FALLBACK } from '../../hooks/useEntityName';
import { useWebSocketStore } from '../../stores/websocket';
import { ChatMarkdown } from './ChatMarkdown';
import { ChatPdfPreview } from './ChatPdfPreview';
import { ModelBadge } from './ModelBadge';
import { ToolCallPill } from './ToolCallPill';
import { Hint } from '../ui/Hint';
import { ExpandOverlay } from '../common/ExpandOverlay';
import { copyToClipboard } from '../../lib/clipboard';
import { renderSegment, renderIntent } from '../../lib/segmentRender';

interface Props {
  message: ChatMessage;
  isLastAssistantComplete?: boolean;
  previousUser?: ChatMessage;
  // Computed once by ChatView (folded into its messages memo) and passed down,
  // so the "any queued message?" check runs once per render instead of an
  // O(N) scan inside every bubble on every streaming delta.
  hasQueuedMessage?: boolean;
}

function fmtTime(ts: number): string {
  return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function formatStats(s: MessageStats): { label: string; hitRate: number; title: string } {
  const total = s.input_tokens || 0;
  const cached = s.cached_tokens || 0;
  const hitRate = total > 0 ? Math.round((cached / total) * 100) : 0;
  const label = total > 0
    ? `${total.toLocaleString()} in / ${cached.toLocaleString()} cached (${hitRate}%) / ${s.output_tokens.toLocaleString()} out`
    : '';
  const title = `input ${total}, cached ${cached} (${hitRate}%), output ${s.output_tokens}`;
  return { label, hitRate, title };
}

function reusableAttachments(
  attachments: ChatAttachment[] | undefined,
  sessionId: string | null,
): ChatAttachment[] {
  if (!attachments?.length || !sessionId) return [];
  return attachments.filter(att => att.session_id === sessionId);
}

function MessageBubbleImpl({ message, isLastAssistantComplete = false, previousUser, hasQueuedMessage = false }: Props) {
  const { id, role, content, attachments, statusText, segments, timestamp, toolCalls, toolResults, status, steered } = message;
  const rowCls = `message-row ${role}${status === 'interrupted' ? ' interrupted' : ''}${status === 'queued' ? ' is-queued' : ''}`;
  const cls = `message-bubble ${role}`;
  const renderMarkdown = role === 'assistant' || role === 'user';
  const hasSegments = renderMarkdown && Array.isArray(segments) && segments.length > 0;
  const segmentsHaveToolCalls = hasSegments && segments!.some(s => s.kind === 'tool_call');
  const hasBody = Boolean(content || statusText || hasSegments);
  const modelInfo = useConversationStore(s => s.getActiveSlice()?.messageModel.get(id));
  const stats = useConversationStore(s => s.getActiveSlice()?.messageStats.get(id));
  const isStreaming = useConversationStore(s => s.getActiveSlice()?.isStreaming ?? false);
  const entityName = useIdentityStore(s => s.name);
  const operatorName = useIdentityStore(s => s.operatorName);
  const sessionId = useWebSocketStore(s => s.sessionId);

  const speakerName =
    role === 'assistant' ? (entityName || ENTITY_FALLBACK) :
    role === 'user' ? (operatorName || 'You') :
    role === 'entity' ? (entityName || ENTITY_FALLBACK) :
    role === 'error' ? 'error' :
    '';

  const showMeta = role === 'assistant' && (modelInfo || stats);
  const statsInfo = stats && stats.input_tokens > 0 ? formatStats(stats) : null;
  const reusableCurrentAttachments = reusableAttachments(attachments, sessionId);
  const reusablePreviousAttachments = reusableAttachments(previousUser?.attachments, sessionId);
  const currentAttachmentsReusable = (attachments?.length ?? 0) === reusableCurrentAttachments.length;
  const previousAttachmentsReusable = (previousUser?.attachments?.length ?? 0) === reusablePreviousAttachments.length;
  const canStartReplayTurn = !isStreaming && !hasQueuedMessage;
  const copyText = content || statusText || segments?.map(s => s.text).join('') || '';
  const canRetry = role === 'user'
    && canStartReplayTurn
    && status !== 'queued'
    && status !== 'interrupted'
    && currentAttachmentsReusable
    && (content.trim().length > 0 || reusableCurrentAttachments.length > 0);
  const canRegenerate = Boolean(
    role === 'assistant'
    && canStartReplayTurn
    && isLastAssistantComplete
    && previousUser
    && previousUser.status !== 'queued'
    && previousAttachmentsReusable
    && (previousUser.content.trim().length > 0 || reusablePreviousAttachments.length > 0)
  );

  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(content);
  const editTextareaRef = useRef<HTMLTextAreaElement | null>(null);
  const canEdit = canRetry;

  useEffect(() => {
    if (!editing) return;
    const el = editTextareaRef.current;
    if (!el) return;
    el.focus();
    el.setSelectionRange(el.value.length, el.value.length);
  }, [editing]);

  const copyMessage = () => { void copyToClipboard(copyText); };

  const retryMessage = () => {
    if (!canRetry) return;
    if (!content.trim() && reusableCurrentAttachments.length === 0) return;
    useConversationStore.getState().sendUserMessage(null, content, reusableCurrentAttachments);
  };

  const regenerateMessage = () => {
    if (!canRegenerate || !previousUser) return;
    if (!previousUser.content.trim() && reusablePreviousAttachments.length === 0) return;
    useConversationStore.getState().sendUserMessage(
      null,
      previousUser.content,
      reusablePreviousAttachments,
    );
  };

  const startEdit = () => {
    if (!canEdit) return;
    setDraft(content);
    setEditing(true);
  };

  const cancelEdit = () => {
    setEditing(false);
    setDraft(content);
  };

  const submitEdit = () => {
    const trimmed = draft.trim();
    if (!trimmed && reusableCurrentAttachments.length === 0) return;
    if (!canEdit) return;
    useConversationStore.getState().sendUserMessage(null, trimmed, reusableCurrentAttachments);
    setEditing(false);
  };

  const onEditKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      cancelEdit();
      return;
    }
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      submitEdit();
    }
  };

  return (
    <div className={rowCls}>
      <div className={cls}>
        {speakerName && (
          <div className="bubble-header">
            <span className="bubble-speaker">{speakerName}</span>
            {status === 'queued' && (
              <span className="bubble-queued-pill t-meta" aria-label={`queued - ${entityName || ENTITY_FALLBACK} will read this after the current turn`}>
                queued
              </span>
            )}
            {steered && (
              <span className="bubble-steered-pill t-meta" aria-label="redirected - this text was folded into the current turn">
                redirected
              </span>
            )}
            {showMeta && modelInfo && <ModelBadge info={modelInfo} />}
          </div>
        )}
        {hasBody && !editing && (
          renderMarkdown ? (
            <div className="bubble-md">
              {hasSegments
                ? segments!.map((segment, idx) =>
                    renderSegment(segment, idx, toolCalls ?? [], toolResults ?? []))
                : (
                  <>
                    {statusText && renderIntent(statusText, 'status-fallback')}
                    {content && <ChatMarkdown>{content}</ChatMarkdown>}
                  </>
                )}
            </div>
          ) : (
            <div className="bubble-plain">{content}</div>
          )
        )}
        {editing && (
          <div className="message-edit">
            <textarea
              ref={editTextareaRef}
              className="message-edit-textarea"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={onEditKeyDown}
              aria-label="Edit message"
            />
            <div className="message-edit-actions">
              <button type="button" onClick={cancelEdit} aria-label="Cancel edit">
                Cancel
              </button>
              <button
                type="button"
                className="is-primary"
                onClick={submitEdit}
                disabled={!draft.trim() && reusableCurrentAttachments.length === 0}
                aria-label="Send edited message"
              >
                Send
              </button>
            </div>
          </div>
        )}
        {attachments && attachments.length > 0 && (
          <div className="bubble-attachments">
            {attachments.map(att => (
              att.kind === 'image' ? (
                <ImageAttachmentExpand key={att.id} attachment={att} />
              ) : att.kind === 'pdf' ? (
                <ChatPdfPreview key={att.id} attachment={att} />
              ) : (
                <a
                  key={att.id}
                  className="bubble-attachment-file"
                  href={att.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  aria-label={`Open ${att.filename}`}
                >
                  <span className="bubble-attachment-file-icon">PDF</span>
                  <span className="bubble-attachment-file-name">{att.filename}</span>
                </a>
              )
            ))}
          </div>
        )}
        {!segmentsHaveToolCalls && toolCalls && toolCalls.map((tc, idx) => (
          <ToolCallPill
            key={`trail-${tc.call_id || tc.name}-${idx}`}
            call={tc}
            result={toolResults?.find(r => r.call_id === tc.call_id)}
          />
        ))}
        <div className="bubble-footer">
          {statsInfo && (
            <Hint label={statsInfo.title} maxWidth={260}>
              <span
                className={`bubble-cache-pill ${statsInfo.hitRate >= 50 ? 'is-hot' : statsInfo.hitRate > 0 ? 'is-warm' : 'is-cold'}`}
              >
                {statsInfo.hitRate > 0 ? 'cache' : 'cold'} {statsInfo.label}
              </span>
            </Hint>
          )}
          {!editing && (
            <div className="message-actions" aria-label={`${speakerName || role} message actions`}>
              <Hint label="Copy message">
                <button type="button" onClick={copyMessage} aria-label="Copy message">
                  Copy
                </button>
              </Hint>
              {canEdit && (
                <Hint label="Edit and resend as a new turn">
                  <button type="button" onClick={startEdit} aria-label="Edit message">
                    Edit
                  </button>
                </Hint>
              )}
              {canRetry && (
                <Hint label="Send this prompt again as a new turn">
                  <button type="button" onClick={retryMessage} aria-label="Resend this message">
                    Retry
                  </button>
                </Hint>
              )}
              {canRegenerate && (
                <Hint label="Resend the previous prompt as a new turn">
                  <button type="button" onClick={regenerateMessage} aria-label="Regenerate response">
                    Regenerate
                  </button>
                </Hint>
              )}
            </div>
          )}
          <span className="bubble-time" style={{ textAlign: role === 'user' ? 'right' : 'left' }}>
            {fmtTime(timestamp)}
          </span>
        </div>
      </div>
    </div>
  );
}

// Memoized: during streaming only the active message object changes reference,
// so every other bubble skips re-render instead of re-running on each delta.
export const MessageBubble = memo(MessageBubbleImpl);

function ImageAttachmentExpand({ attachment }: { attachment: ChatAttachment }) {
  const [open, setOpen] = useState(false);
  const handleClick = (e: React.MouseEvent<HTMLAnchorElement>) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
    e.preventDefault();
    setOpen(true);
  };
  return (
    <>
      <a
        className="bubble-attachment-image"
        href={attachment.url}
        target="_blank"
        rel="noopener noreferrer"
        onClick={handleClick}
        aria-label={`Open ${attachment.filename}`}
      >
        <img src={attachment.url} alt={attachment.filename} loading="lazy" />
      </a>
      <ExpandOverlay
        open={open}
        onClose={() => setOpen(false)}
        title={attachment.filename}
      >
        <img src={attachment.url} alt={attachment.filename} />
      </ExpandOverlay>
    </>
  );
}

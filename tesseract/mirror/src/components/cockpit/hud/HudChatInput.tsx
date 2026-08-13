import { useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useEntityName } from '../../../hooks/useEntityName';
import { useStickToBottom } from '../../../hooks/useStickToBottom';
import { useConversationStore, EMPTY_MESSAGES, EMPTY_APPROVALS } from '../../../stores/conversation';
import { useUIStore } from '../../../stores/ui';
import { usePanelStore } from '../../../cockpit/panelStore';
import { dispatchCommand } from '../../../cockpit/commandDispatch';
import { useWebSocketStore } from '../../../stores/websocket';
import { useToastStore } from '../../../stores/toasts';
import { Hint } from '../../ui/Hint';
import { MessageBubble } from '../../chat/MessageBubble';
import { StreamingBubble } from '../../chat/StreamingBubble';
import { ApprovalCard } from '../../chat/ApprovalCard';
import {
  deleteChatAttachment,
  fetchChatUploadConfig,
  uploadChatAttachment,
  type ChatUploadConfig,
} from '../../../lib/api';
import {
  DEFAULT_CHAT_UPLOAD_CONFIG,
  describeUploadHelp,
  normalizeUploadConfig,
  uploadErrorMessage,
  validateSelectedFiles,
} from '../../../lib/uploadValidation';
import type { ChatAttachment, ChatMessage } from '../../../lib/types';
import { lastQueuedPosition, queueChipLabel } from '../../../lib/chatQueue';
import { canSteer } from '../../../lib/steer';

const SCROLLBACK_LIMIT = 12;

function isRenderable(m: ChatMessage): boolean {
  // Match MessageBubble's `hasBody` so the HUD doesn't silently drop
  // statusText-only or segments-only assistant turns the chat tab shows.
  if (m.role !== 'user' && m.role !== 'assistant' && m.role !== 'entity' && m.role !== 'error') {
    return false;
  }
  return Boolean(
    m.content ||
    m.statusText ||
    (Array.isArray(m.segments) && m.segments.length > 0),
  );
}

export function HudChatInput() {
  const entityName = useEntityName();
  const view = useUIStore((s) => s.view);
  const sendUserMessage = useConversationStore((s) => s.sendUserMessage);
  const sessionId = useWebSocketStore((s) => s.sessionId);
  const messages = useConversationStore((s) => s.getActiveSlice()?.messages ?? EMPTY_MESSAGES);
  const streamingMessageId = useConversationStore((s) => s.getActiveSlice()?.streamingMessageId ?? null);
  const streamingText = useConversationStore((s) => s.getActiveSlice()?.streamingText ?? '');
  const pendingApprovals = useConversationStore((s) => s.getActiveSlice()?.pendingApprovals ?? EMPTY_APPROVALS);
  // Q3 — "redirect now" is only live while a turn is actually streaming.
  const isStreaming = useConversationStore((s) => s.getActiveSlice()?.isStreaming ?? false);
  const sendSteer = useConversationStore((s) => s.sendSteer);

  const inputRef = useRef<HTMLInputElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [isOpen, setIsOpen] = useState(false);
  const [draft, setDraft] = useState('');
  const [pendingAttachments, setPendingAttachments] = useState<ChatAttachment[]>([]);
  const [isUploading, setIsUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [uploadConfig, setUploadConfig] = useState<ChatUploadConfig>(DEFAULT_CHAT_UPLOAD_CONFIG);

  // SC-2 — suppress the ambient chat widget whenever the full Chat surface is
  // present: either the chat tab is focused (`view==='chat'`) OR a Chat glass
  // panel is open but another panel sits on top. Without the panel check the
  // widget would re-appear behind/over an open Chat panel → two chat surfaces.
  const isChatView = view === 'chat';
  const chatPanelOpen = usePanelStore((s) => s.panels.find((p) => p.id === 'chat')?.open ?? false);

  useEffect(() => {
    let alive = true;
    fetchChatUploadConfig()
      .then((cfg) => {
        if (alive) setUploadConfig(normalizeUploadConfig(cfg));
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    setPendingAttachments([]);
    setUploadError(null);
    if (fileInputRef.current) fileInputRef.current.value = '';
  }, [sessionId]);

  // Discard pending attachments via functional setState so we always read
  // the latest list (the keyboard-handler closure captures pendingAttachments
  // at attach-time, which would otherwise be stale). Best-effort backend
  // delete + drop from local state — same shape as ChatInput.removeAttachment.
  const closeWidget = () => {
    setPendingAttachments((current) => {
      for (const att of current) {
        void deleteChatAttachment(att).catch(() => undefined);
      }
      return [];
    });
    setUploadError(null);
    if (fileInputRef.current) fileInputRef.current.value = '';
    setIsOpen(false);
    stickToLatest();
  };

  // Switching to the chat tab collapses the widget — chat tab IS the
  // chat surface, so a floating panel would just be a duplicate.
  useEffect(() => {
    if (isChatView) setIsOpen(false);
  }, [isChatView]);

  // Ctrl+/ toggles the widget open and focuses the composer. Esc closes.
  // Both no-op while the chat tab is active (its own input owns focus).
  useEffect(() => {
    function handler(ev: KeyboardEvent) {
      if (isChatView) return;
      if (ev.ctrlKey && ev.key === '/') {
        ev.preventDefault();
        setIsOpen((open) => {
          if (!open) queueMicrotask(() => inputRef.current?.focus());
          return true;
        });
      } else if (ev.key === 'Escape' && isOpen) {
        ev.preventDefault();
        closeWidget();
      }
    }
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [isChatView, isOpen]);

  // Same rule as the chat tab, from the same hook — including the settle
  // loop this panel never had. A single assignment landed short whenever
  // markdown or an image finished measuring after the scroll.
  const { onScroll: handleScroll, stickToLatest } = useStickToBottom(scrollRef, [
    isOpen,
    messages,
    streamingText,
    pendingApprovals.length,
  ]);

  const uploadHelp = useMemo(() => describeUploadHelp(uploadConfig), [uploadConfig]);
  const uploadAccept = useMemo(
    () => uploadConfig.allowed_mime_types.join(','),
    [uploadConfig.allowed_mime_types],
  );

  const recent = useMemo(() => {
    const filtered = messages.filter(isRenderable);
    return filtered.slice(-SCROLLBACK_LIMIT);
  }, [messages]);

  // Q2 frontend — composer-level chip surfacing the FIFO slot of the
  // message the operator most recently sent, so hitting send during a
  // stream gives an immediate "it queued, here is your slot" signal
  // without scrolling to find the bubble.
  const queueLabel = useMemo(
    () => queueChipLabel(lastQueuedPosition(messages)),
    [messages],
  );

  const uploadFiles = async (files: FileList | File[]) => {
    if (isUploading) return;
    if (!sessionId) {
      useToastStore.getState().push('Connect before attaching files', 'warning');
      return;
    }
    const selected = Array.from(files);
    if (!selected.length) return;
    setUploadError(null);
    const maxFiles = uploadConfig.max_files_per_message;
    const available = maxFiles - pendingAttachments.length;
    if (available <= 0) {
      const msg = `Attach limit is ${maxFiles} files`;
      setUploadError(msg);
      useToastStore.getState().push(msg, 'warning');
      return;
    }
    const accepted = selected.slice(0, available);
    const localError = validateSelectedFiles(accepted, pendingAttachments, uploadConfig, uploadHelp);
    if (localError) {
      setUploadError(localError);
      useToastStore.getState().push(localError, 'warning');
      return;
    }
    setIsUploading(true);
    try {
      for (const file of accepted) {
        const uploaded = await uploadChatAttachment(sessionId, file);
        setPendingAttachments((prev) => [...prev, uploaded]);
      }
    } catch (err) {
      const msg = uploadErrorMessage(err, uploadHelp);
      setUploadError(msg);
      useToastStore.getState().push(`Attach failed: ${msg}`, 'warning');
    } finally {
      setIsUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  };

  function submit() {
    const text = draft.trim();
    if (!text && pendingAttachments.length === 0) return;
    // SC-4 — a recognized cockpit command (open a view, spawn the trio, spawn a
    // content surface) is consumed here and never reaches the assistant chat. Only when
    // there are no attachments: a message carrying files is always a chat turn.
    if (text && pendingAttachments.length === 0 && dispatchCommand(text)) {
      setDraft('');
      return;
    }
    sendUserMessage(null, text, pendingAttachments);
    setDraft('');
    setPendingAttachments([]);
    setUploadError(null);
  }

  // Q3 — fold the draft into the CURRENT turn instead of queuing it
  // behind the FIFO (default Enter/Send while streaming). Mirrors
  // ChatInput.handleSteer.
  function handleSteer() {
    if (!canSteer(isStreaming, draft)) return;
    sendSteer(null, draft);
    setDraft('');
  }

  function removeAttachment(id: string) {
    setPendingAttachments((prev) => prev.filter((a) => a.id !== id));
  }

  // Ctrl/Cmd-V into the HUD composer. Mirrors `ChatInput.handlePaste`:
  // if the clipboard carries files, intercept and upload them; otherwise
  // let the browser's default text paste happen so URLs / snippets keep
  // working.
  function handlePaste(e: React.ClipboardEvent<HTMLDivElement>) {
    if (isUploading || !sessionId) return;
    const files = e.clipboardData?.files;
    if (!files || files.length === 0) return;
    e.preventDefault();
    void uploadFiles(files);
  }

  // Hide entirely while the full Chat surface is present (tab focused or panel open).
  if (isChatView || chatPanelOpen) return null;

  const hasAttachments = pendingAttachments.length > 0;
  const showStreaming = streamingMessageId !== null && streamingText.length > 0;

  const toggleButton = (
    <Hint
      label={isOpen ? 'Close (Esc)' : `Ask ${entityName} (Ctrl+/)`}
      position="top"
      maxWidth={160}
    >
      <button
        type="button"
        className={`hud-chat-toggle${isOpen ? ' is-open' : ''}`}
        onClick={() => {
          if (isOpen) {
            closeWidget();
          } else {
            setIsOpen(true);
            queueMicrotask(() => inputRef.current?.focus());
          }
        }}
        aria-expanded={isOpen}
        aria-label={isOpen ? 'Close ambient chat' : 'Open ambient chat'}
      >
        <svg
          viewBox="0 0 20 20"
          width="1em"
          height="1em"
          stroke="currentColor"
          fill="none"
          strokeWidth={1.5}
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <path d="M2 2h16a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H6l-4 4V3a1 1 0 0 1 1-1z" />
          <circle cx="7" cy="8" r="1" fill="currentColor" stroke="none" />
          <circle cx="13" cy="8" r="1" fill="currentColor" stroke="none" />
        </svg>
      </button>
    </Hint>
  );

  if (!isOpen) return toggleButton;

  // Portal the panel to document.body so its `position: fixed` anchors to
  // the viewport. Otherwise the HUD's `backdrop-filter: blur(...)` makes
  // `.cockpit-hud` the containing block (CSS spec: any of transform /
  // filter / backdrop-filter / perspective on an ancestor changes the
  // containing block of fixed-position descendants), and the panel ends
  // up clipped inside the 48px HUD bar with `overflow: hidden`.
  const panel = (
    <div className="hud-chat-panel" role="dialog" aria-label={`Ambient ${entityName} chat`}>
      <header className="hud-chat-panel-head">
        <span className="hud-chat-panel-title">{entityName}</span>
        <button
          type="button"
          className="hud-chat-panel-close"
          onClick={closeWidget}
          aria-label="Close ambient chat"
        >
          ×
        </button>
      </header>
      <div
        className="hud-chat-panel-scroll"
        ref={scrollRef}
        onScroll={handleScroll}
        aria-live="polite"
      >
        {recent.length === 0 && !showStreaming && pendingApprovals.length === 0 && (
          <div className="hud-chat-panel-empty t-meta">
            Same session as the chat tab. Type below.
          </div>
        )}
        {recent.map((m) => (
          <MessageBubble key={m.id} message={m} />
        ))}
        {showStreaming && <StreamingBubble />}
        {pendingApprovals.map((a) => (
          <ApprovalCard
            key={a.call_id}
            approval={a}
            isPrimary={false}
          />
        ))}
      </div>
      {hasAttachments && (
        <div className="hud-chat-attachments" aria-label="Pending attachments">
          {pendingAttachments.map((att) => (
            <span key={att.id} className="hud-chat-attachment-chip t-meta">
              <span className="hud-chat-attachment-name">{att.filename}</span>
              <button
                type="button"
                className="hud-chat-attachment-remove"
                onClick={() => removeAttachment(att.id)}
                aria-label={`Remove ${att.filename}`}
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}
      {uploadError && <div className="hud-chat-upload-error t-meta">{uploadError}</div>}
      {queueLabel && (
        <div className="chat-queue-pill t-meta" aria-label={queueLabel}>{queueLabel}</div>
      )}
      <div className="hud-chat-composer" onPaste={handlePaste}>
        <input
          ref={fileInputRef}
          type="file"
          className="hud-chat-file-input"
          accept={uploadAccept}
          multiple
          onChange={(e) => {
            if (e.currentTarget.files) void uploadFiles(e.currentTarget.files);
          }}
        />
        <Hint label={`Attach files. ${uploadHelp}`} position="top" maxWidth={280}>
          <button
            type="button"
            className="hud-chat-input-attach"
            onClick={() => fileInputRef.current?.click()}
            disabled={isUploading || !sessionId}
            aria-label="Attach files"
          >
            {isUploading ? '…' : '+'}
          </button>
        </Hint>
        <input
          ref={inputRef}
          className="hud-chat-input-field"
          type="text"
          value={draft}
          placeholder={`Ask ${entityName}…`}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
          aria-label={`Ask ${entityName} from any tab`}
        />
        {canSteer(isStreaming, draft) && (
          <Hint
            label="Redirect now — fold this text into the current turn (Enter/Send would queue it for after)"
            position="top"
            maxWidth={220}
          >
            <button
              type="button"
              className="hud-chat-input-steer"
              onClick={handleSteer}
              aria-label="Redirect now"
            >
              <span aria-hidden="true">↪</span>
            </button>
          </Hint>
        )}
        <Hint label="Send" position="top" maxWidth={80}>
          <button
            type="button"
            className="hud-chat-input-send"
            onClick={submit}
            disabled={!draft.trim() && !hasAttachments}
            aria-label="Send message"
          >
            <span aria-hidden="true">↵</span>
          </button>
        </Hint>
      </div>
    </div>
  );

  return (
    <>
      {toggleButton}
      {createPortal(panel, document.body)}
    </>
  );
}

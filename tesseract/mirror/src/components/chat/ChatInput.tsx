import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { useConversationStore, EMPTY_MESSAGES } from '../../stores/conversation';
import { useWebSocketStore } from '../../stores/websocket';
import { useEntityStore } from '../../stores/entity';
import { useVoiceStore } from '../../stores/voice';
import { getController } from '../../lib/entity/registry';
import { matchingCommands, parseSlashInput, stripQuoteEscape, type SlashCommandDef } from '../../lib/slashCommands';
import { sendCommand } from '../../lib/commands';
import { SlashCommandHint, type SlashCommandHintHandle } from './SlashCommandHint';
import { useToastStore } from '../../stores/toasts';
import { useResetDialogStore } from '../../stores/resetDialog';
import { VoiceStatus } from './VoiceStatus';
import type { ChatAttachment, EntityState } from '../../lib/types';
import { lastQueuedPosition, queueChipLabel } from '../../lib/chatQueue';
import { canSteer } from '../../lib/steer';
import { getTtsPlayer } from '../../lib/voice/tts-player';
import {
  deleteChatAttachment,
  fetchChatUploadConfig,
  uploadChatAttachment,
  type ChatUploadConfig,
} from '../../lib/api';
import {
  DEFAULT_CHAT_UPLOAD_CONFIG,
  describeUploadHelp,
  normalizeUploadConfig,
  uploadErrorMessage,
  validateSelectedFiles,
} from '../../lib/uploadValidation';
import { Hint } from '../ui/Hint';
import { backendAssetUrl } from '../../lib/endpoints';
import { CloseButton } from '../common/CloseButton';
import { ComposerButton } from '../common/ComposerButton';
import { FileTrigger, type FileTriggerHandle } from '../common/FileTrigger';

interface Props {
  variant: 'inline' | 'floating';
}

export function ChatInput({ variant }: Props) {
  const [value, setValue] = useState('');
  const composingRef = useRef(false);
  const prevLenRef = useRef(0);
  const prevStateRef = useRef<EntityState>('idle');
  const hintRef = useRef<SlashCommandHintHandle | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<FileTriggerHandle | null>(null);
  const historyIdxRef = useRef<number | null>(null);
  const draftRef = useRef<string>('');
  const [pendingAttachments, setPendingAttachments] = useState<ChatAttachment[]>([]);
  const [isUploading, setIsUploading] = useState(false);
  const [isDragOver, setIsDragOver] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [uploadConfig, setUploadConfig] = useState<ChatUploadConfig>(DEFAULT_CHAT_UPLOAD_CONFIG);
  const isStreaming = useConversationStore(s => s.getActiveSlice()?.isStreaming ?? false);
  const wsStatus = useWebSocketStore(s => s.status);
  const sessionId = useWebSocketStore(s => s.sessionId);
  const messages = useConversationStore(s => s.getActiveSlice()?.messages ?? EMPTY_MESSAGES);
  const voiceMode = useVoiceStore((s) => s.voiceMode);
  const voiceState = useVoiceStore((s) => s.state);
  const partialTranscript = useVoiceStore((s) => s.partialTranscript);
  const notHeard = useVoiceStore((s) => s.notHeard);
  const woken = useVoiceStore((s) => s.woken);
  const history = useMemo(
    () =>
      messages
        .filter(m => m.role === 'user' && typeof m.content === 'string' && m.content.length > 0)
        .map(m => m.content as string),
    [messages]
  );
  // Audit-3 finding #2: typed input stays alive while a turn is streaming.
  // The backend single-slot queue (`ServerSession.pending_user_text`)
  // accepts a follow-up `chat_message` and drains it once the active turn
  // finishes — Codex/Claude-Code "type ahead" behaviour. Only the WS gate
  // disables input now; isStreaming is kept on the variable solely to
  // toggle the Stop/Send button below.
  const disabled = wsStatus !== 'connected';
  // Hint stays mounted as long as the input starts with '/' — the
  // component itself decides whether to render the picker list (no space
  // yet) or the help row for the active command (after the space). The
  // 'list' flag gates keyboard intercept (arrows, Tab/Enter pick); after
  // the space the operator's arrows/Enter must reach the textarea so
  // history navigation and message send work normally.
  const hintVisible = value.startsWith('/');
  const hintIsList = hintVisible && !value.includes(' ');
  // Grey live preview while VAD reports speech in flight. Drives the
  // textarea placeholder when value is empty AND a chip below the textarea
  // when the operator already has typed/dictated text — without the chip
  // path, a follow-up utterance after a committed dictation would silently
  // run with no visible preview (operator caught: "after stop+continue, no
  // more grey showing").
  const livePreview = voiceState === 'speaking_in' ? partialTranscript.trim() : '';
  const transcribePreview = livePreview && !value.trim() ? livePreview : '';
  const previewChip = livePreview && value.trim() ? livePreview : '';
  const uploadHelp = useMemo(() => describeUploadHelp(uploadConfig), [uploadConfig]);
  const uploadAccept = useMemo(
    () => [...uploadConfig.allowed_mime_types, ...uploadConfig.allowed_extensions].join(','),
    [uploadConfig],
  );

  // Q2 frontend — composer-level chip surfacing the FIFO slot of the
  // message the operator most recently sent, so hitting send during a
  // stream gives an immediate "it queued, here is your slot" signal
  // without scrolling to find the bubble.
  const queueLabel = useMemo(
    () => queueChipLabel(lastQueuedPosition(messages)),
    [messages],
  );

  useEffect(() => {
    let alive = true;
    fetchChatUploadConfig()
      .then((cfg) => {
        if (alive) setUploadConfig(normalizeUploadConfig(cfg));
      })
      .catch(() => undefined);
    return () => { alive = false; };
  }, []);

  useEffect(() => {
    setPendingAttachments([]);
    setUploadError(null);
    fileInputRef.current?.reset();
  }, [sessionId]);

  useLayoutEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    // Autosize against the visible content. When the operator hasn't
    // typed yet but the live dictation preview is filling the placeholder,
    // size the textarea against the preview length so a long utterance
    // doesn't get clipped to a single 24px row (operator request
    // 2026-04-29: transcribe-grey preview should expand the input).
    let target = Math.max(el.scrollHeight, 24);
    if (transcribePreview && !value.trim()) {
      const previewWidth = el.clientWidth || 1;
      const charsPerRow = Math.max(1, Math.floor(previewWidth / 8));
      const rows = Math.max(1, Math.ceil(transcribePreview.length / charsPerRow));
      const previewHeight = Math.min(200, rows * 22);
      target = Math.max(target, previewHeight);
    }
    el.style.height = `${target}px`;
  }, [value, transcribePreview]);

  // Mode C — `dispatch.ts` parks transcribed text in `pendingDictation`
  // when the operator commits in `transcribe` mode. Pull it into the
  // local textarea (appending to any existing draft so quick follow-up
  // dictations don't clobber unsent text), focus, then clear.
  const pendingDictation = useVoiceStore((s) => s.pendingDictation);
  useEffect(() => {
    if (!pendingDictation) return;
    setValue((prev) => {
      const sep = prev && !prev.endsWith(' ') && !prev.endsWith('\n') ? ' ' : '';
      const next = prev + sep + pendingDictation;
      prevLenRef.current = next.length;
      return next;
    });
    historyIdxRef.current = null;
    useVoiceStore.getState().setPendingDictation(null);
    queueMicrotask(() => {
      const el = textareaRef.current;
      if (el) {
        el.focus();
        const len = el.value.length;
        el.setSelectionRange(len, len);
      }
    });
  }, [pendingDictation]);

  const applyHistory = (idx: number) => {
    const entry = history[idx] ?? '';
    historyIdxRef.current = idx;
    setValue(entry);
    prevLenRef.current = entry.length;
    queueMicrotask(() => {
      const el = textareaRef.current;
      if (el) el.setSelectionRange(entry.length, entry.length);
    });
  };

  const exitHistory = () => {
    const draft = draftRef.current;
    historyIdxRef.current = null;
    setValue(draft);
    prevLenRef.current = draft.length;
    queueMicrotask(() => {
      const el = textareaRef.current;
      if (el) el.setSelectionRange(draft.length, draft.length);
    });
  };

  const revertListening = () => {
    const entity = useEntityStore.getState();
    if (entity.state === 'listening') {
      entity.setState(prevStateRef.current);
    }
  };

  const handleStop = () => {
    useConversationStore.getState().cancelStream(null);
  };

  const handleStopVoice = () => {
    useConversationStore.getState().stopVoice();
  };

  // Q3 — "redirect now": fold the draft into the CURRENT turn instead of
  // queuing it behind the FIFO (default Enter/Send while streaming). Only
  // ever fires while streaming with a non-empty draft (button is hidden
  // otherwise via `canSteer`).
  const handleSteer = () => {
    if (!canSteer(isStreaming, value)) return;
    useConversationStore.getState().sendSteer(null, value);
    setValue('');
    setUploadError(null);
    prevLenRef.current = 0;
    historyIdxRef.current = null;
    draftRef.current = '';
    revertListening();
  };

  const isSpeakingBack = voiceState === 'speaking_back';
  const showStop = isStreaming || isSpeakingBack;

  const handleSend = () => {
    const trimmed = value.trim();
    if ((!trimmed && pendingAttachments.length === 0) || disabled || isUploading) return;
    // Recover from error state on operator activity so the next loop_start
    // cleanly enters 'thinking'. Without this the orb would briefly remain
    // 'error' until the first stream_text delta of the new turn.
    const entity0 = useEntityStore.getState();
    if (entity0.state === 'error') entity0.setState('idle');
    getController()?.pulseEvent('user');
    if (voiceMode === 'speak') {
      void getTtsPlayer().arm();
      useWebSocketStore.getState().sendMessage('voice_mode_set', { mode: 'speak' });
    }
    const escaped = stripQuoteEscape(trimmed);
    if (escaped !== trimmed) {
      useConversationStore.getState().sendUserMessage(null, escaped, pendingAttachments);
    } else {
      const parsed = parseSlashInput(trimmed);
      if (pendingAttachments.length === 0 && parsed.kind === 'command' && parsed.cmd) {
        // Bare /reset opens the confirm dialog instead of dispatching directly.
        // /reset reflect and /reset clear bypass the dialog (used by the dialog
        // itself + by power users / scripts).
        if (parsed.cmd === '/reset') {
          useResetDialogStore.getState().openDialog();
        } else {
          sendCommand(parsed.cmd);
        }
      } else if (pendingAttachments.length === 0 && trimmed.startsWith('/') && trimmed.length > 1) {
        const head = trimmed.slice(1).split(/\s+/)[0].toLowerCase();
        const suggestions = head ? matchingCommands(head).slice(0, 2) : [];
        const hint = suggestions.length
          ? `Unknown command /${head} — did you mean ${suggestions.map((c) => '/' + c.name).join(' or ')}?`
          : `Unknown command /${head}. To send literally, wrap in quotes: "/${head}"`;
        useToastStore.getState().push(hint);
        return;
      } else {
        useConversationStore.getState().sendUserMessage(null, trimmed, pendingAttachments);
      }
    }
    setValue('');
    setPendingAttachments([]);
    setUploadError(null);
    prevLenRef.current = 0;
    historyIdxRef.current = null;
    draftRef.current = '';
    revertListening();
  };

  const pickCommand = (cmd: SlashCommandDef) => {
    // /reset always opens the confirm dialog, regardless of arg shape.
    if (cmd.name === 'reset') {
      useResetDialogStore.getState().openDialog();
      setValue('');
      prevLenRef.current = 0;
      return;
    }
    if (cmd.takesArg) {
      setValue(`/${cmd.name} `);
      return;
    }
    sendCommand(`/${cmd.name}`);
    setValue('');
    prevLenRef.current = 0;
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (hintIsList && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
      e.preventDefault();
      hintRef.current?.stepFocus(e.key === 'ArrowDown' ? 1 : -1);
      return;
    }
    if (hintIsList && (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey))) {
      const picked = hintRef.current?.selectFocused();
      if (picked) {
        e.preventDefault();
        pickCommand(picked);
        return;
      }
    }
    if (!hintIsList && (e.key === 'ArrowUp' || e.key === 'ArrowDown') && history.length > 0) {
      const el = e.currentTarget;
      const before = el.value.slice(0, el.selectionStart);
      const after = el.value.slice(el.selectionEnd);
      const atFirstLine = !before.includes('\n');
      const atLastLine = !after.includes('\n');
      if (e.key === 'ArrowUp' && atFirstLine) {
        e.preventDefault();
        if (historyIdxRef.current === null) {
          draftRef.current = value;
          applyHistory(history.length - 1);
        } else if (historyIdxRef.current > 0) {
          applyHistory(historyIdxRef.current - 1);
        }
        return;
      }
      if (e.key === 'ArrowDown' && atLastLine && historyIdxRef.current !== null) {
        e.preventDefault();
        const next = historyIdxRef.current + 1;
        if (next >= history.length) {
          exitHistory();
        } else {
          applyHistory(next);
        }
        return;
      }
    }
    if (e.key === 'Enter' && !e.shiftKey && !composingRef.current) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const next = e.target.value;
    const added = next.length - prevLenRef.current;
    if (added > 0) {
      getController()?.getSignals().onUserInput(added);
    }
    prevLenRef.current = next.length;
    if (historyIdxRef.current !== null) {
      historyIdxRef.current = null;
      draftRef.current = next;
    }
    setValue(next);
  };

  const handleFocus = () => {
    if (wsStatus !== 'connected') return;
    const entity = useEntityStore.getState();
    // Flip to listening from any non-active (non-loop-driven) state.
    // Includes agent-sticky states (deep_focus / dreaming / happy) so the
    // operator engaging the input always pulls the orb into listening —
    // otherwise a sticky deep_focus from the previous turn would block
    // the listening cue until the next loop_start fires.
    if (
      entity.state === 'idle' ||
      entity.state === 'error' ||
      entity.state === 'deep_focus' ||
      entity.state === 'dreaming' ||
      entity.state === 'happy'
    ) {
      prevStateRef.current = 'idle';
      entity.setState('listening');
    }
  };

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
    if (accepted.length < selected.length) {
      useToastStore.getState().push(`Only ${maxFiles} files can be attached`, 'warning');
    }
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
        setPendingAttachments(prev => [...prev, uploaded]);
      }
    } catch (err) {
      const msg = uploadErrorMessage(err, uploadHelp);
      setUploadError(msg);
      useToastStore.getState().push(`Attach failed: ${msg}`, 'warning');
    } finally {
      setIsUploading(false);
      fileInputRef.current?.reset();
    }
  };

  const removeAttachment = (att: ChatAttachment) => {
    setPendingAttachments(prev => prev.filter(a => a.id !== att.id));
    void deleteChatAttachment(att).catch(() => undefined);
  };

  const handleDrop = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setIsDragOver(false);
    if (disabled || isUploading) return;
    void uploadFiles(e.dataTransfer.files);
  };

  // Ctrl/Cmd-V into the chat input. We accept any file type the backend
  // accepts (image / pdf / audio / video / arbitrary "document"). Plain
  // text from the clipboard is left to the browser's default paste — we
  // only swallow the event when there are actual files attached, so
  // pasting URLs / text never gets eaten by mistake.
  const handlePaste = (e: React.ClipboardEvent<HTMLDivElement>) => {
    if (disabled || isUploading) return;
    const files = e.clipboardData?.files;
    if (!files || files.length === 0) return;
    e.preventDefault();
    void uploadFiles(files);
  };

  const inputRow = (
    <div className="chat-input-stack">
      <VoiceStatus />
      <div
        className={`chat-input-wrap${isDragOver ? ' is-drag-over' : ''}`}
        onDragOver={(e) => {
          e.preventDefault();
          if (!disabled) setIsDragOver(true);
        }}
        onDragLeave={() => setIsDragOver(false)}
        onDrop={handleDrop}
        onPaste={handlePaste}
      >
        {hintVisible && (
          <SlashCommandHint ref={hintRef} inputValue={value} onPick={pickCommand} />
        )}
        {pendingAttachments.length > 0 && (
          <div className="pending-attachments">
            {pendingAttachments.map(att => (
              <div className="pending-attachment" key={att.id}>
                {att.kind === 'image' ? (
                  <img
                    src={backendAssetUrl(att.url)}
                    alt={att.filename}
                    className="pending-attachment-thumb"
                  />
                ) : (
                  <span className="pending-attachment-doc">PDF</span>
                )}
                <span className="pending-attachment-name">{att.filename}</span>
                <CloseButton
                  size="inline"
                  onClick={() => removeAttachment(att)}
                  ariaLabel={`Remove ${att.filename}`}
                />
              </div>
            ))}
          </div>
        )}
        {previewChip && (
          <div className="chat-input-preview-chip" aria-live="polite">{previewChip}</div>
        )}
        {woken && (
          // Shown WHILE the operator is still speaking. The gate decides per
          // frame now, so this is the answer to "did it hear me" arriving at
          // the only time it is useful — before they have said the rest.
          <div className="chat-input-preview-chip is-woken" aria-live="polite">
            <span className="chat-input-woken-mark">heard you</span>
            keep going
          </div>
        )}
        {notHeard && (
          // The wake gate refused this. Shown rather than suppressed: the
          // preview had already vanished on speech-end, and nothing
          // replacing it is what makes a missed wake word read as a dead
          // mic. It fades itself — see `NOT_HEARD_MS`.
          <div className="chat-input-preview-chip is-not-heard" aria-live="polite">
            <span className="chat-input-not-heard-mark">not heard</span>
            {notHeard}
          </div>
        )}
        {uploadError && <div className="chat-upload-error">{uploadError}</div>}
        {queueLabel && (
          <div className="chat-queue-pill t-meta" aria-label={queueLabel}>{queueLabel}</div>
        )}
        <FileTrigger
          ref={fileInputRef}
          accept={uploadAccept}
          multiple
          onFiles={(files) => void uploadFiles(files)}
        />
        <textarea
          ref={textareaRef}
          className={`chat-input${transcribePreview ? ' is-dictating-preview' : ''}`}
        placeholder={transcribePreview || 'Message…'}
          aria-label="Message input"
          value={value}
          onChange={handleChange}
          onKeyDown={handleKeyDown}
          onFocus={handleFocus}
          onBlur={revertListening}
          onCompositionStart={() => { composingRef.current = true; }}
          onCompositionEnd={() => { composingRef.current = false; }}
          disabled={disabled}
          rows={1}
        />
        <Hint label={`Attach files. ${uploadHelp}`} position="top" maxWidth={280}>
          <ComposerButton
            verb="attach"
            onClick={() => fileInputRef.current?.open()}
            disabled={disabled || isUploading}
            ariaLabel="Attach files"
          >
            +
          </ComposerButton>
        </Hint>
        {showStop ? (
          <>
            {canSteer(isStreaming, value) && (
              <Hint
                label="Redirect now — fold this text into the current turn (Enter/Send would queue it for after; Stop cancels the turn entirely)"
                position="top"
                maxWidth={260}
              >
                <ComposerButton verb="steer" onClick={handleSteer} ariaLabel="Redirect now">
                  ↪
                </ComposerButton>
              </Hint>
            )}
            <Hint label={isStreaming ? 'Stop generating' : 'Stop voice'} position="top">
              <ComposerButton
                verb="stop"
                onClick={isStreaming ? handleStop : handleStopVoice}
                ariaLabel={isStreaming ? 'Stop generating' : 'Stop voice'}
              >
                ■
              </ComposerButton>
            </Hint>
          </>
        ) : (
          <Hint label="Send message" position="top">
            <ComposerButton
              verb="send"
              onClick={handleSend}
              disabled={disabled || isUploading || (!value.trim() && pendingAttachments.length === 0)}
              ariaLabel="Send message"
            >
              ↵
            </ComposerButton>
          </Hint>
        )}
      </div>
    </div>
  );

  if (variant === 'inline') {
    return <div className="chat-area">{inputRow}</div>;
  }
  return inputRow;
}


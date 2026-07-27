// Slice 2 — region capture → TARS vision. Floating trigger → drag a marquee
// over the cockpit → capture the region → type an instruction → send through the
// existing chat image-attachment path. Mounted once at App root (sibling of the
// cockpit) so it overlays everything. (No enter hotkey — Ctrl+Shift+R is the
// browser hard-reload and stays free; Esc cancels.)

import { useEffect, useState, type PointerEvent as ReactPointerEvent } from 'react';

import { useWebSocketStore } from '../stores/websocket';
import { useConversationStore } from '../stores/conversation';
import { useToastStore } from '../stores/toasts';
import { uploadChatAttachment } from '../lib/api';
import { usePanelStore } from './panelStore';
import {
  useRegionCaptureStore,
  captureRegion,
  rectFromPoints,
  isCapturable,
  type CaptureRect,
} from './regionCaptureStore';

const COMPOSE_W = 360;

function composePosition(rect: CaptureRect): { left: number; top: number } {
  const margin = 12;
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const left = Math.min(Math.max(margin, rect.x), vw - COMPOSE_W - margin);
  // Prefer below the selection; flip above if it would overflow the viewport.
  const below = rect.y + rect.h + margin;
  const top = below + 120 < vh ? below : Math.max(margin, rect.y - 120 - margin);
  return { left, top };
}

export function RegionCapture() {
  const mode = useRegionCaptureStore((s) => s.mode);
  const rect = useRegionCaptureStore((s) => s.rect);
  const preview = useRegionCaptureStore((s) => s.preview);
  const enter = useRegionCaptureStore((s) => s.enter);
  const setRect = useRegionCaptureStore((s) => s.setRect);
  const beginCapture = useRegionCaptureStore((s) => s.beginCapture);
  const setCaptured = useRegionCaptureStore((s) => s.setCaptured);
  const cancel = useRegionCaptureStore((s) => s.cancel);

  const sessionId = useWebSocketStore((s) => s.sessionId);
  const sendUserMessage = useConversationStore((s) => s.sendUserMessage);
  const openPanel = usePanelStore((s) => s.openPanel);

  const [prompt, setPrompt] = useState('');
  const [sending, setSending] = useState(false);

  useEffect(() => {
    // Esc cancels capture. No enter-capture hotkey — Ctrl+Shift+R is the
    // browser's hard-reload and must stay free (operator uses it to refresh).
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && mode !== 'idle') cancel();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [mode, cancel]);

  const onOverlayPointerDown = (e: ReactPointerEvent) => {
    if (mode !== 'selecting') return;
    e.preventDefault();
    const x0 = e.clientX;
    const y0 = e.clientY;
    const onMove = (ev: PointerEvent) => setRect(rectFromPoints(x0, y0, ev.clientX, ev.clientY));
    const onUp = async (ev: PointerEvent) => {
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
      const r = rectFromPoints(x0, y0, ev.clientX, ev.clientY);
      if (!isCapturable(r)) {
        cancel();
        return;
      }
      beginCapture();
      try {
        const { file, preview: shot } = await captureRegion(r);
        // The user may have pressed Esc (cancel → idle) while toCanvas was in
        // flight; only complete if we're still capturing this selection.
        if (useRegionCaptureStore.getState().mode === 'capturing') {
          setCaptured(file, shot, r);
        }
      } catch {
        if (useRegionCaptureStore.getState().mode === 'capturing') {
          useToastStore.getState().push('Region capture failed', 'warning');
          cancel();
        }
      }
    };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
  };

  const send = async () => {
    const file = useRegionCaptureStore.getState().file;
    if (!file) return;
    if (!sessionId) {
      useToastStore.getState().push('Connect before sending to TARS', 'warning');
      return;
    }
    setSending(true);
    try {
      const attachment = await uploadChatAttachment(sessionId, file);
      sendUserMessage(null, prompt.trim() || 'What is shown in this region?', [attachment]);
      openPanel('chat'); // surface TARS's reply
      setPrompt('');
      cancel();
    } catch {
      useToastStore.getState().push('Sending region to TARS failed', 'warning');
    } finally {
      setSending(false);
    }
  };

  return (
    <div className="region-capture-root">
      {mode === 'idle' && (
        <button
          type="button"
          className="region-capture-trigger"
          onClick={enter}
          aria-label="Capture a region for TARS"
          title="Capture a region for TARS"
        >
          ⛶
        </button>
      )}

      {(mode === 'selecting' || mode === 'capturing') && (
        <div
          className="region-capture-overlay"
          data-capturing={mode === 'capturing'}
          onPointerDown={onOverlayPointerDown}
        >
          {rect && (
            <div
              className="region-capture-marquee"
              style={{ left: rect.x, top: rect.y, width: rect.w, height: rect.h }}
            />
          )}
          <div className="region-capture-hint t-meta">
            {mode === 'capturing' ? 'Capturing…' : 'Drag to select a region · Esc to cancel'}
          </div>
        </div>
      )}

      {mode === 'composing' && rect && (
        <div className="region-capture-compose" style={composePosition(rect)}>
          {preview && (
            <img className="region-capture-thumb" src={preview} alt="Captured region" />
          )}
          <input
            className="region-capture-input"
            value={prompt}
            placeholder="Tell TARS what to do…"
            autoFocus
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') void send();
              else if (e.key === 'Escape') cancel();
            }}
          />
          <button
            type="button"
            className="region-capture-send"
            disabled={sending}
            aria-label={sending ? 'Sending…' : 'Send region to TARS'}
            onClick={() => void send()}
          >
            {sending ? '…' : 'Send'}
          </button>
          <button
            type="button"
            className="region-capture-cancel"
            onClick={cancel}
            aria-label="Cancel capture"
          >
            ×
          </button>
        </div>
      )}
    </div>
  );
}

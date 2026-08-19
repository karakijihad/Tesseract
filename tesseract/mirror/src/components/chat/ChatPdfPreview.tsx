import { useEffect, useRef, useState } from 'react';
import { GlobalWorkerOptions, getDocument } from 'pdfjs-dist';
import pdfWorkerUrl from 'pdfjs-dist/build/pdf.worker.mjs?url';
import type { ChatAttachment } from '../../lib/types';
import { promoteAttachmentToVault } from '../../lib/api';
import { useToastStore } from '../../stores/toasts';
import { Button } from '../common/Button';

interface Props {
  attachment: ChatAttachment;
}

type LoadState = 'idle' | 'loading' | 'ready' | 'error';
type PromoteState = 'idle' | 'pending' | 'done' | 'error';

let workerInitialized = false;
function ensureWorker() {
  if (workerInitialized) return;
  GlobalWorkerOptions.workerSrc = pdfWorkerUrl;
  workerInitialized = true;
}

export function ChatPdfPreview({ attachment }: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const frameRef = useRef<HTMLDivElement | null>(null);
  const [open, setOpen] = useState(false);
  const [state, setState] = useState<LoadState>('idle');
  const [pageCount, setPageCount] = useState<number | null>(null);
  const [promote, setPromote] = useState<PromoteState>('idle');

  useEffect(() => {
    if (!open) return;
    ensureWorker();
    let cancelled = false;
    let renderTask: { cancel: () => void; promise: Promise<unknown> } | null = null;
    const loadingTask = getDocument({ url: attachment.url });

    async function renderFirstPage() {
      setState('loading');
      setPageCount(null);
      try {
        const pdf = await loadingTask.promise;
        if (cancelled) return;
        const page = await pdf.getPage(1);
        if (cancelled) return;
        const baseViewport = page.getViewport({ scale: 1 });
        const maxWidth = Math.max(240, frameRef.current?.clientWidth ?? 560);
        const scale = Math.min(1.6, Math.max(0.5, maxWidth / baseViewport.width));
        const viewport = page.getViewport({ scale });
        const canvas = canvasRef.current;
        const ctx = canvas?.getContext('2d');
        if (!canvas || !ctx) throw new Error('canvas unavailable');
        const dpr = window.devicePixelRatio || 1;
        canvas.width = Math.floor(viewport.width * dpr);
        canvas.height = Math.floor(viewport.height * dpr);
        canvas.style.width = `${Math.floor(viewport.width)}px`;
        canvas.style.height = `${Math.floor(viewport.height)}px`;
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        renderTask = page.render({ canvas, canvasContext: ctx, viewport });
        await renderTask.promise;
        if (!cancelled) {
          setPageCount(pdf.numPages);
          setState('ready');
        }
      } catch {
        if (!cancelled) setState('error');
      }
    }

    void renderFirstPage();
    return () => {
      cancelled = true;
      renderTask?.cancel();
      void loadingTask.destroy();
    };
  }, [attachment.url, open]);

  const sizeLabel = attachment.size > 0
    ? `${(attachment.size / (1024 * 1024)).toFixed(2)} MB`
    : 'PDF';

  const onPromote = async () => {
    if (promote === 'pending' || promote === 'done') return;
    setPromote('pending');
    try {
      await promoteAttachmentToVault(attachment);
      setPromote('done');
      useToastStore.getState().push(`Saved ${attachment.filename} to vault`, 'info');
    } catch (err) {
      setPromote('error');
      const msg = err instanceof Error ? err.message : 'promote failed';
      useToastStore.getState().push(`Vault save failed: ${msg}`, 'warning');
    }
  };

  const promoteLabel =
    promote === 'pending' ? 'Saving…' :
    promote === 'done' ? 'In vault' :
    promote === 'error' ? 'Retry vault' :
    'Save to vault';

  return (
    <div className="bubble-pdf-card">
      <div className="bubble-pdf-head">
        <div className="bubble-pdf-icon">PDF</div>
        <div className="bubble-pdf-main">
          <div className="bubble-pdf-name">{attachment.filename}</div>
          <div className="bubble-pdf-meta t-meta">
            {pageCount ? `${pageCount} pages` : sizeLabel}
          </div>
        </div>
        <div className="bubble-pdf-actions">
          <Button
            onClick={() => setOpen(v => !v)}
            active={open}
            ariaLabel={open ? 'Hide PDF preview' : 'Show PDF preview'}
          >
            {open ? 'Hide' : 'Preview'}
          </Button>
          <a
            href={attachment.url}
            target="_blank"
            rel="noopener noreferrer"
            aria-label={`Open ${attachment.filename} in new tab`}
          >
            Open
          </a>
          <Button
            onClick={onPromote}
            disabled={promote === 'pending' || promote === 'done'}
            ariaLabel={`Save ${attachment.filename} to the vault`}
          >
            {promoteLabel}
          </Button>
        </div>
      </div>
      {open && (
        <div className="bubble-pdf-preview" ref={frameRef}>
          {state === 'loading' && <div className="bubble-pdf-state t-meta">Loading preview...</div>}
          {state === 'error' && (
            <div className="bubble-pdf-state t-meta">
              Preview unavailable. Open the PDF in a new tab.
            </div>
          )}
          <canvas
            ref={canvasRef}
            className={`bubble-pdf-canvas${state === 'ready' ? ' is-ready' : ''}`}
            aria-label={`Preview of ${attachment.filename}`}
          />
        </div>
      )}
    </div>
  );
}

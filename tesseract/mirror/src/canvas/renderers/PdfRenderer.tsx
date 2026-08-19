// pdf surface — renders `props.url` a page at a time via pdf.js.
//
// pdf.js is imported lazily: it is the heaviest thing on the canvas, and an
// operator who never opens a PDF should not pay for it in the bundle. The
// worker URL is resolved the same way `components/chat/ChatPdfPreview` does.

import { useEffect, useRef, useState } from 'react';

import { backendAssetUrl } from '../../lib/endpoints';
import type { RendererProps } from './index';
import { IconButton } from '../../components/common/IconButton';

type LoadState = 'loading' | 'ready' | 'error';

let workerReady = false;

async function loadPdfjs() {
  const pdfjs = await import('pdfjs-dist');
  if (!workerReady) {
    const worker = await import('pdfjs-dist/build/pdf.worker.mjs?url');
    pdfjs.GlobalWorkerOptions.workerSrc = worker.default;
    workerReady = true;
  }
  return pdfjs;
}

export function PdfRenderer({ descriptor, report }: RendererProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const frameRef = useRef<HTMLDivElement | null>(null);
  const [state, setState] = useState<LoadState>('loading');
  const [page, setPage] = useState(1);
  const [pageCount, setPageCount] = useState<number | null>(null);

  const src = descriptor.props?.url;
  const url = typeof src === 'string' && src ? backendAssetUrl(src) : null;

  useEffect(() => {
    if (!url) return;
    let cancelled = false;
    let renderTask: { cancel: () => void } | null = null;

    (async () => {
      setState('loading');
      try {
        const pdfjs = await loadPdfjs();
        const pdf = await pdfjs.getDocument({ url }).promise;
        if (cancelled) return;
        setPageCount(pdf.numPages);

        const target = Math.min(Math.max(1, page), pdf.numPages);
        const rendered = await pdf.getPage(target);
        if (cancelled) return;

        const base = rendered.getViewport({ scale: 1 });
        const width = Math.max(240, frameRef.current?.clientWidth ?? 560);
        const scale = Math.min(2, Math.max(0.4, width / base.width));
        const viewport = rendered.getViewport({ scale });

        const canvas = canvasRef.current;
        const ctx = canvas?.getContext('2d');
        if (!canvas || !ctx) throw new Error('canvas unavailable');

        const dpr = window.devicePixelRatio || 1;
        canvas.width = Math.floor(viewport.width * dpr);
        canvas.height = Math.floor(viewport.height * dpr);
        canvas.style.width = `${Math.floor(viewport.width)}px`;
        canvas.style.height = `${Math.floor(viewport.height)}px`;
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

        // `canvas` alongside `canvasContext`: pdf.js needs the element itself,
        // not just its 2D context, and its types require both.
        renderTask = rendered.render({ canvas, canvasContext: ctx, viewport });
        await (renderTask as unknown as { promise: Promise<void> }).promise;
        if (!cancelled) setState('ready');
      } catch {
        if (!cancelled) setState('error');
      }
    })();

    return () => {
      cancelled = true;
      renderTask?.cancel();
    };
  }, [url, page]);

  useEffect(() => {
    if (!report) return;
    if (!url) report('errored', 'no pdf: props.url is missing or not a string');
    else if (state === 'error') report('errored', 'pdf.js could not render this document');
  }, [report, url, state]);

  if (!url) {
    return <div className="surface-pdf surface-pdf--empty t-meta">no pdf</div>;
  }

  if (state === 'error') {
    return (
      <div className="surface-pdf surface-pdf--empty t-meta">
        this pdf couldn&rsquo;t be rendered here. Ask to open it outside.
      </div>
    );
  }

  const total = pageCount ?? 1;
  return (
    <div className="surface-pdf" ref={frameRef}>
      <div className="surface-pdf__page">
        <canvas ref={canvasRef} />
      </div>
      <div className="surface-pdf__bar">
        <IconButton
          onClick={() => setPage((p) => Math.max(1, p - 1))}
          disabled={page <= 1}
          ariaLabel="Previous page"
        >
          ‹
        </IconButton>
        <span className="t-meta">
          {state === 'loading' ? 'loading…' : `page ${page} of ${total}`}
        </span>
        <IconButton
          onClick={() => setPage((p) => Math.min(total, p + 1))}
          disabled={pageCount === null || page >= total}
          ariaLabel="Next page"
        >
          ›
        </IconButton>
      </div>
    </div>
  );
}

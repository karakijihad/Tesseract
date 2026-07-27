// Y-2 — file surface. Previews a file three ways depending on which prop
// the tool supplied: `props.text` (text body), `props.image_url` (image),
// or `props.pdf_url` (PDF — link out; inline pdf.js viewer is a later
// polish). `backendAssetUrl` rewrites bare `/api/...` refs to the resolved
// backend origin (dev cross-origin).

import { backendAssetUrl } from '../../lib/endpoints';
import type { RendererProps } from './index';

export function FileRenderer({ descriptor }: RendererProps) {
  const props = descriptor.props ?? {};
  const imageUrl = props.image_url;
  const pdfUrl = props.pdf_url;
  const text = props.text;

  if (typeof imageUrl === 'string') {
    return (
      <div className="surface-file surface-file--image">
        <img src={backendAssetUrl(imageUrl)} alt={descriptor.title ?? 'image'} loading="lazy" />
      </div>
    );
  }
  if (typeof pdfUrl === 'string') {
    return (
      <div className="surface-file surface-file--pdf">
        <a href={backendAssetUrl(pdfUrl)} target="_blank" rel="noopener noreferrer">
          Open PDF ↗
        </a>
      </div>
    );
  }
  return (
    <pre className="surface-file surface-file--text">
      {typeof text === 'string' ? text : '(no preview available)'}
    </pre>
  );
}

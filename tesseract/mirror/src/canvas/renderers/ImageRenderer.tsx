// image surface — renders a generated/standalone image (e.g. `image_generate`
// output) as a fit-to-card `<img>`. Distinct from `file` (which previews a
// file by `image_url`/`pdf_url`/`text`): an `image` surface IS the image, so
// `props.url` is the canonical source. `backendAssetUrl` rewrites bare
// `/api/...` refs to the resolved backend origin (dev cross-origin).

import { useEffect, useState } from 'react';

import { backendAssetUrl } from '../../lib/endpoints';
import type { RendererProps } from './index';

export function ImageRenderer({ descriptor, report }: RendererProps) {
  const props = descriptor.props ?? {};
  const src = props.url ?? props.image_url ?? props.src;
  const [failed, setFailed] = useState(false);
  const missing = typeof src !== 'string' || !src;

  // A broken `props.url` draws the browser's own placeholder, which reads as
  // a rendering bug rather than a dead link. Say which it is.
  useEffect(() => {
    if (!report) return;
    if (missing) report('errored', 'no image: props carried none of url / image_url / src');
    else if (failed) report('errored', 'the image source could not be loaded');
  }, [report, missing, failed]);

  if (typeof src !== 'string' || !src) {
    return <div className="surface-image surface-image--empty">(no image)</div>;
  }
  if (failed) {
    return (
      <div className="surface-image surface-image--empty t-meta">
        this image couldn&rsquo;t be loaded
      </div>
    );
  }
  return (
    <div className="surface-image">
      <img
        src={backendAssetUrl(src)}
        alt={descriptor.title ?? 'image'}
        loading="lazy"
        onError={() => setFailed(true)}
      />
    </div>
  );
}

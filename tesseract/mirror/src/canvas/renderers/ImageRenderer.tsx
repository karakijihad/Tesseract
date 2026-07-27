// image surface — renders a generated/standalone image (e.g. `image_generate`
// output) as a fit-to-card `<img>`. Distinct from `file` (which previews a
// file by `image_url`/`pdf_url`/`text`): an `image` surface IS the image, so
// `props.url` is the canonical source. `backendAssetUrl` rewrites bare
// `/api/...` refs to the resolved backend origin (dev cross-origin).

import { backendAssetUrl } from '../../lib/endpoints';
import type { RendererProps } from './index';

export function ImageRenderer({ descriptor }: RendererProps) {
  const props = descriptor.props ?? {};
  const src = props.url ?? props.image_url ?? props.src;
  if (typeof src !== 'string' || !src) {
    return <div className="surface-image surface-image--empty">(no image)</div>;
  }
  return (
    <div className="surface-image">
      <img src={backendAssetUrl(src)} alt={descriptor.title ?? 'image'} loading="lazy" />
    </div>
  );
}

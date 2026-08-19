// video surface — plays `props.url` inline. No dependency: WebView2 handles
// H.264/AAC, WebM and Ogg natively.
//
// `preload="metadata"` so opening a card costs a header read rather than the
// whole file; a 2GB recording should not download because a card exists.

import { useEffect, useState } from 'react';

import { backendAssetUrl } from '../../lib/endpoints';
import type { RendererProps } from './index';

export function VideoRenderer({ descriptor, report }: RendererProps) {
  const [failed, setFailed] = useState(false);
  const src = descriptor.props?.url;
  const missing = typeof src !== 'string' || !src;

  // Same reason the card gets, sent to the model — an unplayable card looks
  // identical to a playing one in a listing that only knows the descriptor.
  useEffect(() => {
    if (!report) return;
    if (missing) report('errored', 'no video: props.url is missing or not a string');
    else if (failed) report('errored', 'the container or codec is not supported here');
  }, [report, missing, failed]);

  if (typeof src !== 'string' || !src) {
    return <div className="surface-media surface-media--empty t-meta">no video</div>;
  }

  // A silent black rectangle is the worst outcome — it reads as a bug rather
  // than as "this codec isn't available here", which is the actionable fact.
  if (failed) {
    return (
      <div className="surface-media surface-media--empty t-meta">
        this video can&rsquo;t play here &mdash; the container or codec
        isn&rsquo;t supported. Ask to open it outside.
      </div>
    );
  }

  return (
    <div className="surface-media">
      <video
        className="surface-media__player"
        src={backendAssetUrl(src)}
        controls
        preload="metadata"
        onError={() => setFailed(true)}
      />
    </div>
  );
}

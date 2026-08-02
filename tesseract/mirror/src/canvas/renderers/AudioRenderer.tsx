// audio surface — plays `props.url` inline. No dependency; the browser's own
// transport is the whole UI.

import { useState } from 'react';

import { backendAssetUrl } from '../../lib/endpoints';
import type { RendererProps } from './index';

export function AudioRenderer({ descriptor }: RendererProps) {
  const [failed, setFailed] = useState(false);
  const src = descriptor.props?.url;

  if (typeof src !== 'string' || !src) {
    return <div className="surface-media surface-media--empty t-meta">no audio</div>;
  }

  if (failed) {
    return (
      <div className="surface-media surface-media--empty t-meta">
        this audio can&rsquo;t play here &mdash; the format isn&rsquo;t
        supported. Ask to open it outside.
      </div>
    );
  }

  return (
    <div className="surface-media surface-media--audio">
      {descriptor.title ? (
        <div className="surface-media__label t-meta">{descriptor.title}</div>
      ) : null}
      <audio
        className="surface-media__player"
        src={backendAssetUrl(src)}
        controls
        preload="metadata"
        onError={() => setFailed(true)}
      />
    </div>
  );
}

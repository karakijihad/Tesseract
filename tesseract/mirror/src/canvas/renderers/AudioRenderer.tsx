// audio surface — plays `props.url` inline. No dependency; the browser's own
// transport is the whole UI.

import { useEffect, useRef, useState } from 'react';

import { backendAssetUrl } from '../../lib/endpoints';
import { useMediaCommands } from '../useMediaCommands';
import type { RendererProps } from './index';

/** What `surface_control` can ask a media card. */
const MEDIA_CONTROLS = ['play', 'pause', 'mute', 'unmute', 'volume', 'seek', 'read'];

export function AudioRenderer({ descriptor, report }: RendererProps) {
  const [failed, setFailed] = useState(false);
  // Same channel the video card answers on, same reason it is cheap.
  const player = useRef<HTMLAudioElement>(null);
  useMediaCommands(descriptor.id, descriptor.view, player);
  const src = descriptor.props?.url;
  const missing = typeof src !== 'string' || !src;

  useEffect(() => {
    if (!report) return;
    if (missing) report('errored', 'no audio: props.url is missing or not a string');
    else if (failed) report('errored', 'the audio format is not supported here');
    else report('mounted', '', MEDIA_CONTROLS);
  }, [report, missing, failed]);

  if (typeof src !== 'string' || !src) {
    return <div className="surface-media surface-media--empty t-meta">no audio</div>;
  }

  if (failed) {
    return (
      <div className="surface-media surface-media--empty t-meta">
        this audio cannot play here, because the format is not
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
        ref={player}
        className="surface-media__player"
        src={backendAssetUrl(src)}
        controls
        preload="metadata"
        onError={() => setFailed(true)}
      />
    </div>
  );
}

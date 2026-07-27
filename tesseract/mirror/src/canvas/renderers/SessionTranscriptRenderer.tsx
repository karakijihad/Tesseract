// Render a controller session's transcript as a Surface-Protocol card. Opened
// from the activity map's `controller_session` rows (the cockpit "chat" session
// among them). Reuses ControllerMirrorBlock — the same WS observer bridge the
// chat uses — which connects to `/ws/controller/<session_id>` and, per
// `controller_ws.py` (`from_offset=0`), replays the FULL transcript then
// live-follows. The delegate-transcript renderer can't show these (it only
// reads CLI delegate stdout streams keyed by call_id), so a session row used to
// open an empty "transcript not available" card. Card chrome (title, close,
// drag, resize) is owned by the SurfaceCard wrapper.

import { ControllerMirrorBlock } from '../../components/chat/ControllerMirrorBlock';
import type { RendererProps } from './index';

export function SessionTranscriptRenderer({ descriptor }: RendererProps) {
  const sessionId = String(descriptor.props?.session_id ?? '');
  if (!sessionId) {
    return <div className="spawn-card-empty t-meta">No session bound to this card.</div>;
  }
  return <ControllerMirrorBlock session_id={sessionId} ws_path={`/ws/controller/${sessionId}`} />;
}

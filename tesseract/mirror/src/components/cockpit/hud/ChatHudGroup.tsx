import { useIdentityStore } from '../../../stores/identity';
import { useUIStore } from '../../../stores/ui';
import { Hint } from '../../ui/Hint';
import { HudMicButton } from './HudMicButton';
import { StatsChip } from './StatsChip';

function SessionsButton() {
  const toggle = useUIStore((s) => s.toggleDrawer);
  return (
    <Hint label="Sessions — save, load, resume" position="top" maxWidth={200}>
      <button
        type="button"
        className="hud-sessions"
        onClick={toggle}
        aria-label="Open sessions drawer"
      >
        <span aria-hidden="true">☰</span>
      </button>
    </Hint>
  );
}

function ChatModelBadge() {
  const modelName = useIdentityStore((s) => s.modelName);
  const provider = useIdentityStore((s) => s.provider);
  if (!modelName) return null;
  return (
    <Hint label={`Chat model — ${provider}/${modelName}`} position="top" maxWidth={240}>
      <span
        className="hud-model"
        aria-label={`Chat model ${modelName}`}
      >
        {modelName}
      </span>
    </Hint>
  );
}

export function ChatHudGroup() {
  return (
    <div className="hud-group hud-group--chat" role="group" aria-label="Chat controls and tokens">
      <HudMicButton />
      <SessionsButton />
      <ChatModelBadge />
      <StatsChip />
    </div>
  );
}

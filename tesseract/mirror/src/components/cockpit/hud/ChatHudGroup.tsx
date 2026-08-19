import { useUIStore } from "../../../stores/ui";
import { Hint } from "../../ui/Hint";
import { HudMicButton } from "./HudMicButton";
import { StatsChip } from "./StatsChip";

function SessionsButton() {
  const toggle = useUIStore((s) => s.toggleDrawer);
  return (
    <Hint label="Sessions — save, load, resume" maxWidth={200}>
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

/** Mic, sessions, tokens.
 *
 * The chat model chip that used to sit here is gone: the top HUD names the
 * model two inches away, and one fact in two places is one place too many.
 * The fold machinery went with it — with the model chip and the observer group
 * removed there is nothing left that stops fitting.
 */
export function ChatHudGroup() {
  return (
    <div
      className="hud-group hud-group--chat"
      role="group"
      aria-label="Chat controls and tokens"
    >
      <HudMicButton />
      <SessionsButton />
      <StatsChip />
    </div>
  );
}

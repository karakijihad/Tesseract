import { HudMicButton } from "./HudMicButton";
import { StatsChip } from "./StatsChip";

/** Mic and tokens.
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
      <StatsChip />
    </div>
  );
}

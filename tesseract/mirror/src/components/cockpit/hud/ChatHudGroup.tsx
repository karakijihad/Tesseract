import { useIdentityStore } from "../../../stores/identity";
import { useUIStore } from "../../../stores/ui";
import { Hint } from "../../ui/Hint";
import { HudMicButton } from "./HudMicButton";
import { HudSection } from "./HudSection";
import { StatsChip } from "./StatsChip";

// Brain section face (folded mode) — diamond chip standing in for the
// model/sessions/stats cluster.
const BrainSectionIcon = () => <span aria-hidden="true">◈</span>;

// Hint direction: 'right' inside the folded vertical stack (a top hint would
// cover the stack item above — 2026-07-31 review finding), 'top' in the bar.
type HintPos = "top" | "right";

function SessionsButton({ hintPos = "top" }: { hintPos?: HintPos }) {
  const toggle = useUIStore((s) => s.toggleDrawer);
  return (
    <Hint
      label="Sessions — save, load, resume"
      position={hintPos}
      maxWidth={200}
    >
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

function ChatModelBadge({ hintPos = "top" }: { hintPos?: HintPos }) {
  const modelName = useIdentityStore((s) => s.modelName);
  const provider = useIdentityStore((s) => s.provider);
  if (!modelName) return null;
  return (
    <Hint
      label={`Chat model — ${provider}/${modelName}`}
      position={hintPos}
      maxWidth={240}
    >
      <span className="hud-model" aria-label={`Chat model ${modelName}`}>
        {modelName}
      </span>
    </Hint>
  );
}

interface Props {
  /** Sectioned dock (2026-07-31): when the bar stops fitting, the sessions /
   *  model / stats chips fold into a ◈ section stack. The mic always stays a
   *  dedicated button (voice-first) — only its mode pill hides via CSS. */
  folded?: boolean;
}

export function ChatHudGroup({ folded = false }: Props) {
  return (
    <div
      className="hud-group hud-group--chat"
      role="group"
      aria-label="Chat controls and tokens"
    >
      <HudMicButton />
      {folded ? (
        <HudSection id="brain" label="Chat brain" icon={<BrainSectionIcon />}>
          <SessionsButton hintPos="right" />
          <ChatModelBadge hintPos="right" />
          <StatsChip hintPosition="right" />
        </HudSection>
      ) : (
        <>
          <SessionsButton />
          <ChatModelBadge />
          <StatsChip />
        </>
      )}
    </div>
  );
}

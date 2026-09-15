import { Range } from "../../components/common/Range";
import { WindowBar } from "../../components/common/WindowBar";
import { measureBar, type CompactionFacts } from "../../lib/compaction";

interface Props {
  facts: CompactionFacts;
  ratio: number;
  disabled: boolean;
  onRatio: (next: number) => void;
  onRatioCommit: () => void;
}

/** The setting: `WindowBar`'s reading, with the slider that moves it laid
 *  between the track and the verdict. This reads a DRAFT (the ratio the
 *  operator is dragging, not yet committed) rather than a live conversation,
 *  so `facts` carries no `conversationTokens` and the bar draws no
 *  "conversation now" mark. */
export function CompactionBar({
  facts,
  ratio,
  disabled,
  onRatio,
  onRatioCommit,
}: Props) {
  // The same measurement `WindowBar` draws from, read once here only for the
  // two numbers the handle needs to stay inside. Not a second opinion: same
  // facts, same ratio, same pure arithmetic, so it cannot disagree with what
  // gets drawn.
  const { minRatio, maxRatio } = measureBar(facts, ratio);

  return (
    <WindowBar facts={facts} ratio={ratio}>
      <Range
        min={minRatio}
        max={maxRatio}
        step={0.01}
        value={ratio}
        onChange={onRatio}
        onCommit={onRatioCommit}
        disabled={disabled}
        ariaLabel="wrap this conversation up when it reaches this share of the window"
      />
    </WindowBar>
  );
}

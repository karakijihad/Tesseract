import { BreakersSection } from './right/BreakersSection';
import { ObserverSection } from './right/ObserverSection';

// The TARS identity header (name / mode / model / state / last-reflected)
// moved to the floating top status pill (CockpitShell::TopStatusHud), so the
// right rail is now just its Breakers / Observer sections — no duplicated
// stat block. (Lifeline section removed — prune wave 1, Batch 3.)
export function RightPanel() {
  return (
    <div className="right-panel-inner">
      <div className="right-panel-body">
        <div className="cat-section">
          <BreakersSection />
        </div>
        <div className="syn-div" aria-hidden="true" />
        <div className="cat-section">
          <ObserverSection />
        </div>
      </div>
    </div>
  );
}

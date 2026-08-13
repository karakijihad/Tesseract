import { PulseTailSection } from './PulseTailSection';
import { usePanelStore } from '../../../cockpit/panelStore';
import { KernelFlows } from '../../../cockpit/kernel/KernelFlows';

export function LeftPanel() {
  const resetRail = usePanelStore((s) => s.resetRail);
  return (
    <div className="left-panel-inner">
      <div className="cat-section cat-section--kernel">
        <div className="panel-head">
          <h2 className="t-head left-panel-cat-title">Kernel</h2>
          <button
            type="button"
            className="panel-head-reset t-caption"
            onClick={() => resetRail('kernel')}
          >
            reset
          </button>
        </div>
        <KernelFlows />
      </div>
      <div className="syn-div" aria-hidden="true" />
      <div className="cat-section cat-section--pulse">
        <h2 className="t-head left-panel-cat-title">Pulse</h2>
        <PulseTailSection />
      </div>
    </div>
  );
}

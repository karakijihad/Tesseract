import { PulseTailSection } from './PulseTailSection';
import { KernelFlows } from '../../../cockpit/kernel/KernelFlows';

export function LeftPanel() {
  return (
    <div className="left-panel-inner">
      <div className="cat-section cat-section--kernel">
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

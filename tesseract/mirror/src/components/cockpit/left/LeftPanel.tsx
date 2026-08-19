import { KernelFlows } from '../../../cockpit/kernel/KernelFlows';

export function LeftPanel() {
  return (
    <div className="left-panel-inner">
      <div className="cat-section cat-section--kernel">
        <KernelFlows />
      </div>
    </div>
  );
}

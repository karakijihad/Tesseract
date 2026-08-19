import { FLOWS } from './flows';
import { FlowRail } from './FlowRail';

// One rail, no tabs. The kernel is watched, not navigated: every stage of
// every pipeline is on the column at once, so sending a message makes the
// thing move without the operator having to pick a tab first.

export function KernelFlows() {
  return (
    <div className="kernel-flows">
      {FLOWS.map((flow) => (
        <FlowRail key={flow.id} flow={flow} />
      ))}
    </div>
  );
}

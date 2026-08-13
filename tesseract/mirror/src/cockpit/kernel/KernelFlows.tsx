import { FLOWS } from './flows';
import { FlowRail } from './FlowRail';

// One rail, no tabs. The old column showed the whole kernel at once — the
// flow, then its groups under sub-headers — and that is what the operator
// watches: send a message and the thing moves. Every stage of every pipeline
// is here, divided by the same `.syn-sub` header the column always used.

export function KernelFlows() {
  return (
    <div className="kernel-flows">
      {FLOWS.map((flow) => (
        <div key={flow.id} className="kernel-stage">
          <div className="syn-sub">
            <span className="syn-sub-txt">{flow.label}</span>
            <span className="syn-sub-line" aria-hidden="true" />
          </div>
          <FlowRail flow={flow} />
        </div>
      ))}
    </div>
  );
}

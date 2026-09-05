import { useEffect } from 'react';

import { useKernelManifest } from './flows';
import { FlowRail } from './FlowRail';

// One rail, no tabs. The kernel is watched, not navigated: every stage of
// every pipeline is on the column at once, so sending a message makes the
// thing move without the operator having to pick a tab first.
//
// What it draws arrives from the runtime rather than from a file compiled into
// the app, so a seat changed in `roles.yaml` shows here without a rebuild.

export function KernelFlows() {
  const flows = useKernelManifest((s) => s.flows);
  const status = useKernelManifest((s) => s.status);
  const error = useKernelManifest((s) => s.error);
  const load = useKernelManifest((s) => s.load);

  useEffect(() => {
    if (status === 'idle') void load();
  }, [status, load]);

  if (status === 'error') {
    return (
      <div className="kernel-flows">
        <p className="t-meta">
          The kernel rail could not be read, so nothing below is drawn. {error}
        </p>
      </div>
    );
  }

  if (flows.length === 0) {
    return (
      <div className="kernel-flows">
        <p className="t-meta">Reading what the runtime is made of.</p>
      </div>
    );
  }

  return (
    <div className="kernel-flows">
      {flows.map((flow) => (
        <FlowRail key={flow.id} flow={flow} />
      ))}
    </div>
  );
}

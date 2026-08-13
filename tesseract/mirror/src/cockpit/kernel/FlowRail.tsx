import type { Flow, FlowNode } from './flows';
import { useFlowActivity } from './useFlowActivity';

// The synapse column, unchanged — same markup, same `.syn-*` classes, same
// stylesheet. Only what it draws is new: five flows instead of the chat turn
// alone. A second visual language was tried here and removed; the old one
// reads better in a 280px rail, and reusing it literally is what keeps the
// new tabs from drifting away from it.

function depthClass(depth: 0 | 1 | 2 | undefined): string {
  if (depth === 1) return 'd1';
  if (depth === 2) return 'd2';
  return '';
}

function nodeClass(node: FlowNode, lit: boolean, flashing: boolean): string {
  return [
    'syn-node',
    depthClass(node.depth),
    lit ? 'is-active' : '',
    lit && flashing ? 'is-flashing' : '',
    node.kind === 'gate' ? 'is-decision' : '',
  ]
    .filter(Boolean)
    .join(' ');
}

interface Props {
  flow: Flow;
}

export function FlowRail({ flow }: Props) {
  const { active, flashing } = useFlowActivity(flow.id);
  const anyActive = flow.nodes.some((n) => active.has(n.id));

  return (
    <div className={`syn-flow${anyActive ? ' has-activity' : ''}`}>
      {flow.nodes.map((node) => {
        const lit = active.has(node.id);
        return (
          <div
            key={node.id}
            className={nodeClass(node, lit, flashing)}
            data-kind={node.kind}
            data-signal={node.signal}
          >
            <span className="syn-dot" aria-hidden="true" />
            <span className="syn-lbl">{node.label}</span>
          </div>
        );
      })}
    </div>
  );
}

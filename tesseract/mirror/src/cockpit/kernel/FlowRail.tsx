import type { Flow, FlowNode } from './flows';
import { useFlowActivity } from './useFlowActivity';

// The rail draws what `flows.ts` declares and nothing more. Every field in the
// manifest reaches the screen: `kind` picks the marker, `tone` colours it,
// `sub` is the second line — which is also where a loop or an exit states
// itself, since a separate note line under the node said the same thing twice
// as long. The head carries the flow's live position, the way the old
// column's category head carried the tool that had just fired.

type NodeState = 'cursor' | 'trail' | 'cold' | 'unwired';

function nodeClass(node: FlowNode, state: NodeState, flashing: boolean): string {
  return [
    'kf-node',
    node.depth ? `kf-d${node.depth}` : '',
    `is-${state}`,
    state === 'cursor' && flashing ? 'is-flashing' : '',
  ]
    .filter(Boolean)
    .join(' ');
}

interface Props {
  flow: Flow;
}

export function FlowRail({ flow }: Props) {
  const { cursor, trail, unwired, flashing, counts } = useFlowActivity(flow.id);
  const at = flow.nodes.find((n) => n.id === cursor);

  return (
    <section className={`kf-flow${cursor ? ' is-live' : ''}`} aria-label={flow.label}>
      <header className="kf-head">
        <h3 className="t-meta kf-head-name">{flow.label}</h3>
        <span className="kf-head-rule" aria-hidden="true" />
        <span className="t-meta kf-head-at">{at ? at.label : 'idle'}</span>
      </header>

      <ol className="kf-nodes">
        {flow.nodes.map((node) => {
          const state: NodeState =
            node.id === cursor
              ? 'cursor'
              : trail.has(node.id)
                ? 'trail'
                : unwired.has(node.id)
                  ? 'unwired'
                  : 'cold';
          const count = counts[node.id];
          return (
            <li
              key={node.id}
              className={nodeClass(node, state, flashing)}
              data-kind={node.kind}
              data-tone={node.tone ?? 'default'}
            >
              <span className="kf-mark" aria-hidden="true" />
              <span className="kf-text">
                <span className="kf-label">
                  {node.label}
                  {count ? (
                    <span className="kf-count" aria-label={`${count} fires this session`}>
                      {count}
                    </span>
                  ) : null}
                </span>
                {node.sub ? <span className="kf-sub">{node.sub}</span> : null}
              </span>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

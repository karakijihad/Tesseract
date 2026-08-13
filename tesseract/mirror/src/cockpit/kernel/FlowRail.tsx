import type { Flow, FlowNode } from './flows';
import { useFlowActivity } from './useFlowActivity';

// The vertical rail the Kernel panel has always used: a hairline spine, a dot
// per stage, indentation for nesting. It reads in a 280px column, which a
// left-to-right schematic does not.
//
// Kind is carried by the dot, not by a second colour system: a gate is
// hollow (it can refuse), a store is square (it holds something), a seat is
// dashed (a role fills it).

function dotClass(node: FlowNode): string {
  return [
    'krn-dot',
    `krn-dot--${node.kind}`,
    `krn-tone--${node.tone ?? 'default'}`,
  ].join(' ');
}

interface Props {
  flow: Flow;
  /** Per-node live values, replacing the static sub-label when present. */
  counts?: Record<string, string>;
}

export function FlowRail({ flow, counts }: Props) {
  const { active, flashing } = useFlowActivity(flow.id);
  const anyActive = flow.nodes.some((n) => active.has(n.id));

  return (
    <div className={`krn-rail${anyActive ? ' has-activity' : ''}`}>
      {flow.nodes.map((node) => {
        const lit = active.has(node.id);
        const sub = counts?.[node.id] ?? node.sub;
        const notes = flow.notes?.filter((n) => n.after === node.id) ?? [];
        return (
          <div key={node.id}>
            <div
              className={`krn-node krn-d${node.depth ?? 0}${lit ? ' is-lit' : ''}${
                lit && flashing ? ' is-flashing' : ''
              }`}
              data-signal={node.signal}
            >
              <span className={dotClass(node)} aria-hidden="true" />
              <span className="krn-label">{node.label}</span>
              {sub && <span className="krn-sub">{sub}</span>}
            </div>
            {notes.map((note) => (
              <div
                key={note.text}
                className={`krn-note krn-d${node.depth ?? 0} krn-tone--${note.tone ?? 'default'}`}
              >
                {note.text}
              </div>
            ))}
          </div>
        );
      })}
    </div>
  );
}

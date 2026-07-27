import { useEffect, useRef, useState } from 'react';
import { useEntityStore } from '../../../stores/entity';
import { useToolActivityStore } from '../../../stores/toolActivity';
import {
  ENTITY_TO_NODE,
  SYNAPSE_NODES,
  TOOL_GROUPS,
  type SynapseNode,
  type ToolGroup,
} from './synapse-nodes';

const FLASH_MS = 220;

function depthClass(depth: 0 | 1 | 2): string {
  if (depth === 1) return 'd1';
  if (depth === 2) return 'd2';
  return '';
}

function KernelNode({
  node,
  activeId,
  flashing,
}: {
  node: SynapseNode;
  activeId: string;
  flashing: boolean;
}) {
  const isActive = node.id === activeId;
  const classes = [
    'syn-node',
    depthClass(node.depth),
    isActive ? 'is-active' : '',
    isActive && flashing ? 'is-flashing' : '',
    node.kind === 'decision' ? 'is-decision' : '',
  ]
    .filter(Boolean)
    .join(' ');
  return (
    <div
      className={classes}
      data-phase={node.dataPhase}
      data-kind={node.kind}
      title={`${node.label} (${node.source})`}
    >
      <span className="syn-dot" aria-hidden="true" />
      <span className="syn-lbl">{node.label}</span>
    </div>
  );
}

function ToolGroupBlock({
  group,
  lastTool,
  toolFlashing,
}: {
  group: ToolGroup;
  lastTool: string | null;
  toolFlashing: boolean;
}) {
  const groupActive = lastTool !== null && group.tools.some((t) => t.name === lastTool);

  if (group.collapsed) {
    const classes = [
      'syn-node',
      'd1',
      groupActive ? 'is-active' : '',
      groupActive && toolFlashing ? 'is-flashing' : '',
    ]
      .filter(Boolean)
      .join(' ');
    const title = `${group.label} — ${group.tools.map((t) => t.name).join(', ')}`;
    return (
      <div className="syn-cat syn-cat-tools">
        <div className="syn-sub">
          <span className="syn-sub-txt">{group.label}</span>
          <span className="syn-sub-line" />
        </div>
        <div className="syn-flow">
          <div
            className={classes}
            data-phase={`tool-group-${group.id}`}
            data-kind="tool"
            title={title}
          >
            <span className="syn-dot" aria-hidden="true" />
            <span className="syn-lbl">{groupActive ? lastTool : group.label.toLowerCase()}</span>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="syn-cat syn-cat-tools">
      <div className="syn-sub">
        <span className="syn-sub-txt">{group.label}</span>
        <span className="syn-sub-line" />
      </div>
      <div className="syn-flow">
        {group.tools.map((tool, idx) => {
          const isActive = tool.name === lastTool;
          const classes = [
            'syn-node',
            'd1',
            isActive ? 'is-active' : '',
            isActive && toolFlashing ? 'is-flashing' : '',
          ]
            .filter(Boolean)
            .join(' ');
          return (
            <div
              key={`${group.id}-${tool.name}-${idx}`}
              className={classes}
              data-phase={`tool-${tool.name}`}
              data-kind="tool"
              title={tool.name}
            >
              <span className="syn-dot" aria-hidden="true" />
              <span className="syn-lbl">{tool.label}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function useFlash(value: unknown): boolean {
  const [flashing, setFlashing] = useState(false);
  const previousRef = useRef(value);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (previousRef.current === value) return;
    previousRef.current = value;
    setFlashing(true);
    if (timerRef.current !== null) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => setFlashing(false), FLASH_MS);
    return () => {
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [value]);

  return flashing;
}

export function SynapseSection() {
  const entityState = useEntityStore((s) => s.state);
  const lastTool = useToolActivityStore((s) => s.lastTool);
  const lastToolFiredAt = useToolActivityStore((s) => s.firedAt);
  const activeId = ENTITY_TO_NODE[entityState];
  const kernelFlashing = useFlash(activeId);
  const toolFlashing = useFlash(lastToolFiredAt);

  return (
    <>
      <div className="syn-cat">
        <div className="syn-cat-head">
          <span>Kernel Path</span>
          <span className="syn-cat-origin">chat.py · tools.py</span>
        </div>
        <div className="syn-flow">
          {SYNAPSE_NODES.map((node) => (
            <KernelNode
              key={node.id}
              node={node}
              activeId={activeId}
              flashing={kernelFlashing}
            />
          ))}
        </div>
      </div>
      <div className="syn-cat">
        <div className="syn-cat-head">
          <span>Tools</span>
          <span className="syn-cat-origin">{lastTool ?? 'idle'}</span>
        </div>
        {TOOL_GROUPS.map((group) => (
          <ToolGroupBlock
            key={group.id}
            group={group}
            lastTool={lastTool}
            toolFlashing={toolFlashing}
          />
        ))}
      </div>
    </>
  );
}

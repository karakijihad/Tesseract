import { useRef, useCallback } from 'react';
import type { PaneNode, PaneLeaf, PaneSplit } from '../types';
import { useTerminalStore } from '../../stores/terminal';
import { TerminalInstance } from './TerminalInstance';

// ── Resize Handle ───────────────────────────────────────

function ResizeHandle({ splitId, direction }: { splitId: string; direction: 'horizontal' | 'vertical' }) {
  const handleRef = useRef<HTMLDivElement>(null);

  const onMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    const parent = handleRef.current?.parentElement;
    if (!parent) return;

    const rect = parent.getBoundingClientRect();
    const isHoriz = direction === 'horizontal';

    const onMove = (ev: MouseEvent) => {
      const pos = isHoriz ? ev.clientY - rect.top : ev.clientX - rect.left;
      const size = isHoriz ? rect.height : rect.width;
      const ratio = Math.max(0.15, Math.min(0.85, pos / size));
      useTerminalStore.getState().setPaneRatio(splitId, ratio);
    };

    const onUp = () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    };

    document.body.style.cursor = isHoriz ? 'row-resize' : 'col-resize';
    document.body.style.userSelect = 'none';
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  }, [splitId, direction]);

  return (
    <div
      ref={handleRef}
      className={`wt-resize wt-resize--${direction}`}
      onMouseDown={onMouseDown}
    />
  );
}

// ── Terminal Pane (leaf) ────────────────────────────────

function TerminalPane({ pane }: { pane: PaneLeaf }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const focusedPaneId = useTerminalStore((s) => s.focusedPaneId);
  const isFocused = focusedPaneId === pane.id;

  const onFocus = () => {
    useTerminalStore.getState().setFocusedPane(pane.id);
  };

  const onClose = (e: React.MouseEvent) => {
    e.stopPropagation();
    useTerminalStore.getState().closePane(pane.id);
  };

  let ownerLabel: string;
  let ownerTone: string;
  if (pane.owner === 'entity') {
    ownerLabel = 'tars-owned';
    ownerTone = 'is-owner_entity';
  } else {
    ownerLabel = 'operator-owned';
    ownerTone = 'is-owner_user';
  }

  return (
    <div className={`wt-pane${isFocused ? ' is-focused' : ''}`}>
      <button
        type="button"
        className="wt-pane-close"
        onClick={onClose}
        aria-label="Close pane"
        title="Close pane (Alt+Shift+X)"
      >
        ×
      </button>
      <div className={`wt-pane-header t-meta ${ownerTone}`} title={`pane ${pane.id}`}>
        {ownerLabel}
      </div>
      <div className="wt-canvas" ref={containerRef} onClick={onFocus}>
        {pane.ptyStatus === 'running' || pane.ptyStatus === 'starting' ? (
          <TerminalInstance paneId={pane.id} containerRef={containerRef} />
        ) : pane.ptyStatus === 'error' ? (
          <div className="wt-pane-msg wt-pane-msg--error">
            <span>{pane.errorMessage || 'Terminal error'}</span>
          </div>
        ) : (
          <div className="wt-pane-msg">
            <span>Stopped</span>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Split Node ──────────────────────────────────────────

function SplitNode({ node }: { node: PaneSplit }) {
  const isHoriz = node.direction === 'horizontal';
  const firstSize = `${node.ratio * 100}%`;
  const secondSize = `${(1 - node.ratio) * 100}%`;

  return (
    <div className={`wt-split wt-split--${node.direction}`}>
      <div className="wt-split-child" style={isHoriz ? { height: firstSize } : { width: firstSize }}>
        <PaneTree node={node.first} />
      </div>
      <ResizeHandle splitId={node.id} direction={node.direction} />
      <div className="wt-split-child" style={isHoriz ? { height: secondSize } : { width: secondSize }}>
        <PaneTree node={node.second} />
      </div>
    </div>
  );
}

// ── PaneTree (entry point) ──────────────────────────────

export function PaneTree({ node }: { node: PaneNode }) {
  if (node.type === 'leaf') return <TerminalPane pane={node} />;
  return <SplitNode node={node} />;
}

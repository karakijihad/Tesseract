import type { ReactNode } from 'react';

import './BulkBar.css';

import { Button } from './Button';

interface BulkBarProps {
  /** How many rows are selected. At zero the bar does not render, so no
   *  caller has to remember the guard. */
  count: number;
  onClear: () => void;
  /** True while a run is in flight. Says so, and the caller disables its own
   *  verbs on the same flag. */
  busy?: boolean;
  /** What the count is scoped to, when the visible rows are not the whole
   *  list. The inbox pages, so it says "on this page"; the chat rail does
   *  not, so it says nothing. */
  scope?: string;
  /** The verbs. Each one is the caller's, because what can be done to a row
   *  is the surface's own question. */
  children: ReactNode;
}

/** What can be done to the rows that are selected.
 *
 * The shell is shared and the verbs are not: every surface with a selection
 * needs the same count, the same way out and the same "working" state, and no
 * two of them agree on what the verbs are. The inbox had this privately and
 * the chat rail is the second to want it.
 *
 * `Clear` is here rather than in each caller because it is the only control on
 * the bar that means the same thing everywhere, and a surface that forgot it
 * would leave a selection with no way out but ticking every box again.
 */
export function BulkBar({ count, onClear, busy = false, scope, children }: BulkBarProps) {
  if (count <= 0) return null;
  return (
    <div className="bulk-bar" role="group" aria-label="Actions for selected rows">
      <span className="t-meta bulk-bar__count">
        {count} selected{scope ? ` ${scope}` : ''}
      </span>
      {children}
      <Button onClick={onClear} disabled={busy}>
        Clear
      </Button>
      {busy && <span className="t-meta">working…</span>}
    </div>
  );
}

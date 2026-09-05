import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { RefObject } from 'react';

export interface RowSelection<T extends string = string> {
  selected: ReadonlySet<T>;
  isSelected: (id: T) => boolean;
  /** One row's box. `next` is what the box now reads. */
  toggle: (id: T, next: boolean) => void;
  /** The header box. On means every visible row; off means none at all. */
  toggleAll: (next: boolean) => void;
  clear: () => void;
  allVisibleSelected: boolean;
  someVisibleSelected: boolean;
  /** Give this to the header `Checkbox`'s `inputRef`. */
  selectAllRef: RefObject<HTMLInputElement | null>;
  /** What survives a bulk action: everything the verb did not touch, plus
   *  what it tried and could not do. */
  settle: (attempted: readonly T[], failed: ReadonlySet<T>) => void;
}

/** Selecting several rows, for any surface that lists them.
 *
 * The inbox built this first and the chat rail is the second to want it, so it
 * lives here rather than being written twice. Three of the things it does are
 * the ones a second copy would get wrong:
 *
 * **The selection is scoped to what is VISIBLE.** Ticking a header box that
 * silently also selects the ninety rows on the next four pages is how a bulk
 * delete becomes a surprise. What you can see is what you have selected, and a
 * row that pages or filters out of view leaves the selection with it: a
 * decision must not stay armed against something off-screen.
 *
 * **`indeterminate` is a DOM property with no React prop.** The header box is
 * neither on nor off while some rows are ticked, and the only way to say so is
 * to set it on the node, which is what `selectAllRef` is for.
 *
 * **`settle` is the one the inbox had to fix in the field.** After a bulk run,
 * narrowing the selection to what FAILED also drops the rows that were selected
 * but not eligible for that verb, so on a mixed list one verb silently
 * deselected the rows beside it and the next verb acted on nothing. What
 * survives is what the verb did not attempt, plus what it attempted and could
 * not do.
 *
 * `visibleIds` needs no memoising: the hook keys its own work on the ids
 * themselves, so a fresh array of the same ids on every render costs nothing.
 */
export function useRowSelection<T extends string = string>(
  visibleIds: readonly T[],
): RowSelection<T> {
  const [selected, setSelected] = useState<Set<T>>(new Set());
  const selectAllRef = useRef<HTMLInputElement | null>(null);

  // The ids, not the array, decide when the prune below runs: a caller that
  // rebuilds its list every render would otherwise re-run it every time. The
  // ids are read back off a ref rather than parsed out of the key, so nothing
  // here depends on what an id may not contain.
  const latest = useRef(visibleIds);
  latest.current = visibleIds;
  const key = visibleIds.join(' ');

  useEffect(() => {
    setSelected(prev => {
      if (prev.size === 0) return prev;
      const visible = new Set<T>(latest.current);
      const next = new Set([...prev].filter(id => visible.has(id)));
      return next.size === prev.size ? prev : next;
    });
  }, [key]);

  const allVisibleSelected = visibleIds.length > 0 && visibleIds.every(id => selected.has(id));
  const someVisibleSelected = visibleIds.some(id => selected.has(id));

  useEffect(() => {
    if (selectAllRef.current) {
      selectAllRef.current.indeterminate = someVisibleSelected && !allVisibleSelected;
    }
  }, [someVisibleSelected, allVisibleSelected]);

  // Every ACTION is stable, and that is a contract rather than a tidiness.
  // All four reach state through the functional form and the visible ids
  // through `latest`, so none of them needs a dependency — and a caller that
  // puts one in an effect's dependency list must be able to trust it.
  //
  // They were rebuilt on every selection change, and both call sites paid for
  // it: the chat rail clears its selection when the tab changes, keyed on
  // `[tab, clear]`, so ticking a box changed `selected`, minted a new `clear`,
  // re-ran the effect and wiped the tick that had just happened. The same
  // shape with an unguarded `clear` spun the render loop that killed the test
  // worker. A hook handing out callbacks whose identity moves with its state
  // is the trap; this is the fix, in the one place both callers share.
  const toggle = useCallback((id: T, next: boolean) => {
    setSelected(prev => {
      const s = new Set(prev);
      if (next) s.add(id);
      else s.delete(id);
      return s;
    });
  }, []);

  const toggleAll = useCallback((next: boolean) => {
    setSelected(prev =>
      next ? new Set<T>(latest.current) : prev.size === 0 ? prev : new Set<T>(),
    );
  }, []);

  // The same set when it is already empty, so clearing nothing renders nothing.
  const clear = useCallback(() => {
    setSelected(prev => (prev.size === 0 ? prev : new Set<T>()));
  }, []);

  const settle = useCallback((attempted: readonly T[], failed: ReadonlySet<T>) => {
    const tried = new Set(attempted);
    setSelected(prev => new Set([...prev].filter(id => !tried.has(id) || failed.has(id))));
  }, []);

  return useMemo(
    () => ({
      selected,
      isSelected: (id: T) => selected.has(id),
      toggle,
      toggleAll,
      clear,
      allVisibleSelected,
      someVisibleSelected,
      selectAllRef,
      settle,
    }),
    [selected, allVisibleSelected, someVisibleSelected, toggle, toggleAll, clear, settle],
  );
}

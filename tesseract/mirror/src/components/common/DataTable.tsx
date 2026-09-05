import type { ReactNode } from "react";

export interface DataColumn {
  /** Heading text. Lower case, like the rest of the app's meta tier. */
  label: ReactNode;
  /** Track size for this column, as a grid track (`200px`, `minmax(0, 1fr)`).
   *  The caller decides the shape of its own data; the table decides
   *  everything else.
   *
   *  **Not `max-content` or `auto`.** Every row is its own grid, so a
   *  content-sized track measures the row it is in and nothing else: each row
   *  picks a different width and no column lines up with its heading. Model
   *  roles shipped exactly that. Give a fixed width, or `minmax(0, 1fr)` for
   *  the column that should absorb the slack. */
  width: string;
}

export interface DataRow {
  key: string;
  cells: ReactNode[];
}

interface DataTableProps {
  columns: DataColumn[];
  rows: DataRow[];
  /** Names the table for assistive tech. */
  label: string;
  /** How tall it may grow before it scrolls inside itself rather than growing
   *  the pane. Every settings table wants the same one; a surface with a
   *  genuinely different budget passes its own. */
  maxHeight?: string;
  /** Shown in place of the rows when there are none. */
  empty?: ReactNode;
}

/** The app's one table.
 *
 * Four of these were hand-rolled and no two agreed: the tool glossary was a
 * bordered card with a pinned head and its own scroll, the command floor was
 * a striped list with no head at all inside a second card, Local models was
 * four stacked panels, and Model roles grew the pane instead of scrolling.
 * Same object, four answers, and moving between two settings rows read as
 * moving between two applications.
 *
 * What is shared: the border, the ground, the pinned head, the row
 * separators, the hover, and the promise that the table scrolls inside itself
 * so the pane around it never moves. What stays with the caller: which
 * columns exist, how wide they are, and what goes in a cell.
 *
 * Deliberately not a `<table>`. Every one of these needs a grid so a caller
 * can size a column in `fr`, and `role="table"` on a grid gives assistive
 * tech the same structure without the layout fight.
 */
export function DataTable({
  columns,
  rows,
  label,
  maxHeight,
  empty,
}: DataTableProps) {
  const template = columns.map((c) => c.width).join(" ");
  const style = {
    ["--data-table-columns" as string]: template,
    ...(maxHeight ? { ["--data-table-max-height" as string]: maxHeight } : {}),
  };
  return (
    <div className="data-table" role="table" aria-label={label} style={style}>
      <div className="data-table__head t-meta" role="row">
        {columns.map((c, i) => (
          <span key={i} role="columnheader">
            {c.label}
          </span>
        ))}
      </div>
      {rows.length === 0 && empty !== undefined ? (
        <div className="data-table__empty t-meta">{empty}</div>
      ) : (
        rows.map((r) => (
          <div key={r.key} className="data-table__row" role="row">
            {r.cells.map((cell, i) => (
              <span key={i} role="cell" className="data-table__cell">
                {cell}
              </span>
            ))}
          </div>
        ))
      )}
    </div>
  );
}

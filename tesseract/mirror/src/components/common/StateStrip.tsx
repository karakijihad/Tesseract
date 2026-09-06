// The panel's one line, and the heading over a group of them.
//
// Health, Managed system, Memory and Channels are all the same act: a list of
// things, each with a state, its own sentence, when it last said anything, and
// sometimes one number. Four rooms writing that four times is how a panel ends
// up with four row shapes, so it is one component and the rooms differ only in
// what they put in it.
//
// The shape is deliberately bare. The diagnosis that produced this design was
// six bordered cards nested inside a bordered pane, so a line has a state edge
// and nothing else: no border, no card, no chrome. That is what lets a room
// hold twenty facts without a scrollbar.
//
// `Row` underneath, for the half that is shared: the cursor, the focus ring
// and the activation contract. A line with nowhere to go is not a Row at all,
// because a thing that cannot be opened must not look like it can.

import type { ReactNode } from 'react';
import { Row } from '../../components/common/Row';
import type { Obligation, OperationalState } from '../../lib/api';

export interface StateLine {
  key: string;
  /** The state the backend wrote. Never one decided here. */
  state: OperationalState;
  /** What the row asks of whoever reads it, which is what its colour says.
   *
   *  A row in a ROOM is a claim about now and carries one. A row in a record
   *  list is not: an entry card's ten past runs are outcomes, and colouring
   *  them by what they want would flatten every one of them to the same
   *  neutral. Those pass none, and the edge falls back to the state, which is
   *  their subject. The distinction is declared rather than accidental, and it
   *  is the panel's own rule: a row is actionable or it is explicitly a
   *  record. */
  obligation?: Obligation;
  /** What to call that state on screen. The backend's word. */
  label?: string;
  name: string;
  /** What is true of it, in the producer's own sentence. */
  said: ReactNode;
  /** The clock, already in words. */
  when?: string;
  /** The one number worth putting beside it. */
  value?: string;
  /** A word that is true of the line itself rather than of its state: who
   *  wrote it, in Managed system. It rides the row and not a heading over a
   *  band of rows, because a row read on its own still has to carry it. The
   *  backend's word; a line with nothing to mark simply has none. */
  tag?: string;
  /** Set when the line has somewhere to go. */
  onOpen?: () => void;
  /** Controls that act on this line, not on the row. */
  actions?: ReactNode;
  /** What this line contains, under it. A run is a subgraph and its steps are
   *  the answer to what it did; they belong beneath the run rather than in a
   *  level of their own, because a reader opening one is still reading the
   *  history above it. Rendered full width under the whole row. */
  more?: ReactNode;
}

export function Band({
  label,
  count,
}: {
  label: string;
  count?: number;
}): React.ReactElement {
  return (
    <div className="autonomy-band">
      <span className="autonomy-band__label t-meta t-label">{label}</span>
      {count !== undefined && (
        <span className="autonomy-band__count t-meta">{count}</span>
      )}
    </div>
  );
}

function Body({ line }: { line: StateLine }): React.ReactElement {
  return (
    <>
      <span className="state-line__edge" aria-hidden="true" />
      <span className="state-line__name">
        {line.name}
        {/* The edge's colour IS the state, and a colour is announced by
            nothing. The word goes in as real content rather than as an
            `aria-label` on a bare div, which most readers ignore. */}
        <span className="visually-hidden">{`, ${line.label ?? line.state}`}</span>
      </span>
      {/* Before the sentence in the source, whatever the grid does with it:
          this is the only element here a reader meets as content rather than
          through the row's own label, and "what it is, who wrote it, what it
          says" is the order that reads. */}
      {line.tag && <span className="state-line__tag t-meta">{line.tag}</span>}
      <span className="state-line__said">{line.said}</span>
      {line.value ? (
        <span className="state-line__value t-meta">{line.value}</span>
      ) : (
        // Holds the grid CELL, so it carries the same class as the thing it
        // stands in for: the row is laid out by named areas, and an item with
        // no area auto-places into whichever cell is free next. It is
        // furniture, so nothing reads it out.
        <span className="state-line__value" aria-hidden="true" />
      )}
      <span className="state-line__when t-meta">{line.when ?? ''}</span>
      {line.actions}
      {line.more && <div className="state-line__more">{line.more}</div>}
    </>
  );
}

export function StateStrip({
  lines,
  whole = false,
}: {
  lines: StateLine[];
  /** Show each sentence in full rather than clamped to two lines.
   *
   *  The clamp is what lets a room hold twenty facts without a scrollbar, and
   *  it is right wherever the line opens onto the whole text. Where nothing
   *  opens, it cuts the one sentence the line exists to carry, mid-word:
   *  `vector index was not (faiss=skipped fts=51 replayed=0 removed=0…` is
   *  what a step's own reason read as. Set by the caller, decided here. */
  whole?: boolean;
}): React.ReactElement {
  // Derived rather than passed: a room that marks its rows should not also
  // have to declare that it does, and the layout for a mark costs width the
  // rooms without one must not pay.
  const tagged = lines.some((line) => line.tag);
  return (
    <div
      className={`state-strip${whole ? ' state-strip--whole' : ''}${
        tagged ? ' state-strip--tagged' : ''
      }`}
    >
      {lines.map((line) =>
        line.onOpen ? (
          <Row
            key={line.key}
            onClick={line.onOpen}
            className={`state-line state-line--${line.obligation ?? line.state}`}
            ariaLabel={`${line.name}, ${line.label ?? line.state}${
              line.tag ? `, ${line.tag}` : ''
            }`}
          >
            <Body line={line} />
          </Row>
        ) : (
          // Not a Row: nothing opens, so nothing about it may suggest it does.
          <div
            key={line.key}
            className={`state-line state-line--${line.obligation ?? line.state}`}
          >
            <Body line={line} />
          </div>
        ),
      )}
    </div>
  );
}

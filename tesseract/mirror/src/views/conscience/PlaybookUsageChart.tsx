import { useState } from 'react';

import { Disclosure } from '../../components/common/Disclosure';
import { Meter } from '../../components/common/Meter';
import type { PlaybookUsageRow } from '../../stores/conscience';

/** How often each playbook was read, and how the work that followed went.
 *
 * The chart beside this one can answer only the first question. The tool
 * ledger records that a tool was called and nothing about the call; a playbook
 * load carries the revision and the session, and the turn records say how that
 * turn ended, so a procedure can be read as "reached for eleven times, worked
 * nine of them" rather than only as a count.
 *
 * **Three numbers, and the third is not a rounding error.** Worked and went
 * wrong are turns that closed. A load with no closed turn around it is
 * neither, and it is shown as its own figure rather than folded into either,
 * because a fortnight of turns still open would otherwise read as a procedure
 * that stopped working.
 *
 * The unread list is the half worth opening, exactly as it is for tools: a
 * playbook carried on every turn and never once read is costing that space
 * for nothing, and the switch that changes it is the file this panel names.
 * What a carried playbook actually puts in the prompt is its description, its
 * version, its status and its `use_when` — the trigger, the preconditions and
 * the steps all come from `playbook_search`, which is the off-list path.
 */
export function PlaybookUsageChart({
  rows,
  days,
  carriedCount,
  path,
  turnScopeNote,
}: {
  rows: PlaybookUsageRow[];
  days: number;
  carriedCount: number;
  path: string;
  /** Why the calls and cost figures are the turn's. Read off the backend so
   *  this panel and the chat answer cannot word it differently. */
  turnScopeNote: string;
}) {
  const [showUnread, setShowUnread] = useState(false);
  const [open, setOpen] = useState<string | null>(null);

  const read = rows.filter((r) => r.loads > 0);
  const unread = rows.filter((r) => r.loads === 0);
  const unreadCarried = unread.filter((r) => r.carried).length;
  const max = Math.max(1, ...read.map((r) => r.loads));

  return (
    <figure className="playbook-usage">
      <p className="playbook-usage__count">
        <strong>{read.length}</strong> of {rows.length} playbooks read in the
        last {days} days. {carriedCount} of them arrive on every turn with
        when to use them.
      </p>

      {read.length === 0 ? (
        <p className="t-meta">
          None have been read in this window. A playbook is counted when its
          file is opened, so this fills in as the assistant follows one.
        </p>
      ) : (
        <ol className="playbook-usage__bars">
          {read.map((r) => {
            const closed = r.succeeded + r.failed;
            return (
              <li key={r.playbook} className="playbook-usage__row">
                <span className="playbook-usage__name">{r.playbook}</span>
                <Meter value={r.loads / max} />
                <span className="playbook-usage__figures t-meta">
                  {r.loads} {r.loads === 1 ? 'read' : 'reads'}
                  {closed > 0
                    ? `, ${r.succeeded} of ${closed} worked`
                    : r.ungraded > 0
                      ? ', none of them in a turn whose task was checked'
                      : ', none of them in a turn that has closed yet'}
                  {r.corrections > 0
                    ? `, ${r.corrections} corrected`
                    : ''}
                  {/* What the flag actually says. It said 'searched for
                      first', which infers a `playbook_search` from the flag
                      alone, and `file_read` records a load however the path
                      was found. */}
                  {r.carried ? '' : ', not carried'}
                </span>
                {/* Whether the last rewrite of this playbook was worth
                    making. Undecided is the common answer and is said as
                    one: a fresh revision has barely been read. */}
                {r.previous_version ? (
                  <span className="playbook-usage__figures t-meta">
                    {r.improved === null
                      ? `too early to say whether it is better than v${r.previous_version}`
                      : r.improved
                        ? `better than v${r.previous_version}`
                        : `worse than v${r.previous_version}`}
                  </span>
                ) : null}
                {/* The turn's cost, not the playbook's, which is what the
                    caption says and why the wording is "those turns". */}
                <span className="playbook-usage__figures t-meta">
                  those turns made {r.turn_calls} tool calls and{' '}
                  {r.turn_cost_usd === null
                    ? 'their cost was not measured'
                    : `cost $${r.turn_cost_usd.toFixed(4)}`}
                </span>
                {(r.revisions.length > 1 ||
                  r.unjoined > 0 ||
                  r.ungraded > 0 ||
                  r.retries > 0) && (
                  <div className="playbook-usage__detail">
                    <Disclosure
                      variant="row"
                      open={open === r.playbook}
                      onToggle={() =>
                        setOpen(open === r.playbook ? null : r.playbook)
                      }
                      ariaControls={`playbook-usage-${r.playbook}`}
                    >
                      {r.revisions.length > 1
                        ? `${r.revisions.length} revisions`
                        : 'What the counts leave out'}
                    </Disclosure>
                    {open === r.playbook && (
                      <ul
                        id={`playbook-usage-${r.playbook}`}
                        className="playbook-usage__revisions t-meta"
                      >
                        {r.revisions.map((rev) => (
                          <li key={rev.version}>
                            v{rev.version}: {rev.loads}{' '}
                            {rev.loads === 1 ? 'read' : 'reads'},{' '}
                            {rev.succeeded} worked, {rev.failed} went wrong,{' '}
                            {rev.corrections} corrected
                            {rev.retries > 0
                              ? `, ${rev.retries} read again mid task`
                              : ''}
                            {rev.unjoined > 0
                              ? `, ${rev.unjoined} in a turn with no closing record`
                              : ''}
                            {rev.ungraded > 0
                              ? `, ${rev.ungraded} in a turn that closed its task without checks`
                              : ''}
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                )}
              </li>
            );
          })}
        </ol>
      )}

      <figcaption className="playbook-usage__caption t-meta">
        Worked means the turn that read it closed as done. A turn that closed
        its task with no project checks behind it counts as neither, because
        the only evidence there is what the assistant said. A read again mid
        task is the cheapest sign that the steps did not carry the work
        through. {turnScopeNote} Edit which ones arrive every turn in {path}.
      </figcaption>

      {unread.length > 0 && (
        <>
          <Disclosure
            variant="row"
            open={showUnread}
            onToggle={() => setShowUnread((v) => !v)}
            ariaControls="playbook-usage-unread"
          >
            {unread.length} never read in this window
          </Disclosure>
          {showUnread && (
            <div id="playbook-usage-unread">
              <p className="t-meta playbook-usage__unread-lead">
                {unreadCarried > 0
                  ? `${unreadCarried} of these arrive on every turn anyway, listed first. Those are the ones costing something for nothing.`
                  : 'None of these arrive on every turn, so none of them is costing anything.'}
              </p>
              <ul className="playbook-usage__unread">
                {[...unread]
                  .sort((a, b) => {
                    const weight = Number(b.carried) - Number(a.carried);
                    return weight !== 0
                      ? weight
                      : a.playbook.localeCompare(b.playbook);
                  })
                  .map((r) => (
                    <li
                      key={r.playbook}
                      className={`t-meta${r.carried ? ' is-carried' : ''}`}
                    >
                      {r.playbook}
                    </li>
                  ))}
              </ul>
            </div>
          )}
        </>
      )}

      {/* The same figures as text. A bar length is never the only way to read a
          chart, and a screen reader gets numbers rather than a shape. */}
      <table className="visually-hidden">
        <caption>Playbook usage over the last {days} days</caption>
        <thead>
          <tr>
            <th scope="col">Playbook</th>
            <th scope="col">Reads</th>
            <th scope="col">Worked</th>
            <th scope="col">Went wrong</th>
            <th scope="col">Corrected</th>
            <th scope="col">No closing record</th>
            <th scope="col">Closed without checks</th>
            <th scope="col">Calls in those turns</th>
            <th scope="col">Cost of those turns</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={`${r.playbook}-row`}>
              <th scope="row">{r.playbook}</th>
              <td>{r.loads}</td>
              <td>{r.succeeded}</td>
              <td>{r.failed}</td>
              <td>{r.corrections}</td>
              <td>{r.unjoined}</td>
              <td>{r.ungraded}</td>
              <td>{r.turn_calls}</td>
              <td>
                {/* Three answers, not two. A playbook nobody read has no
                    turns to have cost anything, and calling that "not
                    measured" is the same mistake as calling an unread ledger
                    $0.0000, one row over. */}
                {r.turns === 0
                  ? '—'
                  : r.turn_cost_usd === null
                    ? 'not measured'
                    : `$${r.turn_cost_usd.toFixed(4)}`}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  );
}

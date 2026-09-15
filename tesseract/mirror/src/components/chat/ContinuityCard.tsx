import { useId, useState } from 'react';
import { Block } from '../common/Block';
import { Disclosure } from '../common/Disclosure';
import { Fact } from '../common/Fact';
import { Markdown } from '../common/Markdown';
import { continuitySummary, type ContinuityPackage } from '../../lib/continuityPackage';
import './ContinuityCard.css';

interface Props {
  package: ContinuityPackage;
  timestamp: number;
}

// One title, whatever mark carried the text: a live boundary note reaches
// this under `mark: "boundary"` and the same words reload out of history
// under `_runtime: "boundary"`, `"continuity"` or `"carry_on"` depending on
// what happened next. The card is built from the CONTENT, so all of them draw
// the same thing here rather than four labels for one event.
const TITLE = 'Where the work stood when this conversation was cleared';

function List({ items }: { items: string[] }) {
  return (
    <ul className="continuity-card__list">
      {items.map((item, i) => (
        <li key={i}>
          <Markdown variant="inline">{item}</Markdown>
        </li>
      ))}
    </ul>
  );
}

/**
 * The continuity package `brain/continuity.py` writes, drawn as a card
 * instead of a paragraph of labelled lines.
 *
 * Collapsed, it still reads: the title says what this is and one line under
 * it says what the work was for. Opening it lays out every field the boundary
 * actually filled in, in the order a person would ask them, and nothing it
 * left blank gets a row.
 */
export function ContinuityCard({ package: pkg, timestamp }: Props) {
  const [open, setOpen] = useState(false);
  const bodyId = useId();
  const time = new Date(timestamp).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
  });

  return (
    <div className="continuity-card">
      <Block title={TITLE} meta={time}>
        <p className="continuity-card__summary t-meta">{continuitySummary(pkg)}</p>
        <Disclosure
          open={open}
          onToggle={() => setOpen(v => !v)}
          className="continuity-card__toggle"
          ariaControls={bodyId}
        >
          <span className="t-meta">{open ? 'hide the detail' : 'show the detail'}</span>
        </Disclosure>
        {open && (
          <div className="continuity-card__fields" id={bodyId}>
            {pkg.objective && <Fact label="Objective">{pkg.objective}</Fact>}
            {pkg.phase && <Fact label="Phase">{pkg.phase}</Fact>}
            {pkg.completed.length > 0 && (
              <Fact label="Done">
                <List items={pkg.completed} />
              </Fact>
            )}
            {pkg.remaining.length > 0 && (
              <Fact label="Remaining">
                <List items={pkg.remaining} />
              </Fact>
            )}
            {pkg.nextAction && <Fact label="Next">{pkg.nextAction}</Fact>}
            {pkg.openQuestions.length > 0 && (
              <Fact label="Open questions">
                <List items={pkg.openQuestions} />
              </Fact>
            )}
            {pkg.blockedBy && <Fact label="Blocked by">{pkg.blockedBy}</Fact>}
            {pkg.workRoot && <Fact label="Working from">{pkg.workRoot}</Fact>}
            {pkg.artifacts.length > 0 && (
              <Fact label="Artifacts">
                <List items={pkg.artifacts} />
              </Fact>
            )}
          </div>
        )}
      </Block>
    </div>
  );
}

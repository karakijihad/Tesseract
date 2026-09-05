// Narrowing what the picture draws.
//
// Two halves, and the difference between them is what a person is asking. A
// filter over RECORDS takes dots off the picture: only memories, only what
// arrived since a given drawing. A filter over LINKS decides which connections
// are drawn, and it also decides which ones the picture follows when a
// neighbourhood is expanded, so narrowing to the links a record states itself
// and then following them out walks the stated ones.
//
// Every word naming a kind, a class or a kind of link comes with the payload.
// The only words this file writes are about the question being asked.
//
// **Checklists, not chips, and never folded away.** They were chips behind a
// disclosure, which is the shape for a control you reach for occasionally
// beside something else. This is a column of its own now with nothing
// competing for it, and the operator asked for a checklist: a chip says what
// is ON and leaves what is OFF looking like a suggestion, where a box says
// both at once. Nothing on this surface has to be opened to find out what is
// narrowing the picture.
//
// **An empty list means every one of them.** That is not the same claim as
// every box ticked and the difference matters when a new kind of record
// arrives: with none ticked it is drawn, and with all ticked it would not be.
// So the head of each group says which state it is in rather than leaving a
// column of empty boxes to be read as "nothing is shown".

import { Button } from '../../components/common/Button';
import { Checkbox } from '../../components/common/Checkbox';
import { Select } from '../../components/common/Select';
import type { GraphResponse } from '../../lib/api';
import { arrivals, narrowing, toggle, type Lens } from './lens';

const WHENEVER = '';

/** One group of boxes over a list the payload named.
 *
 *  `chosen` empty means every one of them, so the caption says so: a column of
 *  unticked boxes over a picture showing everything reads as broken until you
 *  work out why.
 */
function Choices({
  label,
  options,
  chosen,
  onToggle,
}: {
  label: string;
  options: { key: string; label: string }[];
  chosen: string[];
  onToggle: (key: string) => void;
}): React.ReactElement {
  return (
    <div className="graph-filters__group">
      <span className="t-meta t-label">
        {label}
        <span className="graph-filters__state">
          {chosen.length === 0 ? 'all of them' : `${chosen.length} chosen`}
        </span>
      </span>
      <div className="graph-filters__list">
        {options.map((option) => (
          <Checkbox
            key={option.key}
            label={option.label}
            checked={chosen.includes(option.key)}
            onChange={() => onToggle(option.key)}
          />
        ))}
      </div>
    </div>
  );
}

export function Filters({
  data,
  lens,
  onNarrow,
  onClear,
}: {
  data: GraphResponse;
  lens: Lens;
  onNarrow: (patch: Partial<Lens>) => void;
  onClear: () => void;
}): React.ReactElement {
  const on = narrowing(lens);
  const days = arrivals(data);

  return (
    <div className="graph-filters" data-testid="graph-filters">
      <div className="graph-filters__head">
        <span className="t-meta">
          {on === 0
            ? 'nothing is narrowing the picture'
            : `${on} on`}
        </span>
        {on > 0 && (
          <Button onClick={onClear} ariaLabel="take every filter off">
            show everything again
          </Button>
        )}
      </div>

      <Choices
        label="What kind of record"
        options={data.kinds}
        chosen={lens.kinds}
        onToggle={(key) => onNarrow({ kinds: toggle(lens.kinds, key) })}
      />

      <div className="graph-filters__group">
        <span className="t-meta t-label">When it arrived</span>
        <Select
          value={lens.since ?? WHENEVER}
          options={[
            { value: WHENEVER, label: 'whenever' },
            ...days.map((day) => ({
              value: day.at,
              label: `${day.day} or later`,
            })),
          ]}
          onChange={(next) =>
            onNarrow({ since: next === WHENEVER ? null : next })
          }
          ariaLabel="when the record arrived on the map"
        />
      </div>

      <Choices
        label="Where the link came from"
        options={data.provenance}
        chosen={lens.provenance}
        onToggle={(key) =>
          onNarrow({ provenance: toggle(lens.provenance, key) })
        }
      />

      <Choices
        label="What kind of link"
        options={data.links}
        chosen={lens.linkTypes}
        onToggle={(key) => onNarrow({ linkTypes: toggle(lens.linkTypes, key) })}
      />

      <div className="graph-filters__group">
        <span className="t-meta t-label">Only some of the links</span>
        <div className="graph-filters__list">
          <Checkbox
            label="only the ones between two compartments"
            checked={lens.crossingOnly}
            onChange={() => onNarrow({ crossingOnly: !lens.crossingOnly })}
          />
          <Checkbox
            label={
              data.delta.known
                ? 'only the ones the last drawing added'
                : 'the last drawing did not record what it added'
            }
            checked={lens.newOnly}
            disabled={!data.delta.known}
            onChange={() => onNarrow({ newOnly: !lens.newOnly })}
          />
        </div>
      </div>
    </div>
  );
}

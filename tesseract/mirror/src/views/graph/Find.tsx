// Finding a record by name, and moving the picture to it.
//
// The one control on this surface that moves the picture. Everything else
// takes records off it or puts them back: positions are settled by the build,
// so a person who has learnt where something sits keeps that, and the view
// only travels when they ask it to.
//
// It does not take focus when it appears: the map arrives a moment after the
// panel is opened, and a field that grabbed the keyboard then would take it
// from wherever the operator was typing.
//
// It searches what the payload already holds, which is why there is no round
// trip: every record on the picture arrived with it, so typing answers as fast
// as it is typed. What is NOT on the picture cannot be found here, and the
// hint says so rather than letting an empty result read as an empty library.

import { useState } from 'react';

import { Input } from '../../components/common/Input';
import { MenuItem } from '../../components/common/MenuItem';
import { clock } from '../../lib/time';
import type { GraphResponse } from '../../lib/api';
import { matches } from './lens';

/** What a kind of record is called, in the backend's words. An unknown kind
 *  falls back to what the payload sent rather than to a phrase this file made
 *  up. */
function said(data: GraphResponse, kind: string): string {
  return data.kinds.find((word) => word.key === kind)?.label ?? kind;
}

export function Find({
  data,
  onFind,
}: {
  data: GraphResponse;
  /** Open that record and take the picture to it. */
  onFind: (id: string) => void;
}): React.ReactElement {
  const [query, setQuery] = useState('');
  const hits = matches(data, query);

  return (
    <div className="graph-find">
      <Input
        type="search"
        value={query}
        onChange={setQuery}
        ariaLabel="find a record by name"
        placeholder="find a record by name"
        className="graph-find__field"
        testId="graph-find"
      />
      {query.trim() !== '' && hits.length === 0 && (
        <p className="t-meta">
          Nothing on the picture is called that. The map draws the most
          connected records, so something the library holds can be missing from
          it.
        </p>
      )}
      {hits.length > 0 && (
        <div className="graph-find__hits">
          {hits.map((hit) => (
            <MenuItem
              key={hit.id}
              onClick={() => {
                setQuery('');
                onFind(hit.id);
              }}
              ariaLabel={`open ${hit.title || hit.id}`}
              className="graph-find__hit"
            >
              <span className="graph-find__name">{hit.title || hit.id}</span>
              {/* What kind of record it is and when it arrived. Measured on
                  the live map: a job that has run eight times is eight nodes
                  with the same title, and a list of eight identical names is
                  a list nobody can choose from. The kind's words are the
                  payload's. */}
              <span className="graph-find__where t-meta">
                {said(data, hit.kind)}
                {hit.firstSeen && `, ${clock(hit.firstSeen)}`}
              </span>
              <span className="graph-find__where t-meta">{hit.locator}</span>
            </MenuItem>
          ))}
        </div>
      )}
    </div>
  );
}

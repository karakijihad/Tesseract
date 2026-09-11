// Where a visit starts.
//
// The map never opens on the whole library. Six hundred records drawn at once
// is a picture of a library rather than a picture of anything IN it, and a
// reader cannot tell it apart from a picture of all three and a half thousand.
// So the surface opens on the ways in, each one saying how much it holds, and
// the picture is drawn around whichever is chosen.
//
// Everything here counts what the payload sent. What each way in MEANS is the
// backend's: what arrived in the last drawing is the graph's own record of it,
// the most connected records are the same ranking the atlas page prints, and
// whether a link crosses between compartments is a fact on the link.

import { MenuItem } from '../../components/common/MenuItem';
import { Note } from '../../components/common/Note';
import type { GraphResponse } from '../../lib/api';
import { Find } from './Find';
import type { Way } from './lens';

interface Opening {
  way: Way;
  name: string;
  /** What it shows, and why it is worth starting there. */
  why: string;
  count: number;
  /** Set when there is nothing to open, and it says which kind of nothing. */
  instead?: string;
}

function openings(data: GraphResponse): Opening[] {
  const crossing = new Set<string>();
  for (const edge of data.edges) {
    if (!edge.crosses) continue;
    crossing.add(edge.subject);
    crossing.add(edge.object);
  }
  return [
    {
      way: 'new',
      name: 'What arrived in the last drawing',
      why: 'the records the last pass added, and what they attached to',
      count: data.delta.nodes.length,
      instead: !data.delta.known
        ? 'the drawing that made this map did not record what was new'
        : data.delta.nodes.length === 0
          ? 'the last pass added nothing that is on the picture'
          : undefined,
    },
    {
      way: 'crossing',
      name: 'What links two compartments',
      why: 'a memory kept out of something it did, and the like. These are what make it one brain rather than four piles',
      count: crossing.size,
      instead:
        crossing.size === 0
          ? 'nothing on the picture links two compartments yet'
          : undefined,
    },
    {
      way: 'hubs',
      name: 'The most connected records',
      why: 'what the rest of the library points at, and everything one link out from them',
      count: data.hubs.length,
      instead:
        data.hubs.length === 0
          ? 'nothing on the picture has more than one connection'
          : undefined,
    },
    {
      way: 'orphans',
      name: 'What nothing points at',
      why: 'records nothing connects to, in either direction, so no walk of the graph arrives. Searching for their words still finds them',
      count: data.orphans.length,
      instead:
        data.orphans.length === 0
          ? 'nothing on the picture has zero connections'
          : undefined,
    },
    {
      way: 'all',
      name: 'Everything the map drew',
      why: 'the whole working set at once, which is a lot to read and the honest place to start looking for a shape',
      count: data.drawn.nodes,
    },
  ];
}

export function Ways({
  data,
  onEnter,
  onFind,
}: {
  data: GraphResponse;
  onEnter: (way: Way) => void;
  onFind: (id: string) => void;
}): React.ReactElement {
  return (
    <div className="graph-ways" data-testid="graph-ways">
      <Note>
        The map opens on a question rather than on everything at once. Pick
        somewhere to start, or find a record by name. Whichever you choose, you
        can follow the links out from there.
      </Note>

      <Find data={data} onFind={onFind} />

      <div className="graph-ways__list">
        {openings(data).map((opening) => (
          <MenuItem
            key={opening.way}
            onClick={() => onEnter(opening.way)}
            disabled={opening.instead !== undefined}
            ariaLabel={opening.name}
            className="graph-ways__row"
          >
            <span className="graph-ways__name">
              {opening.name}
              {opening.instead === undefined && (
                <span className="graph-ways__count t-meta">
                  {opening.count.toLocaleString()}
                </span>
              )}
            </span>
            <span className="graph-ways__why t-meta">
              {opening.instead ?? opening.why}
            </span>
          </MenuItem>
        ))}
      </div>
    </div>
  );
}

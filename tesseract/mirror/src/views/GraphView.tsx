// The graph surface. Its own screen, on ruling 12, because a canvas demands
// the whole of one and the one thing it cannot help with is a failed run.
//
// The Atlas room keeps saying whether the map is current and what it could not
// make sense of. This says how everything connects, and nothing else.
//
// **It opens on a question, never on the library.** Six hundred records drawn
// at once is a picture of a library rather than a picture of anything in it,
// and a reader cannot tell it apart from a picture of all three and a half
// thousand. So the first thing here is the ways in, and the picture is drawn
// around whichever one is chosen. `views/graph/lens.ts` is the whole of what
// that means; this file is where it is put on a screen.
//
// **Every word about the machine here is the backend's.** The sentence over
// the picture, what a compartment is called, what a kind of record is called,
// what a kind of link says, and what the map does not reach all arrive with
// the payload. What this file says in its own words is about the app: that the
// picture is loading, that the read failed, how to move around it, and how
// much of what arrived is currently on screen.

import { useEffect, useMemo, useState } from 'react';

import { Block } from '../components/common/Block';
import { Button } from '../components/common/Button';
import { Chip } from '../components/common/Chip';
import { Note } from '../components/common/Note';
import { Segmented } from '../components/common/Segmented';
import { Tabs } from '../components/common/Tabs';
import { Hint } from '../components/ui/Hint';
import { ViewHeader } from '../components/common/ViewHeader';
import { clock } from '../lib/time';
import type { GraphRecord, GraphResponse } from '../lib/api';
import { useGraphStore } from '../stores/graph';
import { Filters } from './graph/Filters';
import { Find } from './graph/Find';
import { GraphCanvas } from './graph/GraphCanvas';
import { Inspector } from './graph/Inspector';
import { Ways } from './graph/Ways';
import { REACH_MAX, narrowing, view, type Lens, type Way } from './graph/lens';

/** How often the surface re-reads while it is open. The map is drawn once a
 *  night, so this is slow on purpose: it is the backstop, and a redraw from
 *  the Atlas room is heard about rather than waited for. */
const POLL_MS = 120_000;

/** How far out the picture follows the links, as a person picks it. The
 *  wording is the app's own, because this is a control and not a claim about
 *  the library. */
const STEPS = [
  { key: 0, label: 'just these' },
  { key: 1, label: '1 step out' },
  { key: 2, label: '2 steps' },
  { key: 3, label: '3 steps' },
].slice(0, REACH_MAX + 1);

/** What the column beside the map holds, one at a time.
 *
 *  It was a stack of five cards behind a scroll: the open record, this
 *  picture, how far it follows the links, what is narrowed, the compartments.
 *  Measured at the cockpit's default panel, that was 523 px of rail in a
 *  453 px column, so something was always below the fold and the thing below
 *  it was whichever the reader had not scrolled to. Cards made the pile
 *  legible; they did not stop it being a pile.
 *
 *  Tabs, because these are not a sequence and are not read together. A person
 *  is either reading a record, or narrowing what is drawn, or moving around
 *  the picture. */
type Rail = 'record' | 'picture' | 'narrow' | 'about';

/** How much of the map to fetch. The values the backend understands: empty is
 *  the ceiling `atlas.yaml` sets, and `all` is every record the graph holds.
 *
 *  A control and not a setting, because it is a trade the person looking at
 *  the picture makes rather than one the config makes for them. Measured on
 *  this machine: the shipped ceiling is 600 records and repaints in 4 ms;
 *  everything is 4,651 and repaints in 26, which is over a frame, so dragging
 *  stops being smooth. Both are useful and neither is right for every
 *  question. */
const AMOUNTS = [
  { key: '', label: 'the most connected' },
  { key: '2000', label: 'more of it' },
  { key: 'all', label: 'everything' },
];

/** The surface, over what it is handed.
 *
 *  Split from the store for the reason every room on the Autonomy panel is:
 *  what it draws for a given payload is the thing worth holding still, and a
 *  test that has to mount a store to ask is a test of the store.
 */
export function GraphSurface({
  data,
  status,
  error,
  selected,
  region,
  record,
  recordStatus,
  recordError,
  lens,
  focus,
  onSelect,
  onHold,
  onRefresh,
  onEnter,
  onFind,
  onExpand,
  onPin,
  onNarrow,
  onClearFilters,
  onRestart,
  onBack,
  chrome = true,
  drawn,
  onDraw,
  about,
}: {
  data: GraphResponse | null;
  status: 'idle' | 'loading' | 'ready' | 'error';
  error: string | null;
  selected: string | null;
  region: string | null;
  record: GraphRecord | null;
  recordStatus: 'idle' | 'loading' | 'ready' | 'error';
  recordError: string | null;
  lens: Lens;
  focus: { id: string; nonce: number } | null;
  onSelect: (id: string | null) => void;
  onHold: (region: string | null) => void;
  onRefresh: () => void;
  onEnter: (way: Way) => void;
  onFind: (id: string) => void;
  onExpand: (by: number) => void;
  onPin: (id: string) => void;
  onNarrow: (patch: Partial<Lens>) => void;
  onClearFilters: () => void;
  onRestart: () => void;
  /** Leave the map for the room it belongs to. Absent where there is nowhere
   *  to go back to, which is the map drawn INSIDE that room. */
  onBack?: () => void;
  /** Whether the surface draws its own title and actions. False when it is
   *  embedded in a room that has already named it: two headings for one thing
   *  is the surface arguing with the room about what it is. */
  chrome?: boolean;
  /** How much of the map was asked for, and how to ask for a different
   *  amount. This is what is FETCHED, not what is drawn from what arrived, so
   *  it belongs to the store rather than to the lens. */
  drawn?: string;
  onDraw?: (drawn: string) => void;
  /** What the room around the map knows about it: whether it is current, what
   *  it does not cover, what the last pass did. Rendered as a tab in the same
   *  column as everything else made of words, because the operator's rule for
   *  this surface is two containers and the picture owns one of them. */
  about?: React.ReactNode;
}): React.ReactElement {
  // What is on the picture, worked out once and used by both the canvas and
  // the sentence over it, so what a person sees and what they are told cannot
  // differ. Held across renders because a drag repaints and this walks every
  // link.
  const shown = useMemo(
    () => (data ? view(data, lens) : null),
    [data, lens],
  );

  // Which of the three the column is showing. Keyed on the SELECTION and on
  // nothing else: clicking a record is the ask, so it opens the record, and
  // the slow poll behind this surface re-renders without moving the tab under
  // a reader who is mid-filter.
  const [tab, setTab] = useState<Rail>('picture');
  useEffect(() => {
    if (selected) setTab('record');
  }, [selected]);

  const frame = `graph-view${chrome ? '' : ' graph-view--embedded'}`;

  if (status === 'error') {
    return (
      <div className={frame} data-testid="graph-view">
        {chrome && <ViewHeader title="The map" />}
        <Note tone="bad">The map could not be read. {error}</Note>
      </div>
    );
  }

  if (data === null || shown === null) {
    return (
      <div className={frame} data-testid="graph-view">
        {chrome && <ViewHeader title="The map" />}
        <p className="t-meta">Reading the map.</p>
      </div>
    );
  }

  // `built` is null when there is nothing to draw at all, and the payload's
  // own sentence is the only thing separating a machine nobody has mapped from
  // a file that cannot be read.
  const drawable = data.built !== null && data.nodes.length > 0;
  const pinnedNodes = lens.pinned
    .map((id) => data.nodes.find((n) => n.id === id))
    .filter((node): node is GraphResponse['nodes'][number] => node !== undefined);

  return (
    <div className={frame} data-testid="graph-view">
      {chrome && (
        <ViewHeader
          title="The map"
          meta={
            data.built
              ? `drawn ${clock(data.built.at)}${
                  data.built.stale ? ', by an older version of the drawing' : ''
                }`
              : undefined
          }
          actions={
            <>
              {/* The way out, first, because a full-screen canvas is the one
                  place in the cockpit with nothing else on it to click. The
                  map's home is the Atlas room and this goes there rather than
                  merely closing: closing leaves the operator on the orb, which
                  is not where they came from. */}
              {onBack && (
                <Button onClick={onBack} ariaLabel="go back to the Atlas room">
                  back to Atlas
                </Button>
              )}
              <Button onClick={onRefresh} ariaLabel="read the map again">
                refresh
              </Button>
            </>
          }
        />
      )}

      {/* **Two containers, always the same two.** Everything made of words on
          the left, the picture on the right with the whole of what is left.
          The operator's rule, and it now holds before a way in is chosen as
          well as after: the surface used to swap between two layouts, so the
          tabs, the filters and everything the room knows about the map did
          not exist until you had picked something. A control you cannot reach
          until you have already made a choice is a control you cannot use to
          make it.

          Before a way in is chosen the stage is empty on purpose. Drawing the
          whole library there would be exactly the picture this surface exists
          not to open on. */}
      <div className="graph-body">
          <div className="graph-side">
            {/* One at a time, chosen. The record leads because it is what a
                click just asked for, and it carries a mark rather than a
                count: there is either one open or there is not. */}
            <Tabs
              label="what the column beside the map is showing"
              fill
              items={[
                {
                  key: 'record' as const,
                  label: 'The record',
                  badge: selected ? '1' : null,
                },
                { key: 'picture' as const, label: 'This picture' },
                {
                  key: 'narrow' as const,
                  label: 'Narrowed',
                  badge: narrowing(lens) || null,
                },
                ...(about
                  ? [{ key: 'about' as const, label: 'About the map' }]
                  : []),
              ]}
              active={tab}
              onSelect={setTab}
            />

            {data.built !== null && data.built.unplaced > 0 && (
              <Note tone="warn">
                {data.built.unplaced} of these were left without a place by the
                last drawing, so they are all sitting on top of each other in
                the middle. The next nightly pass gives them one.
              </Note>
            )}

            {tab === 'record' && !selected && (
              <p className="t-meta">
                Click a record on the map and it opens here.
              </p>
            )}

            {tab === 'record' && selected && (
              <Inspector
                data={data}
                record={record}
                status={recordStatus}
                error={recordError}
                selected={selected}
                onOpen={onSelect}
                onStartHere={onFind}
                onPin={onPin}
                pinned={lens.pinned.includes(selected)}
              />
            )}

            {tab === 'picture' && lens.way === null && (
              <>
                <p className="graph-said">{data.said}</p>
                {/* When there is nothing to draw, the sentence above IS the
                    answer. A note repeating it would say the same thing
                    twice. */}
                {drawable && (
                  <Ways data={data} onEnter={onEnter} onFind={onFind} />
                )}
                {/* On the way in, where somebody is deciding what to look at,
                    rather than under a picture where it reads as a caveat on
                    what they are already looking at. */}
                <Block title="What the map does not reach">
                  {data.notReached.map((row) => (
                    <p key={row.what} className="t-meta">
                      <b>{row.what}</b> {row.why}
                    </p>
                  ))}
                </Block>
              </>
            )}

            {tab === 'picture' && lens.way !== null && (
              <>
            <Block
              title="This picture"
              meta={<span className="t-meta">{shown.nodes.size}</span>}
            >
              {/* Two sentences, and they answer two different questions. The
                  backend's says how much of the LIBRARY reached the browser
                  and in what order it chose; the app's says how much of THAT
                  is on the screen right now. A picture showing a tenth of what
                  arrived misleads exactly as much as one showing a tenth of
                  the library, so neither sentence can stand for the other. */}
              <p className="graph-said">{data.said}</p>
              <p className="t-meta">{shown.said}</p>
              <Find data={data} onFind={onFind} />
              <div className="graph-acts">
                <Button
                  onClick={onRestart}
                  ariaLabel="choose a different way in"
                >
                  start somewhere else
                </Button>
              </div>
              {data.drawn.edgesHidden > 0 && (
                <p className="t-meta">
                  {data.drawn.edgesHidden === 1
                    ? '1 link the map sent has an end it did not draw'
                    : `${data.drawn.edgesHidden} links the map sent have an end it did not draw`}
                </p>
              )}
              <p className="t-meta">
                {data.delta.known
                  ? `${data.delta.nodes.length} arrived in the last drawing`
                  : 'what arrived in the last drawing was not recorded'}
              </p>
            </Block>

            {/* The name says what the control does; the hint says what a step
                IS. "2 steps" on a button is not a unit anybody can read, and
                the only place that was explained was a label the eye never
                reaches. */}
            <Block
              title="How far it follows the links"
              titleHint="The picture starts from the records your way in chose. Each step out adds everything those records link to, so one step adds their neighbours and two adds the neighbours of those."
            >
              <Segmented
                label="how far out the picture follows the links"
                items={STEPS}
                value={lens.reach}
                onSelect={(step) => onExpand(step - lens.reach)}
                className="graph-tools__reach"
              />
            </Block>

            {onDraw && (
              <Block
                title="How much of it to draw"
                titleHint="The map holds more than a picture can show at once, so it draws the most connected first. Asking for everything draws every record the graph holds, which is worth doing to find something and slower to move around."
              >
                <Segmented
                  label="how much of the map to draw"
                  items={AMOUNTS}
                  value={drawn ?? ''}
                  onSelect={onDraw}
                  className="graph-tools__reach"
                />
              </Block>
            )}
              </>
            )}

            {tab === 'about' && about}

            {tab === 'narrow' && (
              <>
            <Filters
              data={data}
              lens={lens}
              onNarrow={onNarrow}
              onClear={onClearFilters}
            />

            {pinnedNodes.length > 0 && (
              <Block
                title="Kept in the picture"
                meta={<span className="t-meta">{pinnedNodes.length}</span>}
              >
                <div className="graph-pins">
                  {pinnedNodes.map((node) => (
                    <Chip
                      key={node.id}
                      active
                      onClick={() => onPin(node.id)}
                      ariaLabel={`stop keeping ${node.title || node.id} in the picture`}
                    >
                      {node.title || node.id}
                    </Chip>
                  ))}
                </div>
                {pinnedNodes.length > 1 && (
                  <p className="t-meta">
                    {shown.shared.length === 0
                      ? 'nothing on the picture touches all of these'
                      : `${shown.shared.length} record${
                          shown.shared.length === 1 ? '' : 's'
                        } all of these touch`}
                  </p>
                )}
              </Block>
            )}

            {/* The compartments, counting what is on the picture NOW over
                what the compartment holds. The payload's own `drawn` is about
                the whole working set, and printing it beside a narrowed
                picture said a compartment had more records on screen than the
                picture had records at all.

                These are the compartments' only name now. The canvas used to
                float each one over its own records, and a layout that settles
                a record beside whatever it links to made that a claim the
                picture could not support: a run that references a memory sits
                among the memories, so the body's name was written across the
                middle of them. A chip can be pressed, which the floating text
                never could. */}
            <Block
              title="Compartments"
              titleHint="The four halves of one brain. Press one to hold the picture on it, and everything else fades rather than leaving, so a link that crosses between two is still visible."
            >
              <div className="graph-legend">
                {data.regions
                  .filter((band) => (shown.byRegion.get(band.key) ?? 0) > 0)
                  .map((band) => (
                    <Chip
                      key={band.key}
                      active={region === band.key}
                      onClick={() => onHold(region === band.key ? null : band.key)}
                    >
                      {band.label}
                      <span className="graph-legend__count t-meta">
                        {shown.byRegion.get(band.key) === band.count
                          ? band.count
                          : `${shown.byRegion.get(band.key)} of ${band.count}`}
                      </span>
                    </Chip>
                  ))}
              </div>
            </Block>
              </>
            )}

          </div>

          <div
            className={`graph-stage${
              drawable && lens.way !== null ? '' : ' graph-stage--waiting'
            }`}
          >
            {(!drawable || lens.way === null) && (
              <p className="t-meta">
                {drawable
                  ? 'Pick a way in and the map is drawn around it.'
                  : 'There is nothing on the map to draw.'}
              </p>
            )}
            {drawable && lens.way !== null && (
              <>
              <GraphCanvas
                data={data}
                shown={shown}
                selected={selected}
                onSelect={onSelect}
                region={region}
                pinned={lens.pinned}
                focus={focus}
                /* The question, not the answer: the camera is fitted when a
                   way in changes and holds while its filters are worked. */
                frameKey={`${lens.way}:${lens.named ?? ''}`}
              />
              {/* The key to the colours, over the picture rather than under
                  it. A colour says what a record IS, so the key belongs beside
                  the thing it explains, and every band under the map is a band
                  the map does not get to use. Not clickable: holding the
                  picture on a compartment is a different question, and the
                  legend above already asks it. */}
              <div className="graph-key">
                {data.kinds
                  .filter((kind) => (shown.byKind.get(kind.key) ?? 0) > 0)
                  .map((kind) => (
                    <span key={kind.key} className="graph-key__item t-meta">
                      {/* The colour is the stylesheet's, keyed on the kind. An
                          inline `var(...)` would put a paint decision in a
                          view. */}
                      <span
                        className={`graph-key__dot graph-key__dot--${kind.key}`}
                      />
                      {kind.label}
                      <span className="graph-key__count">
                        {shown.byKind.get(kind.key)}
                      </span>
                    </span>
                  ))}
              </div>
            {/* Short enough to read without meaning to, with the rest on
                hover. It was five lines across the foot of the canvas, which
                is a paragraph sitting on top of the thing it describes: after
                the first read it is furniture, and it was covering records. */}
            <p className="graph-hint t-meta">
              <Hint
                label="Scroll to zoom, and the names come back as you go in. Drag the background to move the picture, or drag a record to pull it out of a knot. Click a record to open it. Anything you move goes back where the map put it the next time you open this."
                position="top"
              >
                <span>how to move around</span>
              </Hint>
            </p>
              </>
            )}
          </div>
        </div>

    </div>
  );
}

/** The surface, connected. Reads on mount and polls slowly behind that: the
 *  map is drawn once a night, and a redraw from the Atlas room is heard about
 *  rather than waited for.
 *
 *  **Two places draw this and they share one store.** The Atlas room draws it
 *  at the top of itself, because the map is what that room is about and a
 *  door to it was a room describing something the reader could not see; the
 *  cockpit panel draws it full size for when the picture is the work. One
 *  store means the way in, the filters and the open record survive the move
 *  between them, so opening it full size continues the question rather than
 *  starting it again. */
export function GraphView({
  chrome = true,
  onBack,
  about,
}: {
  chrome?: boolean;
  onBack?: () => void;
  about?: React.ReactNode;
} = {}): React.ReactElement {
  const data = useGraphStore((s) => s.data);
  const status = useGraphStore((s) => s.status);
  const error = useGraphStore((s) => s.error);
  const selected = useGraphStore((s) => s.selected);
  const region = useGraphStore((s) => s.region);
  const record = useGraphStore((s) => s.record);
  const recordStatus = useGraphStore((s) => s.recordStatus);
  const recordError = useGraphStore((s) => s.recordError);
  const lens = useGraphStore((s) => s.lens);
  const focus = useGraphStore((s) => s.focus);
  const load = useGraphStore((s) => s.load);
  const select = useGraphStore((s) => s.select);
  const holdOn = useGraphStore((s) => s.holdOn);
  const enter = useGraphStore((s) => s.enter);
  const find = useGraphStore((s) => s.find);
  const expand = useGraphStore((s) => s.expand);
  const pin = useGraphStore((s) => s.pin);
  const narrow = useGraphStore((s) => s.narrow);
  const clearFilters = useGraphStore((s) => s.clearFilters);
  const restart = useGraphStore((s) => s.restart);
  const drawn = useGraphStore((s) => s.drawn);
  const drawAmount = useGraphStore((s) => s.drawAmount);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  return (
    <GraphSurface
      data={data}
      status={status}
      error={error}
      selected={selected}
      region={region}
      record={record}
      recordStatus={recordStatus}
      recordError={recordError}
      lens={lens}
      focus={focus}
      onSelect={select}
      onHold={holdOn}
      onRefresh={() => void load()}
      onEnter={enter}
      onFind={find}
      onExpand={expand}
      onPin={pin}
      onNarrow={narrow}
      onClearFilters={clearFilters}
      onRestart={restart}
      chrome={chrome}
      onBack={onBack}
      drawn={drawn}
      onDraw={drawAmount}
      about={about}
    />
  );
}

// Atlas. The map of how everything it knows connects.
//
// **The map is drawn here, at the top, and the rest of the room is about it.**
// It used to be a door at the bottom and a note explaining that a canvas
// cannot share a screen with a failed run. That was the wrong way round: this
// room is ABOUT the map, so a reader arrived at four bands describing
// something they could not see and had to press a button to find out what any
// of it meant. The picture leads now, and the bands under it say whether it is
// current, what it does not cover, and what the last pass did. The full-size
// panel is still there for when the picture is the work rather than the
// subject, and it shares this one's store, so opening it continues the
// question rather than starting it again.
//
// Three bands under the picture, in the order a reader needs them: what the
// map says about itself, what it does not cover at all, and what the last pass
// over it did.
// The middle one is not a caveat bolted on. A map that quietly stops at the
// edge of what it indexed reads as a map of everything, and the reader would
// take a region nothing builds yet for an empty one.
//
// **This file authors nothing about the map.** Every count and every sentence
// is the payload's. The one thing it does say in its own words is about the
// app: whether the surface the door leads to has been built, which is a fact
// about this frontend and not about the machine.

import { useEffect, useState } from 'react';
import { Button } from '../../components/common/Button';
import { Disclosure } from '../../components/common/Disclosure';
import { Note } from '../../components/common/Note';
import { RowActions } from '../../components/common/Row';
import { Band, StateStrip, type StateLine } from '../../components/common/StateStrip';
import { Hint } from '../../components/ui/Hint';
import { askAssistant, sendCommand } from '../../lib/commands';
import type { AtlasConflict, AtlasDanglingLink, AtlasResponse } from '../../lib/api';
import { useAutonomyStore } from '../../stores/autonomy';
import { useGraphStore } from '../../stores/graph';
import { usePanelStore } from '../../cockpit/panelStore';
import { useStaleStore } from '../../stores/stale';
import { clock } from '../../lib/time';
import { GraphView } from '../GraphView';

/** The step that redraws the map, by the name the schedule gives it. The
 *  runtime refuses a name nothing declares and says so, so this is a name and
 *  not a claim. */
const REDRAW = 'atlas_build';

/** How often the room re-reads while it is open. The map changes once a night
 *  and a redraw takes seconds, so this is slow: the poll is the backstop, and
 *  a redraw is heard about rather than waited for. */
const POLL_MS = 90_000;

/** What the backend calls this room's reading when a step that draws the map
 *  has run. The other half of `orchestrator/panel_refresh.py::ATLAS_KEY`, and
 *  the reason the poll above can stay slow: the map moves when somebody moved
 *  it, and they are looking at the row when it does. */
const STALE_KEY = 'autonomy.atlas';

/** The words that go to the assistant when a disagreement is questioned.
 *
 * Everything in it is the conflict's own: the kind the atlas gave it, its
 * detail sentence, and the real ids of both records it names, never a
 * rendered label. `atlas_query` can seed on either one, because a conflict
 * never invents a side: both subjects are records the atlas already holds.
 */
export function askedAboutConflict(c: AtlasConflict): string {
  return [
    'The atlas found two records that disagree and I want it looked at.',
    `It calls this ${c.kind}: ${c.detail}.`,
    `The records are ${c.subjects.join(' and ')}.`,
    'Use atlas_query starting from both of them, then tell me which one is',
    'right, or how they should both stand.',
  ].join(' ');
}

/** The words that go to the assistant when a dangling link is questioned.
 *
 * Named on `citing`, the surviving side of the edge, never on `missing`: the
 * missing id is not a node and `atlas_query` would drop it as a seed and
 * answer empty, which would look like it worked and found nothing. Only
 * called where `citing` is not empty; the room offers no button at all
 * otherwise, because there is then no record left to ask about.
 */
export function askedAboutDangling(d: AtlasDanglingLink): string {
  return [
    `A connection in the atlas points at ${d.missing}, and nothing by that`,
    `name is there any more. ${d.citing} wrote it down at ${d.locator}.`,
    `Use atlas_query starting from ${d.citing}, then tell me whether the`,
    'reference still holds or should be dropped.',
  ].join(' ');
}

export function AtlasRoomView({
  data,
  status,
  error,
  onRedraw,
  redrawing,
  onOpen,
  onSeeOrphans,
}: {
  data: AtlasResponse | null;
  status: 'idle' | 'loading' | 'ready' | 'error';
  error: string | null;
  onRedraw: () => void;
  redrawing: boolean;
  /** Opens the graph surface. A panel, not a route: the cockpit summons
   *  every view the same way, and the assistant's `cockpit_show` reaches the
   *  same store, so the door and "show me the map" are one act. */
  onOpen: () => void;
  /** Opens the graph surface already drawn around what nothing points at.
   *  Separate from `onOpen`: this one also chooses the way in, because the
   *  row it sits on already knows which records are worth looking at. */
  onSeeOrphans: () => void;
}): React.ReactElement {
  const [openDisagreements, setOpenDisagreements] = useState(false);
  const [openDangling, setOpenDangling] = useState(false);

  if (status === 'error') {
    return <Note tone="bad">The map could not be read. {error}</Note>;
  }
  if (status !== 'ready' || data === null) return <></>;

  const rows = (lines: AtlasResponse['graph']): StateLine[] =>
    lines.map((line) => ({
      key: line.name,
      state: line.state,
      obligation: line.obligation,
      label: line.label,
      name: line.name,
      said: line.said,
      when: clock(line.at),
      value: line.value,
    }));

  const graph = rows(data.graph);
  // Redrawing acts on the map as a whole, so it sits on the row that IS the
  // map rather than on a row it would not touch.
  if (graph.length > 0) {
    graph[0] = {
      ...graph[0],
      actions: (
        <RowActions className="state-acts">
          <Hint label="Rebuilds the whole map from what the runtime holds right now. It does not resolve a disagreement or connect anything that is missing, and the next nightly pass runs the same rebuild on its own.">
            <Button
              onClick={onRedraw}
              disabled={redrawing}
              ariaLabel="Redraw the map now"
            >
              {redrawing ? 'redrawing' : 'redraw now'}
            </Button>
          </Hint>
        </RowActions>
      ),
    };
  }

  // The three counts below now carry who, in `data.disagreements`,
  // `data.dangling` and `data.orphans`. Matched by name rather than by
  // position, because the row order is `routes/autonomy_atlas.py::graph`'s to
  // choose and this file authors nothing about the map.
  //
  // The three do not get the same control, and that is deliberate rather than
  // an oversight: a conflict's subjects and an orphan are both real, present
  // atlas ids, safe to hand `atlas_query`. A dangling link's missing id is
  // never a node and never can be, so there is nothing honest to seed a
  // picture with; what IS honest is the surviving record that still cites it,
  // which is a fact about a connection rather than about a record on its own,
  // so it goes to the assistant rather than onto the canvas.
  const disagreeIdx = graph.findIndex((line) => line.name === 'records that disagree');
  if (disagreeIdx >= 0 && data.disagreements.length > 0) {
    graph[disagreeIdx] = {
      ...graph[disagreeIdx],
      more: (
        <>
          <Disclosure
            open={openDisagreements}
            onToggle={() => setOpenDisagreements((open) => !open)}
          >
            {openDisagreements
              ? 'hide them'
              : `see ${data.disagreements.length === 1 ? 'it' : 'them'}`}
          </Disclosure>
          {openDisagreements && (
            <StateStrip
              whole
              lines={data.disagreements.map((conflict) => ({
                key: conflict.id,
                state: 'degraded' as const,
                name: conflict.kind,
                said: `${conflict.detail} The records: ${conflict.subjects.join(' and ')}.`,
                actions: (
                  <RowActions className="state-acts">
                    <Hint label="Puts this pair to the assistant in the chat, naming both records and what the atlas says they disagree about. It can use atlas_query to look at how each one connects. Nothing is resolved by pressing this.">
                      <Button
                        onClick={() => askAssistant(askedAboutConflict(conflict))}
                        ariaLabel={`Ask about the ${conflict.kind} disagreement`}
                      >
                        ask about it
                      </Button>
                    </Hint>
                  </RowActions>
                ),
              }))}
            />
          )}
        </>
      ),
    };
  }

  const danglingIdx = graph.findIndex((line) => line.name === 'connections into nothing');
  if (danglingIdx >= 0 && data.dangling.length > 0) {
    graph[danglingIdx] = {
      ...graph[danglingIdx],
      more: (
        <>
          <Disclosure open={openDangling} onToggle={() => setOpenDangling((open) => !open)}>
            {openDangling
              ? 'hide them'
              : `see ${data.dangling.length === 1 ? 'it' : 'them'}`}
          </Disclosure>
          {openDangling && (
            <StateStrip
              whole
              lines={data.dangling.map((link) => ({
                key: `${link.missing}:${link.locator}`,
                state: 'degraded' as const,
                name: link.missing,
                said: link.citing
                  ? `${link.citing} still cites it, written down at ${link.locator}.`
                  : `Written down at ${link.locator}. Neither end of this one is on the map any more, so there is nothing left to ask about.`,
                actions: link.citing ? (
                  <RowActions className="state-acts">
                    <Hint label="Puts this to the assistant in the chat, naming the record that still holds the reference and what it points at. It can use atlas_query to check whether the reference still holds. Nothing is changed by pressing this.">
                      <Button
                        onClick={() => askAssistant(askedAboutDangling(link))}
                        ariaLabel={`Ask about the connection to ${link.missing}`}
                      >
                        ask about it
                      </Button>
                    </Hint>
                  </RowActions>
                ) : undefined,
              }))}
            />
          )}
        </>
      ),
    };
  }

  const orphanIdx = graph.findIndex((line) => line.name === 'nothing points at these');
  if (orphanIdx >= 0 && data.orphans.length > 0) {
    graph[orphanIdx] = {
      ...graph[orphanIdx],
      actions: (
        <RowActions className="state-acts">
          <Hint label="Opens the map already drawn around every record nothing connects to, so you can look at each one and what it says on its own.">
            <Button onClick={onSeeOrphans} ariaLabel="see what nothing points at, on the map">
              see them on the map
            </Button>
          </Hint>
        </RowActions>
      ),
    };
  }

  // What this room knows ABOUT the map, handed to the map to render in the
  // same column as everything else made of words. The operator's rule for
  // this surface is two containers: the picture owns one and every word owns
  // the other, so a band that sat under the picture was a third thing.
  const about = (
    <>
      <p className="t-meta">
        How current the map is, what it has not reached yet, and what its
        last drawing did.
      </p>

      <div className="autonomy-group">
        <Band label="The map" count={data.graph.length} />
        <StateStrip lines={graph} />
      </div>

      <div className="autonomy-group">
        <Band label="What it does not cover yet" count={data.notReached.length} />
        <StateStrip whole lines={rows(data.notReached)} />
      </div>

      <div className="autonomy-group">
        <Band label="The last pass over it" count={data.lastPass.length} />
        {data.lastPass.length === 0 ? (
          // The backend says WHY there is nothing: never run, an unreadable
          // record, or a pass that did not draw. One sentence here would
          // flatten three claims into one.
          <p className="t-meta">{data.lastPassSaid}</p>
        ) : (
          <StateStrip
            whole
            lines={data.lastPass.map((step) => ({
              key: step.stage,
              state: step.state,
              label: step.label,
              name: step.stage,
              said: (
                <>
                  {step.reason && <span>{step.reason}</span>}
                  {step.summary && (
                    <span className="t-meta entry-step__for">{step.summary}</span>
                  )}
                </>
              ),
              when: step.took,
              value: step.changed ? `${step.changed} changed` : '',
            }))}
          />
        )}
      </div>

      <div className="atlas-map__acts">
        <Button onClick={onOpen} ariaLabel="open the map at full size">
          open it full size
        </Button>
      </div>
    </>
  );

  // The room IS the map. Everything it used to say under the picture is a tab
  // beside it now, so this is one surface with two containers rather than a
  // picture with four bands hanging off the bottom of it.
  return (
    <div className="atlas-map">
      <GraphView chrome={false} about={about} />
    </div>
  );
}


export function AtlasRoom(): React.ReactElement {
  const atlas = useAutonomyStore((s) => s.atlas);
  const fetchAtlas = useAutonomyStore((s) => s.fetchAtlas);
  const [redrawing, setRedrawing] = useState(false);
  // A counter, not a flag: two redraws in a row are two nudges, and nothing
  // here has to clear it.
  const staleTick = useStaleStore((s) => s.ticks[STALE_KEY] ?? 0);

  useEffect(() => {
    void fetchAtlas();
    const timer = window.setInterval(() => void fetchAtlas(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [fetchAtlas, staleTick]);

  // Any completed re-read clears the pressed state, including one where the
  // map did not move: a refused redraw changes nothing on screen, and without
  // this the control would sit reading `redrawing` until the room changed.
  // The re-read that matters is the one the nudge above causes, which arrives
  // when the step finishes rather than at the next poll boundary.
  useEffect(() => setRedrawing(false), [atlas.lastFetched]);

  const openPanel = usePanelStore((s) => s.openPanel);
  const enterGraphWay = useGraphStore((s) => s.enter);

  return (
    <AtlasRoomView
      data={atlas.data}
      status={atlas.status}
      error={atlas.error}
      redrawing={redrawing}
      onOpen={() => openPanel('graph')}
      onSeeOrphans={() => {
        // The way in first, then the door: opening the panel before the
        // store has a way chosen would draw the entry state for one frame
        // rather than the picture this button promises.
        enterGraphWay('orphans');
        openPanel('graph');
      }}
      onRedraw={() => {
        setRedrawing(true);
        // The same tool a channel calls and the same gate it passes. The
        // answer arrives in the chat, where an approval is decided, and the
        // map is re-read here when it lands.
        sendCommand('/pipeline_run_stage', ` ${REDRAW}`);
      }}
    />
  );
}

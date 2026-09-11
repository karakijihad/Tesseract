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
import { Note } from '../../components/common/Note';
import { RowActions } from '../../components/common/Row';
import { Band, StateStrip, type StateLine } from '../../components/common/StateStrip';
import { Hint } from '../../components/ui/Hint';
import { sendCommand } from '../../lib/commands';
import type { AtlasResponse } from '../../lib/api';
import { useAutonomyStore } from '../../stores/autonomy';
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

export function AtlasRoomView({
  data,
  status,
  error,
  onRedraw,
  redrawing,
  onOpen,
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
}): React.ReactElement {
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
  //
  // The other three rows here carry no button on purpose. Each is a COUNT —
  // how many pairs disagree, how many connections name a record that is not
  // here, how many nothing points at — and the payload that produces it
  // (`routes/autonomy_atlas.py::graph`) never names which records they are.
  // `atlas_query` needs a record or a search term to investigate, and there
  // is none here to hand it; offering a button that opens on nothing would be
  // the inert-looking row the panel's own rule forbids.
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

  return (
    <AtlasRoomView
      data={atlas.data}
      status={atlas.status}
      error={atlas.error}
      redrawing={redrawing}
      onOpen={() => openPanel('graph')}
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

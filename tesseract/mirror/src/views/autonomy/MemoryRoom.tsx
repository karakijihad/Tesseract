// Memory. The library it works from, and what last night changed in it.
//
// Three bands, in the order a reader needs them: whether it can be searched
// at all, what is in it, and what the nightly pass did. Retrieval leads for
// the reason the door leads in Channels: a library nothing can search is a
// library that is not being used, whatever is in it.
//
// **This file authors nothing about the library.** Every count, every size and
// every sentence is the payload's, and the steps of the last pass carry the
// stage's own reason, rendered as it was written.

import { useEffect } from 'react';
import { Note } from '../../components/common/Note';
import { Band, StateStrip, type StateLine } from '../../components/common/StateStrip';
import type { MemoryResponse } from '../../lib/api';
import { useAutonomyStore } from '../../stores/autonomy';
import { clock } from '../../lib/time';

export function MemoryRoomView({
  data,
  status,
  error,
}: {
  data: MemoryResponse | null;
  status: 'idle' | 'loading' | 'ready' | 'error';
  error: string | null;
}): React.ReactElement {
  if (status === 'error') {
    return <Note tone="bad">The library could not be read. {error}</Note>;
  }
  if (status !== 'ready' || data === null) return <></>;

  const rows = (lines: MemoryResponse['trees']): StateLine[] =>
    lines.map((line) => ({
      key: line.name,
      state: line.state,
      label: line.label,
      name: line.name,
      said: line.said,
      when: clock(line.at),
      value: line.value,
    }));

  return (
    <>
      <div className="autonomy-group">
        <Band label="Whether it can be searched" count={data.retrieval.length} />
        <StateStrip lines={rows(data.retrieval)} />
      </div>

      <div className="autonomy-group">
        <Band label="What is in it" count={data.trees.length} />
        <StateStrip lines={rows(data.trees)} />
      </div>

      <div className="autonomy-group">
        <Band label="What the last pass did to it" count={data.lastNight.length} />
        {data.lastNight.length === 0 ? (
          // The backend says WHY there is nothing: never run, unreadable
          // record, or a pass that touched nothing. Three different claims,
          // and one sentence here would flatten them back into one.
          <p className="t-meta">{data.lastNightSaid}</p>
        ) : (
          <StateStrip
            // Nothing opens onto the rest of a step's reason, so it is shown
            // whole rather than clamped to two lines.
            whole
            lines={data.lastNight.map((step) => ({
              key: step.stage,
              state: step.state,
              label: step.label,
              name: step.stage,
              said: (
                <>
                  {step.reason && <span>{step.reason}</span>}
                  {/* What the step is FOR, from the stage that declares it. */}
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
    </>
  );
}

export function MemoryRoom(): React.ReactElement {
  const memory = useAutonomyStore((s) => s.memory);
  const fetchMemory = useAutonomyStore((s) => s.fetchMemory);

  // The rail fills this whether or not the room is open, so opening it is
  // usually free. A room that lands on an empty store asks once.
  useEffect(() => {
    if (memory.status === 'idle') void fetchMemory();
  }, [memory.status, fetchMemory]);

  return (
    <MemoryRoomView data={memory.data} status={memory.status} error={memory.error} />
  );
}

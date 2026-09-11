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
//
// Three controls now, and only two read their effect back into this room. A
// failed step of the last pass can be run again, off its schedule, and the
// vault can be checked on demand; neither writes a sentence of its own, and
// what happened is read back here the next time the room asks. Searching is
// the third and is shaped differently on purpose: `memory_search`'s answer is
// the whole of what was asked for, not a status line, and this room has
// nowhere to render a found record. So the query goes to the assistant rather
// than straight to the tool, and the answer is read in the chat, not here.
//
// **No row in the other two bands gets a fourth.** `memory_update` needs a
// real record id, and neither a tree's byte count nor a pipeline stage's
// `changed`/`refused` total is one — the walk that produces the tree count
// touches individual files and drops them once counted
// (`routes/autonomy_memory.py::_tree`), and a stage report never carried a
// record id to begin with. Inventing one to hang a button on would be the
// exact defect this room exists to avoid.

import { useEffect, useState } from 'react';
import { Button } from '../../components/common/Button';
import { Input } from '../../components/common/Input';
import { Note } from '../../components/common/Note';
import { RowActions } from '../../components/common/Row';
import { Hint } from '../../components/ui/Hint';
import { Band, StateStrip, type StateLine } from '../../components/common/StateStrip';
import { askAssistant, sendCommand } from '../../lib/commands';
import type { MemoryLine, MemoryResponse } from '../../lib/api';
import { useAutonomyStore } from '../../stores/autonomy';
import { clock } from '../../lib/time';

/** The vault is the one tree here with a tool that reads it. A tree nothing
 *  has written yet has nothing to check, which is why the row that says so
 *  carries no button either: `vault_lint` would only confirm the same
 *  emptiness the row already states. */
function treeActions(line: MemoryLine): React.ReactNode {
  if (line.name !== 'vault' || line.state === 'not_instrumented') return undefined;
  return (
    <RowActions className="state-acts">
      <Hint label="Checks the vault wiki for orphans, stale pages, contradictions and missing hubs, and writes down what it can fix safely.">
        <Button onClick={() => sendCommand('/vault_lint')} ariaLabel="Check the vault now">
          check it
        </Button>
      </Hint>
    </RowActions>
  );
}

/** The words that go to the assistant when the operator asks to search memory.
 *
 * `memory_search` needs nothing but this text, so the tool could be aimed
 * directly with `sendCommand`. It is not. That channel's answer is a toast:
 * a kernel-tool slash result is truncated at 320 characters with no way to
 * read the rest (`stores/dispatch/command.ts::handleCommand`), which is fine
 * for `vault_lint`, whose real answer is the tree count on the next poll, and
 * is not fine here, where the search result IS the whole of what was asked
 * for. Sent to the assistant instead, the same call lands in the chat, where
 * `ToolCallPill` shows it in full with an Expand control, the one place in
 * this app a search answer is actually readable.
 *
 * The query is the operator's own, placed verbatim and never reworded. The
 * two sentences around it are the only words this room supplies, and they ask
 * for a report rather than composing one: where each hit came from, and a
 * plain no when nothing matches.
 */
export function askedToSearch(query: string): string {
  return [
    'Search memory for this and tell me what you find, including where each',
    'record came from. Say plainly if nothing matches.',
    `What to search for: ${query}`,
  ].join(' ');
}

/** The one control on the search band. A query typed here goes to the
 *  assistant, never straight to the tool. Why is `askedToSearch`'s own
 *  comment; what the operator sees is one line saying the answer shows up in
 *  the chat, not in this room, since nothing here has ever asked a question
 *  before. */
function SearchMemory(): React.ReactElement {
  const [query, setQuery] = useState('');
  const trimmed = query.trim();
  const submit = () => {
    if (!trimmed) return;
    askAssistant(askedToSearch(trimmed));
    setQuery('');
  };
  return (
    <div className="memory-search">
      <p className="t-meta">
        Type what you are looking for and the assistant will search memory and
        answer in the chat.
      </p>
      <div className="memory-search__row">
        <Input
          type="search"
          value={query}
          onChange={setQuery}
          placeholder="search memory"
          ariaLabel="search memory"
          className="memory-search__field"
          onKeyDown={(e) => e.key === 'Enter' && submit()}
        />
        <Hint label="Sends this to the assistant, which searches memory and reports what it finds in the chat.">
          <Button onClick={submit} disabled={!trimmed} ariaLabel="search memory">
            search
          </Button>
        </Hint>
      </div>
    </div>
  );
}

function rows(
  lines: MemoryResponse['trees'],
  actionsFor?: (line: MemoryLine) => React.ReactNode,
): StateLine[] {
  return lines.map((line) => ({
    key: line.name,
    state: line.state,
    obligation: line.obligation,
    label: line.label,
    name: line.name,
    said: line.said,
    when: clock(line.at),
    value: line.value,
    actions: actionsFor?.(line),
  }));
}

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

  return (
    <>
      <p className="t-meta">
        Whether the library can be searched right now, what is on disk, and
        what last night&apos;s maintenance pass did to it.
      </p>

      <div className="autonomy-group">
        <Band label="Whether it can be searched" count={data.retrieval.length} />
        <StateStrip lines={rows(data.retrieval)} />
        <SearchMemory />
      </div>

      <div className="autonomy-group">
        <Band label="What is in it" count={data.trees.length} />
        <StateStrip lines={rows(data.trees, treeActions)} />
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
              // A step that found nothing to do is the commonest healthy
              // outcome and gets no button. A step that FAILED is the one
              // that most needs one, so its control shows without being
              // hovered, the same as a row that is asking for attention.
              actions:
                step.state === 'failed' ? (
                  <RowActions className="state-acts state-acts--waiting">
                    <Hint label="Runs this one step of tonight's maintenance again, right now, outside its schedule.">
                      <Button
                        onClick={() =>
                          sendCommand(
                            '/pipeline_run_stage',
                            ` stage=${JSON.stringify(step.stage)}`,
                          )
                        }
                        ariaLabel={`Run ${step.stage} again`}
                      >
                        run it again
                      </Button>
                    </Hint>
                  </RowActions>
                ) : undefined,
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

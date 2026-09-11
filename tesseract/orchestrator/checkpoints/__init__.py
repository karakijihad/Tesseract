"""What the boundary wrote down: the working state a fresh context is rebuilt from.

A consolidation reflects and then either continues or stops. Reflection already
writes what the conversation taught, as deltas into the memory store. What it
never wrote is what the conversation was DOING, and without that a continue has
nothing to rebuild from and a stop leaves nothing to come back to.

So every boundary writes one checkpoint. Both outcomes: a reset writes one too,
because the memory writer needs no separate logic for the two and only the
orchestration afterwards differs.

**References, never copies.** A transcript, a prompt, a tool result and a file
each already have an owner. A checkpoint holding a copy of one is a second truth
that drifts from the first, and the drift is invisible because both look
authoritative. `artifacts` holds paths and ids; nothing here holds a body.

**One JSONL per CONVERSATION, resolved at call time.** Resolved at call time
for the reason `orchestrator/autonomy/journal.py` is: a test pointing
`TESSERACT_HOME` somewhere else is answered by that home without re-importing
the module. Append-only, and a read never blocks a write.

Keyed on the chat rather than on the day, because every reader is. It was
per-day, and both real readers scanned that with a `days=7` window and a
`limit=1000` cap: a conversation returned to after eight days could not find
its own last boundary, and on a busy machine a chat's own rows could fall
outside the thousand most recent. Both failed OPEN and in silence, so the
cycle check went blind on exactly the long-running work it exists for.

This file used to argue against the change in these words, "an index is a
second truth to keep in step with the first". The argument is right about an
index and does not apply here: a per-chat file is not a second copy, it is a
different primary layout. The latest boundary is the last line of one file and
the recent ones are its tail, so both holes close by construction rather than
by raising a limit.

Changing a layout is a data migration whether or not anyone calls it one.
`_migrate_daily_files` folds the per-day files in once, because without it
every boundary a conversation had already written became unfindable on the day
the store moved, in silence, which is the failure this layout was chosen to
close.

**Not a workspace event.** Those are reserved for threads the operator is
expected to answer, and a checkpoint is runtime bookkeeping nobody is being
asked about. Putting it in the inbox would cost the operator attention on every
boundary the runtime crossed by itself.

**The recovery work has landed, onto this same record.** The line above used to
say `recovery_behaviour` and receipts belonged elsewhere and would arrive here
later; this is later. The split is by what a field describes: the fields
describing the WORK are the boundary's, the fields describing an external
EFFECT are recovery's, and neither writes the other's. What this file still
must not grow is a second store for the second half.

**A boundary now says which of six it is, and the readers are not
interchangeable because of it.** `latest_for_chat` answers with the last
CONSOLIDATION, because that is what a continuity package is rebuilt from and a
cursor written before a tool call is not a boundary anyone reflected at.
`latest_step` answers with the last row of any kind, which is where recovery
resumes. One store, two questions, and reading the wrong one is silent: the
tail would still be a real checkpoint, just not the one that means anything.

**Keyed on the conversation, or on the run when there is none.** A scheduled
turn and a sub-agent have no `chat_id` at all, and before this they all shared
one `_unkeyed` file: exactly the unattended work this record exists to resume,
pooled into a stream where one run cannot find its own last step. A `run_id`
keys its own file when there is no chat.

Six modules, one per thing that can change on its own: `record.py` is the shape,
`layout.py` is where a row goes and every limit, `migrate.py` is the finished
one-time fold, `store.py` writes and prunes, `read.py` answers about one file
and `scan.py` about all of them. It was one file of a thousand lines holding
all six, which meant a change to the schema and a change to the retention
ceiling were read, and reviewed, in the same place.

This file is the surface every caller already imports, so nothing outside the
package changed name or location.
"""

from tesseract.orchestrator.checkpoints.layout import (
    MAX_BYTES,
    MIGRATED_MARK,
    RECENT_CALLS,
    TAIL_BYTES,
    UNKEYED,
    WHOLE_FILE,
    checkpoint_dir,
    checkpoint_path,
)
from tesseract.orchestrator.checkpoints.migrate import fold_daily_files
from tesseract.orchestrator.checkpoints.read import (
    closed_calls,
    latest_for_chat,
    latest_step,
    open_calls,
    recent_for_chat,
    row_ts,
    tail,
)
from tesseract.orchestrator.checkpoints.record import (
    BOUNDARIES,
    CONSOLIDATION,
    FIELD_CHARS,
    LIST_CAP,
    Checkpoint,
    build,
)
from tesseract.orchestrator.checkpoints.scan import find, latest_for_run, unresolved
from tesseract.orchestrator.checkpoints.store import step, write

__all__ = [
    "MAX_BYTES",
    "MIGRATED_MARK",
    "TAIL_BYTES",
    "fold_daily_files",
    "tail",
    "BOUNDARIES",
    "CONSOLIDATION",
    "Checkpoint",
    "LIST_CAP",
    "FIELD_CHARS",
    "UNKEYED",
    "build",
    "closed_calls",
    "write",
    "latest_for_chat",
    "latest_for_run",
    "latest_step",
    "open_calls",
    "unresolved",
    "step",
    "recent_for_chat",
    "checkpoint_dir",
    "checkpoint_path",
    "find",
    "row_ts",
    "RECENT_CALLS",
    "WHOLE_FILE",
]

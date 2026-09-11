import { useId, useState } from 'react';
import { Disclosure } from '../common/Disclosure';
import './RuntimeNote.css';

interface Props {
  /** One of `brain/chat.py::RUNTIME_ORIGINS`. */
  origin: string | undefined;
  /** The text the turn actually carried. */
  content: string;
  timestamp: number;
}

/** What each injection is, in the words of someone who did not write it.
 *
 *  Keyed by the mark rather than by the text, which is the whole point of the
 *  mark: the sentences themselves ship in a public repo, so matching on them
 *  would let anyone who can reach a turn have their words drawn as the
 *  runtime's. A value with no row here still draws, saying only that the
 *  app started the turn itself, because a note nobody can read beats a bubble
 *  wearing the wrong name. */
const LABELS: Record<string, string> = {
  spawn_complete: 'A background task finished',
  spawn_stalled: 'A background task is still running',
  card_press: 'You used a control on a card',
  reflection: 'End of session reflection',
  recovery: 'The app picked this work back up after it stopped',
  morning: 'The app decided what to work on today',
  workday: 'The app worked on one of the steps it decided',
  carry_on: 'The app carried the work on after clearing this conversation',
  continuity: 'What the last consolidation carried over',
  boundary: 'What happened when this conversation was consolidated',
};

const FALLBACK = 'The app started this on its own';

/**
 * A turn nobody typed, drawn as a line rather than as a speaker.
 *
 * Several things start a turn nobody typed: a background task finishing, one
 * running past its threshold, a press on a card the assistant drew, the
 * end-of-session reflection, the app picking work back up after it stopped in
 * the middle of it, the day it planned for itself, and a consolidation that
 * carried the work on. Every one of them reaches the model as `role: "user"`,
 * because that is the only role a provider lets a caller place
 * mid-conversation, and the transcript drew every one of them wearing the
 * operator's name. So a chat took two turns on its own and the record said
 * the operator had asked for them.
 *
 * It is a rule with a label and no bubble, which is what the mockup settled
 * on: the runtime is not a third participant and should not read like one.
 * The text is still there, one click away, because "why did this chat move"
 * is exactly the question the note exists to answer.
 */
export function RuntimeNote({ origin, content, timestamp }: Props) {
  const [open, setOpen] = useState(false);
  const bodyId = useId();
  const time = new Date(timestamp).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
  });
  const label = (origin && LABELS[origin]) || FALLBACK;
  const hasBody = content.trim().length > 0;

  return (
    <div className="runtime-note">
      <div className="runtime-note__rule">
        {hasBody ? (
          <Disclosure
            open={open}
            onToggle={() => setOpen(v => !v)}
            className="runtime-note__toggle"
            ariaControls={bodyId}
          >
            <span className="t-meta">
              {label} · {time} · {open ? 'hide' : 'what it said'}
            </span>
          </Disclosure>
        ) : (
          <span className="runtime-note__label t-meta">
            {label} · {time}
          </span>
        )}
      </div>
      {open && hasBody && (
        <p className="runtime-note__body t-meta" id={bodyId}>
          {content}
        </p>
      )}
    </div>
  );
}

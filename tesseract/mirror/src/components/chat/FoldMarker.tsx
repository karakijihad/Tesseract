import './FoldMarker.css';

interface Props {
  timestamp: number;
}

/**
 * The divider a fold leaves behind.
 *
 * The transcript keeps every message it has drawn, so nothing on screen
 * changes when the runtime summarises the older half. Without a line at the
 * moment it happened, a reader has no way to tell that the model can no
 * longer see the top of the page verbatim, and the divider says what to do
 * about it.
 *
 * It marks that a fold happened here, not exactly where the boundary fell:
 * the model's history and this transcript do not share indices.
 */
export function FoldMarker({ timestamp }: Props) {
  const time = new Date(timestamp).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
  });
  return (
    <div className="fold-marker" role="separator">
      <span className="fold-marker__label t-meta">
        Summarised earlier messages · {time} · ask about anything above and I
        will read it back
      </span>
    </div>
  );
}

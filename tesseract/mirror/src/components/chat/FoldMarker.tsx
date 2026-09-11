import './FoldMarker.css';

interface Props {
  timestamp: number;
}

/**
 * The divider an ARCHIVED conversation still carries.
 *
 * Nothing writes one now. The runtime summarised the older half of a long
 * conversation until the fold was deleted, and a conversation from before
 * that is still on disk: reloading one draws its summary, and without this
 * line it would draw as something the operator said. It also tells a reader
 * that the model cannot see the top of that page verbatim, and what to do
 * about it.
 *
 * It marks that a fold happened here, not exactly where it fell: the model's
 * history and this transcript do not share indices.
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

import { usePulseStore, type PulseEntry } from '../../../stores/pulse';

function formatTs(ts: string): string {
  try {
    return new Date(ts).toLocaleTimeString([], {
      hour12: false,
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    });
  } catch {
    return '';
  }
}

function Row({ entry }: { entry: PulseEntry }) {
  return (
    <div className="pulse-event" data-severity={entry.severity}>
      <span className="ev-ts">{formatTs(entry.ts)}</span>
      <span className={`ev-tag ${entry.tag}`}>{entry.tag}</span>
      <span className="ev-msg">{entry.label}</span>
    </div>
  );
}

export function PulseTailSection() {
  const entries = usePulseStore((s) => s.entries);
  const tail = entries.slice(0, 3);

  return (
    <section className="pulse-tail" aria-label="Pulse">
      <header className="pulse-tail-head">
        <h3 className="t-meta pulse-tail-name">Pulse</h3>
        <span className="pulse-tail-rule" aria-hidden="true" />
        <span className="t-meta pulse-tail-origin">last 3</span>
      </header>
      <div className="pulse-stream pulse-tail-stream">
        {tail.length === 0 ? (
          <p className="pulse-tail-empty">Waiting for events…</p>
        ) : (
          tail.map((e) => <Row key={e.id} entry={e} />)
        )}
      </div>
    </section>
  );
}

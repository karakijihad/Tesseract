import type { CacheResponse, CacheTurn } from '../../stores/conscience';

/** What the prompt cache did, per turn.
 *
 * Two questions, in the order they get asked. What the turns just taken did,
 * newest first, and then which turns stopped reusing what the provider already
 * held. The ranking is by tokens re-read, which the ledger holds, rather than
 * by money wasted, which would need a price for a request nobody made.
 *
 * **Both, because a good turn is invisible in a ranking.** A turn that reused
 * its whole prefix sits at the bottom of a list sorted by what was re-read, so
 * the ranking alone could say what to fix and could not say whether the change
 * just made had cost anything. Someone reading it for that came away thinking
 * their session was missing.
 *
 * **A turn, not a call.** A turn is up to eighty model calls, and the second
 * call reads almost everything the first one wrote. A list of calls would put
 * a turn's own tool traffic at the top forever and say nothing anyone can act
 * on.
 *
 * **Said nothing is not said zero.** Some providers report no cached count at
 * all. Averaging those in as zero draws a total miss on a runtime that was
 * never asked, so a turn with nothing reported shows no percentage and the
 * count of quiet calls travels with the headline figure.
 */
export function CacheReport({ reading }: { reading: CacheResponse }) {
  const { summary, turns, latest } = reading;
  if (!summary || !summary.calls) return null;

  // Each list scales its own bars. Sharing one denominator would flatten every
  // recent turn against the worst turn of the week and show nothing.
  const worst = Math.max(1, ...turns.map((t) => t.uncached_tokens));
  const recentWorst = Math.max(1, ...(latest ?? []).map((t) => t.uncached_tokens));

  return (
    <figure className="cache-report">
      <header className="cache-report__head">
        <p className="cache-report__rate">
          <strong>{formatRate(summary.hit_rate)}</strong>
          <span className="cache-report__unit">read from cache</span>
        </p>
        <p className="cache-report__totals t-meta">
          {summary.cached_tokens.toLocaleString()} of{' '}
          {summary.input_tokens.toLocaleString()} tokens, over {summary.turns}{' '}
          turns in {reading.days} {reading.days === 1 ? 'day' : 'days'}
        </p>
        <p className="cache-report__totals t-meta">
          {summary.uncached_tokens.toLocaleString()} read again at full price.{' '}
          {summary.written_tokens.toLocaleString()} written to the cache.
        </p>
        {summary.unreported_calls > 0 && (
          <p className="cache-report__caveat t-meta">
            {summary.unreported_calls} of {summary.calls} calls reported no
            cached count, so they count towards the total but not towards the
            percentage. Background models often report nothing.
          </p>
        )}
      </header>

      {latest && latest.length > 0 && (
        <>
          <h4 className="cache-report__heading">
            <span className="t-label">Latest turns</span>
          </h4>
          <ol className="cache-report__rows">
            {latest.map((turn) => (
              <TurnRow key={`latest-${turn.turn_id}`} turn={turn} worst={recentWorst} />
            ))}
          </ol>
        </>
      )}

      <h4 className="cache-report__heading">
        <span className="t-label">Turns that re-read the most</span>
      </h4>
      <ol className="cache-report__rows">
        {turns.map((turn) => (
          <TurnRow key={turn.turn_id} turn={turn} worst={worst} />
        ))}
      </ol>
    </figure>
  );
}

function TurnRow({ turn, worst }: { turn: CacheTurn; worst: number }) {
  const share = Math.max(0.02, turn.uncached_tokens / worst);
  return (
    <li className="cache-report__row">
      <span className="cache-report__name">{turn.turn_id}</span>
      <span className="cache-report__figures t-meta">
        {turn.uncached_tokens.toLocaleString()} re-read
        <span className="cache-report__rate-cell">{formatRate(turn.hit_rate)}</span>
      </span>
      <span className="cache-report__descr t-meta">
        {turn.local_date} · {turn.role || 'no role'} · {turn.calls}{' '}
        {turn.calls === 1 ? 'call' : 'calls'} ·{' '}
        {turn.input_tokens.toLocaleString()} in
      </span>
      <span className="cache-report__track">
        <span
          className="cache-report__bar"
          style={{ inlineSize: `${(share * 100).toFixed(1)}%` }}
        />
      </span>
    </li>
  );
}

/** A percentage, or the mark this app uses for a value that does not exist.
 *  Never 0% for a turn nobody measured. */
function formatRate(rate: number | null): string {
  if (rate === null) return '—';
  return `${Math.round(rate * 100)}%`;
}

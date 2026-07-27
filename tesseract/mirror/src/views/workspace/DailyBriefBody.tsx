/* MO-9-14 — newsletter renderer for the `daily_brief` workspace event.
 *
 * Payload shape (MO-9-14 baseline + AU-23 initiatives + AU-24 ecosystem):
 *   {
 *     kind: 'daily_brief',
 *     date: 'YYYY-MM-DD',
 *     sections: {
 *       yesterday_in_tesseract: '<voice-prose paragraph>',
 *       yesterday_with_you:     '<voice-prose paragraph>',
 *       what_i_learned:         '<voice-prose paragraph>',
 *       vault:                  ['<bullet>'],
 *       ecosystem:              '<voice-prose paragraph>',  // AU-24
 *       initiatives:            ['<bullet>'],               // AU-23
 *       world: { tech: [card], science: [card], politics: [card] },
 *     },
 *     cost_cap_reached: boolean,
 *   }
 *
 * Per-card actions POST /api/brief/feedback to feed the operator's
 * InterestsProfile. Voice-friendly hint text uses var(--text-meta) per
 * the project HARD RULE.
 */
import { useState } from 'react';
import { BACKEND_BASE } from '../../lib/endpoints';
import { ChatMarkdown } from '../../components/chat/ChatMarkdown';

const PILLAR_LABEL: Record<string, string> = {
  tech: 'Tech',
  science: 'Science',
  politics: 'Politics',
};

const PILLAR_ORDER = ['tech', 'science', 'politics'] as const;

type Signal = 'interested' | 'not_for_me' | 'dig_deeper' | 'commented';

interface WorldCard {
  title: string;
  summary: string;
  source: string;
  url: string;
  published_at: string;
  image_url: string;
}

function asString(v: unknown, fallback = ''): string {
  return typeof v === 'string' ? v : fallback;
}

function asWorldCards(v: unknown): WorldCard[] {
  if (!Array.isArray(v)) return [];
  return v
    .filter((c): c is Record<string, unknown> => typeof c === 'object' && c !== null)
    .map((c) => ({
      title: asString(c.title),
      summary: asString(c.summary),
      source: asString(c.source),
      url: asString(c.url),
      published_at: asString(c.published_at),
      image_url: asString(c.image_url),
    }));
}

// Tavily-extracted page text often arrives with the source site's
// navigation bullets and section markers still embedded ("* Browse World",
// "## Latest Stories", etc). The cron renderer should strip these but
// doesn't yet — defensive cleanup in the renderer keeps the newsletter
// readable while that fix is in flight. Order matters: strip block markers
// first, then collapse inline emphasis, then whitespace.
function cleanSummary(raw: string, maxChars = 240): string {
  if (!raw) return '';
  let s = raw
    .replace(/[   ]/g, ' ')
    .split('\n')
    .map((line) =>
      line
        .replace(/^\s*\d+\.\s+/, '')
        .replace(/^\s*[#>*\-•]+\s*/, ''),
    )
    .filter((line) => line.trim() !== '')
    .join(' ');
  s = s
    .replace(/\*\*+([^*]+?)\*\*+/g, '$1')
    .replace(/\*([^*]+?)\*/g, '$1')
    .replace(/`+([^`]+?)`+/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/^\s*#{1,6}\s+/g, '')
    .replace(/\s{2,}/g, ' ')
    .trim();
  if (s.length <= maxChars) return s;
  const cut = s.slice(0, maxChars);
  const lastStop = Math.max(cut.lastIndexOf('. '), cut.lastIndexOf('? '), cut.lastIndexOf('! '));
  return (lastStop > maxChars * 0.5 ? cut.slice(0, lastStop + 1) : cut.trimEnd()) + '…';
}

function shortDate(iso: string): string {
  if (!iso) return '';
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  if (!m) return iso;
  const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  return `${months[Number(m[2]) - 1] ?? m[2]} ${Number(m[3])}`;
}

interface ReactionRowProps {
  date: string;
  pillar: string;
  card: WorldCard;
}

function ReactionRow({ date, pillar, card }: ReactionRowProps) {
  const [busy, setBusy] = useState<Signal | null>(null);
  const [done, setDone] = useState<Signal | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const fire = async (signal: Signal) => {
    setBusy(signal);
    setErr(null);
    try {
      const res = await fetch(`${BACKEND_BASE}/api/brief/feedback`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          date,
          pillar,
          url: card.url,
          // The renderer scores `score_url(title + summary)`; the
          // feedback route accepts either an explicit topic or the
          // card URL as the key. Using the URL means the operator's
          // affinity accumulates per source until they add explicit
          // topic vocabulary in a later phase.
          signal,
        }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(asString((body as Record<string, unknown>)?.error, 'feedback failed'));
      }
      setDone(signal);
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'feedback failed');
    } finally {
      setBusy(null);
    }
  };

  const isDone = (s: Signal) => done === s;
  const tip = (s: Signal): string => {
    switch (s) {
      case 'interested': return 'More like this';
      case 'not_for_me': return 'Less like this';
      case 'dig_deeper': return 'Dig deeper on this topic';
      case 'commented': return 'Open a comment thread';
    }
  };

  return (
    <div className="brief-card-actions">
      {(['interested', 'not_for_me', 'dig_deeper', 'commented'] as Signal[]).map((s) => (
        <button
          key={s}
          type="button"
          className={`brief-card-action${isDone(s) ? ' is-done' : ''}`}
          title={tip(s)}
          aria-label={tip(s)}
          disabled={busy !== null || done !== null}
          onClick={() => fire(s)}
        >
          {s === 'interested' && '👍'}
          {s === 'not_for_me' && '👎'}
          {s === 'dig_deeper' && '⛏'}
          {s === 'commented' && '💬'}
        </button>
      ))}
      {err && <span className="t-meta brief-card-err">{err}</span>}
      {done && !err && <span className="t-meta brief-card-thanks">noted</span>}
    </div>
  );
}

interface DailyBriefBodyProps {
  payload: Record<string, unknown>;
}

export function DailyBriefBody({ payload }: DailyBriefBodyProps) {
  const date = asString(payload.date);
  const sectionsRaw = (typeof payload.sections === 'object' && payload.sections !== null)
    ? (payload.sections as Record<string, unknown>)
    : {};
  const yesterday = asString(sectionsRaw.yesterday_in_tesseract);
  const withYou = asString(sectionsRaw.yesterday_with_you);
  const learned = asString(sectionsRaw.what_i_learned);
  const vault = Array.isArray(sectionsRaw.vault)
    ? sectionsRaw.vault.filter((x): x is string => typeof x === 'string')
    : [];
  const ecosystem = asString(sectionsRaw.ecosystem);
  const initiatives = Array.isArray(sectionsRaw.initiatives)
    ? sectionsRaw.initiatives.filter((x): x is string => typeof x === 'string')
    : [];
  const worldRaw = (typeof sectionsRaw.world === 'object' && sectionsRaw.world !== null)
    ? (sectionsRaw.world as Record<string, unknown>)
    : {};
  const capHit = payload.cost_cap_reached === true;

  const prose: { key: string; label: string; body: string }[] = [
    { key: 'in-tess',  label: 'in TESSERACT',  body: yesterday },
    { key: 'with-you', label: 'with you',      body: withYou   },
    { key: 'learned',  label: 'what I learned', body: learned   },
  ].filter((p) => p.body);

  return (
    <div className="workspace-detail-body brief-body brief-body--newsletter">
      {prose.length > 0 && (
        <details className="brief-prose">
          <summary className="brief-prose-summary">
            <span>Yesterday</span>
            <span className="t-meta">{prose.length} note{prose.length === 1 ? '' : 's'} · expand</span>
          </summary>
          <div className="brief-prose-body">
            {prose.map((p) => (
              <div key={p.key} className="brief-prose-row">
                <span className="brief-prose-label t-meta">{p.label}</span>
                <div className="brief-prose-md"><ChatMarkdown>{p.body}</ChatMarkdown></div>
              </div>
            ))}
          </div>
        </details>
      )}

      {vault.length > 0 && (
        <section className="brief-section brief-section--tight">
          <h3 className="brief-section-title">Vault</h3>
          <ul className="brief-vault-list">
            {vault.map((line, i) => <li key={i}><ChatMarkdown>{line}</ChatMarkdown></li>)}
          </ul>
        </section>
      )}

      {ecosystem && (
        <section className="brief-section brief-section--tight">
          <h3 className="brief-section-title">Ecosystem</h3>
          <div className="brief-ecosystem-prose"><ChatMarkdown>{ecosystem}</ChatMarkdown></div>
        </section>
      )}

      <section className="brief-section brief-section--tight">
        <h3 className="brief-section-title">World</h3>
        {capHit && (
          <p className="t-meta brief-cap-warn">
            World section partial — cost cap reached.
          </p>
        )}
        {PILLAR_ORDER.map((pillar) => {
          const cards = asWorldCards(worldRaw[pillar]);
          return (
            <div key={pillar} className="brief-pillar">
              <h4 className="brief-pillar-title">{PILLAR_LABEL[pillar]}</h4>
              {cards.length === 0 ? (
                <p className="t-meta">No fresh items today.</p>
              ) : (
                <ul className="brief-news-list">
                  {cards.map((card, i) => (
                    <li key={`${card.url || i}`} className="brief-news-row">
                      {card.image_url && (
                        <a
                          className="brief-news-thumb"
                          href={card.url || card.image_url}
                          target="_blank"
                          rel="noreferrer noopener"
                          aria-hidden="true"
                          tabIndex={-1}
                        >
                          <img src={card.image_url} alt="" loading="lazy" />
                        </a>
                      )}
                      <div className="brief-news-text">
                        <div className="brief-news-title">
                          {card.url ? (
                            <a
                              href={card.url}
                              target="_blank"
                              rel="noreferrer noopener"
                              className="linked-anchor"
                            >
                              {card.title || '(untitled)'}
                            </a>
                          ) : (
                            <span>{card.title || '(untitled)'}</span>
                          )}
                        </div>
                        {card.summary && (
                          <div className="brief-news-summary"><ChatMarkdown>{cleanSummary(card.summary)}</ChatMarkdown></div>
                        )}
                        <div className="brief-news-meta t-meta">
                          {card.source && <span>{card.source}</span>}
                          {card.source && card.published_at && <span> · </span>}
                          {card.published_at && <span>{shortDate(card.published_at)}</span>}
                        </div>
                        <ReactionRow date={date} pillar={pillar} card={card} />
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          );
        })}
      </section>

      {initiatives.length > 0 && (
        <section className="brief-section brief-section--tight">
          <h3 className="brief-section-title">Initiatives</h3>
          <ul className="brief-vault-list">
            {initiatives.map((line, i) => <li key={i}><ChatMarkdown>{line}</ChatMarkdown></li>)}
          </ul>
        </section>
      )}
    </div>
  );
}

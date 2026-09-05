/* Newsletter renderer for the `daily_brief` workspace event.
 *
 * Payload shape:
 *   {
 *     kind: 'daily_brief',
 *     date: 'YYYY-MM-DD',
 *     sections: {
 *       yesterday_in_tesseract: '<voice-prose paragraph>',
 *       yesterday_with_you:     '<voice-prose paragraph>',
 *       what_i_learned:         '<voice-prose paragraph>',
 *       vault:                  ['<bullet>'],
 *     },
 *   }
 *
 * Voice-friendly hint text uses var(--text-meta) per the project HARD RULE.
 */
import { Markdown } from '../../components/common/Markdown';

function asString(v: unknown, fallback = ''): string {
  return typeof v === 'string' ? v : fallback;
}

interface DailyBriefBodyProps {
  payload: Record<string, unknown>;
}

export function DailyBriefBody({ payload }: DailyBriefBodyProps) {
  const sectionsRaw = (typeof payload.sections === 'object' && payload.sections !== null)
    ? (payload.sections as Record<string, unknown>)
    : {};
  const yesterday = asString(sectionsRaw.yesterday_in_tesseract);
  const withYou = asString(sectionsRaw.yesterday_with_you);
  const learned = asString(sectionsRaw.what_i_learned);
  const vault = Array.isArray(sectionsRaw.vault)
    ? sectionsRaw.vault.filter((x): x is string => typeof x === 'string')
    : [];

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
                <div className="brief-prose-md"><Markdown>{p.body}</Markdown></div>
              </div>
            ))}
          </div>
        </details>
      )}

      {vault.length > 0 && (
        <section className="brief-section brief-section--tight">
          <h3 className="brief-section-title">Vault</h3>
          <ul className="brief-vault-list">
            {vault.map((line, i) => <li key={i}><Markdown>{line}</Markdown></li>)}
          </ul>
        </section>
      )}
    </div>
  );
}

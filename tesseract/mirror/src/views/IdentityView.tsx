import { useEffect, useMemo } from 'react';
import { Markdown } from '../components/common/Markdown';
import { useSoulStore } from '../stores/soul';
import { sendCommand } from '../lib/commands';
import { formatRelative } from '../lib/time';
import { parseSoul } from '../lib/soul';
import { Hint } from '../components/ui/Hint';
import { IdentityCard } from './identity/IdentityCard';
import { VoicePicker } from './identity/VoicePicker';
import { DocsEditor } from './identity/DocsEditor';

/** Who it is — the name, the voice, the documents, and SOUL.md.
 *
 * AS-5 folded the read-only Soul tab into this one. The SOUL.md cards and
 * `/reflect` stayed put: what it has grown into belongs beside what it was
 * configured as.
 */
export function IdentityView() {
  const content = useSoulStore((s) => s.content);
  const lastReflectedAt = useSoulStore((s) => s.lastReflectedAt);
  const fetchSoul = useSoulStore((s) => s.fetchSoul);

  // Refresh when the operator opens the tab so the chip + blocks reflect
  // any external SOUL.md edits made while a different view was active.
  useEffect(() => {
    fetchSoul();
  }, [fetchSoul]);

  const blocks = useMemo(() => parseSoul(content), [content]);

  return (
    <div className="identity-view">
      <header className="identity-view-head">
        <div className="identity-view-title-row">
          <h1 className="t-head identity-view-title">Identity</h1>
          <span className="t-meta identity-view-meta">
            Last reflected: {formatRelative(lastReflectedAt)} · {blocks.length} sections
          </span>
        </div>
        <Hint label="Run /reflect — re-read SOUL.md and update memory synthesis" position="bottom" maxWidth={260}>
          <button
            type="button"
            className="identity-view-refresh"
            onClick={() => sendCommand('/reflect')}
          >
            refresh
          </button>
        </Hint>
      </header>

      <div className="identity-view-body">
        <IdentityCard />
        <VoicePicker />
        <DocsEditor />

        <div className="identity-view-card-heading t-meta identity-soul-heading">
          Soul
        </div>
        {blocks.length === 0 ? (
          <div className="t-caption identity-view-empty">
            {content.trim() ? 'SOUL.md has no ## sections yet' : 'SOUL.md is empty'}
          </div>
        ) : (
          <div className="identity-view-grid">
            {blocks.map((b) => (
              <section key={b.heading} className="identity-view-card soul-block">
                <div className="soul-block-heading identity-view-card-heading t-meta">{b.heading}</div>
                <div className="soul-block-body">
                  <Markdown>{b.body}</Markdown>
                </div>
              </section>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

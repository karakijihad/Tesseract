import { useEffect, useMemo } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkBreaks from 'remark-breaks';
import { useSoulStore } from '../stores/soul';
import { useIdentityStore } from '../stores/identity';
import { sendCommand } from '../lib/commands';
import { formatRelative } from '../lib/time';
import { parseSoul } from '../lib/soul';
import { Hint } from '../components/ui/Hint';

export function SoulView() {
  const content = useSoulStore((s) => s.content);
  const lastReflectedAt = useSoulStore((s) => s.lastReflectedAt);
  const fetchSoul = useSoulStore((s) => s.fetchSoul);
  // Individual primitive selectors — F1 lesson: a single selector returning
  // a fresh object literal triggers the Zustand getSnapshot infinite loop.
  const name = useIdentityStore((s) => s.name);
  const operatorName = useIdentityStore((s) => s.operatorName);
  const version = useIdentityStore((s) => s.version);
  const modelName = useIdentityStore((s) => s.modelName);
  const provider = useIdentityStore((s) => s.provider);
  const securityMode = useIdentityStore((s) => s.securityMode);

  // Refresh when the operator opens the tab so the chip + blocks reflect
  // any external SOUL.md edits made while a different view was active.
  useEffect(() => {
    fetchSoul();
  }, [fetchSoul]);

  const blocks = useMemo(() => parseSoul(content), [content]);
  const hasIdentity = !!(name || operatorName || version || modelName || securityMode);

  return (
    <div className="soul-view">
      <header className="soul-view-head">
        <div className="soul-view-title-row">
          <h1 className="t-head soul-view-title">Soul</h1>
          <span className="t-meta soul-view-meta">
            Last reflected: {formatRelative(lastReflectedAt)} · {blocks.length} sections
          </span>
        </div>
        <Hint label="Run /reflect — re-read SOUL.md and update memory synthesis" position="bottom" maxWidth={260}>
          <button
            type="button"
            className="soul-view-refresh"
            onClick={() => sendCommand('/reflect')}
          >
            refresh
          </button>
        </Hint>
      </header>

      <div className="soul-view-body">
        {hasIdentity && (
          <section className="soul-view-card soul-view-identity">
            <div className="soul-view-card-heading t-meta">Identity</div>
            <dl className="soul-identity-grid t-caption">
              {name && (<><dt>name</dt><dd>{name}</dd></>)}
              {operatorName && (<><dt>operator</dt><dd>{operatorName}</dd></>)}
              {version && (<><dt>version</dt><dd>{version}</dd></>)}
              {modelName && (
                <>
                  <dt>model</dt>
                  <dd>
                    {modelName}
                    {provider && (
                      <span className="t-meta soul-identity-provider">{` · ${provider}`}</span>
                    )}
                  </dd>
                </>
              )}
              {securityMode && (<><dt>mode</dt><dd>{securityMode}</dd></>)}
            </dl>
          </section>
        )}

        {blocks.length === 0 ? (
          <div className="t-caption soul-view-empty">
            {content.trim() ? 'SOUL.md has no ## sections yet' : 'SOUL.md is empty'}
          </div>
        ) : (
          <div className="soul-view-grid">
            {blocks.map((b) => (
              <section key={b.heading} className="soul-view-card soul-block">
                <div className="soul-block-heading soul-view-card-heading t-meta">{b.heading}</div>
                <div className="soul-block-body">
                  <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]}>{b.body}</ReactMarkdown>
                </div>
              </section>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

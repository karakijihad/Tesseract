import { useState } from 'react';
import { useEntityName } from '../../../hooks/useEntityName';
import type { MemorySuggestionKind, MemoryTarget } from '../../../lib/types';
import { useSuggestionsStore, type SuggestionEntry } from '../../../stores/suggestions';
import { Hint } from '../../ui/Hint';
import { CloseButton } from '../../common/CloseButton';
import { Disclosure } from '../../common/Disclosure';

function formatTarget(t: MemoryTarget): string {
  if (t.kind === 'memory_path') return t.path;
  if (t.kind === 'topic_slug') return `#${t.slug}`;
  return `turn ${t.turn_index}: "${t.text.slice(0, 48)}${t.text.length > 48 ? '…' : ''}"`;
}

function kindClass(kind: MemorySuggestionKind): string {
  return `suggestion-badge suggestion-badge-${kind}`;
}

function confidenceClass(c: number): string {
  if (c >= 0.85) return 'suggestion-conf is-high';
  if (c >= 0.70) return 'suggestion-conf is-mid';
  return 'suggestion-conf is-low';
}

export function ObserverSuggestions() {
  const entityName = useEntityName();
  const suggestions = useSuggestionsStore(s => s.suggestions);
  const dismiss = useSuggestionsStore(s => s.dismiss);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  if (suggestions.length === 0) {
    return (
      <div className="t-caption right-section-empty">
        No suggestions yet — {entityName} will see them as the observer runs.
      </div>
    );
  }

  const newestFirst = [...suggestions].reverse();

  return (
    <ul className="right-section-list suggestions-list">
      {newestFirst.map((entry: SuggestionEntry) => {
        const expanded = expandedId === entry.observation_id;
        return (
          <li key={entry.observation_id} className="suggestion-row">
            <div className="suggestion-row-head">
              <Disclosure
                variant="row"
                className="suggestion-summary"
                onToggle={() => setExpandedId(expanded ? null : entry.observation_id)}
                open={expanded}
              >
                <span className={`${kindClass(entry.kind)} t-meta`}>{entry.kind}</span>
                <span className="suggestion-target t-caption">{formatTarget(entry.target)}</span>
                <Hint label={`confidence ${entry.confidence.toFixed(2)}`}>
                  <span className={`${confidenceClass(entry.confidence)} t-meta`}>
                    {entry.confidence.toFixed(2)}
                  </span>
                </Hint>
                <span className="suggestion-reason t-caption">{entry.reason}</span>
              </Disclosure>
              <Hint label={`Dismiss this suggestion (${entityName} already saw it once on its turn after firing)`}>
                <CloseButton
                  size="inline"
                  onClick={() => dismiss(entry.observation_id)}
                  ariaLabel={`Dismiss ${entry.kind} suggestion`}
                />
              </Hint>
            </div>
            {expanded && (
              <pre className="suggestion-json t-caption">
                {JSON.stringify(entry, null, 2)}
              </pre>
            )}
          </li>
        );
      })}
    </ul>
  );
}

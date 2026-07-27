import { useState } from 'react';
import type { MemorySuggestionKind, MemoryTarget } from '../../../lib/types';
import { useSuggestionsStore, type SuggestionEntry } from '../../../stores/suggestions';

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
  const suggestions = useSuggestionsStore(s => s.suggestions);
  const dismiss = useSuggestionsStore(s => s.dismiss);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  if (suggestions.length === 0) {
    return (
      <div className="t-caption right-section-empty">
        No suggestions yet — TARS will see them as the observer runs.
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
              <button
                type="button"
                className="suggestion-summary"
                onClick={() => setExpandedId(expanded ? null : entry.observation_id)}
                aria-expanded={expanded}
              >
                <span className={`${kindClass(entry.kind)} t-meta`}>{entry.kind}</span>
                <span className="suggestion-target t-caption">{formatTarget(entry.target)}</span>
                <span className={`${confidenceClass(entry.confidence)} t-meta`} title={`confidence ${entry.confidence.toFixed(2)}`}>
                  {entry.confidence.toFixed(2)}
                </span>
                <span className="suggestion-reason t-caption">{entry.reason}</span>
              </button>
              <button
                type="button"
                className="suggestion-dismiss t-meta"
                onClick={() => dismiss(entry.observation_id)}
                aria-label={`Dismiss ${entry.kind} suggestion`}
                title="Dismiss this suggestion (TARS already saw it once on its turn after firing)"
              >
                ×
              </button>
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

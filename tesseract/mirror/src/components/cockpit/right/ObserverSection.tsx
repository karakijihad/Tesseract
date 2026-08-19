import { useObservationsStore } from '../../../stores/observations';
import { Markdown } from '../../common/Markdown';
import { useObserverStore } from '../../../stores/observer';
import { useSuggestionsStore } from '../../../stores/suggestions';
import { ObserverStatsChip } from './ObserverStatsChip';
import { ObserverSuggestions } from './ObserverSuggestions';
import { Hint } from '../../ui/Hint';
import { Button } from '../../common/Button';

function formatTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function secondsAgo(ts: number): number {
  return Math.max(0, Math.floor((Date.now() - ts) / 1000));
}

export function ObserverSection() {
  const observations = useObservationsStore(s => s.observations);
  const pending = useObservationsStore(s => s.pending);
  const firesTotal = useObservationsStore(s => s.fires_total);
  const resetObservations = useObservationsStore(s => s.reset);
  const suggestions = useSuggestionsStore(s => s.suggestions);
  const resetSuggestions = useSuggestionsStore(s => s.reset);
  const armState = useObserverStore(s => s.state);
  const arm = useObserverStore(s => s.arm);
  const disarm = useObserverStore(s => s.disarm);

  const newestFirst = [...observations].reverse();
  const isArmed = armState !== 'off';
  const hasContent = observations.length > 0 || suggestions.length > 0;

  const handleClear = () => {
    resetObservations();
    resetSuggestions();
  };

  return (
    <section className="right-section">
      <div className="right-section-header">
        <span className="t-caption obs-header-count">
          {observations.length} stored · {firesTotal} total fires
          {pending && <span className="right-section-spinner" aria-label="observer pending">◌</span>}
        </span>
      </div>
          <div className="observer-arm-row t-meta">
            <span className="observer-arm-state">arm: {armState}</span>
            <div className="observer-arm-actions">
              {/* The section reported `arm: off` and gave no way to change it —
                  the only control lived in the bottom HUD group that the tab
                  replaced. A state readout with no control beside it is the
                  half of a feature that cannot be used. */}
              <Hint
                label={
                  isArmed
                    ? 'Observer is on — background passes run on your turns'
                    : 'Observer is off — arm it to run background passes'
                }
              >
                <Button
                  tone={isArmed ? 'good' : 'default'}
                  active={isArmed}
                  onClick={() => (isArmed ? disarm() : arm())}
                >
                  {isArmed ? 'disarm' : 'arm'}
                </Button>
              </Hint>
              <Hint label="Clear stored observations + suggestions from this Mirror">
                <Button onClick={handleClear} disabled={!hasContent}>
                  clear
                </Button>
              </Hint>
            </div>
          </div>
          {isArmed && <ObserverStatsChip />}
          {newestFirst.length === 0 ? (
            <div className="t-caption right-section-empty">
              no observations yet — type /observe in chat, or arm the observer for background passes
            </div>
          ) : (
            <ul className="right-section-list observer-list">
              {newestFirst.map((entry, idx) => (
                <li
                  key={`${entry.timestamp}-${idx}`}
                  className="observation-row"
                >
                  <div className="observation-meta">
                    <span className={`observation-badge observation-badge-${entry.mode} t-meta`}>
                      {entry.mode}
                    </span>
                    <span className="t-meta observation-time">{formatTime(entry.timestamp)}</span>
                    {entry.last_fire_ts != null && (
                      <span className="t-meta obs-delta-chip">
                        last fire {secondsAgo(entry.last_fire_ts)}s ago
                      </span>
                    )}
                  </div>
                  <div className="observation-text t-caption"><Markdown>{entry.observation}</Markdown></div>
                </li>
              ))}
            </ul>
          )}
      <div className="observer-suggestions-header t-meta t-label">Suggestions</div>
      <ObserverSuggestions />
    </section>
  );
}

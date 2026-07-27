import { useCallback, useEffect, useState } from 'react';

import {
  ApiError,
  fetchKokoroStatus,
  fetchOllamaStatus,
  fetchPiperStatus,
  fetchWhisperStatus,
  postKokoroAction,
  postOllamaAction,
  postPiperAction,
  postWhisperAction,
  type KokoroStatusResponse,
  type OllamaStatusResponse,
  type PiperStatusResponse,
  type WhisperStatusResponse,
} from '../../lib/api';

// Status-chip cadence only — chat/voice never touch these endpoints. 30s
// keeps the ollama /api/tags probe (and its TIME_WAIT sockets) off the hot
// path; a dead service shows red at most 30s late.
const POLL_INTERVAL_MS = 30_000;

export function LocalModelsSection() {
  const [status, setStatus] = useState<OllamaStatusResponse | null>(null);
  const [whisper, setWhisper] = useState<WhisperStatusResponse | null>(null);
  const [piper, setPiper] = useState<PiperStatusResponse | null>(null);
  const [kokoro, setKokoro] = useState<KokoroStatusResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [whisperBusy, setWhisperBusy] = useState(false);
  const [piperBusy, setPiperBusy] = useState(false);
  const [kokoroBusy, setKokoroBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    // Fetch in parallel — each call hits a separate /api/system/{name}
    // endpoint, and Ollama's tag fetch alone can take ~1s. Running
    // them sequentially stretches the cycle to ~4s; parallel keeps
    // it under 1s and stops one slow service from blocking the rest.
    // allSettled so one backend going down doesn't blank the other
    // three panels — operator still sees fresh state for what works.
    const [s, w, p, k] = await Promise.allSettled([
      fetchOllamaStatus(),
      fetchWhisperStatus(),
      fetchPiperStatus(),
      fetchKokoroStatus(),
    ]);
    if (s.status === 'fulfilled') setStatus(s.value);
    if (w.status === 'fulfilled') setWhisper(w.value);
    if (p.status === 'fulfilled') setPiper(p.value);
    if (k.status === 'fulfilled') setKokoro(k.value);
    const failed = [s, w, p, k].find((r) => r.status === 'rejected') as
      | PromiseRejectedResult
      | undefined;
    if (failed) {
      setError(
        failed.reason instanceof Error
          ? failed.reason.message
          : 'status fetch failed',
      );
    } else {
      setError(null);
    }
  }, []);

  useEffect(() => {
    void refresh();
    // Light poll so the operator sees state shifts (e.g. Ollama crashing
    // outside Mirror, or auto-start completing) without a full reload.
    // Pause when the tab is hidden — no point hitting Ollama every 5s
    // for a panel the operator can't see, and it accumulates TIME_WAIT
    // sockets to localhost:11434 on Windows.
    let id: number | null = null;
    const start = () => {
      if (id !== null) return;
      id = window.setInterval(() => {
        void refresh();
      }, POLL_INTERVAL_MS);
    };
    const stop = () => {
      if (id !== null) {
        window.clearInterval(id);
        id = null;
      }
    };
    const onVisibility = () => {
      if (document.hidden) {
        stop();
      } else {
        void refresh();
        start();
      }
    };
    if (!document.hidden) start();
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      stop();
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [refresh]);

  const onToggle = async () => {
    if (!status || busy) return;
    setBusy(true);
    setError(null);
    try {
      const action = status.running ? 'stop' : 'start';
      const res = await postOllamaAction(action);
      setStatus((prev) =>
        prev
          ? {
              ...prev,
              running: res.running,
              embedding_present: res.embedding_present,
              owned_by_mirror: res.owned_by_mirror,
            }
          : prev,
      );
      void refresh();
    } catch (err) {
      // 409 = "running but Mirror doesn't own it" — surface so operator
      // knows the toggle isn't broken; they need to stop it manually.
      if (err instanceof ApiError) {
        setError(err.message);
      } else if (err instanceof Error) {
        setError(err.message);
      } else {
        setError('toggle failed');
      }
    } finally {
      setBusy(false);
    }
  };

  const onUnloadWhisper = async () => {
    if (!whisper || whisperBusy || (!whisper.loaded && !whisper.disabled)) return;
    setWhisperBusy(true);
    setError(null);
    try {
      await postWhisperAction('unload');
      const fresh = await fetchWhisperStatus();
      setWhisper(fresh);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'whisper unload failed');
    } finally {
      setWhisperBusy(false);
    }
  };

  const onPiperAction = async (action: 'unload' | 'warm') => {
    if (!piper || piperBusy) return;
    setPiperBusy(true);
    setError(null);
    try {
      await postPiperAction(action);
      const fresh = await fetchPiperStatus();
      setPiper(fresh);
    } catch (err) {
      setError(err instanceof Error ? err.message : `piper ${action} failed`);
    } finally {
      setPiperBusy(false);
    }
  };

  const onKokoroAction = async (action: 'unload' | 'warm') => {
    if (!kokoro || kokoroBusy) return;
    setKokoroBusy(true);
    setError(null);
    try {
      await postKokoroAction(action);
      const fresh = await fetchKokoroStatus();
      setKokoro(fresh);
    } catch (err) {
      setError(err instanceof Error ? err.message : `kokoro ${action} failed`);
    } finally {
      setKokoroBusy(false);
    }
  };

  if (!status) {
    return (
      <section className="settings-section">
        <h3 className="settings-section__title">Local models — Ollama</h3>
        <div className="t-meta">{error ?? '(loading…)'}</div>
      </section>
    );
  }

  const stateLabel = status.running
    ? status.embedding_present
      ? 'running · embedding model loaded'
      : 'running · embedding model missing (run `ollama pull`)'
    : 'stopped';

  const ownedHint = status.owned_by_mirror
    ? 'started by Mirror — stop will terminate it'
    : status.running
      ? 'started outside Mirror — stop refused (manual stop required)'
      : 'will spawn `ollama serve` on start';

  return (
    <section className="settings-section">
      <h3 className="settings-section__title">Local models — Ollama</h3>
      <div className="t-meta" style={{ marginBottom: '0.5rem' }}>
        Embedding model {status.embedding_model} runs on Ollama at {status.base_url}.
        Required for memory dedupe + retrieval. Toggle to start or stop.
      </div>
      <div className="cost-row">
        <label className="cost-row__label">Status</label>
        <span className={status.running ? 't-meta' : 't-meta'}>
          {stateLabel}
        </span>
        <span className="cost-row__spend t-meta">{ownedHint}</span>
      </div>
      <div className="cost-row">
        <label className="cost-row__label">Available models</label>
        <span className="t-meta">
          {status.tags.length === 0 ? '—' : status.tags.join(', ')}
        </span>
      </div>
      <div className="cost-row cost-row--actions">
        <button
          type="button"
          className="cost-row__save"
          onClick={onToggle}
          disabled={busy}
        >
          {busy ? '…' : status.running ? 'Stop' : 'Start'}
        </button>
        <button
          type="button"
          className="cost-row__save"
          onClick={() => {
            void refresh();
          }}
          disabled={busy}
          style={{ marginLeft: '0.5rem' }}
        >
          Refresh
        </button>
      </div>
      {error && <div className="settings-error">{error}</div>}
      <div className="cost-row" style={{ marginTop: '0.75rem' }}>
        <label className="cost-row__label">Whisper STT</label>
        <span className="t-meta">
          {whisper?.configured
            ? `${whisper.model} · ${whisper.device}/${whisper.compute_type}`
            : 'not configured'}
        </span>
        <span className="cost-row__spend t-meta">
          {whisper?.disabled
            ? `disabled: ${whisper.disabled_reason}`
            : whisper?.loaded
              ? 'loaded in Mirror process'
              : 'lazy-loads on first voice transcription'}
        </span>
      </div>
      <div className="cost-row cost-row--actions">
        <button
          type="button"
          className="cost-row__save"
          onClick={onUnloadWhisper}
          disabled={whisperBusy || (!whisper?.loaded && !whisper?.disabled)}
        >
          {whisperBusy ? '…' : whisper?.disabled ? 'Reset Whisper' : 'Unload Whisper'}
        </button>
      </div>
      <div className="cost-row" style={{ marginTop: '0.75rem' }}>
        <label className="cost-row__label">Piper TTS</label>
        <span className="t-meta">
          {piper?.configured
            ? `${piper.model_path.split(/[\\/]/).pop() || piper.model_path} · ${piper.sample_rate ?? '?'} Hz`
            : 'not configured'}
        </span>
        <span className="cost-row__spend t-meta">
          {piper?.disabled
            ? `disabled: ${piper.disabled_reason}`
            : piper?.loaded
              ? `loaded · presets: ${piper.presets.join(', ') || '—'}`
              : 'lazy-loads on first synthesis'}
        </span>
      </div>
      <div className="cost-row cost-row--actions">
        <button
          type="button"
          className="cost-row__save"
          onClick={() => void onPiperAction(piper?.disabled || !piper?.loaded ? 'warm' : 'unload')}
          disabled={piperBusy || !piper?.configured}
        >
          {piperBusy
            ? '…'
            : piper?.disabled
              ? 'Reset Piper'
              : piper?.loaded
                ? 'Unload Piper'
                : 'Load Piper'}
        </button>
      </div>
      <div className="cost-row" style={{ marginTop: '0.75rem' }}>
        <label className="cost-row__label">Kokoro TTS</label>
        <span className="t-meta">
          {kokoro?.configured
            ? `${kokoro.model_path.split(/[\\/]/).pop() || kokoro.model_path} · ${kokoro.sample_rate ?? '?'} Hz · ${kokoro.device}`
            : 'not configured'}
        </span>
        <span className="cost-row__spend t-meta">
          {kokoro?.disabled
            ? `disabled: ${kokoro.disabled_reason}`
            : kokoro?.loaded
              ? `loaded · ${kokoro.cached[0]?.provider ?? '?'} · mix: ${
                  Object.entries(kokoro.mix)
                    .map(([v, w]) => `${v}:${w}`)
                    .join(' + ') || '—'
                }`
              : 'lazy-loads on first synthesis'}
        </span>
      </div>
      <div className="cost-row cost-row--actions">
        <button
          type="button"
          className="cost-row__save"
          onClick={() =>
            void onKokoroAction(kokoro?.disabled || !kokoro?.loaded ? 'warm' : 'unload')
          }
          disabled={kokoroBusy || !kokoro?.configured}
        >
          {kokoroBusy
            ? '…'
            : kokoro?.disabled
              ? 'Reset Kokoro'
              : kokoro?.loaded
                ? 'Unload Kokoro'
                : 'Load Kokoro'}
        </button>
      </div>
    </section>
  );
}

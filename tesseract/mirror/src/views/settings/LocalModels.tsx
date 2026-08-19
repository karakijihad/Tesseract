import { Note } from '../../components/common/Note';
import { useCallback, useEffect, useState } from 'react';

import {
  ApiError,
  fetchDependencies,
  fetchKokoroStatus,
  fetchOllamaStatus,
  fetchWhisperStatus,
  postKokoroAction,
  postModelDownload,
  postOllamaAction,
  postWhisperAction,
  type DependencyReport,
  type KokoroStatusResponse,
  type ModelFilesStatus,
  type ModelLane,
  type OllamaStatusResponse,
  type WhisperStatusResponse,
} from '../../lib/api';
import { Button } from '../../components/common/Button';

// Status-chip cadence only — chat/voice never touch these endpoints. 30s
// keeps the ollama /api/tags probe (and its TIME_WAIT sockets) off the hot
// path; a dead service shows red at most 30s late.
const POLL_INTERVAL_MS = 30_000;

/** Last good state, outliving the component that fetched it.
 *
 * The same reasoning as `useCachedFetch`'s cache, which this panel cannot use
 * because it is four endpoints behind one `allSettled` rather than one fetch.
 * The rail mounts one section at a time, so leaving this row and coming back
 * unmounted it and took its `useState` with it — and the panel is gated on
 * `status`, so the whole thing went back to `(loading…)` and refetched from
 * zero every visit. Nothing here has changed in the two seconds you were
 * elsewhere; the poll below revalidates.
 *
 * Written unconditionally in `refresh`, not through a mount-scoped effect: a
 * response is worth keeping whoever asked for it, including a visit switched
 * away from before its fetch landed.
 */
const SNAPSHOT: {
  status: OllamaStatusResponse | null;
  whisper: WhisperStatusResponse | null;
  kokoro: KokoroStatusResponse | null;
  deps: DependencyReport | null;
} = { status: null, whisper: null, kokoro: null, deps: null };

// `device: auto` resolves per machine at model load, so the configured value
// is a placeholder until something is cached. Report what actually loaded —
// `status()["cached"]` carries the resolved device, the config field does not.
function whisperDevice(whisper: WhisperStatusResponse): string {
  const loaded = whisper.cached[0];
  if (loaded) return `${loaded.device}/${loaded.compute_type}`;
  return `${whisper.device}/${whisper.compute_type}`;
}

// The state first-run setup can leave behind: a lane the operator declined,
// then re-enabled in Settings → Capabilities. The lane is configured and the
// engine is installed, but its model files were never fetched, so it latches
// on first use. Saying so — and offering the download — is the difference
// between a fixable state and a mystery.
function ModelFilesRow({
  files,
  lane,
  label,
  size,
  onDownload,
}: {
  files: ModelFilesStatus | null | undefined;
  lane: ModelLane;
  label: string;
  size: string;
  onDownload: (lane: ModelLane) => void;
}) {
  // `null` means the lane isn't configured at all — nothing is missing, so
  // there is nothing to offer.
  if (!files || files.files_present !== false) return null;
  return (
    <div className="cost-row cost-row--actions">
      <span className="t-meta">
        {files.download_error
          ? files.download_error
          : `${label} files are not downloaded — this lane stays silent until they are (${size}).`}
      </span>
      <Button
        onClick={() => onDownload(lane)}
        disabled={files.downloading}
        tone="primary"
      >
        {files.downloading ? 'Downloading…' : 'Download'}
      </Button>
    </div>
  );
}

// The reconciler's verdict for one lane, when it has something to say.
//
// Deliberately renders NOTHING for a healthy dependency. The existing rows
// already report presence and offer a download; what this adds is the case
// presence cannot express — the files are here and are the wrong ones — which
// otherwise looks identical to a working install right up until it misbehaves.
function DriftRow({
  report,
  dependency,
}: {
  report: DependencyReport | null;
  dependency: string;
}) {
  const record = report?.dependencies?.[dependency];
  if (!record || record.state !== 'stale') return null;
  return (
    <div className="cost-row">
      <span className="t-meta">
        {record.reason ||
          'this is not the version this build expects — it will be replaced on the next launch'}
      </span>
    </div>
  );
}

export function LocalModelsSection() {
  const [status, setStatus] = useState<OllamaStatusResponse | null>(
    SNAPSHOT.status,
  );
  const [whisper, setWhisper] = useState<WhisperStatusResponse | null>(
    SNAPSHOT.whisper,
  );
  const [kokoro, setKokoro] = useState<KokoroStatusResponse | null>(
    SNAPSHOT.kokoro,
  );
  const [deps, setDeps] = useState<DependencyReport | null>(SNAPSHOT.deps);
  const [busy, setBusy] = useState(false);
  const [whisperBusy, setWhisperBusy] = useState(false);
  const [kokoroBusy, setKokoroBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    // Fetch in parallel — each call hits a separate /api/system/{name}
    // endpoint, and Ollama's tag fetch alone can take ~1s. Running
    // them sequentially stretches the cycle to ~4s; parallel keeps
    // it under 1s and stops one slow service from blocking the rest.
    // allSettled so one backend going down doesn't blank the other
    // panels — operator still sees fresh state for what works.
    const [s, w, k, d] = await Promise.allSettled([
      fetchOllamaStatus(),
      fetchWhisperStatus(),
      fetchKokoroStatus(),
      // Reads the artifact the launch pass wrote — no probing, no network.
      fetchDependencies(),
    ]);
    if (s.status === 'fulfilled') SNAPSHOT.status = s.value;
    if (w.status === 'fulfilled') SNAPSHOT.whisper = w.value;
    if (k.status === 'fulfilled') SNAPSHOT.kokoro = k.value;
    if (d.status === 'fulfilled') SNAPSHOT.deps = d.value;
    if (s.status === 'fulfilled') setStatus(s.value);
    if (w.status === 'fulfilled') setWhisper(w.value);
    if (k.status === 'fulfilled') setKokoro(k.value);
    if (d.status === 'fulfilled') setDeps(d.value);
    // `deps` is deliberately absent from the error check below: it is the
    // newest of them and the only one whose absence costs nothing on
    // screen, so an install that predates it must not blank the panel.
    const failed = [s, w, k].find((r) => r.status === 'rejected') as
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

  const onOllamaAction = async (action: 'start' | 'stop' | 'install') => {
    if (!status || busy) return;
    setBusy(true);
    setError(null);
    try {
      const res = await postOllamaAction(action);
      setStatus((prev) =>
        prev
          ? {
              ...prev,
              running: res.running,
              embedding_present: res.embedding_present,
              owned_by_mirror: res.owned_by_mirror,
              binary_present: res.binary_present,
              installing: res.installing,
              install_error: res.install_error,
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
      SNAPSHOT.whisper = fresh;
      setWhisper(fresh);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'whisper unload failed');
    } finally {
      setWhisperBusy(false);
    }
  };

  const onDownload = async (lane: ModelLane) => {
    setError(null);
    try {
      await postModelDownload(lane);
      // The POST returns once the fetch is SCHEDULED — a 1.6 GB snapshot
      // would outlive any request timeout. Refresh so the row flips to
      // "Downloading…" now, and let the poll carry it to done.
      void refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : `${lane} download failed`);
    }
  };

  const onKokoroAction = async (action: 'unload' | 'warm') => {
    if (!kokoro || kokoroBusy) return;
    setKokoroBusy(true);
    setError(null);
    try {
      await postKokoroAction(action);
      const fresh = await fetchKokoroStatus();
      SNAPSHOT.kokoro = fresh;
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
        <div className="t-meta">{error ?? '(loading…)'}</div>
      </section>
    );
  }

  // Absent binary is its own state, not a kind of "stopped": a start toggle
  // cannot fix it, and semantic search stays off until something installs it.
  const stateLabel = status.installing
    ? 'installing — downloading Ollama and the embedding model…'
    : status.install_error
      ? `install failed: ${status.install_error}`
      : !status.binary_present
    ? 'not installed — semantic search is off (keyword search still works)'
    : status.running
      ? !status.embedding_model
        ? 'running'
        : // `tags_error` first: the backend reports `embedding_present` as
          // true when it could not read the tag list, so that an unreadable
          // daemon does not raise a false "missing" badge. Checking
          // `embedding_present` before this would render "loaded" over a
          // model nobody actually looked for.
          status.tags_error
          ? `running · could not check models (${status.tags_error})`
          : status.embedding_present
            ? 'running · embedding model loaded'
            : 'running · embedding model missing'
      : 'stopped';

  const ownedHint = !status.binary_present
    ? 'Install downloads Ollama and pulls the embedding model'
    : status.owned_by_mirror
      ? 'started by Mirror — stop will terminate it'
      : status.running
        ? 'started outside Mirror — stop refused (manual stop required)'
        : 'will spawn `ollama serve` on start';

  return (
    <section className="settings-section">
      <div className="t-meta" style={{ marginBottom: '0.5rem' }}>
        {status.embedding_model
          ? `Embedding model ${status.embedding_model} runs on Ollama at ${status.base_url}. Required for memory dedupe + retrieval. Toggle to start or stop.`
          : `Ollama runs at ${status.base_url}. Toggle to start or stop.`}
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
          {status.tags_error
            ? 'unknown — the daemon did not answer'
            : status.tags.length === 0
              ? '—'
              : status.tags.join(', ')}
        </span>
      </div>
      <div className="cost-row cost-row--actions">
        {status.binary_present ? (
          <Button
            onClick={() => void onOllamaAction(status.running ? 'stop' : 'start')}
            disabled={busy}
            tone="primary"
          >
            {busy ? '…' : status.running ? 'Stop' : 'Start'}
          </Button>
        ) : (
          // The recovery path for a first run whose silent install was blocked
          // or declined. The per-launch retry runs `--no-install` on purpose,
          // so without this button the only way back was a typed command.
          <Button
            onClick={() => void onOllamaAction('install')}
            disabled={busy || status.installing}
            tone="primary"
          >
            {status.installing ? 'Installing…' : 'Install Ollama'}
          </Button>
        )}
        {status.running && !status.embedding_present && (
          <Button
            onClick={() => void onOllamaAction('install')}
            disabled={busy || status.installing}
            tone="primary"
          >
            {status.installing ? 'Pulling…' : 'Pull embedding model'}
          </Button>
        )}
        <Button
          onClick={() => {
            void refresh();
          }}
          disabled={busy}
          tone="primary"
        >
          Refresh
        </Button>
      </div>
      {error && <Note tone="bad">{error}</Note>}
      <div className="cost-row" style={{ marginTop: '0.75rem' }}>
        <label className="cost-row__label">Whisper STT</label>
        <span className="t-meta">
          {whisper?.configured
            ? `${whisper.model} · ${whisperDevice(whisper)}`
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
        <Button
          onClick={onUnloadWhisper}
          disabled={whisperBusy || (!whisper?.loaded && !whisper?.disabled)}
          tone="primary"
        >
          {whisperBusy ? '…' : whisper?.disabled ? 'Reset Whisper' : 'Unload Whisper'}
        </Button>
      </div>
      <ModelFilesRow
        files={whisper}
        lane="whisper"
        label="Speech recognition model"
        size="~1.6 GB"
        onDownload={onDownload}
      />
      <DriftRow report={deps} dependency="whisper" />
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
        <Button
          onClick={() =>
            void onKokoroAction(kokoro?.disabled || !kokoro?.loaded ? 'warm' : 'unload')
          }
          disabled={kokoroBusy || !kokoro?.configured}
          tone="primary"
        >
          {kokoroBusy
            ? '…'
            : kokoro?.disabled
              ? 'Reset Kokoro'
              : kokoro?.loaded
                ? 'Unload Kokoro'
                : 'Load Kokoro'}
        </Button>
      </div>
      <ModelFilesRow
        files={kokoro}
        lane="kokoro"
        label="Kokoro voice"
        size="~340 MB"
        onDownload={onDownload}
      />
      <DriftRow report={deps} dependency="kokoro" />
      <DriftRow report={deps} dependency="reranker" />
    </section>
  );
}

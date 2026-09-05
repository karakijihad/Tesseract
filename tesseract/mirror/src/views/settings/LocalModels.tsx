import { Note } from '../../components/common/Note';
import { useCallback, useEffect, useMemo, useState } from 'react';

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
  fetchCatalog,
  postModelRef,
} from '../../lib/api';
import { Button } from '../../components/common/Button';
import { Select } from '../../components/common/Select';
import { DataTable } from '../../components/common/DataTable';
import { Hint } from '../../components/ui/Hint';
import { useCachedFetch } from '../../lib/useCachedFetch';
import type { CatalogEntry, CatalogResponse } from '../../lib/types';

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
    <Hint
      label={
        files.download_error ||
        `${label} files are not on this machine. The lane stays silent until they are (${size}).`
      }
    >
      <Button
        onClick={() => onDownload(lane)}
        disabled={files.downloading}
        tone="primary"
      >
        {files.downloading ? 'Downloading…' : `Download ${size}`}
      </Button>
    </Hint>
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
    <Note tone="warn">
      {record.reason ||
        'What is installed is not the version this build expects. It will be replaced on the next launch.'}
    </Note>
  );
}

const LOCAL_COLUMNS = [
  { label: "model", width: "160px" },
  { label: "status", width: "minmax(0, 1fr)" },
  { label: "selection", width: "240px" },
  { label: "actions", width: "200px" },
];

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
  const [swapping, setSwapping] = useState<string | null>(null);

  // The same cache key Model roles uses, so opening both costs one fetch.
  // The picker writes through `postModelRef`, which is that panel's writer
  // too: this surface shows a different question about the same setting, and
  // a second endpoint for it would be a second answer.
  const { data: catalog, set: setCatalog } = useCachedFetch<CatalogResponse>(
    'settings.catalog',
    fetchCatalog,
  );

  const optionsByTarget = useMemo(() => {
    const out = new Map<string, { value: string; label: string }[]>();
    for (const meta of catalog?.targets ?? []) {
      const allowed = meta.allowed_kinds;
      out.set(
        meta.target,
        (catalog?.entries ?? [])
          .filter((e: CatalogEntry) => allowed.length === 0 || allowed.includes(e.kind))
          .map((e: CatalogEntry) => ({
            value: e.ref,
            label: `${e.model} · ${e.tier}.${e.provider}`,
          })),
      );
    }
    return out;
  }, [catalog]);

  const swapRef = async (target: string, ref: string) => {
    if (!catalog || catalog.current[target] === ref) return;
    setSwapping(target);
    setError(null);
    try {
      await postModelRef({ target, ref });
      setCatalog({ ...catalog, current: { ...catalog.current, [target]: ref } });
      void refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : `${target} model change failed`);
    } finally {
      setSwapping(null);
    }
  };

  const refresh = useCallback(async (recheck = false) => {
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
      // Reads the artifact the launch pass wrote: no probing, no network. The
      // Refresh button passes `true`, which runs a real pass. Without that the
      // panel kept reporting a conflict the operator had already fixed, until
      // the next relaunch, because nothing else ever rewrites the artifact.
      fetchDependencies(recheck),
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
    ? 'installing: downloading Ollama and the embedding model…'
    : status.install_error
      ? `install failed: ${status.install_error}`
      : !status.binary_present
    ? 'not installed, so search falls back to keywords'
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
      ? 'started by Mirror, so Stop will end it'
      : status.running
        ? 'started outside Mirror, so Stop is refused and you have to end it yourself'
        : 'will spawn `ollama serve` on start';

  // One row per thing that can be installed, in the order an operator meets
  // them: the daemon, what it serves, then the two speech lanes. Each row
  // answers the same three questions, which is what makes them a table
  // rather than four stacked panels with shapes of their own.
  const picker = (target: string, ariaLabel: string) => {
    const options = optionsByTarget.get(target) ?? [];
    const current = catalog?.current[target] ?? '';
    if (options.length === 0) return <span className="t-meta">not selectable</span>;
    return (
      <Select
        value={current}
        disabled={swapping === target}
        onChange={(v) => void swapRef(target, v)}
        ariaLabel={ariaLabel}
        options={[
          ...(current && !options.some((o) => o.value === current)
            ? [{ value: current, label: current + ' (not in the catalog)' }]
            : []),
          ...(!current ? [{ value: '', label: 'not configured' }] : []),
          ...options,
        ]}
      />
    );
  };

  return (
    <section className="settings-section">
      <Note>
        What runs on this machine rather than over the network. Ollama serves
        the embedding model at {status.base_url}; the two speech lanes load
        into this process the first time something needs them.
      </Note>
      {error && <Note tone="bad">{error}</Note>}

      <DataTable
        label="What runs on this machine"
        columns={LOCAL_COLUMNS}
        rows={[
          {
            key: "ollama",
            cells: [
              <span className="local-table__name">Ollama</span>,
              // How many models are downloaded is part of what state the
              // daemon is in, so it reads here. It sat in the selection column
              // saying "4 pulled", which is not a selection and is not
              // something you can pick.
              <span className="local-table__status t-meta">
                {stateLabel}
                {status.binary_present && (
                  <Hint
                    label={
                      status.tags_error
                        ? 'The daemon did not answer: ' + status.tags_error
                        : status.tags.length === 0
                          ? 'Nothing is downloaded yet'
                          : status.tags.join(', ')
                    }
                  >
                    <span className="t-meta">
                      {' · '}
                      {status.tags_error
                        ? 'model list unavailable'
                        : `${status.tags.length} downloaded`}
                    </span>
                  </Hint>
                )}
              </span>,
              // A daemon is not a model, so there is nothing here to choose.
              <span className="local-table__pick t-meta">{'—'}</span>,
              <span className="local-table__actions">
                <Hint label={ownedHint}>
                  {status.binary_present ? (
                    <Button
                      onClick={() =>
                        void onOllamaAction(status.running ? 'stop' : 'start')
                      }
                      disabled={busy}
                      tone="primary"
                    >
                      {busy ? 'working' : status.running ? 'Stop' : 'Start'}
                    </Button>
                  ) : (
                    // The recovery path for a first run whose silent install was
                    // blocked or declined. The per-launch retry runs
                    // `--no-install` on purpose, so without this the only way back
                    // was a typed command.
                    <Button
                      onClick={() => void onOllamaAction('install')}
                      disabled={busy || status.installing}
                      tone="primary"
                    >
                      {status.installing ? 'Installing' : 'Install'}
                    </Button>
                  )}
                </Hint>
                <Hint label="Re-reads the state of everything above, and checks the installed packages again rather than trusting what was found at launch.">
                  <Button
                    onClick={() => void refresh(true)}
                    disabled={busy}
                    tone="primary"
                  >
                    Refresh
                  </Button>
                </Hint>
              </span>,
            ],
          },
          {
            key: "embedding",
            cells: [
              <span className="local-table__name">Embedding</span>,
              <span className="local-table__status t-meta">
                {!status.binary_present
                  ? 'Ollama is not installed'
                  : !status.running
                    ? 'Ollama is not running'
                    : status.tags_error
                      ? 'could not check'
                      : status.embedding_present
                        ? 'loaded'
                        : 'not pulled'}
              </span>,
              <span className="local-table__pick">
                {picker('embeddings', 'embedding model')}
              </span>,
              <span className="local-table__actions">
                {status.running && !status.embedding_present && (
                  <Button
                    onClick={() => void onOllamaAction('install')}
                    disabled={busy || status.installing}
                    tone="primary"
                  >
                    {status.installing ? 'Pulling' : 'Pull'}
                  </Button>
                )}
              </span>,
            ],
          },
          {
            key: "stt",
            cells: [
              <span className="local-table__name">Speech to text</span>,
              <span className="local-table__status t-meta">
                {whisper?.disabled
                  ? 'off: ' + whisper.disabled_reason
                  : !whisper?.configured
                    ? 'not configured'
                    : whisper.loaded
                      ? 'loaded on ' + whisperDevice(whisper)
                      : 'loads on first use, on ' + whisperDevice(whisper)}
              </span>,
              <span className="local-table__pick">
                {picker('voice_stt', 'speech to text model')}
              </span>,
              <span className="local-table__actions">
                <Button
                  onClick={onUnloadWhisper}
                  disabled={whisperBusy || (!whisper?.loaded && !whisper?.disabled)}
                  tone="primary"
                >
                  {whisperBusy ? 'working' : whisper?.disabled ? 'Reset' : 'Unload'}
                </Button>
                <ModelFilesRow
                  files={whisper}
                  lane="whisper"
                  label="The speech recognition model"
                  size="1.6 GB"
                  onDownload={onDownload}
                />
              </span>,
            ],
          },
          {
            key: "tts",
            cells: [
              <span className="local-table__name">Text to speech</span>,
              <span className="local-table__status t-meta">
                {kokoro?.disabled
                  ? 'off: ' + kokoro.disabled_reason
                  : !kokoro?.configured
                    ? 'not configured'
                    : kokoro.loaded
                      ? 'loaded on ' + (kokoro.cached[0]?.provider ?? 'an unnamed provider')
                      : 'loads on first use'}
              </span>,
              <span className="local-table__pick">
                {picker('voice_tts', 'text to speech model')}
              </span>,
              <span className="local-table__actions">
                <Button
                  onClick={() =>
                    void onKokoroAction(
                      kokoro?.disabled || !kokoro?.loaded ? 'warm' : 'unload',
                    )
                  }
                  disabled={kokoroBusy || !kokoro?.configured}
                  tone="primary"
                >
                  {kokoroBusy
                    ? 'working'
                    : kokoro?.disabled
                      ? 'Reset'
                      : kokoro?.loaded
                        ? 'Unload'
                        : 'Load'}
                </Button>
                <ModelFilesRow
                  files={kokoro}
                  lane="kokoro"
                  label="The Kokoro voice"
                  size="340 MB"
                  onDownload={onDownload}
                />
              </span>,
            ],
          },
        ]}
      />

      <DriftRow report={deps} dependency="whisper" />
      <DriftRow report={deps} dependency="kokoro" />
      <DriftRow report={deps} dependency="reranker" />
      <DriftRow report={deps} dependency="package-conflicts" />
    </section>
  );
}

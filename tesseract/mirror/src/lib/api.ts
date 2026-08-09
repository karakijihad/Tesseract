import type {
  SoulResponse,
  BreakersResponse,
  IdentityResponse,
  IdentityChangedData,
  IdentitySavePatch,
  Envelope,
  ScheduleResponse,
  ScheduleRolesResponse,
  TerminalConfig,
  ToolsResponse,
  ModeResponse,
  CompactThresholdResponse,
  CostSettingsResponse,
  CostStateData,
  ConfigFilesResponse,
  ToolPermissionResponse,
  RoleModelsUpdateInput,
  RoleModelsUpdateResponse,
  CatalogResponse,
  ModelRefUpdateInput,
  ModelRefUpdateResponse,
  ChatAttachment,
  Agent,
  AgentDetail,
  AgentsListResponse,
  Alarm,
  AlarmsResponse,
} from "./types";
import { isEnvelope } from "./envelope";
import { BACKEND_BASE } from "./endpoints";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    // The parsed error body, when the backend sent one. A 409 from the
    // workspace doc save carries the current bytes and a diff — losing
    // those to a flattened message would leave the operator with a
    // conflict they can't resolve.
    public payload?: Record<string, unknown>,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export interface ChatUploadConfig {
  max_file_mb: number;
  max_total_mb: number;
  max_files_per_message: number;
  allowed_mime_types: string[];
  allowed_extensions: string[];
}

const BASE = BACKEND_BASE;
// 15s — Settings polls fire every 5s; a 5s timeout fights the next poll
// when the backend stalls behind a long turn or a piper warm-up.
const TIMEOUT_MS = 15_000;
const UPLOAD_TIMEOUT_MS = 60_000;

async function timedFetch(
  input: string,
  init: RequestInit,
  timeoutMs: number,
): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(
    () =>
      controller.abort(
        new DOMException(
          `Request timed out after ${timeoutMs}ms`,
          "TimeoutError",
        ),
      ),
    timeoutMs,
  );
  try {
    return await fetch(input, { ...init, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

// Network-flake retry. The backend's aiohttp loop occasionally stalls
// behind a heavy turn (LLM streaming, piper warm-up, faiss index reload)
// long enough that an in-flight Settings fetch trips its 15s timeout
// or the OS drops the connection — surfacing as `TypeError: Failed to
// fetch` even though the next request would succeed. Retry these
// transient network failures only; do NOT retry HTTP errors (those are
// real and the caller's UI must surface them as-is).
const RETRY_DELAYS_MS = [300, 800];

function _isTransientNetError(err: unknown): boolean {
  // `Failed to fetch` (browser network failure) is a TypeError.
  // AbortError fires when the timeout abort controller trips.
  if (err instanceof TypeError) return true;
  if (
    err instanceof DOMException &&
    (err.name === "AbortError" || err.name === "TimeoutError")
  ) {
    return true;
  }
  return false;
}

async function _retryingFetch(
  url: string,
  init: RequestInit,
  timeoutMs: number,
): Promise<Response> {
  let lastErr: unknown = null;
  for (let attempt = 0; attempt <= RETRY_DELAYS_MS.length; attempt++) {
    try {
      return await timedFetch(url, init, timeoutMs);
    } catch (err) {
      lastErr = err;
      if (!_isTransientNetError(err) || attempt === RETRY_DELAYS_MS.length) {
        throw err;
      }
      await new Promise((r) => setTimeout(r, RETRY_DELAYS_MS[attempt]));
    }
  }
  throw lastErr;
}

async function apiFetch<T>(path: string): Promise<T> {
  const res = await _retryingFetch(`${BASE}${path}`, {}, TIMEOUT_MS);
  if (!res.ok) {
    throw new ApiError(res.status, `HTTP ${res.status}: ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

export async function fetchCostState(): Promise<CostStateData> {
  return apiFetch<CostStateData>("/api/cost/state");
}

// ── Capability report (Task 14b) ────────────────────────
// Nothing is required — every provider/key is optional. This reports what's
// available, what's off, and why (no key vs disabled in providers.yaml).
// Never a gate: no `ready` flag, no secret values, names only.

// "ready" = actually checked and good. "unavailable" = checked and NOT
// good (reason says which — disabled / missing key / binary not on PATH).
// "unverified" = enabled, keyless, non-cli — nothing cheap here confirms
// it further (e.g. Ollama reachability, whisper/piper model files); see
// Settings -> Local Models for that live diagnostic instead of asserting
// availability this report doesn't actually know.
export type CapabilityProviderStatus = "ready" | "unavailable" | "unverified";

export interface CapabilityProvider {
  tier: string;
  provider: string;
  // `enabled` is the AND of the two below — what the runtime acts on.
  // The separate flags drive the two separate checkboxes: a provider keeps
  // its own `true` while its tier is off, so re-enabling the tier restores
  // exactly the per-provider picture the operator left behind.
  enabled: boolean;
  tier_enabled: boolean;
  provider_enabled: boolean;
  key_name: string | null;
  key_present: boolean | null;
  status: CapabilityProviderStatus;
  reason: string | null;
}

export interface CapabilityChatCandidate {
  provider: string;
  model: string;
  available: boolean;
  reason: string | null;
}

export interface CapabilityChat {
  available: boolean;
  reason: string | null;
  candidates: CapabilityChatCandidate[];
}

export interface CapabilityIntegration {
  name: string;
  key_name: string;
  key_present: boolean;
}

// cli-auth DESIGN.md §4/§5 — per roles.yaml role, whether its primary
// resolves to an unauthenticated cli provider with no covering fallback.
export interface CapabilityRole {
  role: string;
  broken: boolean;
  reason: string | null;
  login_hint: string | null;
}

export interface CapabilitiesResponse {
  env_path: string;
  chat: CapabilityChat;
  providers: CapabilityProvider[];
  roles: CapabilityRole[];
  // Whether the operator dismissed the first-run notice (DESIGN.md §5),
  // persisted backend-side under <TESSERACT_HOME>/runtime/.
  notice_dismissed: boolean;
  integrations: CapabilityIntegration[];
}

export async function fetchCapabilities(): Promise<CapabilitiesResponse> {
  return apiFetch<CapabilitiesResponse>("/api/capabilities");
}

// Forces a fresh cli-auth probe; returns the same report shape.
export async function postCapabilitiesReverify(): Promise<CapabilitiesResponse> {
  return apiPost<CapabilitiesResponse>(
    "/api/capabilities/reverify",
    {},
    { retryable: true },
  );
}

// Persists dismissal of the first-run cli-auth notice.
export async function postCapabilitiesDismiss(): Promise<CapabilitiesResponse> {
  return apiPost<CapabilitiesResponse>(
    "/api/capabilities/dismiss",
    {},
    { retryable: true },
  );
}

// Flips a tier switch (`provider: null`) or one provider's, in providers.yaml.
// Returns the refreshed report, so callers don't need a follow-up GET.
export async function postProviderEnabled(
  tier: string,
  provider: string | null,
  enabled: boolean,
): Promise<CapabilitiesResponse> {
  return apiPost<CapabilitiesResponse>("/api/capabilities/provider-enabled", {
    tier,
    provider,
    enabled,
  });
}

export interface RuntimeRestartResponse {
  status: string;
  intent: string;
  continuation_id: string;
  reason: string;
}

// No session_id — the backend accepts any localhost caller for this route
// (routes/runtime.py::post_restart_for_code_drift), which covers exactly
// this cold-boot case where no operator chat session exists yet.
export async function postRuntimeRestart(
  reason: string,
): Promise<RuntimeRestartResponse> {
  return apiPost<RuntimeRestartResponse>(
    "/api/runtime/restart_for_code_drift",
    { reason },
  );
}

export async function fetchSoul(): Promise<SoulResponse> {
  return apiFetch<SoulResponse>("/api/soul");
}

export async function fetchBreakers(): Promise<BreakersResponse> {
  return apiFetch<BreakersResponse>("/api/breakers");
}

export async function fetchIdentity(): Promise<IdentityResponse> {
  return apiFetch<IdentityResponse>("/api/identity");
}

// Every field is optional — the Identity tab saves one control at a time
// and the backend writes only the keys it is sent. Set-to-X, so retryable.
export async function saveIdentity(
  patch: IdentitySavePatch,
): Promise<IdentityChangedData> {
  return apiPost<IdentityChangedData>("/api/identity", patch, {
    retryable: true,
  });
}

export async function fetchSchedule(): Promise<ScheduleResponse> {
  return apiFetch<ScheduleResponse>("/api/schedule");
}

export async function fetchEventsSince(
  sessionId: string,
  since: string,
  limit = 200,
): Promise<Envelope[]> {
  const params = new URLSearchParams({
    session_id: sessionId,
    since,
    limit: String(limit),
  });
  const result = await apiFetch<{ events: unknown[] }>(`/api/events?${params}`);
  return result.events.filter(isEnvelope);
}

// ── Terminal (Phase 9) ──────────────────────────────────

export async function uploadChatAttachment(
  sessionId: string,
  file: File,
): Promise<ChatAttachment> {
  const form = new FormData();
  form.append("file", file);
  const res = await timedFetch(
    `${BASE}/api/uploads/chat/${encodeURIComponent(sessionId)}`,
    { method: "POST", body: form },
    UPLOAD_TIMEOUT_MS,
  );
  if (!res.ok) {
    let msg = `HTTP ${res.status}: ${res.statusText}`;
    try {
      const err = await res.json();
      if (err?.error) msg = String(err.error);
    } catch {
      // keep default message
    }
    throw new ApiError(res.status, msg);
  }
  const raw = (await res.json()) as { attachment: ChatAttachment };
  return {
    ...raw.attachment,
    url: `${BASE}${raw.attachment.url}`,
  };
}

export async function fetchChatUploadConfig(): Promise<ChatUploadConfig> {
  return apiFetch<ChatUploadConfig>("/api/uploads/chat/config");
}

export async function deleteChatAttachment(att: ChatAttachment): Promise<void> {
  const res = await timedFetch(
    `${BASE}/api/uploads/chat/${encodeURIComponent(att.session_id)}/${encodeURIComponent(att.id)}`,
    { method: "DELETE" },
    TIMEOUT_MS,
  );
  if (!res.ok)
    throw new ApiError(res.status, `HTTP ${res.status}: ${res.statusText}`);
}

export interface VaultPromoteResponse {
  ok: boolean;
  vault_path?: string;
  error?: string;
}

export async function promoteAttachmentToVault(
  att: ChatAttachment,
): Promise<VaultPromoteResponse> {
  const res = await timedFetch(
    `${BASE}/api/uploads/chat/${encodeURIComponent(att.session_id)}/${encodeURIComponent(att.id)}/promote-to-vault`,
    { method: "POST" },
    UPLOAD_TIMEOUT_MS,
  );
  if (!res.ok) {
    let msg = `HTTP ${res.status}: ${res.statusText}`;
    try {
      const err = await res.json();
      if (err?.error) msg = String(err.error);
    } catch {
      // keep default
    }
    throw new ApiError(res.status, msg);
  }
  return res.json();
}

export async function fetchTerminalConfig(): Promise<TerminalConfig> {
  const raw = await apiFetch<{ terminal: TerminalConfig }>(
    "/api/terminal/config",
  );
  return raw.terminal;
}

export async function setTerminalTheme(
  theme: string,
): Promise<{ active_theme: string }> {
  const res = await fetch(`${BASE}/api/terminal/theme`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ theme }),
  });
  if (!res.ok)
    throw new ApiError(res.status, `HTTP ${res.status}: ${res.statusText}`);
  return res.json();
}

export interface TerminalRecordingMeta {
  id: string;
  size: number;
  modified: number;
}

export async function fetchRecordings(): Promise<TerminalRecordingMeta[]> {
  const raw = await apiFetch<{ recordings: TerminalRecordingMeta[] }>(
    "/api/terminal/recordings",
  );
  return raw.recordings;
}

export async function fetchRecordingText(id: string): Promise<string> {
  const res = await fetch(
    `${BASE}/api/terminal/recordings/${encodeURIComponent(id)}`,
  );
  if (!res.ok) {
    throw new ApiError(res.status, `HTTP ${res.status}: ${res.statusText}`);
  }
  return res.text();
}

// ── Settings (Phase 14) ─────────────────────────────────

export async function fetchTools(): Promise<ToolsResponse> {
  return apiFetch<ToolsResponse>("/api/tools");
}

interface PostOptions {
  // Retry transient network errors. Only set this for endpoints whose
  // body is idempotent (set-to-X writes like role-models, session-caps,
  // model-ref, cost limits). NEVER set it for POSTs that create things
  // or fire side-effecting decisions (workspace decision) — a
  // network-flake retry on those could double-create.
  retryable?: boolean;
}

async function apiPost<T>(
  path: string,
  body: unknown,
  options: PostOptions = {},
): Promise<T> {
  const init: RequestInit = {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  const res = options.retryable
    ? await _retryingFetch(`${BASE}${path}`, init, TIMEOUT_MS)
    : await timedFetch(`${BASE}${path}`, init, TIMEOUT_MS);
  if (!res.ok) {
    let msg = `HTTP ${res.status}: ${res.statusText}`;
    let payload: Record<string, unknown> | undefined;
    try {
      const err = await res.json();
      if (err && typeof err === "object") payload = err as Record<string, unknown>;
      if (err?.error) msg = String(err.error);
      if (err?.detail) msg = `${msg}: ${String(err.detail)}`;
    } catch {
      // response had no JSON body — keep the default message
    }
    throw new ApiError(res.status, msg, payload);
  }
  return res.json() as Promise<T>;
}

export async function postMode(mode: string): Promise<ModeResponse> {
  return apiPost<ModeResponse>("/api/mode", { mode }, { retryable: true });
}

export interface CompactionUpdateInput {
  role: string;
  ratio?: number;
  keep_recent_turns?: number;
}

export async function postCompactThreshold(
  input: CompactionUpdateInput,
): Promise<CompactThresholdResponse> {
  return apiPost<CompactThresholdResponse>(
    "/api/settings/compact-threshold",
    input,
    { retryable: true },
  );
}

export async function fetchConfigFiles(): Promise<ConfigFilesResponse> {
  return apiFetch<ConfigFilesResponse>("/api/settings/config-files");
}

export async function postToolPermission(
  name: string,
  posture: "auto" | "ask" | "deny",
): Promise<ToolPermissionResponse> {
  return apiPost<ToolPermissionResponse>(
    "/api/settings/tool-permission",
    { name, posture },
    { retryable: true },
  );
}

export async function postRoleModels(
  input: RoleModelsUpdateInput,
): Promise<RoleModelsUpdateResponse> {
  return apiPost<RoleModelsUpdateResponse>("/api/settings/role-models", input, {
    retryable: true,
  });
}

export async function fetchCatalog(): Promise<CatalogResponse> {
  return apiFetch<CatalogResponse>("/api/settings/catalog");
}

export async function postModelRef(
  input: ModelRefUpdateInput,
): Promise<ModelRefUpdateResponse> {
  return apiPost<ModelRefUpdateResponse>("/api/settings/model-ref", input, {
    retryable: true,
  });
}

export interface CostUpdateInput {
  warning_at_pct?: number;
  per_role?: Record<string, number>;
}

export async function postCostSettings(
  update: CostUpdateInput,
): Promise<CostSettingsResponse> {
  return apiPost<CostSettingsResponse>("/api/settings/cost", update, {
    retryable: true,
  });
}

export interface VoiceCostUpdateInput {
  tts?: Record<
    string,
    { cost_per_million_chars?: number; daily_budget_usd?: number }
  >;
  stt?: Record<
    string,
    { cost_per_audio_hour?: number; daily_budget_usd?: number }
  >;
}

export async function postVoiceCostSettings(
  update: VoiceCostUpdateInput,
): Promise<CostSettingsResponse> {
  return apiPost<CostSettingsResponse>("/api/settings/voice-cost", update, {
    retryable: true,
  });
}

export interface OllamaStatusResponse {
  running: boolean;
  base_url: string;
  embedding_model: string;
  tags: string[];
  embedding_present: boolean;
  owned_by_mirror: boolean;
  // Distinguishes "stopped" from "never installed" — the two need different
  // offers, and only the second one is a download worth asking about.
  binary_present: boolean;
  // An install runs detached (it can take the better part of an hour), so its
  // progress and outcome arrive through the status poll, not the POST reply.
  installing: boolean;
  install_error: string | null;
}

export interface OllamaActionResponse {
  ok: true;
  message: string;
  running: boolean;
  embedding_present: boolean;
  owned_by_mirror: boolean;
  binary_present: boolean;
  installing: boolean;
  install_error: string | null;
}

export async function fetchOllamaStatus(): Promise<OllamaStatusResponse> {
  return apiFetch<OllamaStatusResponse>("/api/system/ollama");
}

export async function postOllamaAction(
  action: "start" | "stop" | "install",
): Promise<OllamaActionResponse> {
  return apiPost<OllamaActionResponse>("/api/system/ollama", { action });
}

/** The model-file fields every local-model lane carries.
 *
 * `files_present` is `null` when the lane is not configured at all — which
 * is different from "configured but the files were never downloaded", the
 * state an operator lands in after declining a lane during first-run setup
 * and re-enabling it later.
 */
export interface ModelFilesStatus {
  files_present: boolean | null;
  downloading: boolean;
  download_error: string;
}

export type ModelLane = "whisper" | "kokoro" | "piper";

export interface ModelDownloadResponse extends ModelFilesStatus {
  ok: true;
}

/** Schedules the fetch and returns immediately — the Whisper snapshot alone
 * is 1.6 GB. Progress is read back from the lane's own status. */
export async function postModelDownload(
  lane: ModelLane,
): Promise<ModelDownloadResponse> {
  return apiPost<ModelDownloadResponse>("/api/system/models/download", { lane });
}

export interface WhisperStatusResponse extends ModelFilesStatus {
  configured: boolean;
  provider: string;
  model: string;
  device: string;
  compute_type: string;
  language: string | null;
  timeout_seconds: number | null;
  preload: boolean;
  disabled: boolean;
  disabled_reason: string;
  loaded: boolean;
  cached: Array<{ model: string; device: string; compute_type: string }>;
}

export interface WhisperActionResponse {
  ok: true;
  message: string;
}

export async function fetchWhisperStatus(): Promise<WhisperStatusResponse> {
  return apiFetch<WhisperStatusResponse>("/api/system/whisper");
}

export async function postWhisperAction(
  action: "unload",
): Promise<WhisperActionResponse> {
  return apiPost<WhisperActionResponse>("/api/system/whisper", { action });
}

export interface PiperStatusResponse extends ModelFilesStatus {
  configured: boolean;
  model_path: string;
  config_path: string;
  sample_rate: number | null;
  preload: boolean;
  presets: string[];
  disabled: boolean;
  disabled_reason: string;
  loaded: boolean;
  cached: Array<{ model_path: string }>;
  provider_key: string;
}

export interface PiperActionResponse {
  ok: true;
  message: string;
}

export async function fetchPiperStatus(): Promise<PiperStatusResponse> {
  return apiFetch<PiperStatusResponse>("/api/system/piper");
}

export async function postPiperAction(
  action: "unload" | "warm",
): Promise<PiperActionResponse> {
  return apiPost<PiperActionResponse>("/api/system/piper", { action });
}

export interface KokoroStatusResponse extends ModelFilesStatus {
  configured: boolean;
  model_path: string;
  voices_path: string;
  mix: Record<string, number>;
  lang: string;
  device: string;
  sample_rate: number | null;
  preload: boolean;
  presets: string[];
  disabled: boolean;
  disabled_reason: string;
  loaded: boolean;
  cached: Array<{
    model_path: string;
    voices_path: string;
    device: string;
    provider: string;
  }>;
  provider_key: string;
}

export interface KokoroActionResponse {
  ok: true;
  message: string;
}

export async function fetchKokoroStatus(): Promise<KokoroStatusResponse> {
  return apiFetch<KokoroStatusResponse>("/api/system/kokoro");
}

export async function postKokoroAction(
  action: "unload" | "warm",
): Promise<KokoroActionResponse> {
  return apiPost<KokoroActionResponse>("/api/system/kokoro", { action });
}

// ── Sessions (Phase 15X — preview/rename/duplicate) ────

export interface SessionPreview {
  session_id: string;
  started_at: string;
  ended_at: string | null;
  turn_count: number;
  model: string;
  turns: Array<{ role: "user" | "assistant"; text: string }>;
}

export async function fetchSessionPreview(id: string): Promise<SessionPreview> {
  return apiFetch<SessionPreview>(
    `/api/sessions/${encodeURIComponent(id)}/preview`,
  );
}

export async function postSessionRename(
  id: string,
  newName: string,
): Promise<{ ok: true; session_id: string }> {
  return apiPost(`/api/sessions/${encodeURIComponent(id)}/rename`, {
    new_name: newName,
  });
}

export async function postSessionDuplicate(
  id: string,
  destName: string,
): Promise<{ ok: true; session_id: string }> {
  return apiPost(`/api/sessions/${encodeURIComponent(id)}/duplicate`, {
    dest_name: destName,
  });
}

// ── Schedule (Phase 18 Task B — agent-authored jobs) ────

export interface ScheduleHandlerEntry {
  dotpath: string;
  label: string;
}

export interface ScheduleHandlersResponse {
  handlers: ScheduleHandlerEntry[];
}

export interface ScheduleCreateInput {
  name: string;
  cadence: string;
  handler: string;
  enabled?: boolean;
  on_failure?: "log" | "alert" | "disable";
  max_retries?: number;
  backoff_seconds?: number;
  config?: Record<string, unknown>;
}

export interface ScheduleCreateResponse {
  name: string;
  cadence: string;
  handler: string;
  enabled: boolean;
  on_failure: string;
}

export async function fetchScheduleHandlers(): Promise<ScheduleHandlersResponse> {
  return apiFetch<ScheduleHandlersResponse>("/api/schedule/handlers");
}

export async function fetchScheduleRoles(): Promise<ScheduleRolesResponse> {
  return apiFetch<ScheduleRolesResponse>("/api/schedule/roles");
}

export async function postScheduleCreate(
  input: ScheduleCreateInput,
): Promise<ScheduleCreateResponse> {
  return apiPost<ScheduleCreateResponse>("/api/schedule/create", input);
}

export async function deleteScheduleJob(
  name: string,
): Promise<{ removed: string }> {
  const res = await fetch(`${BASE}/api/schedule/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
  if (!res.ok) {
    let msg = `HTTP ${res.status}: ${res.statusText}`;
    try {
      const err = await res.json();
      if (err?.error) msg = String(err.error);
    } catch {
      /* keep default */
    }
    throw new ApiError(res.status, msg);
  }
  return res.json();
}

// ── Alarms panel ──────────────────────────────────────

export async function fetchAlarms(): Promise<AlarmsResponse> {
  return apiFetch<AlarmsResponse>("/api/alarms");
}

export async function cancelAlarm(
  handle: string,
): Promise<{ cancelled: Alarm }> {
  const res = await fetch(`${BASE}/api/alarms/${encodeURIComponent(handle)}`, {
    method: "DELETE",
  });
  if (!res.ok) {
    let msg = `HTTP ${res.status}: ${res.statusText}`;
    try {
      const err = await res.json();
      if (err?.error) msg = String(err.error);
    } catch {
      /* keep default */
    }
    throw new ApiError(res.status, msg);
  }
  return res.json();
}

export async function snoozeAlarm(
  handle: string,
  duration: string,
): Promise<{ snoozed: Alarm }> {
  return apiPost<{ snoozed: Alarm }>(
    `/api/alarms/${encodeURIComponent(handle)}/snooze`,
    { duration },
  );
}

export async function createAlarm(payload: {
  label: string;
  when: string;
  message?: string;
}): Promise<{ alarm: Alarm }> {
  return apiPost<{ alarm: Alarm }>("/api/alarms", payload);
}

// ── Settings v2 (Phase 18 Task C) ──────────────────────

export interface VoiceStylePreset {
  surface: string; // "intent" | "answer"
  ref: string; // catalog ref the preset belongs to
  // Whatever knobs that provider exposes — no fixed shape, so a new
  // provider's presets render without a frontend change.
  settings: Record<string, string | number | boolean>;
}

export interface VoiceSettingsResponse {
  // Per-surface synthesis presets, read-only. They live on the catalog
  // entry in providers.yaml (a voice's character travels with the
  // voice); a roles.yaml per-ref block may override a surface. Edit
  // either directly — agent-side mutation is locked off.
  style_presets: VoiceStylePreset[];
  // Wake-word gate. Only `enabled` is writable here; the phrase is
  // `<prefix> <entity_name>` and the threshold is a config edit.
  wake_word_enabled: boolean;
  wake_word_prefix: string;
  wake_word_threshold: number | null;
  entity_name: string;
}

export interface VoiceSettingsUpdate {
  wake_word_enabled: boolean;
}

export async function fetchVoiceSettings(): Promise<VoiceSettingsResponse> {
  return apiFetch<VoiceSettingsResponse>("/api/settings/voice");
}

export async function postVoiceSettings(
  update: VoiceSettingsUpdate,
): Promise<VoiceSettingsUpdate> {
  return apiPost<VoiceSettingsUpdate>("/api/settings/voice", update, {
    retryable: true,
  });
}

// ── Voice catalog + selection (AS-5) ────────────────────
// Every `kind: tts` entry the catalog holds. The picker renders whatever
// this returns — a voice added to providers.yaml appears with no code
// change, and nothing here names a provider.

export interface CatalogVoice {
  ref: string;
  tier: string;
  provider: string;
  model_id: string;
  adapter: string;
  // Optional catalog fields — a provider that sets neither renders by
  // ref rather than having the picker invent a display name for it.
  label: string;
  gender: string;
  enabled: boolean;
}

export interface VoiceCatalogResponse {
  voices: CatalogVoice[];
  primary: string;
  fallbacks: string[];
  sample_text: string;
}

export interface VoicePrimaryResponse {
  primary: string;
  fallbacks: string[];
  applied: boolean;
  live_update_failed: boolean;
  live_update_error: string | null;
}

export interface VoiceTestResponse {
  provider: string;
  byte_count: number;
  audio_b64: string;
  char_count: number;
}

export async function fetchVoiceCatalog(): Promise<VoiceCatalogResponse> {
  return apiFetch<VoiceCatalogResponse>("/api/voice/catalog");
}

export async function postVoicePrimary(
  ref: string,
): Promise<VoicePrimaryResponse> {
  return apiPost<VoicePrimaryResponse>(
    "/api/voice/primary",
    { ref },
    { retryable: true },
  );
}

// Not retryable: synthesis is the slow part and a retry would queue a
// second audition behind the first rather than recover anything.
export async function postVoiceTest(text?: string): Promise<VoiceTestResponse> {
  return apiPost<VoiceTestResponse>("/api/voice/test", text ? { text } : {});
}

// ── Workspace documents (AS-5) ──────────────────────────
// The operator writing the documents the assistant otherwise proposes
// against. `hash` is the concurrency token: read it, echo it back on
// save, and a 409 means the file moved underneath the editor.

export interface WorkspaceDocRow {
  path: string;
  label: string;
  exists: boolean;
  bytes: number;
  lines: number;
  hash: string;
  modified_at: number | null;
}

export interface WorkspaceDoc {
  path: string;
  label: string;
  content: string;
  hash: string;
}

export interface WorkspaceDocSaved {
  path: string;
  label: string;
  hash: string;
  bytes: number;
  no_op_reason: "duplicate" | "unchanged" | null;
}

export async function fetchWorkspaceDocs(): Promise<{
  docs: WorkspaceDocRow[];
  count: number;
}> {
  return apiFetch<{ docs: WorkspaceDocRow[]; count: number }>(
    "/api/workspace/docs",
  );
}

export async function fetchWorkspaceDoc(path: string): Promise<WorkspaceDoc> {
  return apiFetch<WorkspaceDoc>(
    `/api/workspace/doc?path=${encodeURIComponent(path)}`,
  );
}

// Not retryable: a repeat POST after an ambiguous failure would carry a
// hash the first attempt already consumed, turning a maybe-saved edit
// into a spurious conflict.
export async function saveWorkspaceDoc(input: {
  path: string;
  content: string;
  expected_hash: string;
}): Promise<WorkspaceDocSaved> {
  return apiPost<WorkspaceDocSaved>("/api/workspace/doc", input);
}

export interface CapabilitySnapshot {
  python_version: string;
  node_version: string | null;
  pnpm_version: string | null;
  gpu: {
    vendor: string;
    name: string | null;
    memory_mb: number | null;
    cuda: boolean;
  };
  ram_total_gb: number | null;
  disk_free_gb: number | null;
  mic_devices: number | null;
  platform: { system: string; release: string; machine: string };
}

export async function fetchSystem(
  refresh = false,
): Promise<CapabilitySnapshot> {
  const path = refresh
    ? "/api/settings/system?refresh=1"
    : "/api/settings/system";
  return apiFetch<CapabilitySnapshot>(path);
}

export type SessionResumePolicy =
  | "today_only"
  | "today_plus_yesterday"
  | "n_days"
  | "always";

export interface SessionPolicyResponse {
  policy: SessionResumePolicy;
  days: number;
  show_config_reload_toasts: boolean;
}

export interface SessionPolicyUpdate {
  policy?: SessionResumePolicy;
  days?: number;
  show_config_reload_toasts?: boolean;
}

export async function fetchSessionPolicy(): Promise<SessionPolicyResponse> {
  return apiFetch<SessionPolicyResponse>("/api/settings/session-policy");
}

export async function postSessionPolicy(
  update: SessionPolicyUpdate,
): Promise<SessionPolicyUpdate> {
  return apiPost<SessionPolicyUpdate>("/api/settings/session-policy", update, {
    retryable: true,
  });
}

export interface SessionCapsResponse {
  tool_iteration_cap: number;
  consecutive_error_cap: number;
  deny_rules_locked: boolean;
}

export interface SessionCapsUpdate {
  tool_iteration_cap?: number;
  consecutive_error_cap?: number;
}

export async function fetchSessionCaps(): Promise<SessionCapsResponse> {
  return apiFetch<SessionCapsResponse>("/api/settings/session-caps");
}

export async function postSessionCaps(
  update: SessionCapsUpdate,
): Promise<SessionCapsResponse> {
  return apiPost<SessionCapsResponse>("/api/settings/session-caps", update, {
    retryable: true,
  });
}

// ── Agents (MO-3, read-only) ────────────────────────────

export async function fetchAgents(): Promise<AgentsListResponse> {
  return apiFetch<AgentsListResponse>("/api/agents");
}

export async function fetchPendingAgents(): Promise<AgentsListResponse> {
  return apiFetch<AgentsListResponse>("/api/agents/pending");
}

export async function fetchAgent(name: string): Promise<AgentDetail> {
  return apiFetch<AgentDetail>(`/api/agents/${encodeURIComponent(name)}`);
}

export interface AgentSourceResponse {
  name: string;
  path: string;
  source: string;
}

export async function fetchAgentSource(
  name: string,
): Promise<AgentSourceResponse> {
  return apiFetch<AgentSourceResponse>(
    `/api/agents/${encodeURIComponent(name)}/source`,
  );
}

export async function saveAgentSource(
  name: string,
  source: string,
): Promise<{ name: string; path: string; saved: boolean }> {
  return apiPost(`/api/agents/${encodeURIComponent(name)}/source`, { source });
}

export async function toggleAgentDisabled(
  name: string,
  disabled: boolean,
): Promise<Agent> {
  return apiPost<Agent>(`/api/agents/${encodeURIComponent(name)}/toggle`, {
    disabled,
  });
}

// ── Workspace operator-post (Workstream D) ─────────────

export interface OperatorPostInput {
  title: string;
  body: string;
  source: "button" | "scratchpad" | "voice" | "hotkey";
}

export async function postOperatorPost(
  input: OperatorPostInput,
  awaitReply = true,
): Promise<{ event_id: string }> {
  const qs = awaitReply ? "" : "?await_reply=false";
  return apiPost<{ event_id: string }>(
    `/api/workspace/operator-post${qs}`,
    input,
  );
}

export type { Agent, AgentDetail };

// ── Controller sessions (B3/B6) ─────────────────────────

export interface ControllerSession {
  session_id: string;
  origin: string;
  status: string;
  title: string;
  last_active_at: string;
  operator_facing: boolean;
}

export interface ControllerSessionsResponse {
  sessions: ControllerSession[];
}

export async function fetchControllerSessions(): Promise<ControllerSessionsResponse> {
  return apiFetch<ControllerSessionsResponse>("/api/controller/sessions");
}

// ── Autonomy Dashboard (AU-7 S1) ────────────────────────

export interface AgendaListResponse {
  items: AgendaItem[];
}

export interface AgendaItem {
  id: string;
  created_at: string;
  updated_at: string;
  source: string;
  goal: string;
  rationale: string;
  risk_class: "autonomous" | "propose" | "operator_gate" | "absolute_deny";
  approvals_required: ApprovalGate[];
  priority_score: number;
  score_components: Record<string, number>;
  budget_tokens_cap: number;
  budget_seconds_cap: number;
  budget_tokens_spent: number;
  budget_seconds_spent: number;
  status:
    | "unvetted"
    | "proposed"
    | "selected"
    | "running"
    | "awaiting_operator"
    | "resume_queued"
    | "blocked"
    | "done"
    | "cancelled"
    | "abandoned"
    | "superseded";
  status_history: AgendaTransition[];
  blocked_reason: string | null;
  last_decision: string | null;
  linked_missions: string[];
  linked_workers: string[];
  operator_priority: number;
}

export interface ApprovalGate {
  kind: string;
  target: string;
  fulfilled: boolean;
  fulfilled_at: string | null;
  fulfilled_by: string | null;
}

export interface AgendaTransition {
  from_status: string | null;
  to_status: string;
  at: string;
  reason: string;
  by: string;
}

export interface ActiveWorkersResponse {
  workers: ActiveWorker[];
}

export interface ActiveWorker {
  id: string;
  kind: string;
  created_at: string;
  updated_at: string;
  agenda_item_id: string;
  mission_id: string | null;
  risk_class: string;
  role: string;
  status: string;
  last_heartbeat: string | null;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
  billing: "subscription" | "api" | "unknown";
  duration_seconds: number;
  retry_count: number;
  summary: string;
  last_transition: {
    at: string;
    from_status: string;
    to_status: string;
    reason: string;
  } | null;
}

export interface GovernorPause {
  source: string;
  paused_at: string;
  detector: string;
  reason: string;
  evidence: Record<string, unknown>;
}

export interface GovernorTickPayload {
  at: string;
  pauses_added: string[];
  workers_cancelled: string[];
  items_blocked: string[];
}

export interface GovernorStateResponse {
  running: boolean;
  config: {
    cadence_seconds: number;
    loop_n: number;
    loop_window_hours: number;
    cost_threshold_multiplier: number;
    trust_consecutive_rejections: number;
  };
  last_tick: GovernorTickPayload | null;
  pauses: GovernorPause[];
  timestamp: string;
}

export interface LatestRecoveryResponse {
  recovery: {
    boot_id: string | null;
    downtime_seconds: number;
    scans: Record<string, Record<string, number>>;
    operator_attention: { kind: string; id: string; reason: string }[];
    started_at: string | null;
  } | null;
  state: "recovering" | "ready";
  timestamp: string;
}

export async function fetchAgenda(): Promise<AgendaListResponse> {
  return apiFetch<AgendaListResponse>("/api/agenda");
}

export interface WorkerStatusTransition {
  at: string;
  from_status: string;
  to_status: string;
  reason: string;
}

export interface WorkerArtifact {
  path: string;
  kind: string;
  size_bytes: number | null;
  sha256: string | null;
}

export interface WorkerDetail extends ActiveWorker {
  prompt: string;
  inputs: Record<string, unknown>;
  worktree_path: string | null;
  pid: number | null;
  pane_id: string | null;
  cli_invocation: string[] | null;
  exit_code: number | null;
  error_class: string | null;
  error_message: string | null;
  transcript_path: string | null;
  parent_worker_id: string | null;
  status_history: WorkerStatusTransition[];
  artifacts: WorkerArtifact[];
}

export interface WorkerDetailResponse {
  worker: WorkerDetail;
}

export async function fetchWorkerDetail(
  id: string,
): Promise<WorkerDetailResponse> {
  return apiFetch<WorkerDetailResponse>(
    `/api/workers/${encodeURIComponent(id)}`,
  );
}

export async function fetchActiveWorkers(): Promise<ActiveWorkersResponse> {
  return apiFetch<ActiveWorkersResponse>("/api/workers/active");
}

export async function fetchGovernorState(): Promise<GovernorStateResponse> {
  return apiFetch<GovernorStateResponse>("/api/governor/state");
}

export async function fetchLatestRecovery(): Promise<LatestRecoveryResponse> {
  return apiFetch<LatestRecoveryResponse>("/api/recovery/latest");
}

// TC-1 — operator journal.
export type OperatorJournalEventType =
  | "approval"
  | "dispatch"
  | "outcome"
  | "advice_only"
  | "follow_up_draft";

export interface OperatorJournalRow {
  ts: string;
  event_type: OperatorJournalEventType | string;
  agenda_item_id: string | null;
  worker_id: string | null;
  summary: string | null;
  artifacts: number | null;
  follow_up_draft_id: string | null;
  [extra: string]: unknown;
}

export interface OperatorJournalResponse {
  rows: OperatorJournalRow[];
  limit: number;
  days: number;
}

export async function fetchOperatorJournal(
  limit: number = 50,
): Promise<OperatorJournalResponse> {
  return apiFetch<OperatorJournalResponse>(
    `/api/autonomy/journal?limit=${limit}`,
  );
}

// ── Autonomy Dashboard mutators (AU-7 S2) ────────────────
// Every endpoint is operator-session-gated server-side; session_id comes
// from useWebSocketStore and is resolved by _require_operator_session.

async function apiPatch<T>(path: string, body: unknown): Promise<T> {
  const res = await timedFetch(
    `${BASE}${path}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
    TIMEOUT_MS,
  );
  if (!res.ok) {
    let msg = `HTTP ${res.status}: ${res.statusText}`;
    try {
      const err = await res.json();
      if (err?.error) msg = String(err.error);
    } catch {
      // keep default message
    }
    throw new ApiError(res.status, msg);
  }
  return res.json() as Promise<T>;
}

export interface AgendaMutationResponse {
  item: AgendaItem;
  deduped?: boolean;
  noop?: boolean;
  already_terminal?: boolean;
  fulfilled_count?: number;
  transitioned?: boolean;
}

export async function patchAgendaItem(
  id: string,
  body: {
    session_id: string;
    operator_priority?: number;
    risk_class?: string;
    rationale?: string;
  },
): Promise<AgendaMutationResponse> {
  return apiPatch<AgendaMutationResponse>(
    `/api/agenda/${encodeURIComponent(id)}`,
    body,
  );
}

export async function postCancelAgendaItem(
  id: string,
  body: { session_id: string; reason?: string },
): Promise<AgendaMutationResponse> {
  return apiPost<AgendaMutationResponse>(
    `/api/agenda/${encodeURIComponent(id)}/cancel`,
    body,
  );
}

export interface AgendaComment {
  id: string;
  at: string;
  role: "operator" | "agent";
  by: string;
  body: string;
}

export interface AgendaCommentsResponse {
  item_id: string;
  comments: AgendaComment[];
}

export interface AgendaCommentAddedResponse {
  item_id: string;
  comment: AgendaComment;
}

export async function fetchAgendaComments(
  id: string,
): Promise<AgendaCommentsResponse> {
  return apiFetch<AgendaCommentsResponse>(
    `/api/agenda/${encodeURIComponent(id)}/comments`,
  );
}

export async function postAgendaComment(
  id: string,
  body: { session_id: string; body: string },
): Promise<AgendaCommentAddedResponse> {
  return apiPost<AgendaCommentAddedResponse>(
    `/api/agenda/${encodeURIComponent(id)}/comments`,
    body,
  );
}

export async function postApproveAgendaItem(
  id: string,
  body: {
    session_id: string;
    gate_kinds?: string[];
  },
): Promise<AgendaMutationResponse> {
  return apiPost<AgendaMutationResponse>(
    `/api/agenda/${encodeURIComponent(id)}/approve`,
    body,
  );
}

export async function postResumeAgendaItem(
  id: string,
  body: { session_id: string },
): Promise<AgendaMutationResponse> {
  return apiPost<AgendaMutationResponse>(
    `/api/agenda/${encodeURIComponent(id)}/resume`,
    body,
  );
}

export interface UnpauseSourceResponse {
  source: string;
  was_paused: boolean;
  pause?: GovernorPause;
}

export async function postUnpauseSource(
  source: string,
  body: { session_id: string; reason?: string },
): Promise<UnpauseSourceResponse> {
  return apiPost<UnpauseSourceResponse>(
    `/api/agenda/sources/${encodeURIComponent(source)}/unpause`,
    body,
  );
}

// ── Pruned ledger (AU-7 Phase 3) — admission-gate discards ─────

export interface PruneRecord {
  item_id: string | null;
  source: string;
  goal: string;
  stage: string;
  reason: string;
  ts: string;
}

export interface PrunedResponse {
  records: PruneRecord[];
  counts: Record<string, Record<string, number>>;
}

export async function fetchPruned(
  windowHours: number = 168,
): Promise<PrunedResponse> {
  return apiFetch<PrunedResponse>(
    `/api/autonomy/pruned?window_hours=${windowHours}`,
  );
}

export interface MuteSourceResponse {
  source: string;
  muted: boolean;
}

export async function postMuteSource(
  source: string,
  body: { session_id: string },
): Promise<MuteSourceResponse> {
  return apiPost<MuteSourceResponse>(
    `/api/autonomy/source/${encodeURIComponent(source)}/mute`,
    body,
  );
}

export async function postUnmuteSource(
  source: string,
  body: { session_id: string },
): Promise<MuteSourceResponse> {
  return apiPost<MuteSourceResponse>(
    `/api/autonomy/source/${encodeURIComponent(source)}/unmute`,
    body,
  );
}

export interface RuntimeShutdownResponse {
  status: string;
  intent: string;
  source: string;
  reason: string;
}

export async function postRuntimeShutdown(body: {
  session_id: string;
  reason?: string;
}): Promise<RuntimeShutdownResponse> {
  return apiPost<RuntimeShutdownResponse>("/api/runtime/shutdown", body);
}

export interface CloseAllActivitiesResponse {
  closed: string[];
  errored: string[];
  skipped: string[];
  counts: { closed: number; errored: number; skipped: number };
}

// Cancel every cancellable running unit (lanes/mcp_sessions/delegates) at once.
// controller_session/routine/autonomy are skipped server-side.
export async function closeAllActivities(): Promise<CloseAllActivitiesResponse> {
  return apiPost<CloseAllActivitiesResponse>("/api/activity/close-all", {});
}

export interface CloseActivityResponse {
  closed: boolean;
}

// Cancel a single cancellable running unit (lane/mcp_session/delegate).
export async function closeActivity(
  activityId: string,
): Promise<CloseActivityResponse> {
  return apiPost<CloseActivityResponse>(
    `/api/activity/${encodeURIComponent(activityId)}/close`,
    {},
  );
}

// ── ASK-over-MCP operator approvals ──────────────────────────────────────
export interface McpApproval {
  approval_id: string;
  verb: string;
  client: string;
}

// Pending ASK-over-MCP approvals (an external CLI called a write verb and is
// held awaiting the operator). Returns [] if the registry isn't ready.
// Operator-session-gated server-side: the client being asked about runs on this
// machine too, so the live chat session is the credential, not localhost.
export async function getMcpApprovals(sessionId: string): Promise<McpApproval[]> {
  if (!sessionId) return [];
  try {
    const res = await timedFetch(
      `${BASE}/api/mcp/approvals?session_id=${encodeURIComponent(sessionId)}`,
      {},
      TIMEOUT_MS,
    );
    if (!res.ok) return [];
    const body = (await res.json()) as { items?: McpApproval[] };
    return body.items ?? [];
  } catch {
    return [];
  }
}

// Approve or reject a held MCP verb. Not retryable — a flake-retry could settle
// a re-issued approval twice.
export async function decideMcpApproval(
  approvalId: string,
  approved: boolean,
  sessionId: string,
): Promise<void> {
  await apiPost(
    `/api/mcp/approvals/${encodeURIComponent(approvalId)}/decision`,
    { approved, session_id: sessionId },
  );
}

// ── Parked background-spawn asks (trio W4 — ask-instead-of-die) ───────────
// A background spawn's ASK that outlived its live 30s card parks here instead
// of denying; the operator settles it from the approvals pane (M3).
export interface ParkedAsk {
  approval_id: string;
  call_id: string;
  session_id: string;
  tool_name: string;
  input_summary: string;
  spawn_handle_id: string | null;
  parked_at: string;
  // Controller-daemon parking (Option B, 2026-07-13): "controller" for an
  // ask relayed from the agent_controller daemon, "chat" (default) for a
  // Mirror-process background-spawn ask. Absent on older cached responses.
  origin?: "chat" | "controller";
}

// Parked asks awaiting the operator, across sessions. Returns [] on error.
export async function getParkedAsks(): Promise<ParkedAsk[]> {
  try {
    const res = await timedFetch(`${BASE}/api/asks/parked`, {}, TIMEOUT_MS);
    if (!res.ok) return [];
    const body = (await res.json()) as { items?: ParkedAsk[] };
    return body.items ?? [];
  } catch {
    return [];
  }
}

// Approve or deny a parked ask by its server-minted approval_id (M13 — the
// collision-safe key). Not retryable.
export async function decideParkedAsk(
  approvalId: string,
  approved: boolean,
): Promise<void> {
  await apiPost(`/api/asks/${encodeURIComponent(approvalId)}/decision`, {
    approved,
  });
}

// -- AU-10 outbound notifications ----------------------------------------

export interface NotificationCategoryRow {
  category: string;
  exempt: boolean;
}

export interface NotificationChannelRow {
  name: string;
  enabled: boolean;
  muted_yaml: string[];
  muted_runtime: string[];
  muted_effective: string[];
}

export interface NotificationsConfig {
  categories: NotificationCategoryRow[];
  channels: NotificationChannelRow[];
}

export async function getNotificationsConfig(): Promise<NotificationsConfig> {
  return apiFetch<NotificationsConfig>("/api/notifications/config");
}

export interface NotificationsMuteResponse {
  channel: string;
  category: string;
  muted: boolean;
  muted_runtime: string[];
}

export async function postNotificationMute(body: {
  session_id: string;
  channel: string;
  category: string;
  muted: boolean;
}): Promise<NotificationsMuteResponse> {
  return apiPost<NotificationsMuteResponse>("/api/notifications/mute", body);
}

export interface NotificationsRatesRow {
  channel: string;
  category: string;
  cap_per_hour: number;
  used_last_hour: number;
  exempt: boolean;
}

export interface NotificationsRatesResponse {
  rows: NotificationsRatesRow[];
}

export async function getNotificationsRates(): Promise<NotificationsRatesResponse> {
  return apiFetch<NotificationsRatesResponse>("/api/notifications/rates");
}

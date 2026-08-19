export type EnvelopeCategory =
  | "loop"
  | "session"
  | "planning"
  | "routing"
  | "execution"
  | "offlocal"
  | "cli"
  | "terminal"
  | "sandbox"
  | "error"
  | "background"
  | "entity"
  | "command_result"
  | "command"
  | "workspace"
  | "agenda"
  | "workers"
  | "governor"
  | "schedule"
  | "cost"
  | "voice"
  | "canvas"
  | "activity"
  | "chat"
  | "other";

export interface Envelope<T = Record<string, unknown>> {
  type: string;
  category: EnvelopeCategory;
  session_id: string;
  timestamp: string;
  data: T;
  // WP-2: per-turn discriminator. `syn:<event_id>:<short>` prefix means
  // the envelope belongs to a synthetic workspace turn — dispatch.ts uses
  // this to route stream_text + tool envelopes away from the chat
  // conversation store. Null/absent for chat turns and out-of-turn
  // broadcasts (catchup, schedule events, etc.).
  turn_id?: string;
  // mirror-multi-chat inc.A — the chat a turn-scoped envelope belongs to
  // (32-hex chat_id). Stamped by `make_envelope` for the 19 turn-scoped
  // types; dispatch routes each event to the matching conversation slice.
  // Null/absent for voice/session-scoped and out-of-turn broadcasts → the
  // store falls back to the active chat.
  chat_id?: string;
}

/** True if the envelope was emitted inside a synthetic workspace turn. */
export function isSyntheticTurn(env: Envelope<unknown>): boolean {
  return typeof env.turn_id === "string" && env.turn_id.startsWith("syn:");
}

export type PulseTag =
  | "triage"
  | "tool"
  | "memory"
  | "agent"
  | "model"
  | "system"
  | "chat"
  | "perm"
  | "route"
  | "loop"
  | "bg"
  | "other";

export type EntityState =
  | "idle"
  | "thinking"
  | "speaking"
  | "spawning"
  | "council"
  | "listening"
  | "error"
  | "happy"
  | "deep_focus"
  | "dreaming";

export interface ToolCall {
  call_id: string;
  name: string;
  input: Record<string, unknown>;
}

export interface ToolResult {
  call_id: string;
  output: string;
  is_error: boolean;
  metadata?: Record<string, unknown>;
}

// `tool_call` segments interleave a tool-pill placeholder into the
// chronological text timeline. They carry the `call_id` and `name`;
// the live result is looked up from `currentToolResults` (streaming) or
// `toolResults` (frozen) at render time so we don't duplicate state.
// `text` stays optional/empty for the tool_call kind.
// `spoken` segments are the abbreviated spoken form of a long reply. They
// ride the timeline so the captions surface can prefer them, but they are
// NOT rendered in the transcript — the `answer` right after says the same
// thing at length, and printing both reads as a stutter.
export interface AssistantStreamSegment {
  kind: "intent" | "spoken" | "answer" | "tool_call" | "system_note";
  text: string;
  call_id?: string;
  name?: string;
}

export interface ChatAttachment {
  id: string;
  session_id: string;
  filename: string;
  mime_type: string;
  size: number;
  kind: "image" | "pdf" | "file";
  url: string;
  created_at: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "entity" | "error";
  content: string;
  attachments?: ChatAttachment[];
  statusText?: string;
  segments?: AssistantStreamSegment[];
  timestamp: number;
  toolCalls?: ToolCall[];
  toolResults?: ToolResult[];
  status?: "complete" | "interrupted" | "queued";
  // FIFO queue slot (1-based) assigned client-side at send time while a
  // turn is streaming (Q2 frontend). Only meaningful while status is
  // `queued` — cleared when the bubble flips to `complete`/`interrupted`.
  queuePosition?: number;
  // Q3 frontend — set client-side at send time by `sendSteer`: this bubble
  // redirected a running turn (WS `steer`) rather than queuing behind it.
  // Distinct from `status: 'queued'` so the "redirected" pill and the
  // "queued" pill never collide on the same bubble.
  steered?: boolean;
}

export interface ApprovalRequest {
  call_id: string;
  name: string;
  input: Record<string, unknown>;
  reason: string;
  received_at: number;
  resolved: boolean;
}

// ── Envelope payloads (server → client) ──────────────────

export interface SessionCreatedData {
  session_id: string;
  started_at: string;
  // mirror-multi-chat inc.B — backend's active chat_id at connect. The
  // conversation store seeds its initial slice under this key so turn-scoped
  // envelopes (which carry the same id) route by exact match.
  active_chat_id: string;
  // mirror-multi-chat P3 — the open-chat list (newest-first, with titles) so
  // the tab strip rehydrates on (re)connect and survives a page reload.
  chats?: { chat_id: string; title: string }[];
}

export interface SessionMeta {
  // A conversation's id, stamped once when it was created. It was a filename
  // minted from the clock until 2026-08-18, which is why nothing may derive a
  // date or a name from it.
  chat_id: string;
  title: string;
  created_at: string;
  started_at: string;
  ended_at: string | null;
  turn_count: number;
  model: string;
  archived?: boolean;
}

export interface SessionListData {
  sessions: SessionMeta[];
}

export interface RawHistoryEntry {
  role: "user" | "assistant" | "tool" | "system";
  content?: string | Array<Record<string, unknown>> | null;
  timestamp?: string;
  tool_calls?: Array<{
    id: string;
    function: { name: string; arguments: string };
  }>;
  tool_call_id?: string;
  // `_meta` is written by `ChatSession._append_assistant_message` and read
  // by `rawHistoryToMessages` so resumed bubbles show the model badge and
  // token-cache pill they had live.
  _meta?: {
    model?: { role: string; provider: string; model: string; tier: string };
    usage?: {
      input_tokens: number;
      output_tokens: number;
      cached_tokens: number;
    };
  };
}

export interface SessionSavedData {
  session_id: string;
  chat_id: string;
  title: string;
  path: string;
}

export type SessionResetData = Record<string, never>;

export interface SessionDeletedData {
  chat_id: string;
  title: string;
}

export interface SessionCompactFileData {
  chat_id: string;
  title: string;
  tokens_before: number;
  tokens_after: number;
}

export interface SessionCompactData {
  tokens_before: number;
  tokens_after: number;
  trigger: "manual" | "auto";
}

export interface CostBudgetStateData {
  spent_usd: number;
  warning_usd: number;
  cap_usd: number;
  role_spent_usd: number;
  role_cap_usd: number | null;
  warning: boolean;
  blocked: boolean;
}

// `kind` separates voice spend from chat/observer rows so the HUD's voice
// cost chip can sum `voice_tts` + `voice_stt` totals without parsing the
// `role` string. Backend emits this on every `cost_delta` envelope —
// `make_cost_delta` in `mirror/server/envelope.py`.
export type CostDeltaKind = "chat" | "voice_tts" | "voice_stt";

export interface CostDeltaData {
  kind: CostDeltaKind;
  role: string;
  model: string;
  cost_usd: number;
  daily_total_usd: number;
  role_total_usd: number;
  state: CostBudgetStateData;
}

// Catch-up snapshot. Sent once per WS connect (`cost_state` envelope) and
// returned by `GET /api/cost/state`. Primes the HUD chips with today's
// spend without waiting for the next billed turn (which may not happen
// for minutes after a reload or right after midnight rollover).
export interface CostStateRoleEntry {
  role_total_usd: number;
  role_cap_usd: number | null;
  last_model: string;
}

export interface CostStateVoiceProvider {
  spent_usd: number;
  cap_usd: number;
  rate: number;
}

export interface CostStateData {
  global: {
    spent_usd: number;
    warning_usd: number;
    cap_usd: number;
    warning: boolean;
    blocked: boolean;
  };
  roles: Record<string, CostStateRoleEntry>;
  voice_providers: {
    tts: Record<string, CostStateVoiceProvider>;
    stt: Record<string, CostStateVoiceProvider>;
  };
  local_date: string;
  enabled: boolean;
  // Cost UX overhaul (2026-04-27). `overage_unlocked` = scope_keys the
  // operator approved-to-continue today; HUD chips in those scopes
  // render red when spent>cap (overage display) instead of staying
  // blocked. `warned` suppresses duplicate 75% toasts after a reload.
  overage_unlocked?: string[];
  warned?: string[];
}

/** 75% warning toast envelope. Backend's check_warning is one-shot
 * per scope per day — this fires once when crossed. Frontend pushes a
 * warning toast naming the scope. */
export interface CostWarningData {
  scope_key: string;
  scope_label: string;
  spent_usd: number;
  cap_usd: number;
  pct: number;
}

/** 100% overage confirm-to-continue. Frontend renders a CostOverageCard;
 * operator's Yes/No comes back as `cost_overage_response` carrying
 * `{call_id, approved}`. */
export interface CostOverageAskData {
  call_id: string;
  scope_key: string;
  scope_label: string;
  spent_usd: number;
  cap_usd: number;
}

export interface SessionStatsData {
  tokens: number;
  system_tokens: number;
  turns: number;
  compact_threshold_tokens: number;
  compact_threshold_ratio: number;
}

export interface LoopStartData {
  turn: number;
  workspace_origin?: { event_id: string; comment_id: string };
}

export interface LoopEndData {
  turn: number;
  tokens_used: number;
  workspace_origin?: { event_id: string; comment_id: string };
}

export interface StreamTextData {
  delta: string;
  kind?: "intent" | "status" | "spoken" | "answer" | "thinking";
}

export interface StreamToolCallStartData {
  call_id: string;
  name: string;
}

export interface StreamToolCallDeltaData {
  call_id: string;
  delta: string;
}

export interface StreamToolCallEndData {
  call_id: string;
  name: string;
  input: Record<string, unknown>;
}

export interface StreamToolResultData {
  call_id: string;
  output: string;
  is_error: boolean;
  metadata?: Record<string, unknown>;
}

export interface StreamStopData {
  stop_reason: string;
  input_tokens?: number;
  output_tokens?: number;
  cached_tokens?: number;
}

export interface StreamErrorData {
  message: string;
  reason?: string;
  // Absent or 'error' → system-class failure (model/api/cli/server). Frontend
  // fires the red error orb + adds a chat error bubble. 'warning' →
  // operator-input miss (typo, missing session, etc.); toast only, orb stays
  // calm. 'soft' → post-commit provider hiccup (FallbackAdapter recovers via
  // the next iteration); rendered as an inline `system_note` segment inside
  // the active assistant bubble — no orb flip, no toast, no red card.
  severity?: "warning" | "error" | "soft";
  // Present on `severity: 'soft'` envelopes (and only useful there). Tagged
  // by `adapter_chain.py`'s post-commit branches for incident correlation.
  kind?: "post_commit_partial" | "post_commit_exception";
  model?: string;
  chain_index?: number;
  provider_error?: string;
  request_id?: string;
  // Present on `severity: 'soft'` envelopes from chat.py when the tool-
  // iteration cap is hit and the loop soft-resets. `resets` is the
  // monotonically increasing per-turn reset counter.
  resets?: number;
}

// category: 'command_result' — slash-command outcome (only emitted on
// failure). `severity=warning` → toast only, orb stays normal (e.g. delete
// not_found, an operator typo). `severity=error` → toast + orb red (e.g.
// io_error). See `_cmd_delete` in tesseract/mirror/server/ws.py.
export type CommandSeverity = "warning" | "error";

export interface CommandResultData {
  command: string;
  ok: boolean;
  reason: string;
  reason_code?: string;
  severity: CommandSeverity;
}

export interface ToolAskData {
  call_id: string;
  name: string;
  input: Record<string, unknown>;
  reason: string;
}

export interface ToolApprovedData {
  call_id: string;
}

export interface ToolDeniedData {
  call_id: string;
  // `'turn_cancelled'` when the turn task was cancelled mid-ASK (operator
  // hit cancel, WS closed via cleanup_session). Distinct from operator-
  // initiated denial so the chat can render the cause accurately.
  reason?: "turn_cancelled" | string;
}

export interface ToolAutoData {
  call_id: string;
  name: string;
}

export interface ToolDeniedHardData {
  call_id: string;
  name: string;
  reason: string;
}

export interface ModeChangedData {
  from: string;
  to: string;
}

export interface IdentityWakeWord {
  enabled: boolean;
  prefix: string;
}

// Broadcast after `POST /api/identity` lands, and also the route's own
// response body — the values as the live config now holds them, not as
// they were requested.
export interface IdentityChangedData {
  name: string;
  operator_name: string;
  wake_word: IdentityWakeWord;
}

export interface IdentitySavePatch {
  name?: string;
  operator_name?: string;
  wake_word?: Partial<IdentityWakeWord>;
}

export interface SoulUpdatedData {
  content: string;
  source?: string;
  transparency?: boolean;
  last_reflected_at?: string;
  // Set when apply_change short-circuited because the proposed bullet
  // collided with an existing one (`"duplicate"`) or the proposed text
  // was byte-identical to current ("unchanged"). Frontend renders a
  // toast instead of a silent success.
  no_op_reason?: "duplicate" | "unchanged" | null;
}

// category: 'entity' — body-of-the assistant signals. Mirrors EntitySignalsPayload in
// IntensitySignals.ts. Fields go stale at 3000ms; backend pumps every 2000ms
// + on loop_start / loop_end / set_mood result.
export interface EntitySignalsData {
  mood_intensity: number;
  mood_valence: number;
  agents_active: number;
  effort_level: number;
  tokens_per_sec: number;
  consolidation_depth: number;
  dreaming_cycle: number | null;
}

export type ToolCallStatus =
  | "auto"
  | "pending"
  | "parked"
  | "approved"
  | "denied"
  | "hard_denied";

export interface ToolStatusEntry {
  status: ToolCallStatus;
  reason?: string;
}

export interface CliStartData {
  call_id: string;
  tool: string;
}

export interface CliOutputData {
  call_id: string;
  delta: string;
}

export interface CliEndData {
  call_id: string;
  exit_code: number;
}

export interface CliStreamState {
  tool: string;
  lines: string[];
  exit_code?: number;
  // Phase 4 follow-up — set on cli_start so consumers (DelegateCard,
  // ToolCallPill) can show elapsed time without a separate timestamp store.
  started_at?: number;
}

export interface ModelSelectedData {
  role: string;
  provider: string;
  model: string;
  tier: string;
  reasoning_effort?: string;
  // Set by FallbackAdapter when the primary chain entry fails pre-commit
  // and the actual response comes from a fallback. ModelBadge renders
  // a small `↩ fallback` indicator + tooltip with the primary's name
  // and the failure reason. Pulse store ingests it as a routing event.
  is_fallback?: boolean;
  chain_index?: number;
  fallback_reason?: string;
  primary?: {
    provider: string;
    model: string;
    reasoning_effort?: string;
  };
}

// category: 'voice' — STT + TTS state envelopes (Phase 16 S2 / S3).
// `voice_state` drives mic button + orb + chat-input UX. STT is local-only
// post Phase 16 S1 simplification — no `voice_partial` is emitted; the
// final transcript arrives as `voice_final` and is dispatched into the
// typed-chat path so chat_brain consumes it through ChatSession.send.

export type VoiceState =
  | "idle"
  | "listening"
  | "transcribing"
  | "speaking_back"; // S3 — TTS playback active

export interface VoiceStateData {
  state: VoiceState;
}

// `chat` dispatches a turn; `input` hands the text back to the chat
// input; `terminal` types it into the focused pane. Mirrors
// `mirror/server/voice_modes.py::VOICE_DESTINATIONS`.
export type VoiceDestination = "chat" | "input" | "terminal";

export interface VoiceFinalData {
  text: string;
  // Where the SERVER decided this transcript goes, resolved before it
  // began transcribing. Route on this, never on the store's current mode:
  // STT is not instant, so the operator can cycle the HUD pill
  // mid-utterance, and the live value would send a transcript somewhere
  // the backend never agreed to — including typing a chat-bound utterance
  // into a shell. It is the resolved target rather than the raw mic mode
  // so the mapping lives in one place; both ends deriving it is how a
  // fifth mode ends up routed differently by each half. Required: every
  // exit in `_handle_voice_commit` stamps it.
  destination: VoiceDestination;
}

// `voice_woken` — the wake phrase just landed, MID-utterance.
//
// Carries nothing: the content is that it arrived, and when. It fires at most
// once per utterance, on the edge, the moment the streaming decoder hears the
// phrase — not when the operator stops talking. That timing is the reason it
// exists. The gate used to decide on the committed buffer, so someone could
// speak for a minute and only then learn nothing had been listening, and there
// was no instant at which a truthful "go ahead" could have been shown.
export type VoiceWokenData = Record<string, never>;

// `voice_discarded` — an utterance the wake-word gate refused. Arrives
// INSTEAD of `voice_final`, so nothing becomes a chat bubble, a turn, or
// speech.
//
// There is deliberately NO transcript here. The gate decides from audio and
// runs before transcription, so a refused utterance is never sent to a speech
// engine at all — which is what keeps ambient speech away from the cloud
// STT fallback. What the operator said is still visible to them: the
// browser's own Web Speech preview (`voice.partialTranscript`) is local and
// independent of this path.
//
// And no score. The decoder hears the phrase or it hears nothing, so there is
// no confidence number; the length is carried instead, because it separates a
// gate too tight from a microphone that overheard the room.
export interface VoiceDiscardedData {
  audio_seconds: number;
  reason: "wake_word";
}

// S3 stubs — declared now so dispatch handlers don't need a types churn
// when S3 lands; backend doesn't emit these yet.

export interface TtsChunkData {
  audio_b64: string; // base64 WAV chunk, sample rate per provider
  provider: string; // catalog model id of the lane that synthesized it
  sequence: number; // monotonic per turn
  is_final: boolean; // last chunk for this utterance
  // Whether `provider` is the lane the operator chose or one the chain fell
  // through to — the voice twin of the chat `is_fallback` above. The HUD
  // marks the difference: a voice substituted in silence is the same defect
  // as a model that fails to download in silence.
  is_fallback: boolean;
  // The engine name behind `provider`, resolved server-side. `provider` is
  // the bare catalog model id (`af_heart`) so the cost ledger can debit
  // without a second mapping — nothing on this side can turn that into
  // "kokoro", and guessing by splitting it produced exactly the wrong label.
  engine: string;
}

// `voice_instruction` envelope — why the reply is not being spoken.
// Raised by the WS gate when a lane's budget is exhausted or every TTS
// lane is down. Voice and character are config
// (`roles.yaml::voice.tts` + that entry's `synthesis_presets`), so
// nothing on the wire selects them.
export interface VoiceInstructionData {
  instruction?: string;
}

// `entity_state_set` envelope — agent-authored discrete orb state
// (`set_state` tool). Sticky frontend-side until either the assistant calls
// again or a loop event fires its own setState (loop_start →
// thinking, etc.). Backend allows: happy / deep_focus / dreaming /
// idle. Reactive states are loop-driven and not settable here.
export interface EntityStateSetData {
  state: EntityState;
}

// `config_reloaded` envelope — Phase 18 auto-config-reflection.
// Fired by the backend ConfigWatcher whenever a file under
// tesseract/config/*.yaml is saved (and the reloader runs). `ok=false`
// means the edit was malformed and the live config was left alone;
// the toast severity flips to `error` so the operator notices.
export interface ConfigReloadedData {
  file: string;
  summary: string;
  detail: Record<string, unknown>;
  ok: boolean;
}

export interface MessageStats {
  input_tokens: number;
  output_tokens: number;
  cached_tokens: number;
}

export type ObserverMode = "meta" | "maintenance";

export interface ObserverResultData {
  mode: ObserverMode;
  observation: string;
}

export interface ObserverUnavailableData {
  mode: ObserverMode;
  reason?: string;
}

export type MemorySuggestionKind = "remember" | "consolidate" | "reread";

export type MemoryTarget =
  | { kind: "memory_path"; path: string }
  | { kind: "topic_slug"; slug: string }
  | { kind: "quote"; turn_index: number; text: string };

export interface MemorySuggestionData {
  kind: MemorySuggestionKind;
  target: MemoryTarget;
  reason: string;
  confidence: number;
  observation_id: string;
}

export interface ObserverStats {
  fires_total: number;
  tokens_used_total: number;
  last_fired_at: string | null;
  circuit_breaker_state: "green" | "yellow" | "red";
  pending_suggestion_count: number;
  transcript_turns: number;
}

// ── Schedule (S5) ────────────────────────────────────────

export interface ScheduleRetryPolicy {
  max_retries: number;
  backoff_seconds: number;
}

export interface ScheduleJobLastResult {
  ok: boolean;
  detail: string;
  duration_ms: number;
  payload?: Record<string, unknown>;
}

export interface ScheduleJobRuntime {
  name: string;
  cadence: string;
  // A row fires on a clock or on an event, never both. `when` names the
  // condition, `when_reason` is that condition's own sentence about the last
  // time it was asked — "3 of 40 new skill uses since the last pass" is this
  // row's answer to the question a cron row answers with a next-fire time.
  when: string;
  when_config: Record<string, unknown>;
  when_reason: string;
  enabled: boolean;
  circuit_broken: boolean;
  consecutive_failures: number;
  last_fired_at: string | null;
  // Who caused the last run: scheduled | catchup | operator | assistant |
  // alarm. `null` means this backend process has not fired the job — a
  // `last_fired_at` seeded from the run log at boot carries no trigger, and
  // the row says "unknown" rather than assuming it was the schedule.
  last_trigger: string | null;
  last_result: ScheduleJobLastResult | null;
  uses_llm?: boolean;
  model_role?: string | null;
  default_model_role?: string | null;
  effective_model_role?: string | null;
}

export interface ScheduleJob {
  name: string;
  cadence: string;
  when?: string;
  // What this row is for, in the operator's words. Shipped rows carry theirs
  // in the run manifest and operator rows carry their own; the backend sends
  // whichever applies, and this tab shows it rather than restating it.
  summary?: string;
  handler: string;
  enabled: boolean;
  on_failure: "log" | "alert" | "disable";
  retry_policy: ScheduleRetryPolicy;
  config: Record<string, unknown>;
  model_role?: string | null;
  runtime: ScheduleJobRuntime | null;
}

export interface ScheduleResponse {
  jobs: ScheduleJob[];
}

export interface ScheduleRolesResponse {
  roles: string[];
  error?: string;
}

export interface AlarmRecurrence {
  kind: "daily" | "weekdays" | "weekly" | "every";
  weekday?: number;
  interval_seconds?: number;
}

export interface Alarm {
  id: string;
  label: string;
  run_at: string;
  message: string;
  recurrence: AlarmRecurrence | null;
  created_at: string;
}

export interface AlarmsResponse {
  alarms: Alarm[];
}

export interface ScheduleStateData {
  action: string;
  job_name?: string;
  enabled?: boolean;
  cadence?: string;
  circuit_broken?: boolean;
  consecutive_failures?: number;
  reason?: string;
  alarm?: Record<string, unknown>;
  model_role?: string | null;
  effective_model_role?: string | null;
  uses_llm?: boolean;
}

export interface ScheduleJobStartedData {
  job_name: string;
  run_id: string;
  fired_at: string;
}

export interface ScheduleJobDoneData {
  job_name: string;
  run_id: string;
  fired_at?: string;
  ok: boolean;
  detail: string;
  payload: Record<string, unknown>;
  duration_ms: number;
  circuit_broken: boolean;
  // Optional so a frontend running ahead of its backend still parses the
  // envelope — the app self-updates, so version skew is a normal state.
  trigger_source?: string;
}

export interface ScheduleJobFailedData {
  job_name: string;
  run_id: string;
  ok: false;
  detail: string;
  consecutive_failures: number;
  circuit_broken: boolean;
}

// ── REST responses ───────────────────────────────────────

export interface SoulResponse {
  content: string;
  last_reflected_at?: string | null;
}

export interface BreakerInfo {
  name: string;
  state: "open" | "closed";
  failure_count: number;
  last_failure: string | null;
  last_reset: string | null;
}

export interface BreakersResponse {
  breakers: BreakerInfo[];
}

// ── Catalog-backed model picker (Settings → Models) ──────────────
//
// Returned by GET /api/settings/catalog. The Models tab renders one
// dropdown per swappable target, filtered by the kind set the backend
// allows for that target. POST /api/settings/model-ref writes the
// picked ref to roles.yaml.

// Roles are discovered server-side from roles.yaml — adding a new role to
// the YAML surfaces a Settings row automatically. The string type stays
// open so the frontend doesn't have to be updated when a new role lands.
export type ModelRefTarget = string;

export type ProviderModelKind =
  | "chat"
  | "embedding"
  | "tts"
  | "stt"
  | "audio_stt"
  | "image_generation";

export interface CatalogEntry {
  ref: string;
  tier: "api" | "cli" | "local";
  provider: string;
  model: string;
  kind: ProviderModelKind;
  context_window: number;
  /** Advisory — what the catalog says this model is for (`brain`, `tools`,
   *  `vision`, …). Shown in the picker; `allowed_kinds` is what constrains. */
  good_for: string[];
}

export interface ChainEntry {
  ref: string;
  resolved: boolean;
  tier?: "api" | "cli" | "local";
  provider?: string;
  model?: string;
  kind?: ProviderModelKind;
  context_window?: number;
  good_for?: string[];
  /** Whether an adapter could be built from this ref — the same gate the
   *  runtime raises from, not a prediction. Absent from a backend older than
   *  the field, which the row reads as "nothing was reported". */
  available?: boolean;
  /** The runtime's own sentence for why not, naming the flag or variable that
   *  is false. Null when `available`. */
  reason?: string | null;
}

export interface Chain {
  name: string;
  /** Null only when every entry is unresolvable. */
  kind: ProviderModelKind | null;
  entries: ChainEntry[];
  /** The roles that follow this chain — editing it moves all of them. */
  used_by: string[];
}

export interface ChainsResponse {
  chains: Chain[];
}

export interface ChainWriteResponse {
  name: string;
  refs?: string[];
  used_by?: string[];
  deleted?: boolean;
  applied?: boolean;
  live_update_failed?: boolean;
  live_update_error?: string | null;
}

export interface RoleChainResponse {
  role: string;
  chain: string;
  refs: string[];
  applied: boolean;
  live_update_failed?: boolean;
  live_update_error?: string | null;
}

export interface CatalogTargetMeta {
  target: ModelRefTarget;
  kind: ProviderModelKind | null;
  allowed_kinds: ProviderModelKind[];
  current_ref: string | null;
  mode: RoleMode;
  allow_toggle: boolean;
  load_bearing: boolean;
}

export interface CatalogResponse {
  entries: CatalogEntry[];
  current: Record<string, string | null>;
  voice_lanes: { stt_primary: string; tts_primary: string };
  targets: CatalogTargetMeta[];
}

export interface ModelRefUpdateInput {
  target: ModelRefTarget;
  ref: string;
}

export interface ModelRefUpdateResponse {
  target: ModelRefTarget;
  ref: string;
  tier: string;
  provider: string;
  model: string;
  kind: ProviderModelKind;
}

export interface IdentityResolutionEntry {
  model: string;
  provider: string;
  context_window: number;
}

export interface IdentityRoleModel {
  name: string;
  provider: string;
  context_window: number;
  /** Full ordered fallback list — index 0 is primary, the rest are
   *  fallbacks. Populated by the identity route so the Settings UI can
   *  render a dropdown of candidate models per role. */
  resolution?: IdentityResolutionEntry[];
}

export interface IdentityRoleStatus {
  mode: string;
}

/** The two values `roles.yaml` accepts. `inactive` is the config's own
 *  spelling — the loader keeps such a role as an unresolved stub, and every
 *  runtime consumer gates on it. Any other string is refused on write. */
export type RoleMode = "active" | "inactive";

export interface RoleModelsUpdateInput {
  role: string;
  mode?: RoleMode;
  primary_model?: string;
}

export interface RoleModelsUpdateResponse {
  role: string;
  mode: string;
  primary: IdentityResolutionEntry;
}

export interface IdentityCompactThreshold {
  ratio: number;
  context_window: number;
  tokens: number;
  keep_recent_turns: number;
}

export interface IdentityVoiceProviderConfig {
  rate: number;
  /** What `rate` is a price of. TTS lanes use either: a conventional lane
   * bills per character of input, a generative one per second of produced
   * speech. The two differ by five orders of magnitude, so the label beside
   * the number has to follow the lane rather than the side of the subsystem.
   * Optional for back-compat with a backend that predates the field. */
  rate_unit?: "chars" | "audio_hour";
  cap_usd: number;
}

export interface IdentityCostTracking {
  enabled: boolean;
  /** Single percentage applied uniformly to global + every per_role cap +
   * every per-voice-provider cap. Stored as a 0–1 fraction in models.yaml
   * (`warning_at_pct: 0.75`); the UI presents it as a percentage. */
  warning_at_pct: number;
  /** Derived: sum of all `per_role` caps plus every voice provider's
   * `daily_budget_usd`. Read-only on the wire; editing a sub-cap re-totals
   * this on the next response. */
  daily_budget_usd: number;
  per_role: Record<string, number>;
  /** Voice block is optional for back-compat with backends that haven't
   * been restarted after the schema migration — the panel falls back to
   * empty `{tts:{}, stt:{}}` so it still renders chat rows. */
  voice?: {
    tts: Record<string, IdentityVoiceProviderConfig>;
    stt: Record<string, IdentityVoiceProviderConfig>;
  };
}

export interface IdentityResponse {
  name: string;
  operator_name: string;
  version: string;
  security_mode: string;
  model_role: string;
  model_name: string;
  provider: string;
  observer_model: string | null;
  observer_provider: string | null;
  models?: Record<string, IdentityRoleModel>;
  roles?: Record<string, IdentityRoleStatus>;
  compact_thresholds?: Record<string, IdentityCompactThreshold>;
  cost_tracking?: IdentityCostTracking;
}

export interface ToolEntry {
  name: string;
  description: string;
  /** Mode-aware default posture (mode override → tools default). For path-sensitive
   *  tools the *actual* posture at execution time can still differ — see `path_sensitive`. */
  permission: string;
  /** Raw `tools.<name>` value before any mode override is applied. */
  default_posture: string;
  /** True when the current mode adds an override for this tool. */
  mode_override: boolean;
  /** True when this tool has `path_overrides` entries (real posture depends on input path). */
  path_sensitive: boolean;
}

export interface ToolsResponse {
  tools: ToolEntry[];
  mode: string | null;
}

export interface ModeResponse {
  mode: string;
  previous: string;
}

export interface CompactThresholdResponse {
  role: string;
  ratio: number;
  context_window: number;
  tokens: number;
  keep_recent_turns: number;
}

export interface ConfigFileEntry {
  name: string;
  path: string;
  content: string | null;
  lines: number;
  bytes: number;
  missing: boolean;
  truncated?: boolean;
  error?: string;
}

export interface ConfigFilesResponse {
  files: ConfigFileEntry[];
}

export interface ToolPermissionResponse {
  name: string;
  posture: string;
}

export type CostSettingsResponse = IdentityCostTracking;

// ── Terminal ────────────────────────────────────────────

export type PaneOwner = "user" | "entity";

export interface PaneLeaf {
  type: "leaf";
  id: string;
  ptyStatus: "starting" | "running" | "stopped" | "error";
  label: string;
  shell: string;
  errorMessage: string | null;
  owner: PaneOwner;
  observerEnabled: boolean;
  // F6 (terminal daily-driver 2026-07-05) — cumulative raw bytes received
  // for this pane (mirrors the backend's `output_total_chars` cursor).
  // Persisted alongside the pane tree so a page reload can ask to
  // reattach from this point instead of respawning a fresh pty.
  lastSeenCursor?: number;
}

export interface PaneSplit {
  type: "split";
  id: string;
  direction: "horizontal" | "vertical";
  ratio: number;
  first: PaneNode;
  second: PaneNode;
}

export type PaneNode = PaneLeaf | PaneSplit;

export interface TerminalTab {
  id: string;
  label: string;
  root: PaneNode;
}

export interface ShellProfile {
  argv: string[];
  label: string;
  icon?: string;
}

export interface TerminalKeybinding {
  combo: string;
  command: string;
}

export interface TerminalAnsiPalette {
  black?: string;
  red?: string;
  green?: string;
  yellow?: string;
  blue?: string;
  magenta?: string;
  cyan?: string;
  white?: string;
}

export interface TerminalTheme {
  background?: string;
  foreground?: string;
  cursor?: string;
  cursorAccent?: string;
  selectionBackground?: string;
  ansi?: TerminalAnsiPalette;
  bright_ansi?: TerminalAnsiPalette;
}

export interface TerminalConfig {
  default_shell: string;
  shell_profiles: Record<string, ShellProfile>;
  max_panes_per_tab: number;
  max_tabs: number;
  active_theme?: string;
  themes?: Record<string, TerminalTheme>;
  keybindings?: TerminalKeybinding[];
}

// ── Mission reflection (historical) ──────────────────────
// Old `mission_reflection_proposal` workspace events (pre-purge) still
// carry this shape; `EventDetailBody` renders them read-only.
// memory_saves carry stable ids so the per-item approve UI can address
// each candidate independently.
export interface MemoryEntry {
  id: string;
  path: string;
  title: string;
  content: string;
  validation_error?: string | null;
}

export interface VaultIngest {
  id: string;
  title: string;
  text: string;
  tags: string[];
}

export interface ReflectionProposal {
  mission_id: string;
  summary_md: string;
  memory_saves: MemoryEntry[];
  vault_ingests: VaultIngest[];
  agent_improvements: string[];
  drift_notes: string[];
}

// Agents registry.

export type AgentStatus = "active" | "pending";

export interface Agent {
  name: string;
  description: string;
  version: string | null;
  model_role: string;
  resolved_ref: string | null;
  tools: string[] | null;
  status: AgentStatus;
  max_tokens_override: number | null;
  disabled: boolean;
}

export interface AgentDetail extends Agent {
  sections: Record<string, string>;
}

// Keys. Read from `.env.example`, written to <TESSERACT_HOME>/.env.
// No value is ever carried in either direction except the token the backend
// generates, which comes back once in the response that creates it.

export interface EnvKey {
  name: string;
  description: string;
  signup_url: string | null;
  // Set in the file / loaded in the running process. They disagree for
  // exactly as long as it takes to restart, which is what `pending_restart`
  // reports — `.env` is read once at boot, unlike the rest of config/.
  in_file: boolean;
  active: boolean;
  pending_restart: boolean;
}

export interface EnvKeySection {
  /** The section caption in `.env.example`, which IS the tab. */
  title: string;
  keys: EnvKey[];
}

export interface McpClientKey {
  name: string;
  token_env: string;
  trust_tier: string;
  in_file: boolean;
  active: boolean;
}

export interface McpVerb {
  verb: string;
  posture: string;
}

export interface McpSurface {
  /** Whether `/mcp` serves anything. Off by default — the runtime needs no
   *  MCP surface to run itself, so it exists only for an outside tool. */
  enabled: boolean;
  /** One identity, one token. There were four; three were never handed out
   *  by anything and carried the same authority as the one that was. */
  client: McpClientKey | null;
  verbs: McpVerb[];
  endpoint: string | null;
  error: string | null;
}

export interface EnvKeysResponse {
  env_path: string;
  sections: EnvKeySection[];
  pending_restart: boolean;
  mcp: McpSurface;
}

export interface EnvKeysWriteResponse {
  written: string[];
  report: EnvKeysResponse;
}

export interface EnvKeyTokenResponse {
  name: string;
  // The only secret this API returns, and only in the response that mints
  // it: a bearer token the operator cannot read cannot be pasted into the
  // client it exists for.
  token: string;
  report: EnvKeysResponse;
}

export interface AgentsListResponse {
  agents: Agent[];
  errors: string[];
}

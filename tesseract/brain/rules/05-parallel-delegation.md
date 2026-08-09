# Parallel delegation — fire-and-track

ALL delegation verbs default to BACKGROUND (operator directives 2026-05-16, 2026-07-08): `delegate_coder`, `delegate_auditor`, `lane_turn`, `delegate_agent_controller`, and `invoke_agent` each return a `spawn_handle` immediately. Keep talking with the operator, dispatch other work, answer new questions — the spawned work runs in parallel and its completion reaches you as a note at a later turn.

**Delegate by the job, not the vendor.** `delegate_coder` builds; `delegate_auditor` reviews. Which CLI or API model fills each seat is config (`roles.yaml`) — never assume names or models.

The seats are a default, not a constraint: **when the operator names a CLI, honour it** with `provider`. "Ask codex to build this" is `delegate_coder(task=…, provider="codex")` — codex does the coding job for that one call, and the configured seats are untouched. Only pass `provider` when the operator asked for a specific one or the task genuinely needs it; otherwise let config decide, and say which one ran when you report back.

One asymmetry worth knowing: a standing auditor LANE is read-only by construction, so a finding there cannot become a fix. A `delegate_auditor` one-shot runs writeable like any delegation — ask for findings in the brief, and when the boundary has to hold structurally, open a read-only named lane instead.

Track what you spawn:

- `spawn_check(handle)` — peek at status without blocking.
- `spawn_await(handle)` — block for the result; use only when you actually need it now.
- `spawn_cancel(handle)` — abandon a runaway.
- When a `[spawn_done]` / completion note arrives, acknowledge it to the operator and act on the result — don't let finished work go unmentioned.

Pass `background: false` ONLY when the very next step in the SAME turn must consume the result inline and there is nothing else to do meanwhile — that blocks the whole turn for the full duration of the work, including the operator's queued messages. It is a real choice, not a fallback: awaiting inline is right when you cannot write the next sentence without the answer, and wrong for everything else — fan-out, long jobs, anything you could narrate progress on.

Both dispatch paths are bounded the same way, because a delegation IS a lane turn. Foreground requests whose timeout (`timeout` on `delegate_*`, `timeout_s` on `lane_turn`) exceeds the runtime cap (`runtime.yaml::max_foreground_delegate_timeout_s`) are auto-flipped to background, and the result says so — plan for the spawn-handle flow, don't fight it. The result also tells you which way it went (`dispatch: background|foreground`); read that rather than assuming.

Some contexts cannot background at all — no spawn registry means no chat for a completion note to arrive in, so the work is awaited inline whatever you asked for. The result says that too. When it does, the reply in front of you IS the whole result; there is no handle to check and nothing still running.

Scratch hygiene (operator directive 2026-07-12): every delegate brief MUST tell the worker to (a) write any throwaway files — probes, smoke-test scripts, scratch output — under `tesseract/`, never at the repo root, and (b) delete them before reporting done. A finished delegate that leaves stray files behind is an incomplete delegate; if a completion note mentions probe/scratch files, verify they're gone.

Size tasks for the delegate, not the wish: one concern, a handful of files, a timeout it can realistically meet. A 20-minute mega-task (downloads + assets + features + verification in one prompt) is how delegates die at the timeout wire — split it into background spawns and poll with `spawn_check`.

When a delegate fails or times out, the error now carries evidence — which declared `target_paths` files changed during the run, and the transcript tail. READ IT before acting: if files changed, the work partially landed — inspect what's on disk and scope a follow-up to the remainder. Never silently redo from scratch.

There is a per-session cap on concurrently-running spawns. If a spawn returns a "spawn cap reached" error, `spawn_await` or `spawn_cancel` an existing handle before spawning more — don't retry the same call blind. There is also a nesting-depth cap: a "spawn depth cap reached" error means this session is already a nested spawn and may not spawn deeper — do the work inline or report back to the parent.

Steering: work you might need to redirect mid-flight goes on a steerable substrate — a lane (`lane_turn`) or an interactive session (`session_open`) — NOT a one-shot. One-shot `delegate_*` spawns have no input channel; to change their course, `spawn_cancel` and re-dispatch. `work_send(target, message)` steers any steerable target with one verb: a named lane, an interactive session handle, or a controller session id.

Steered directives override the current plan: when the operator redirects mid-turn (WS `steer`, conversation-layer Q3), that takes precedence over whatever delegation plan is already in flight — re-scope or cancel spawned work as the new instruction requires, don't finish the old plan on autopilot.

The operator sees each active spawn as live activity while it executes.

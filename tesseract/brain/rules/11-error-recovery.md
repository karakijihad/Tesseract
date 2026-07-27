# Error recovery — two strikes, then escalate

When you see `[chat_brain error]` in your history, or a `role:tool` message containing an error string, classify the cause before retrying:
- **Yours** (bad path, malformed args, wrong tool, ignored instruction): call `memory_save` once with a one-line feedback note so you don't repeat it next time.
- **External** (5xx, network, rate-limit, transient timeout): no memory action — the runtime already retried or fell back.

Re-attempt the original goal once. If that second attempt fails the same way, STOP — never a third identical attempt. Open `lane_turn` (or `delegate_claude` / `delegate_codex`) to a coder lane with what you tried and the exact errors, or — if the task is ASK-heavy (needs operator-attended approvals you can't grant yourself) — propose that delegation to the operator instead of firing it yourself.

Treat the autonomy digest's ambient failure lines (breaker tripped, spawn(s) stalled/vanished, a tool erroring Nx consecutively) as "escalate now" triggers too — they mean something has already gone wrong repeatedly, possibly before this turn began. Don't re-run the same thing hoping it clears; escalate the same way.

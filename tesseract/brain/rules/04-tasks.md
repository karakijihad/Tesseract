# Operator-visible task checklist

When a turn requires more than ~3 distinct steps, call `tasks_set` first with a short numbered list so the operator can follow your progress in the chat-embedded checklist. Use stable short ids ('1', '2', 'verify'). Mark exactly one step as `in_progress` at a time, flip it via `tasks_update` as you advance, and move it to `completed` when done. Skip the checklist for single-tool answers or trivial 1-2 step turns — the strip is for multi-step work, not decoration.

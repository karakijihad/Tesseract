# Workspace thread isolation — HARD RULE

When the current turn was triggered by a `[workspace_comment_on_<event_id>]` or `[workspace_post_on_<event_id>]` injection, the conversational context is THAT THREAD ONLY. Each workspace thread is its own conversation, separate from the main chat and from other workspace threads.

Concrete rules:
- Deictic terms — "this", "that", "it", "again", "the brief", "the file" — resolve against the comments inside THIS thread, starting with the event payload and your own previous replies in it. They do NOT resolve against your most-recent chat turn or unrelated tool calls.
- Do NOT import or summarize main-chat work, file paths, typecheck results, recent edits, or any narrative the operator did not raise inside this thread. If they wanted that context they would mention it here.
- Do NOT cross-pollinate between workspace threads either — a comment on the daily brief is not context for a comment on a scheduler proposal.
- Tools and knowledge fetches are fine: `memory_search`, `vault_query`, `web_search`, `channel_send`, etc. — those bring in new info, they don't carry chat context.
- If the thread's own state is genuinely too thin to answer, ASK a short clarifying question in-thread via `workspace_reply` rather than guess by reaching into chat.

Concrete miss (2026-05-18, daily brief thread evt_e76fdf1cee9a): operator said "send again" referring to a Telegram send earlier in the same thread. The reply pulled in unrelated GlobalCanvas / typecheck work from the chat session as if that were the topic. That is the exact failure this rule exists to prevent.

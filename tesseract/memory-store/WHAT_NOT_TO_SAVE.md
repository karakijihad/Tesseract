# What NOT to Save in Memory

These categories are excluded from the memory store. Saving them creates
noise, not value. Pattern checks for each category are hardcoded in
`tesseract/memory/what_not_to_save.py`; this file enables them via
its numbered category list (read on startup).

## Excluded Categories

1. **Secrets and credentials** — API keys, tokens, passwords, SSH keys,
   connection strings. Hard-blocked.
2. **Code patterns** — function definitions, class bodies, import lines,
   try/except blocks. The source file is the truth; save observations
   *about* it, not copies of it.
3. **Git history** — commit hashes, `git log` / `git blame` / `git diff`
   output. The repo is authoritative.
4. **Ephemeral task state** — "currently working on X", "in-progress",
   "right now I'm", temporary scratch. The task is in TaskList or the
   workshop folder.
5. **CLAUDE.md content** — the file is always loaded; repeating its
   rules in memory is duplication.
6. **Routine acknowledgements** — "hello", "thanks", "ok", "got it".
   These are turn noise, not facts worth recalling.
7. **Raw file contents** — the file itself is the source of truth.
   Save the conclusion drawn from it, not a copy of it.
8. **Raw tool / command output** — 200-line dumps. Save the one line
   that matters.
9. **Request echoes** — titles/bodies that narrate what the operator
   just asked ("You asked to search for…", "User requested…", "As you
   wanted…"). The operator's message is already in history; restating it
   is noise. Save the *conclusion drawn* from the exchange, not the
   exchange itself.
10. **Turn summaries** — "In this turn…", "Summary of the turn…",
    "Last action / request / query / question / user_question",
    "Recent delegate_X attempts". These are meta-commentary about the
    session, not facts the operator taught you. If a durable fact came
    out of the turn, save the fact directly.
11. **Trivial bodies** — body text shorter than 80 characters. A memory
    worth keeping has context; a memory under 80 chars is almost always
    the title re-stated. Either expand the body with the *why* or skip
    the save.

If a candidate memory fits any category above, `should_save()` returns
False and the write is logged to `events/writes.jsonl` with
`status: "blocked"` and a specific `reason` (`code_pattern` / `git_history`
/ `ephemeral_task_state` / `claude_md_content` / `routine_ack` /
`request_echo` / `turn_summary` / `trivial_body`). The forensic trail
lets the librarian calibrate thresholds over time.

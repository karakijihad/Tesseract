# Trio verification — coder↔auditor relay

Code-modifying delegated work goes through the trio relay when `verify-by-default` is `on` in the `# Trio` block below (config is authoritative — read that state, don't assume): one lane codes, the other verifies, back and forth until clean. You decide when a delegation is code-modifying; the operator can explicitly order a relay ("run this through the trio") or skip it ("no verify pass") — their word wins over the configured default. Announce the relay when you start it ("routing through the trio: coder → auditor").

The lanes, their kinds, and the relay tunables (round cap, default-on) come from the `# Trio` block in this prompt — never assume names or models.

The relay recipe:

1. `lane_named_ensure` the coder lane and the auditor lane (names/kinds from the Trio block). Reuse is idempotent.
2. `lane_turn` the CODER lane with the task (background — chat stays live). Narrate the hop.
3. When its completion note arrives, `spawn_await` the handle for the full reply, then `lane_turn` the AUDITOR lane with a review brief: what was asked, what the coder reports, the files/diff to inspect, and "list Critical/Major findings or reply CLEAN".
4. Auditor findings → relay them back to the coder lane to fix (one round). Auditor CLEAN → report the result to the operator with both lanes' summaries.
5. Stop at the configured round cap. Two consecutive failed rounds (lane errors, or the auditor repeating the same unaddressed finding) → stop and escalate to the operator with the state so far (same two-strikes shape as error recovery).

Rules of the road:

- Every hop is a background `lane_turn` — never block the chat waiting on a lane. Keep answering the operator between hops.
- Narrate each hop as it lands ("auditor found 2 issues — sending back to the coder").
- Lanes are hub-connected (MCP): the auditor can `memory_search`/`vault_search` for context on its own; you don't need to paste the whole world into the brief.
- This relay is for DELEGATED coding work over lanes. The file-audit flow (`## Audit-loop routing`) with `code-auditor`/`code-fixer` agents stays the recipe for repo audits you run through delegate one-shots — cross-reference, don't mix the two mid-task.

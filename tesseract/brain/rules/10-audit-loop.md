## Audit loop — getting your work checked

Seats, not vendors. `delegate_coder` writes; `delegate_auditor` reviews and never
edits. Which model fills each seat is `roles.yaml`'s decision, and it can be a
CLI or an API provider — so never say "ask Codex to review" when you mean "ask
the auditor". Say which worker actually ran when you report back; the result
tells you.

The operator can re-seat either at any time, and can override per call: pass
`provider` to borrow the other worker for one delegation ("ask codex to build
this" → `delegate_coder(task=…, provider="codex")`). Leave it unset otherwise
and let config choose.

**For a second opinion**, that is the whole loop: `delegate_auditor` with the
change and what you want checked. One call. You decide what to do with the
answer — a reviewer reports, you act.

**For a non-trivial code task you are auditing rather than delegating:**

1. Plan the work yourself.
2. Invoke `code-auditor` with the scope to surface issues — produces audit-N.md.
3. If the audit lists Critical or Major, invoke `code-fixer` with audit-N.md as
   input.
4. Invoke `audit-verifier` with the post-fix scope — produces audit-N+1.md.
5. If audit-N+1.md is clean (no Critical, no Major), stop and report. Otherwise
   loop to step 3.

Use `repo-auditor` for periodic full-codebase audits — operator-initiated, slow,
never autonomous.

Two rounds is the working ceiling. If the same finding survives a fix, or two
rounds fail, stop and bring the operator what you have — a third pass on the same
disagreement costs more than it returns.

## Audit-loop routing

For any non-trivial code task:

1. Plan the work yourself.
2. Invoke `code-auditor` (delegate_codex_exec) with the scope to surface issues —
   produces audit-N.md.
3. If the audit lists Critical or Major: invoke `code-fixer` (delegate_claude)
   with audit-N.md as input — claude implements the fixes.
4. Invoke `audit-verifier` (delegate_codex_exec) with the post-fix scope —
   produces audit-N+1.md.
5. If audit-N+1.md is clean (no Critical, no Major), stop and report.
   Otherwise loop to step 3.

Use `repo-auditor` (delegate_codex_exec) for periodic full-codebase audits —
operator-initiated, slow, do not run autonomously.

This flow audits FILES via one-shot delegates. For code-modifying work you
delegate over lanes, the trio relay (`# Trio verification`) is the default
verify loop — don't run both on the same task.

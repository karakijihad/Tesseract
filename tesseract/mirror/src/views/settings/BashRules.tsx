import { Note } from "../../components/common/Note";
import { DataTable } from "../../components/common/DataTable";
import { useCachedFetch } from "../../lib/useCachedFetch";
import { fetchSessionCaps, type DenyRule, type SessionCapsResponse } from "../../lib/api";

/** The command floor, as the runtime holds it.
 *
 * This row said "Locked. The 24-check bash_security DENY list" and the module
 * had 26 checks, a hand-written count beside a list that grows. It is read
 * from the code that owns the rules now, and it renders them rather than
 * asserting they exist, because "you cannot change this" is a claim an
 * operator is entitled to see the substance of.
 */
const RULE_COLUMNS = [
  { label: "check", width: "56px" },
  { label: "what happens", width: "110px" },
  { label: "what it catches", width: "minmax(0, 1fr)" },
];

function DenyRules({
  rules,
  locked,
}: {
  rules: DenyRule[] | undefined;
  locked: boolean | undefined;
}) {
  if (!rules) {
    return (
      <Note tone="warn">
        This backend did not report its rule list. It is still enforcing one,
        because the checks run in the runtime rather than here, but nothing on
        this screen can show you which. Treat the list as unknown rather than
        empty.
      </Note>
    );
  }
  const blocked = rules.filter((r) => r.posture === "blocked");
  const ask = rules.filter((r) => r.posture === "ask");
  const mixed = rules.filter((r) => r.posture === "mixed");
  return (
    <>
      <Note>
        {`${rules.length} checks · ${blocked.length} refuse · ${ask.length} ask` +
          (mixed.length > 0 ? ` · ${mixed.length} both` : "")}
        {". "}
        Every command the assistant runs is checked against these before any
        permission in <code>permissions.yaml</code> is consulted. They are the
        floor the rest of the permission system sits on.
      </Note>
      <Note tone="warn">
        <strong>You cannot change these, and neither can the assistant.</strong>{" "}
        There is no setting here, in <code>permissions.yaml</code>, or in any
        hook, plugin, skill or agent that relaxes them, and a mode that switches
        every tool to AUTO does not reach them either.
        {locked === false &&
          " This backend reports them as unlocked, which it should not. That is a defect worth reporting."}
      </Note>
      <Note>
        Editing <code>tesseract/permissions/bash_security.py</code> by hand is
        the only way to move one, and what happens if you do is exactly what it
        sounds like: the check stops running, for every caller, with nothing
        else standing behind it. The audit log records a refusal by NUMBER, so
        the numbers below are what you will see there. An update replaces that
        file, so a hand edit is also silently reverted on the next one.
      </Note>
      <DataTable
        label="Command checks"
        columns={RULE_COLUMNS}
        rows={rules.map((r) => ({
          key: String(r.check),
          cells: [
            <span className="deny-rules__num t-meta">
              {String(r.check).padStart(2, "0")}
            </span>,
            <span
              className={`deny-rules__posture deny-rules__posture--${r.posture}`}
            >
              {r.posture === "blocked"
                ? "REFUSED"
                : r.posture === "mixed"
                  ? "BOTH"
                  : "ASKS YOU"}
            </span>,
            <span className="deny-rules__what">{r.refuses}</span>,
          ],
        }))}
      />
      <Note>
        <strong>ASKS YOU</strong> means the command stops and waits for you.
        It is never allowed on its own, and with nobody there to answer it
        fails closed. <strong>REFUSED</strong> does not ask at all; there is no
        answer that lets it through.
        {mixed.length > 0 && (
          <>
            {" "}
            <strong>BOTH</strong> is a check with more than one pattern, where
            some ask and some refuse. Read the description for which is which,
            and assume the refusing half applies to you.
          </>
        )}
      </Note>
    </>
  );
}

/** The floor, as its own section. It used to sit under the two loop caps,
 *  which is a settings panel about what the assistant may spend beside a list
 *  of what it may never run. It belongs beside the tools instead. */
export function BashRulesSection() {
  // Same cache key as the caps panel, so opening both costs one round trip.
  const { data: server, error } = useCachedFetch<SessionCapsResponse>(
    "settings.session-caps",
    fetchSessionCaps,
  );
  return (
    <section className="settings-section">
      {error && <Note tone="bad">{error}</Note>}
      <DenyRules rules={server?.deny_rules} locked={server?.deny_rules_locked} />
    </section>
  );
}

// Settings, Credentials. The assistant's own accounts, and the operator's
// only way to fill one in.
//
// This is not the Keys panel with different rows. Keys are the app's own
// infrastructure, provisioned once at install and owned by the app; these
// belong to the assistant, the operator adds them over time, and each one
// carries the metadata a consumer needs to spend it without a per-service
// branch in the runtime: where the value goes, and which hosts it may reach.
//
// No value is ever rendered, and unlike Keys there is no minted-token
// exception: nothing here is a secret the operator has to paste elsewhere. A
// row is ready, waiting, or empty, and typing into a box replaces whatever is
// stored without ever showing it.
//
// An account holds one box unless the assistant asked for more. A sign-in is
// often a set rather than a single token, so the assistant names what its
// service asks it for and each name becomes a box here. That is the only
// thing it can add: it cannot touch where the account may be sent, which is
// the one approval you give, once, through `credential_setup`.
//
// A row the assistant asked for renders first and says so. The sort is the
// store's, not this file's (`credentials/store.py::list_public`), so every
// surface inherits it rather than each one remembering to float a request.
// That request is the whole reason the panel is not just a form: the assistant
// can tell the operator what it needs, and this is where the answer goes.

import { useState } from "react";

import { Block } from "../../components/common/Block";
import { Button } from "../../components/common/Button";
import { DataTable } from "../../components/common/DataTable";
import { Input } from "../../components/common/Input";
import { Note } from "../../components/common/Note";
import {
  clearCredentialValue,
  fetchCredentials,
  removeCredential,
  saveCredential,
} from "../../lib/api";
import type {
  Credential,
  CredentialInjection,
  CredentialsResponse,
} from "../../lib/types";
import { useCachedFetch } from "../../lib/useCachedFetch";

/** What a stored value looks like: a filled field at a length that is not its
 *  own. The same reasoning the Keys panel records — words in an empty box read
 *  as "nothing saved" — and a fixed width, because the length of a secret is
 *  itself information. */
const VALUE_MASK = "••••••••••••";

interface Draft {
  service: string;
  account: string;
  purpose: string;
  hosts: string;
  kind: string;
  name: string;
  prefix: string;
  field: string;
  /** Keyed by field name, and only the boxes actually typed into. An empty
   *  box is left out of the save, so it keeps whatever is stored. */
  values: Record<string, string>;
}

/** The field every account has before anyone asks for another. Matches
 *  `credentials/models.py::PRIMARY_FIELD`, which is what a new account gets
 *  and what a record written before fields existed became. */
const PRIMARY_FIELD = "value";

/** What goes above that one box. Matches `models.py::PRIMARY_LABEL`, which is
 *  what the store writes for a field it mints, so a brand-new account and a
 *  saved one say the same word. */
const PRIMARY_LABEL = "Value";

// `Authorization: Bearer <value>` is what most API tokens want, so it is the
// starting point rather than a service's own shape. Nothing here is specific
// to any one provider, and nothing in the runtime is either.
const BLANK: Draft = {
  service: "",
  account: "",
  purpose: "",
  hosts: "",
  kind: "header",
  name: "Authorization",
  prefix: "Bearer ",
  field: PRIMARY_FIELD,
  values: {},
};

function toDraft(entry: Credential): Draft {
  return {
    service: entry.service,
    account: entry.account,
    purpose: entry.purpose,
    hosts: entry.allowed_hosts.join(", "),
    kind: entry.injection.kind,
    name: entry.injection.name,
    prefix: entry.injection.prefix,
    field: entry.injection.field,
    values: {},
  };
}

function injection(draft: Draft): CredentialInjection {
  return {
    kind: draft.kind as CredentialInjection["kind"],
    name: draft.name.trim(),
    prefix: draft.prefix,
    field: draft.field,
  };
}

/** The boxes with something in them, trimmed. An empty box is not an
 *  instruction to clear; clearing is its own button. */
function typedValues(draft: Draft): Record<string, string> {
  const typed: Record<string, string> = {};
  for (const [name, value] of Object.entries(draft.values)) {
    if (value.trim()) typed[name] = value.trim();
  }
  return typed;
}

function hostList(draft: Draft): string[] {
  return draft.hosts
    .split(",")
    .map((h) => h.trim())
    .filter(Boolean);
}

/** One sentence, ending in exactly one full stop. The purpose is free text
 *  the operator or the assistant wrote, so it arrives with a stop or without
 *  one, and appending a second read as `github.com.. sent inside`. */
function sentence(text: string): string {
  const trimmed = text.trim();
  if (!trimmed) return "";
  return /[.!?]$/.test(trimmed) ? trimmed : `${trimmed}.`;
}

function stateLabel(entry: Credential): string {
  if (entry.requested) return "waiting for you";
  if (!entry.has_value) return "no value stored";
  // Something is stored and something is not. Reading as ready here is the
  // mistake `spend_state` exists to stop, one field further down than the
  // first time it was made.
  if (entry.spend_state === "incomplete") return "some boxes still empty";
  // Stored is not sendable. A value sealed by another Windows account is one
  // this computer can never open, so the row says which one it is instead of
  // reading as ready until a push fails.
  if (entry.spend_state === "foreign") return "saved on another computer";
  if (entry.spend_state === "unverified") return "not checked yet";
  // A value with nowhere it may be sent cannot be spent. The assistant fills
  // that in and you approve the hosts once; until then the row is honest
  // about not being usable rather than reading as ready.
  if (entry.allowed_hosts.length === 0) return "waiting for the assistant";
  return "ready";
}

/** What to do about a row that cannot be spent, said in the row itself. A
 *  label naming a problem with no remedy beside it sends the reader looking,
 *  and this one is not guessable: nothing about the panel suggests that the
 *  Windows account is what a stored value depends on. */
function stateNote(entry: Credential): string | null {
  // Gated on the state the store already worked out, not on a second count
  // of the raw fields. The two disagreed: `spend_state` ranks foreign above
  // incomplete on purpose, because only one of the two is fixed by typing the
  // rest in, and a note computed independently showed "Still needed" for a
  // row whose real problem was that it could never be read on this computer.
  if (entry.spend_state === "incomplete") {
    const missing = entry.fields.filter((f) => !f.has_value);
    return ` Still needed: ${missing.map((f) => f.label).join(", ")}.`;
  }
  if (entry.spend_state === "foreign") {
    return " Saved by a different Windows account, so it cannot be read on this computer. Clear it and add it again.";
  }
  if (entry.spend_state === "unverified") {
    return " It has not been used on this computer yet, so whether it can be read here is unknown until it is.";
  }
  return null;
}

function whereLabel(entry: Credential): string {
  const { kind, name } = entry.injection;
  // The one kind that sends nothing and points at nothing. Every box is handed
  // to a program the assistant wrote, each under its own name, so there is no
  // header to name and no single box that is "the one that is sent". The
  // backend clears both fields on the record for that reason, and a label
  // built from them would read "passed to a program as " with nothing after it.
  if (kind === "env") {
    const names = entry.fields.map((f) => f.name.toUpperCase()).join(", ");
    return names
      ? `passed to the assistant's own code, as ${names}`
      : "passed to the assistant's own code";
  }
  if (entry.allowed_hosts.length === 0) {
    return "the assistant has not said how this is sent yet";
  }
  // Which box is spent, once there is more than one. Without it a row can
  // read as fully filled while the box that gets sent is the empty one, and
  // `credential_list` already tells the assistant this.
  const which =
    entry.fields.length > 1 ? whichField(entry) : "";
  if (kind === "header") return `${which}sent as the ${name} header`;
  if (kind === "query") return `${which}sent as the ${name} query parameter`;
  return `${which}sent inside the web address`;
}

function whichField(entry: Credential): string {
  const spent = entry.fields.find((f) => f.name === entry.injection.field);
  return spent ? `${spent.label} is the one ` : "";
}

export function CredentialsSection() {
  const {
    data: report,
    error,
    setError,
    set,
  } = useCachedFetch<CredentialsResponse>("settings.credentials", () =>
    fetchCredentials(),
  );
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft>(BLANK);
  const [busy, setBusy] = useState(false);
  // One arming slot for the destructive actions, cleared on every report
  // replacement so an arm cannot outlive the state it was aimed at. The Keys
  // panel learned this the hard way: a row armed before a reconnect stayed
  // armed minutes later, and the second click destroyed a credential with no
  // warning in front of it.
  const [armed, setArmed] = useState<string | null>(null);

  const apply = (next: CredentialsResponse) => {
    set(next);
    setArmed(null);
  };

  const field = (key: keyof Draft) => (next: string) =>
    setDraft((d) => ({ ...d, [key]: next }));

  const run = async (act: () => Promise<CredentialsResponse>) => {
    setBusy(true);
    setError(null);
    try {
      apply(await act());
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const startEdit = (entry: Credential) => {
    setEditing(entry.id);
    setDraft(toDraft(entry));
    setArmed(null);
  };

  const startNew = () => {
    setEditing("__new__");
    setDraft(BLANK);
    setArmed(null);
  };

  const save = () =>
    run(async () => {
      const result = await saveCredential({
        id: editing === "__new__" ? null : editing,
        service: draft.service.trim(),
        account: draft.account.trim(),
        purpose: draft.purpose.trim(),
        injection: injection(draft),
        allowed_hosts: hostList(draft),
        // Only the boxes actually typed into. An empty one is left out rather
        // than sent blank: an operator correcting a purpose must not thereby
        // destroy the credential they were describing.
        values: typedValues(draft),
      });
      setEditing(null);
      setDraft(BLANK);
      return result.report;
    });

  if (!report) {
    return (
      <section className="settings-section">
        <div className="t-meta">{error ?? "(loading…)"}</div>
      </section>
    );
  }

  const rows = report.credentials;
  // Counted on `needs_operator`, not `requested`: an ask for one more field on
  // an account that already holds a value deliberately leaves `requested`
  // alone, and a count that read it would leave that ask invisible on the one
  // surface it is addressed to.
  const waiting = rows.filter((r) => r.needs_operator).length;
  // A new account has the one box every account starts with. An existing one
  // has whatever the assistant has asked for since, in the order it asked, so
  // the operator fills them in the order it explained them.
  const boxes =
    editing === "__new__"
      ? [{ name: PRIMARY_FIELD, label: PRIMARY_LABEL, has_value: false }]
      : (rows.find((r) => r.id === editing)?.fields ?? []);
  // What the FORM asks for, and nothing else. It used to also require a
  // header name and at least one host, which is what the form asked for
  // before those two became the assistant's half. The fields went; the rule
  // stayed; the blank draft carries no hosts, so Save could never enable and
  // no account could be added at all. A new row needs a value, because a row
  // added without one has nothing in it.
  const savable =
    draft.service.trim().length > 0 &&
    (editing !== "__new__" ||
      Object.keys(typedValues(draft)).length > 0);

  return (
    <section className="settings-section">
      {error && <Note tone="bad">{error}</Note>}

      {!report.dpapi_available && (
        <Note tone="bad">
          Credentials are encrypted with your Windows sign-in, which this
          machine does not have. Nothing can be saved here, and nothing is
          stored unencrypted instead.
        </Note>
      )}

      <Block
        title={null}
        meta={
          waiting > 0
            ? `${waiting} waiting for you`
            : `${rows.length} ${rows.length === 1 ? "account" : "accounts"}`
        }
      >
        <Note>
          Accounts that belong to the assistant, not to you. It can see which
          ones it has and what each is for, and it can ask you for one it does
          not have. It never sees a value: the runtime puts one into a request
          at the moment the request is sent.
        </Note>

        <DataTable
          label="The assistant's accounts"
          columns={[
            // Sized for what these cells actually hold rather than for the
            // shortest example. A service carries an account under it, and an
            // account is an email address; a state is a sentence now that a
            // value can be stored and unusable at the same time. Both were
            // cut to fit "github.com" and "ready", so every real row wrapped
            // mid-phrase. The floors keep them legible and the `fr` halves
            // let the purpose column take the slack on a wide pane.
            { label: "service", width: "minmax(190px, 0.8fr)" },
            { label: "state", width: "minmax(165px, 0.7fr)" },
            { label: "what it is for", width: "minmax(0, 2fr)" },
            { label: "", width: "170px" },
          ]}
          empty={
            <span className="t-meta">
              No accounts yet. Add one below, or wait for the assistant to ask
              for the one it needs.
            </span>
          }
          rows={rows.map((entry) => ({
            key: entry.id,
            cells: [
              <span className="cred-cell">
                {entry.service}
                {entry.account && (
                  <span className="t-meta">{entry.account}</span>
                )}
              </span>,
              <span className="cred-cell">
                {stateLabel(entry)}
                {entry.has_value && (
                  <span className="t-meta">{VALUE_MASK}</span>
                )}
              </span>,
              <span className="t-meta">
                {sentence(entry.purpose) || "No purpose written yet."}{" "}
                {whereLabel(entry)}
                {entry.allowed_hosts.length > 0
                  ? ` to ${entry.allowed_hosts.join(", ")}.`
                  : "."}
                {stateNote(entry)}
              </span>,
              <span className="cred-row-actions">
                <Button
                  onClick={() => startEdit(entry)}
                  disabled={busy}
                  ariaLabel={`Edit the ${entry.service} account`}
                >
                  {entry.has_value ? "Edit" : "Add value"}
                </Button>
                {entry.has_value && (
                  <Button
                    onClick={() =>
                      armed === `clear:${entry.id}`
                        ? void run(async () => {
                            const r = await clearCredentialValue(entry.id);
                            return r.report;
                          })
                        : setArmed(`clear:${entry.id}`)
                    }
                    disabled={busy}
                    tone="danger"
                    ariaLabel={`Clear the ${entry.service} value`}
                  >
                    {armed === `clear:${entry.id}` ? "Sure?" : "Clear"}
                  </Button>
                )}
                <Button
                  onClick={() =>
                    armed === `remove:${entry.id}`
                      ? void run(async () => {
                          const r = await removeCredential(entry.id);
                          if (editing === entry.id) setEditing(null);
                          return r.report;
                        })
                      : setArmed(`remove:${entry.id}`)
                  }
                  disabled={busy}
                  tone="danger"
                  ariaLabel={`Forget the ${entry.service} account`}
                >
                  {armed === `remove:${entry.id}` ? "Sure?" : "Forget"}
                </Button>
              </span>,
            ],
          }))}
        />

        {/* Three questions plus a box per value, because that is what a
            person knows about an account: what it is with, who it is, what
            it is for, and the secrets themselves. Where each value goes,
            what it is called, what prefix it wants and which hosts it may
            reach are facts about the SERVICE, and the assistant fills those
            in through `credential_setup`, where the host list gets your one
            approval. Asking a person for them was asking them to know that
            one API wants a Bearer header and the next wants a query
            parameter. */}
        {editing === null ? (
          <div className="cred-add">
            <Button onClick={startNew} disabled={busy}>
              Add an account
            </Button>
          </div>
        ) : (
          <div className="cred-form">
            <label className="cred-form__field">
              <span className="t-meta">Service</span>
              <Input
                value={draft.service}
                onChange={field("service")}
                placeholder="the service this account is with"
                ariaLabel="Service"
                disabled={busy}
              />
            </label>
            <label className="cred-form__field">
              <span className="t-meta">Username or account</span>
              <Input
                value={draft.account}
                onChange={field("account")}
                placeholder="the login this belongs to"
                ariaLabel="Username or account"
                disabled={busy}
              />
            </label>
            <label className="cred-form__field cred-form__field--wide">
              <span className="t-meta">What it is for</span>
              <Input
                value={draft.purpose}
                onChange={field("purpose")}
                placeholder="what the assistant may use it for"
                ariaLabel="What it is for"
                disabled={busy}
              />
            </label>
            {boxes.map((box) => (
              <label
                className="cred-form__field cred-form__field--wide"
                key={box.name}
              >
                <span className="t-meta">{box.label}</span>
                <Input
                  value={draft.values[box.name] ?? ""}
                  onChange={(next) =>
                    setDraft((d) => ({
                      ...d,
                      values: { ...d.values, [box.name]: next },
                    }))
                  }
                  type="password"
                  autoComplete="off"
                  placeholder={
                    box.has_value ? VALUE_MASK : "paste the token or password"
                  }
                  ariaLabel={box.label}
                  disabled={busy || !report.dpapi_available}
                />
              </label>
            ))}
            <div className="cred-form__actions">
              <Button
                onClick={() => void save()}
                disabled={busy || !savable}
                tone="primary"
              >
                Save
              </Button>
              <Button
                onClick={() => {
                  setEditing(null);
                  setDraft(BLANK);
                }}
                disabled={busy}
              >
                Cancel
              </Button>
              <span className="t-meta">
                Leaving a box blank keeps whatever is already stored in it.
              </span>
            </div>
          </div>
        )}
      </Block>
    </section>
  );
}

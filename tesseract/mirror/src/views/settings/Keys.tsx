// Settings → Keys. The view that ends "find the .env file in a folder".
//
// Every row comes from `.env.example` — the same file that seeded the
// operator's `.env` — so the list, the prose and the signup address are read
// from what ships rather than kept in a second copy here. That extends to the
// tabs: a section header in the template IS a tab, so grouping the keys is an
// edit to the file the operator can also open by hand, not a table in here
// that would drift from it.
//
// No value is ever rendered: a key is `set` or `not set`. The one secret this
// view shows is a token it just minted, once, because a bearer token nobody
// can read cannot be pasted into the client it exists for — and that lives in
// the MCP tab, which is its own view because MCP is the one thing here an
// operator can meet without already knowing what it is.
//
// The restart is part of the view rather than a warning beside it. `.env` is
// read once at boot while the rest of config/ hot-reloads, so an edit here
// does nothing until the backend comes back — `pending_restart` is the
// backend comparing the file against its own environment.

import { Note } from "../../components/common/Note";
import { useEffect, useMemo, useState } from "react";

import {
  fetchEnvKeys,
  postEnvKeys,
  postGenerateEnvToken,
  postMcpEnabled,
  postRuntimeRestart,
} from "../../lib/api";
import type { EnvKey, EnvKeysResponse } from "../../lib/types";
import { isTauri } from "../../lib/endpoints";
import { useCachedFetch } from "../../lib/useCachedFetch";
import { Hint } from "../../components/ui/Hint";
import { Button } from "../../components/common/Button";
import { Tabs } from "../../components/common/Tabs";
import { KeysMcp } from "./KeysMcp";

/** The tab MCP renders as. Matched against the section title in
 *  `.env.example`, which is where the grouping is decided. */
const MCP_SECTION = "MCP";

async function openSignup(url: string): Promise<void> {
  if (isTauri()) {
    const { openUrl } = await import("@tauri-apps/plugin-opener");
    await openUrl(url);
    return;
  }
  window.open(url, "_blank", "noopener,noreferrer");
}

async function revealEnvFile(path: string): Promise<void> {
  if (!isTauri()) return;
  const { revealItemInDir } = await import("@tauri-apps/plugin-opener");
  await revealItemInDir(path);
}

/** What a set key looks like: a filled field, at a length that is not its own.
 *
 * It stays a PLACEHOLDER rather than becoming a value, so typing replaces it
 * instead of appending to it and the report's "no value ever reaches the
 * browser" contract is untouched. The words that used to sit here — "set —
 * type to replace" — were accurate and read as an empty box: the operator
 * concluded none of eight keys had saved when all eight had.
 *
 * Fixed width, deliberately. The length of a secret is itself information, and
 * a mask that tracked it would leak the one property the field is hiding.
 */
const KEY_MASK = "••••••••••••";

function keyState(key: EnvKey): string {
  if (!key.in_file) return key.active ? "set outside this file" : "not set";
  return key.active ? "set" : "set · needs restart";
}

export function KeysSection() {
  const {
    data: report,
    error,
    setError,
    set: setReport,
  } = useCachedFetch<EnvKeysResponse>("settings.env-keys", fetchEnvKeys);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [minted, setMinted] = useState<string | null>(null);
  const [restarting, setRestarting] = useState(false);
  const [tab, setTab] = useState<string | null>(null);
  // One arming slot for both destructive actions — replacing a live MCP
  // token and clearing a set key. Cleared whenever the report is replaced
  // (below), so an arm cannot outlive the moment it was given.
  const [armed, setArmed] = useState<string | null>(null);

  // Every path that replaces the report goes through this, so an arming
  // click cannot survive the state it was aimed at — a row armed before a
  // reconnect would otherwise still be armed minutes later, and the second
  // click would destroy a credential with no warning in front of it.
  const applyReport = (next: EnvKeysResponse) => {
    setReport(next);
    setArmed(null);
  };

  const refresh = () =>
    fetchEnvKeys()
      .then(applyReport)
      .catch((err) =>
        setError(err instanceof Error ? err.message : String(err)),
      );

  // A cached report must not carry a stale arm — the same reasoning as
  // `applyReport`, applied to the value the cache hands back on a revisit.
  useEffect(() => {
    setArmed(null);
  }, [report]);

  // Typing sets a key; a Clear control clears one. Emptying the box means
  // neither — an operator backing out of an edit must not thereby delete the
  // credential they were editing, which is the trap the first attempt at
  // "let the UI clear a key" walked straight into.
  const pendingEdits = useMemo(
    () => Object.entries(drafts).filter(([, value]) => value.trim() !== ""),
    [drafts],
  );

  const onSave = async () => {
    if (pendingEdits.length === 0) return;
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      const result = await postEnvKeys(Object.fromEntries(pendingEdits));
      applyReport(result.report);
      // Cleared on success only: a failed write must leave what the operator
      // typed in the boxes, or they have to find the key again.
      setDrafts({});
      setNote(
        `Saved ${result.written.join(", ")} — restart for it to take effect.`,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not save");
    } finally {
      setBusy(false);
    }
  };

  // Replacing a token that is already set is unrecoverable — the old value
  // was shown once and is stored nowhere else — so the first click arms and
  // the second commits. An unset token stays one click: there is nothing to
  // lose. What it does NOT do is revoke anything now: the running backend
  // authenticates from the environment it booted with, so the old token
  // keeps working and the new one does not, until a restart.
  const onGenerate = async (name: string, replacing: boolean) => {
    if (replacing && armed !== `token:${name}`) {
      setArmed(`token:${name}`);
      return;
    }
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      const result = await postGenerateEnvToken(name);
      applyReport(result.report);
      setMinted(result.token);
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not generate");
    } finally {
      setBusy(false);
    }
  };

  const onToggleMcp = async (enabled: boolean) => {
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      applyReport((await postMcpEnabled(enabled)).report);
      setNote(
        enabled
          ? "MCP will accept connections after a restart."
          : "MCP is off. It stops serving after a restart.",
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not save");
    } finally {
      setBusy(false);
    }
  };

  // The other destructive action, armed the same way. A cleared key is gone
  // from this machine — most providers show a key once — so it takes the
  // same two clicks the token replacement does.
  const onClear = async (name: string) => {
    if (armed !== `clear:${name}`) {
      setArmed(`clear:${name}`);
      return;
    }
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      const result = await postEnvKeys({ [name]: "" });
      applyReport(result.report);
      setDrafts((d) => {
        const next = { ...d };
        delete next[name];
        return next;
      });
      setNote(`Cleared ${name} — restart for it to take effect.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not clear");
    } finally {
      setBusy(false);
    }
  };

  const onRestart = async () => {
    setRestarting(true);
    setNote(null);
    try {
      await postRuntimeRestart("operator restarted after editing .env");
      setNote("Restarting TESSERACT — this takes a few seconds.");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setRestarting(false);
    }
  };

  if (!report) {
    return (
      <section className="settings-section">
        <div className="t-meta">{error ?? "(loading…)"}</div>
      </section>
    );
  }

  // The tabs ARE the template's sections, in the template's order. Nothing
  // here names a group, so adding one to `.env.example` adds a tab.
  const active = tab ?? report.sections[0]?.title ?? "";
  const section = report.sections.find((s) => s.title === active);
  const mcpClient = report.mcp.client;

  return (
    <section className="settings-section">
      <Tabs
        items={report.sections.map((s) => ({ key: s.title, label: s.title }))}
        active={active}
        onSelect={setTab}
        label="key groups"
      />
      {error && <Note tone="bad">{error}</Note>}

      {active === MCP_SECTION ? (
        <KeysMcp
          mcp={report.mcp}
          minted={minted}
          busy={busy}
          armed={mcpClient ? armed === `token:${mcpClient.token_env}` : false}
          onGenerate={() => {
            if (mcpClient) void onGenerate(mcpClient.token_env, mcpClient.in_file);
          }}
          onToggle={(enabled) => void onToggleMcp(enabled)}
        />
      ) : (
        <>
          <Note>
            Every key is optional and none is stored anywhere but{" "}
            <code>{report.env_path}</code>. Values are never shown here — a key
            reads as set or not set, and typing over one replaces it.
          </Note>
          {section?.keys.map((key) => (
            <div className="key-row" key={key.name}>
              <label className="key-row__name" htmlFor={`key-${key.name}`}>
                {key.name}
              </label>
              <input
                id={`key-${key.name}`}
                type="password"
                className="input cost-row__input"
                autoComplete="off"
                spellCheck={false}
                aria-label={key.name}
                placeholder={key.in_file ? KEY_MASK : "empty"}
                value={drafts[key.name] ?? ""}
                disabled={busy}
                onChange={(e) => {
                  // Typing anywhere disarms: a confirmation given before an
                  // unrelated edit is not a confirmation of what the second
                  // click would do now.
                  setArmed(null);
                  setDrafts((d) => ({ ...d, [key.name]: e.target.value }));
                }}
              />
              <span className="key-row__state t-meta">
                <span>{keyState(key)}</span>
                {key.in_file && (
                  <Hint
                    label={
                      (drafts[key.name] ?? "").trim() !== ""
                        ? "empty the box first — a typed value and a clear are different instructions"
                        : undefined
                    }
                  >
                    {/* `inline`, not the default card: clearing is a rare,
                        deliberate act, and a control at button weight beside
                        every set key reads as the thing to do with one. */}
                    <Button
                      onClick={() => void onClear(key.name)}
                      // A typed replacement and a clear are opposite
                      // instructions for the same key; whichever landed last
                      // would win with the screen still showing the other.
                      disabled={busy || (drafts[key.name] ?? "").trim() !== ""}
                      ariaLabel={`clear ${key.name}`}
                      tone="inline"
                    >
                      {armed === `clear:${key.name}` ? "Confirm clear" : "Clear"}
                    </Button>
                  </Hint>
                )}
              </span>
              <p className="key-row__hint t-meta">
                {key.description}
                {key.signup_url && (
                  <>
                    {" "}
                    <a
                      href={key.signup_url}
                      className="key-row__link"
                      onClick={(e) => {
                        e.preventDefault();
                        void openSignup(key.signup_url as string);
                      }}
                    >
                      {key.signup_url.replace(/^https?:\/\//, "")}
                    </a>
                  </>
                )}
              </p>
            </div>
          ))}

          <div className="cost-row cost-row--actions">
            <Button
              onClick={() => void onSave()}
              disabled={busy || pendingEdits.length === 0}
              tone="primary"
            >
              {busy ? "Saving…" : `Save ${pendingEdits.length || ""}`.trim()}
            </Button>
            {isTauri() && (
              <Button
                onClick={() => void revealEnvFile(report.env_path)}
                tone="primary"
              >
                Open folder
              </Button>
            )}
            {/* The file is editable by hand and by the first-run script, so
                what this panel shows can go stale under it. */}
            <Button onClick={() => void refresh()} tone="primary">
              Refresh
            </Button>
          </div>
        </>
      )}

      {report.pending_restart && (
        <>
          <Note>
            .env is read once at boot, so a key set here is inert until
            TESSERACT restarts. Everything in flight stops.
          </Note>
          <div className="cost-row cost-row--actions">
            <Button
              onClick={() => void onRestart()}
              disabled={restarting}
              tone="primary"
            >
              {restarting ? "Restarting…" : "Restart TESSERACT"}
            </Button>
          </div>
        </>
      )}
      {note && <div className="t-meta">{note}</div>}
    </section>
  );
}

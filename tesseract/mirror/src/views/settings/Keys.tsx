// Settings → API keys. The view that ends "find the .env file in a folder".
//
// Every row comes from `.env.example` — the same file that seeded the
// operator's `.env` — so the list, the prose and the signup address are read
// from what ships rather than kept in a second copy here. No value is ever
// rendered: a key is `set` or `not set`, and the one secret this view shows
// is a token it just minted, once, because a bearer token nobody can read
// cannot be pasted into the client it exists for.
//
// The restart is part of the view rather than a warning beside it. `.env` is
// read once at boot while the rest of config/ hot-reloads, so an edit here
// does nothing until the backend comes back — `pending_restart` is the
// backend comparing the file against its own environment.

import { useEffect, useMemo, useState } from "react";

import {
  fetchEnvKeys,
  postEnvKeys,
  postGenerateEnvToken,
  postRuntimeRestart,
} from "../../lib/api";
import type {
  EnvKey,
  EnvKeysResponse,
  McpClientKey,
} from "../../lib/types";
import { isTauri } from "../../lib/endpoints";
import { useWebSocketStore } from "../../stores/websocket";
import { useFetchRetryTick } from "../../lib/useFetchRetry";

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

function keyState(key: EnvKey | McpClientKey): string {
  if (!key.in_file) return key.active ? "set outside this file" : "not set";
  return key.active ? "set" : "set · needs restart";
}

export function KeysSection() {
  const [report, setReport] = useState<EnvKeysResponse | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [minted, setMinted] = useState<{ name: string; token: string } | null>(
    null,
  );
  const [restarting, setRestarting] = useState(false);
  const [showVerbs, setShowVerbs] = useState(false);
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

  const wsGeneration = useWebSocketStore((s) => s.generation);
  const retryTick = useFetchRetryTick(error !== null);
  useEffect(() => {
    setError(null);
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wsGeneration, retryTick]);

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
      setMinted({ name: result.name, token: result.token });
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not generate");
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
        <h3 className="settings-section__title">API keys</h3>
        <div className="t-meta">{error ?? "(loading…)"}</div>
      </section>
    );
  }

  const mcp = report.mcp;

  return (
    <section className="settings-section">
      <h3 className="settings-section__title">API keys</h3>
      <div className="settings-hint t-meta">
        Every key is optional and none is stored anywhere but{" "}
        <code>{report.env_path}</code>. Values are never shown here — a key
        reads as set or not set, and typing over one replaces it.
      </div>
      {error && <div className="settings-error">{error}</div>}

      {report.sections.map((section) => (
        <div key={section.title}>
          <div className="cost-row" style={{ marginTop: "0.75rem" }}>
            <label className="cost-row__label">{section.title}</label>
            <span className="t-meta" />
            <span className="cost-row__spend t-meta" />
          </div>
          {section.keys.map((key) => (
            <div className="key-row" key={key.name}>
              <label className="key-row__name" htmlFor={`key-${key.name}`}>
                {key.name}
              </label>
              <input
                id={`key-${key.name}`}
                type="password"
                className="cost-row__input"
                autoComplete="off"
                spellCheck={false}
                aria-label={key.name}
                placeholder={key.in_file ? "set — type to replace" : "not set"}
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
                {keyState(key)}
                {key.in_file && (
                  <>
                    {" "}
                    <button
                      type="button"
                      className="key-row__clear"
                      onClick={() => void onClear(key.name)}
                      // A typed replacement and a clear are opposite
                      // instructions for the same key; whichever landed last
                      // would win with the screen still showing the other.
                      disabled={busy || (drafts[key.name] ?? "").trim() !== ""}
                      title={
                        (drafts[key.name] ?? "").trim() !== ""
                          ? "empty the box first — a typed value and a clear are different instructions"
                          : undefined
                      }
                      aria-label={`clear ${key.name}`}
                    >
                      {armed === `clear:${key.name}` ? "Confirm clear" : "Clear"}
                    </button>
                  </>
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
        </div>
      ))}

      <div className="cost-row cost-row--actions">
        <button
          type="button"
          className="cost-row__save"
          onClick={() => void onSave()}
          disabled={busy || pendingEdits.length === 0}
        >
          {busy ? "Saving…" : `Save ${pendingEdits.length || ""}`.trim()}
        </button>
        {isTauri() && (
          <button
            type="button"
            className="cost-row__save"
            onClick={() => void revealEnvFile(report.env_path)}
            style={{ marginLeft: "0.5rem" }}
          >
            Open folder
          </button>
        )}
        {/* The file is editable by hand and by the first-run script, so what
            this panel shows can go stale under it. */}
        <button
          type="button"
          className="cost-row__save"
          onClick={() => void refresh()}
          style={{ marginLeft: "0.5rem" }}
        >
          Refresh
        </button>
      </div>

      {report.pending_restart && (
        <>
          <div className="settings-hint t-meta">
            .env is read once at boot, so a key set here is inert until
            TESSERACT restarts. Everything in flight stops.
          </div>
          <div className="cost-row cost-row--actions">
            <button
              type="button"
              className="cost-row__save"
              onClick={() => void onRestart()}
              disabled={restarting}
            >
              {restarting ? "Restarting…" : "Restart TESSERACT"}
            </button>
          </div>
        </>
      )}
      {note && <div className="t-meta">{note}</div>}

      {/* ── MCP tokens ────────────────────────────────────────
          Beside the surface they unlock, because a bearer token is an
          abstraction until you can see what it opens. */}
      <div className="cost-row" style={{ marginTop: "1rem" }}>
        <label className="cost-row__label">MCP tokens</label>
        <span className="t-meta">{mcp.endpoint ?? "—"}</span>
        <span className="cost-row__spend t-meta">
          {mcp.error ?? "one token per client identity in mcp.yaml"}
        </span>
      </div>
      <div className="settings-hint t-meta">
        These are secrets you generate, not signup keys. Only needed if you
        expose TESSERACT as an MCP server to another tool; a request whose
        token matches no client is refused.
      </div>
      {mcp.clients.map((client) => (
        <div className="cost-row" key={client.token_env}>
          <label className="cost-row__label">{client.name}</label>
          <span className="t-meta">
            {keyState(client)} · {client.trust_tier}
          </span>
          <span className="cost-row__spend t-meta">
            <button
              type="button"
              className="cost-row__save"
              onClick={() => void onGenerate(client.token_env, client.in_file)}
              disabled={busy}
              aria-label={`generate ${client.token_env}`}
            >
              {!client.in_file
                ? "Generate"
                : armed === `token:${client.token_env}`
                  ? "Disconnect and replace"
                  : "Replace"}
            </button>
            {armed === `token:${client.token_env}` && (
              <span className="t-meta">
                {" "}
                the current token is lost — but it keeps working, and the new
                one does not, until you restart
              </span>
            )}
          </span>
        </div>
      ))}
      {minted && (
        <div className="settings-hint t-meta">
          New token for <code>{minted.name}</code>, written to .env and shown
          once: <code className="key-row__token">{minted.token}</code>
        </div>
      )}
      {mcp.verbs.length > 0 && (
        <>
          <div className="cost-row cost-row--actions">
            <button
              type="button"
              className="cost-row__save"
              onClick={() => setShowVerbs((v) => !v)}
            >
              {showVerbs ? "Hide" : `What a token unlocks (${mcp.verbs.length})`}
            </button>
          </div>
          {showVerbs &&
            mcp.verbs.map((verb) => (
              <div className="cost-row" key={verb.verb}>
                <label className="cost-row__label">{verb.verb}</label>
                <span className="t-meta">{verb.posture}</span>
                <span className="cost-row__spend t-meta">
                  {verb.posture === "ask"
                    ? "asks you first"
                    : "runs unattended"}
                </span>
              </div>
            ))}
        </>
      )}
    </section>
  );
}

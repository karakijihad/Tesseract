// Settings → Keys → MCP. The one tab here that is not a list of keys.
//
// MCP is the only surface in the app that exists purely for something else to
// use, so it is the only one an operator can meet without knowing what it is.
// That is why this view explains before it offers: what the protocol is, what
// connecting buys, what the token opens — and only then the switch, the
// address and the block to paste.
//
// It went from four tokens to one. `lane-claude`, `lane-codex` and
// `terminal-manual` were identities nothing ever handed out: no code read
// their env vars, the built-in lanes and the terminal reach the runtime
// directly, and all three sat at the same trust tier with the same verb
// ceiling as each other. Four secrets to mint and rotate bought a distinction
// the runtime never drew. One address, one token, one thing to paste.

import { useState } from "react";

import { Note } from "../../components/common/Note";
import { Button } from "../../components/common/Button";
import { Switch } from "../../components/common/Switch";
import { copyToClipboard } from "../../lib/clipboard";
import type { McpSurface } from "../../lib/types";

/** What an MCP client's config file wants, filled in.
 *
 * Emitted as the `mcpServers` fragment every client shares rather than a
 * whole file, because whose file it is decides the wrapper and we would be
 * guessing. The token is real here — this block exists to be pasted, and a
 * placeholder would make it another thing to go and find.
 */
function configBlock(endpoint: string, token: string): string {
  return JSON.stringify(
    {
      mcpServers: {
        tesseract: {
          type: "http",
          url: endpoint,
          headers: { Authorization: `Bearer ${token}` },
        },
      },
    },
    null,
    2,
  );
}

const TOKEN_HIDDEN = "••••••••••••••••";

export function KeysMcp({
  mcp,
  minted,
  busy,
  armed,
  onGenerate,
  onToggle,
}: {
  mcp: McpSurface;
  /** The token this session just minted, if any — the only moment its value
   *  exists in the browser. */
  minted: string | null;
  busy: boolean;
  armed: boolean;
  onGenerate: () => void;
  onToggle: (enabled: boolean) => void;
}) {
  const [showVerbs, setShowVerbs] = useState(false);
  const [copied, setCopied] = useState<string | null>(null);

  const copy = async (what: string, text: string) => {
    await copyToClipboard(text);
    setCopied(what);
    window.setTimeout(() => setCopied((c) => (c === what ? null : c)), 1500);
  };

  if (mcp.error) {
    return (
      <section className="settings-section">
        <Note tone="bad">{mcp.error}</Note>
      </section>
    );
  }

  const client = mcp.client;
  const endpoint = mcp.endpoint;
  const hasToken = Boolean(client?.in_file);
  // The token is readable exactly once, in the response that minted it. So
  // the paste block is complete only in that moment; afterwards it shows the
  // shape with the secret masked, and generating again is how you get a
  // pasteable one back.
  const pasteable = Boolean(endpoint && minted);

  return (
    <section className="settings-section">
      <Note>
        MCP is how another program talks to this one. A client connects to the
        address below and can then search this assistant's memory and vault,
        watch what it is doing, and ask it to act — under the same permission
        rules a person sitting here gets.
      </Note>
      <Note>
        That includes the CLIs in this app's own terminal. Claude and Codex
        opened here reach the assistant through this same address, so turning
        it on is what gives them its memory and vault, and turning it off
        takes that away from them as well as from anything outside. It ships
        off because a door to everything the assistant knows should be opened
        deliberately.
      </Note>

      <div className="cost-row">
        <label className="cost-row__label">Accept connections</label>
        <span className="t-meta">
          {mcp.enabled
            ? "on — restart for it to start serving"
            : "off — nothing can connect, including this app's terminal"}
        </span>
        <span className="cost-row__spend t-meta">
          <Switch
            on={mcp.enabled}
            onToggle={() => onToggle(!mcp.enabled)}
            disabled={busy}
            ariaLabel="accept MCP connections"
          />
        </span>
      </div>

      <div className="cost-row">
        <label className="cost-row__label">Address</label>
        <span className="t-meta">{endpoint ?? "—"}</span>
        <span className="cost-row__spend t-meta">
          {endpoint && (
            <Button onClick={() => void copy("url", endpoint)} tone="primary">
              {copied === "url" ? "Copied" : "Copy"}
            </Button>
          )}
        </span>
      </div>

      <div className="cost-row">
        <label className="cost-row__label">Token</label>
        <span className="t-meta">
          {minted ?? (hasToken ? TOKEN_HIDDEN : "not generated")}
        </span>
        <span className="cost-row__spend t-meta">
          {minted && (
            <>
              <Button onClick={() => void copy("token", minted)} tone="primary">
                {copied === "token" ? "Copied" : "Copy"}
              </Button>{" "}
            </>
          )}
          <Button
            onClick={onGenerate}
            disabled={busy}
            ariaLabel="generate MCP token"
            tone="primary"
          >
            {!hasToken
              ? "Generate"
              : armed
                ? "Disconnect and replace"
                : "Replace"}
          </Button>
          {/* Inline, not a tooltip: this is the sentence that has to be read
              before the second click, and it says the true thing. Replacing
              revokes NOTHING now — the running backend authenticates from the
              environment it booted with, so the old token goes on working and
              the new one does not, until a restart. */}
          {armed && (
            <span className="t-meta">
              {" "}
              the current token is lost — but it keeps working, and the new one
              does not, until you restart
            </span>
          )}
        </span>
      </div>

      {minted ? (
        <Note>
          This is the only time the token is shown. Copy the block below into
          your client's config now — after you leave this screen it cannot be
          read back, only replaced.
        </Note>
      ) : hasToken ? (
        <Note>
          A token is set but cannot be shown — it was displayed once, when it
          was made. Replace it to get a block you can paste.
        </Note>
      ) : null}

      {endpoint && (
        <>
          <div className="cost-row">
            <label className="cost-row__label">Client config</label>
            <span className="t-meta">
              {pasteable ? "paste into your client" : "shape only — no token"}
            </span>
            <span className="cost-row__spend t-meta">
              <Button
                onClick={() =>
                  void copy(
                    "config",
                    configBlock(endpoint, minted ?? "<your token>"),
                  )
                }
                tone="primary"
              >
                {copied === "config" ? "Copied" : "Copy"}
              </Button>
            </span>
          </div>
          <pre className="key-row__config">
            <code>{configBlock(endpoint, minted ?? "<your token>")}</code>
          </pre>
        </>
      )}

      {mcp.verbs.length > 0 && (
        <>
          <div className="cost-row cost-row--actions">
            <Button onClick={() => setShowVerbs((v) => !v)} tone="primary">
              {showVerbs
                ? "Hide"
                : `What a connected client can do (${mcp.verbs.length})`}
            </Button>
          </div>
          {showVerbs &&
            mcp.verbs.map((verb) => (
              <div className="cost-row" key={verb.verb}>
                <label className="cost-row__label">{verb.verb}</label>
                <span className="t-meta">{verb.posture}</span>
                <span className="cost-row__spend t-meta">
                  {verb.posture === "ask" ? "asks you first" : "runs unattended"}
                </span>
              </div>
            ))}
        </>
      )}
    </section>
  );
}

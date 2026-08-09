import { useCallback, useEffect, useState } from "react";

import { Hint } from "../../components/ui/Hint";
import { useEntityName } from "../../hooks/useEntityName";
import { postMode } from "../../lib/api";
import { useIdentityStore } from "../../stores/identity";

// While `securityMode` is unhydrated, retry the identity fetch on this
// cadence. The store only hydrates on WS-open; if that single fetch lands
// during the backend's boot window and fails, the selector showed NO
// active mode forever (fresh-install bug, 2026-07-29).
const MODE_RETRY_MS = 5000;

type Mode = "max" | "standard" | "headless";

const MODES: Array<{ id: Mode; label: string; hint: string }> = [
  { id: "max", label: "Max", hint: "ASK on every tool call" },
  { id: "standard", label: "Standard", hint: "ASK only on risky tools" },
  { id: "headless", label: "Headless", hint: "All tools auto-approved" },
];

const MODE_ORDER: Record<string, number> = { max: 3, standard: 2, headless: 1 };

function isDowngrade(from: string, to: Mode): boolean {
  const f = MODE_ORDER[from] ?? 0;
  const t = MODE_ORDER[to] ?? 0;
  return t < f;
}

export function ModeSection() {
  const entityName = useEntityName();
  const securityMode = useIdentityStore((s) => s.securityMode);
  const [pending, setPending] = useState<Mode | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (securityMode) return;
    const fetch = () => void useIdentityStore.getState().fetchIdentity();
    fetch();
    const id = window.setInterval(fetch, MODE_RETRY_MS);
    return () => window.clearInterval(id);
  }, [securityMode]);

  const confirmDowngrade = useCallback(async () => {
    if (!pending) return;
    setError(null);
    try {
      await postMode(pending);
    } catch (err) {
      setError(err instanceof Error ? err.message : "mode change failed");
    } finally {
      setPending(null);
    }
  }, [pending]);

  const selectMode = useCallback(
    async (next: Mode) => {
      if (next === securityMode) return;
      setError(null);
      if (isDowngrade(securityMode, next)) {
        setPending(next);
        return;
      }
      try {
        await postMode(next);
      } catch (err) {
        setError(err instanceof Error ? err.message : "mode change failed");
      }
    },
    [securityMode],
  );

  return (
    <section className="settings-section">
      <h3 className="settings-section__title">Security mode</h3>
      <div className="mode-toggle" role="radiogroup" aria-label="Security mode">
        {MODES.map((m) => {
          const active = securityMode === m.id;
          return (
            <Hint key={m.id} label={m.hint} position="top" maxWidth={220}>
              <button
                type="button"
                role="radio"
                aria-checked={active}
                className={`mode-option${active ? " mode-option--active" : ""}`}
                onClick={() => selectMode(m.id)}
              >
                {m.label}
              </button>
            </Hint>
          );
        })}
      </div>
      {securityMode === "headless" && (
        <div className="headless-banner" role="status">
          All tool calls auto-approved in headless mode.
        </div>
      )}
      {error && <div className="settings-error">{error}</div>}
      {pending && (
        <div
          className="mode-modal"
          role="dialog"
          aria-modal="true"
          aria-labelledby="mode-modal-title"
        >
          <div className="mode-modal__card">
            <h4 id="mode-modal-title" className="mode-modal__title">
              Lower security mode?
            </h4>
            <p className="mode-modal__body">
              {entityName} will auto-approve more actions in <strong>{pending}</strong>{" "}
              mode.
            </p>
            <div className="mode-modal__actions">
              <button
                type="button"
                className="mode-modal__btn"
                onClick={() => setPending(null)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="mode-modal__btn mode-modal__btn--primary"
                onClick={confirmDowngrade}
              >
                Confirm
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

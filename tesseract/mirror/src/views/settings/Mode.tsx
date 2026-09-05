import { Note } from "../../components/common/Note";
import { useCallback, useEffect, useState } from "react";

import { useEntityName } from "../../hooks/useEntityName";
import { postMode } from "../../lib/api";
import { useIdentityStore } from "../../stores/identity";
import { Block } from '../../components/common/Block';
import { Button } from '../../components/common/Button';
import { Modal } from '../../components/common/Modal';
import { Segmented } from '../../components/common/Segmented';

// While `securityMode` is unhydrated, retry the identity fetch on this
// cadence. The store only hydrates on WS-open; if that single fetch lands
// during the backend's boot window and fails, the selector showed NO
// active mode forever (fresh-install bug, 2026-07-29).
const MODE_RETRY_MS = 5000;

type Mode = "max" | "free";

const MODES: Array<{ id: Mode; label: string; hint: string }> = [
  { id: "max", label: "Max", hint: "Asks before anything that reaches out" },
  { id: "free", label: "Free", hint: "Acts without asking" },
];

const MODE_ORDER: Record<string, number> = { max: 2, free: 1 };

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
    <Block title="Security mode">
      <Segmented
        items={MODES.map((m) => ({ key: m.id, label: m.label, hint: m.hint }))}
        value={securityMode as Mode}
        onSelect={(id) => void selectMode(id)}
        label="Security mode"
      />
      {securityMode === "free" && (
        <Note tone="warn">
          Every tool runs without asking, apart from the few listed under Tools
          as still asking. Denied tools stay denied.
        </Note>
      )}
      {error && <Note tone="bad">{error}</Note>}
      {pending && (
        <Modal
          onClose={() => setPending(null)}
          ariaLabel="lower security mode"
          ariaLabelledBy="mode-modal-title"
          className="confirm-modal"
        >
          <h4 id="mode-modal-title" className="confirm-modal__title">
            Lower security mode?
          </h4>
          <p className="confirm-modal__body">
            {entityName} will auto-approve more actions in <strong>{pending}</strong>{" "}
            mode.
          </p>
          <div className="confirm-modal__actions">
            <Button onClick={() => setPending(null)}>Cancel</Button>
            <Button tone="primary" onClick={confirmDowngrade}>
              Confirm
            </Button>
          </div>
        </Modal>
      )}
    </Block>
  );
}

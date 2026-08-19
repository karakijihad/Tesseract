import { useEffect } from "react";

import {
  selectBrokenRoles,
  useCapabilityNoticeStore,
} from "../../stores/capabilityNotice";
import "./CliAuthNotice.css";
import { Button } from "../common/Button";

// cli-auth DESIGN.md §5 — non-blocking, dismissible first-run notice. Shown
// when any role is broken (primary is an unauthenticated cli provider with
// no covering fallback); the cockpit stays fully usable underneath — this
// is a fixed card, never a scrim/overlay. Self-suppresses permanently once
// no role is broken, whether or not it was ever dismissed.
export function CliAuthNotice() {
  const roles = useCapabilityNoticeStore((s) => s.roles);
  const loaded = useCapabilityNoticeStore((s) => s.loaded);
  const noticeDismissed = useCapabilityNoticeStore((s) => s.noticeDismissed);
  const verifying = useCapabilityNoticeStore((s) => s.verifying);
  const fetchCaps = useCapabilityNoticeStore((s) => s.fetch);
  const verify = useCapabilityNoticeStore((s) => s.verify);
  const dismiss = useCapabilityNoticeStore((s) => s.dismiss);

  useEffect(() => {
    void fetchCaps();
  }, [fetchCaps]);

  if (!loaded) return null;
  const broken = selectBrokenRoles(roles);
  if (broken.length === 0 || noticeDismissed) return null;

  return (
    <div className="cli-auth-notice" role="status" aria-live="polite">
      <div className="cli-auth-notice__title">Sign-in needed</div>
      <ul className="cli-auth-notice__list">
        {broken.map((r) => (
          <li key={r.role} className="cli-auth-notice__item">
            <span className="cli-auth-notice__role">{r.role}</span>
            <span className="cli-auth-notice__hint t-meta">
              {r.login_hint ?? r.reason}
            </span>
          </li>
        ))}
      </ul>
      <div className="cli-auth-notice__actions">
        <Button tone="primary" onClick={() => void verify()} disabled={verifying}>
          {verifying ? "Verifying…" : "Verify"}
        </Button>
        <Button onClick={() => void dismiss()}>Dismiss</Button>
      </div>
    </div>
  );
}

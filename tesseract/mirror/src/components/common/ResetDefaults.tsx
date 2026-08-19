import { useState } from "react";

import { Button } from "./Button";
import { Hint } from "../ui/Hint";
import { useToastStore, type ToastKind } from "../../stores/toasts";

/** What a reset moved. The two routes that perform one return this shape. */
export interface ResetOutcome {
  changed: string[];
  missing: string[];
}

interface Props {
  /** Runs the reset and reports what moved. A pane whose route returns more
   *  than the outcome (Capabilities returns its whole report) applies that
   *  here and hands back the outcome half. */
  run: () => Promise<ResetOutcome>;
  /** Named on the hover label so the button says what it reaches before it is
   *  clicked — "every switch above", "the loop caps". */
  reach: string;
  /** Called after a successful reset, for a pane that re-fetches its own
   *  state rather than being handed it back. */
  onDone?: () => void;
}

/** "Reset to defaults", on every pane that has one.
 *
 *  A shared act, so a component rather than five panes each remembering to
 *  say "Resetting…" while it runs and to report that nothing moved when
 *  nothing did — a reset that flashes success on an already-default install
 *  reads as a broken button.
 */
export function ResetDefaults({ run, reach, onDone }: Props) {
  const [busy, setBusy] = useState(false);
  // Read at click time rather than subscribed: this button has nothing to
  // re-render when a toast elsewhere comes or goes.
  const push = (message: string, kind: ToastKind) =>
    useToastStore.getState().push(message, kind);

  const onClick = async () => {
    setBusy(true);
    try {
      const { changed, missing } = await run();
      push(
        changed.length === 0
          ? "Already at the shipped defaults — nothing changed."
          : `Reset ${changed.length} setting${changed.length === 1 ? "" : "s"}: ${changed.join(", ")}.`,
        "info",
      );
      if (missing.length > 0) {
        // Not an error: the key is absent from this install's config and
        // `migrate_config_keys` adds it at the next boot. Saying so beats a
        // silent partial reset.
        push(`Not in your config, so left alone: ${missing.join(", ")}`, "warning");
      }
      onDone?.();
    } catch (err) {
      push(err instanceof Error ? err.message : "reset failed", "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Hint
      label={`Put ${reach} back to what this version of TESSERACT ships with. Nothing else in the file is touched.`}
      maxWidth={360}
    >
      <Button onClick={() => void onClick()} disabled={busy}>
        {busy ? "Resetting…" : "Reset to defaults"}
      </Button>
    </Hint>
  );
}

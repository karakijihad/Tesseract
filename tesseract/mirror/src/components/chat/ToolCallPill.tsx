import { useState } from "react";
import type { ToolCall, ToolCallStatus, ToolResult } from "../../lib/types";
import { useConversationStore } from "../../stores/conversation";
import { linkifyText } from "../../lib/linkify";
import { DelegateCard } from "./DelegateCard";
import { ControllerMirrorBlock } from "./ControllerMirrorBlock";
import { DownloadPreview, isDownloadUrl } from "./DownloadPreview";
import { Disclosure } from "../common/Disclosure";

interface Props {
  call: ToolCall;
  result?: ToolResult;
}

const RESULT_TRUNCATE_AT = 500;
const DELEGATE_TOOLS = new Set(["delegate_coder", "delegate_auditor"]);

const STATUS_CLASS: Record<ToolCallStatus, string> = {
  auto: "tool-pill-badge--ok",
  pending: "tool-pill-badge--warn",
  parked: "tool-pill-badge--warn",
  approved: "tool-pill-badge--ok",
  denied: "tool-pill-badge--bad",
  hard_denied: "tool-pill-badge--shield",
};

const STATUS_LABEL: Record<ToolCallStatus, string> = {
  auto: "auto",
  pending: "pending",
  parked: "parked",
  approved: "approved",
  denied: "denied",
  hard_denied: "BLOCKED",
};

// A `denied` status covers two events the backend keeps apart and this
// badge used to flatten: the operator said no, and the prompt ran out with
// nobody answering. The envelope names the second; reading it here is what
// stops the row claiming a decision was made.
const DENIED_REASON_LABEL: Record<string, string> = {
  timeout: "expired",
  park_timeout: "expired",
  turn_cancelled: "cancelled",
};

function badgeLabel(status: ToolCallStatus, reason?: string): string {
  if (status === "denied" && reason) {
    return DENIED_REASON_LABEL[reason] ?? STATUS_LABEL.denied;
  }
  return STATUS_LABEL[status];
}

export function ToolCallPill({ call, result }: Props) {
  const [open, setOpen] = useState(false);
  const [expandResult, setExpandResult] = useState(false);
  const [showHardReason, setShowHardReason] = useState(false);
  const statusEntry = useConversationStore((s) =>
    s.getActiveSlice()?.toolStatus.get(call.call_id),
  );
  const hasCliStream = useConversationStore(
    (s) => s.getActiveSlice()?.cliStreams.has(call.call_id) ?? false,
  );
  const inputStr = JSON.stringify(call.input);

  const resultOutput = result?.output ?? "";
  const resultIsDownload = !result?.is_error && isDownloadUrl(resultOutput);
  const resultTooLong =
    !resultIsDownload && resultOutput.length > RESULT_TRUNCATE_AT;
  const visibleResult =
    resultTooLong && !expandResult
      ? resultOutput.slice(0, RESULT_TRUNCATE_AT)
      : resultOutput;

  const status = statusEntry?.status;
  const isDelegateStream = DELEGATE_TOOLS.has(call.name) && hasCliStream;
  const controllerMeta =
    call.name === "start_controller_session" &&
    result?.metadata?.kind === "child_transcript_ref"
      ? (result.metadata as { session_id: string; ws_path: string })
      : null;
  const isHardDenied = status === "hard_denied";
  const hasArgs =
    call.input != null &&
    typeof call.input === "object" &&
    Object.keys(call.input as Record<string, unknown>).length > 0;

  return (
    <div className="tool-call-pill">
      <Disclosure
        variant="row"
        open={open}
        onToggle={() => setOpen((o) => !o)}
        className="tool-pill-header"
        ariaLabel={`${call.name} call detail`}
      >
        <span className="tool-pill-name">{call.name}</span>
        {status && (
          <span className={`tool-pill-badge ${STATUS_CLASS[status]}`}>
            {badgeLabel(status, statusEntry?.reason)}
          </span>
        )}
        {!open && hasArgs && (
          <span className="tool-pill-input-truncated">{inputStr}</span>
        )}
        <span className="tool-pill-caret t-meta">{open ? "▲" : "▼"}</span>
      </Disclosure>
      {open && (
        <div className="tool-pill-body">
          {hasArgs ? (
            <pre>input: {JSON.stringify(call.input, null, 2)}</pre>
          ) : (
            <pre className="t-meta">no args</pre>
          )}
          {isHardDenied && statusEntry?.reason && (
            <div className="tool-pill-hard-reason">
              <Disclosure
                open={showHardReason}
                onToggle={(e) => {
                  e.stopPropagation();
                  setShowHardReason((v) => !v);
                }}
              >
                {showHardReason ? "Hide block reason" : "Show block reason"}
              </Disclosure>
              {showHardReason && (
                <pre className="tool-pill-hard-reason-text">
                  {statusEntry.reason}
                </pre>
              )}
            </div>
          )}
          {isDelegateStream && <DelegateCard call_id={call.call_id} />}
          {result &&
            !isDelegateStream &&
            !controllerMeta &&
            !resultIsDownload && (
              <pre
                style={{
                  marginTop: 8,
                  borderTop: "1px solid var(--border)",
                  paddingTop: 8,
                }}
              >
                {result.is_error ? "⚠ " : ""}output:{" "}
                {linkifyText(visibleResult)}
                {resultTooLong && !expandResult && "…"}
              </pre>
            )}
          {resultTooLong && !isDelegateStream && !controllerMeta && (
            <Disclosure
              open={expandResult}
              onToggle={(e) => {
                e.stopPropagation();
                setExpandResult((v) => !v);
              }}
            >
              {expandResult
                ? "Collapse output"
                : `Expand output (${resultOutput.length - RESULT_TRUNCATE_AT} more chars)`}
            </Disclosure>
          )}
        </div>
      )}
      {/* X-2 — controller-card surfaces independent of the tool-pill
          expansion gate: a controller session must stay visible in Mirror
          even when the pill itself is collapsed. */}
      {controllerMeta && (
        <ControllerMirrorBlock
          session_id={controllerMeta.session_id}
          ws_path={controllerMeta.ws_path}
        />
      )}
      {result && !isDelegateStream && !controllerMeta && resultIsDownload && (
        <div className="tool-pill-download">
          <DownloadPreview url={resultOutput} />
        </div>
      )}
    </div>
  );
}

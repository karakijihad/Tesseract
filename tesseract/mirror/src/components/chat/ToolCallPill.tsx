import { useState } from "react";
import type { ToolCall, ToolCallStatus, ToolResult } from "../../lib/types";
import { useConversationStore } from "../../stores/conversation";
import { linkifyText } from "../../lib/linkify";
import { DelegateCard } from "./DelegateCard";
import { ControllerMirrorBlock } from "./ControllerMirrorBlock";
import { DownloadPreview, isDownloadUrl } from "./DownloadPreview";

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
      <div className="tool-pill-header" onClick={() => setOpen((o) => !o)}>
        <span className="tool-pill-name">{call.name}</span>
        {status && (
          <span className={`tool-pill-badge ${STATUS_CLASS[status]}`}>
            {STATUS_LABEL[status]}
          </span>
        )}
        {!open && hasArgs && (
          <span className="tool-pill-input-truncated">{inputStr}</span>
        )}
        <span
          style={{ marginLeft: "auto", color: "var(--text-meta)", fontSize: 10 }}
        >
          {open ? "▲" : "▼"}
        </span>
      </div>
      {open && (
        <div className="tool-pill-body">
          {hasArgs ? (
            <pre>input: {JSON.stringify(call.input, null, 2)}</pre>
          ) : (
            <pre className="t-meta">no args</pre>
          )}
          {isHardDenied && statusEntry?.reason && (
            <div className="tool-pill-hard-reason">
              <button
                type="button"
                className="tool-pill-expand-btn"
                onClick={(e) => {
                  e.stopPropagation();
                  setShowHardReason((v) => !v);
                }}
              >
                {showHardReason ? "Hide block reason" : "Show block reason"}
              </button>
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
            <button
              type="button"
              className="tool-pill-expand-btn"
              onClick={(e) => {
                e.stopPropagation();
                setExpandResult((v) => !v);
              }}
            >
              {expandResult
                ? "Collapse output"
                : `Expand output (${resultOutput.length - RESULT_TRUNCATE_AT} more chars)`}
            </button>
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

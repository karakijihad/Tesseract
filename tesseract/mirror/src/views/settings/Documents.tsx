import { Block } from "../../components/common/Block";
import { Note } from "../../components/common/Note";
import { Switch } from "../../components/common/Switch";
import { sendCommand } from "../../lib/commands";
import { useCachedFetch } from "../../lib/useCachedFetch";
import {
  fetchWorkspaceDocuments,
  type WorkspaceDocumentLine,
  type WorkspaceDocumentsResponse,
} from "../../lib/api";

/** The tool that changes which documents are held back, by name. The same one
 *  a channel sends, so this switch and a sentence said on a phone reach one
 *  implementation and one gate. */
const HOLD = "/workspace_hold";

/** What the backend calls this reading when it says one is out of date. The
 *  other half of `orchestrator/panel_refresh.py::WORKSPACE_DOCUMENTS_KEY`, and
 *  what lets the switch show what is in force after the gate is answered
 *  rather than what was asked for. */
const KEY = "settings.workspace-documents";

function said(row: WorkspaceDocumentLine): string {
  if (row.posture === "deny") return "a change is refused";
  if (row.posture === "ask") {
    return row.heldBack
      ? "held back. A change waits for you, whatever the mode says"
      : "a change waits for you";
  }
  return "a change is applied, and filed for you to read";
}

/** Which of your own documents a change has to be asked about.
 *
 * The security mode above answers for all six at once, and that was too
 * blunt: under a mode that acts without asking, the rules the assistant works
 * by were rewritten as freely as a line was added to a diary. The switch here
 * names the exceptions, and an exception only ever holds a document back. A
 * document you let go still follows the mode, so nothing here can hand out
 * more than the mode already does.
 *
 * **What it shows after a switch moves is what is in force, never what was
 * asked.** The tool is gated, so the change happens when you answer the gate
 * and can be refused there. A row reading "held back" while the runtime is
 * applying changes to it is a control reporting a result it never had.
 */
export function DocumentsSection() {
  const { data, error } = useCachedFetch<WorkspaceDocumentsResponse>(
    KEY,
    fetchWorkspaceDocuments,
  );

  return (
    <Block title="Your own documents">
      <Note>
        The assistant never writes these files directly. It proposes a change
        and the workspace inbox settles it. What the security mode decides is
        whether a proposal waits for you or is applied and filed for you to
        read afterwards.
      </Note>
      {error && <Note tone="bad">{error}</Note>}
      {data?.documents.map((row) => (
        <div className="cost-row" key={row.name}>
          <label className="cost-row__label">{row.name}</label>
          <span className="t-meta">{said(row)}</span>
          <span className="cost-row__spend t-meta">
            <Switch
              on={row.heldBack}
              onToggle={() =>
                sendCommand(
                  HOLD,
                  ` document=${row.name} must_ask=${!row.heldBack}`,
                )
              }
              ariaLabel={`Always ask before changing ${row.name}`}
            />
          </span>
        </div>
      ))}
    </Block>
  );
}

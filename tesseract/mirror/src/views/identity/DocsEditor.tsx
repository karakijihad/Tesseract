import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Note } from "../../components/common/Note";

import { useToastStore } from "../../stores/toasts";
import { useWebSocketStore } from "../../stores/websocket";
import { useFetchRetryTick } from "../../lib/useFetchRetry";
import { ApiError } from "../../lib/api";
import {
  fetchWorkspaceDoc,
  fetchWorkspaceDocs,
  saveWorkspaceDoc,
  type WorkspaceDocRow,
} from "../../lib/api";
import { Button } from "../../components/common/Button";
import { Tabs, type TabItem } from "../../components/common/Tabs";
import { Textarea } from "../../components/common/Textarea";

interface Conflict {
  theirs: string;
  hash: string;
  diff: string;
}

/** The operator writing the workspace documents directly.
 *
 * Same allowlist the assistant proposes against, same atomic commit — the
 * operator path skips the proposal card, not the machinery. `hash` is the
 * concurrency token: a save whose hash no longer matches disk means an
 * approved proposal (or an external editor) landed underneath, and the
 * editor shows their bytes rather than letting the operator clobber them.
 */
export function DocsEditor() {
  const [rows, setRows] = useState<WorkspaceDocRow[] | null>(null);
  const [openPath, setOpenPath] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [baseline, setBaseline] = useState("");
  const [hash, setHash] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [conflict, setConflict] = useState<Conflict | null>(null);
  const [error, setError] = useState<string | null>(null);

  const dirty = openPath !== null && draft !== baseline;
  // A ref alongside the state so the `beforeunload` listener reads the
  // live value instead of the one captured when it was installed.
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;

  const wsGeneration = useWebSocketStore((s) => s.generation);
  const retryTick = useFetchRetryTick(error !== null);
  const refreshList = useCallback(() => {
    setError(null);
    fetchWorkspaceDocs()
      .then((res) => setRows(res.docs))
      .catch((err) =>
        setError(err instanceof Error ? err.message : String(err)),
      );
  }, []);
  useEffect(refreshList, [refreshList, wsGeneration, retryTick]);

  // Closing the window mid-edit is the one exit this component can't
  // intercept with its own prompt.
  useEffect(() => {
    const guard = (e: BeforeUnloadEvent) => {
      if (!dirtyRef.current) return;
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, []);

  const open = async (path: string) => {
    if (path === openPath) return;
    if (dirty && !window.confirm("Discard unsaved changes to this document?")) {
      return;
    }
    // A document the workspace does not have is still a tab, because its
    // absence is a fact worth showing. Selecting it moves the tab and says
    // so; reading it would 404.
    if (rows?.find((r) => r.path === path)?.exists === false) {
      setOpenPath(path);
      setDraft("");
      setBaseline("");
      setHash("");
      setConflict(null);
      setError(null);
      return;
    }
    setLoading(true);
    setError(null);
    setConflict(null);
    try {
      const doc = await fetchWorkspaceDoc(path);
      setOpenPath(doc.path);
      setDraft(doc.content);
      setBaseline(doc.content);
      setHash(doc.hash);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  const save = async () => {
    if (!openPath || !dirty) return;
    setSaving(true);
    setError(null);
    try {
      const res = await saveWorkspaceDoc({
        path: openPath,
        content: draft,
        expected_hash: hash,
      });
      setBaseline(draft);
      setHash(res.hash);
      setConflict(null);
      refreshList();
      useToastStore
        .getState()
        .push(
          res.no_op_reason
            ? `${res.label}: no change to save`
            : `${res.label} saved`,
        );
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        const payload = err.payload ?? {};
        setConflict({
          theirs: String(payload.current_content ?? ""),
          hash: String(payload.actual_hash ?? ""),
          diff: String(payload.diff ?? ""),
        });
      } else {
        setError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setSaving(false);
    }
  };

  const takeTheirs = () => {
    if (!conflict) return;
    setDraft(conflict.theirs);
    setBaseline(conflict.theirs);
    setHash(conflict.hash);
    setConflict(null);
    refreshList();
  };

  const rebaseOnTheirs = () => {
    if (!conflict) return;
    // Keep the operator's text, adopt their hash — the next save then
    // commits deliberately on top of what landed, having seen the diff.
    setBaseline(conflict.theirs);
    setHash(conflict.hash);
    setConflict(null);
  };

  const openRow = rows?.find((r) => r.path === openPath) ?? null;

  // The tab strip IS the document list. A row that is missing keeps its tab
  // and says so on it, so the set of documents reads the same whether or not
  // this workspace has all of them.
  const tabs: TabItem[] = useMemo(
    () =>
      (rows ?? []).map((r) => ({
        key: r.path,
        label: r.label,
        badge: r.exists ? r.lines : "missing",
      })),
    [rows],
  );

  return (
    <div className="docs-editor">
      {error && <Note tone="bad">{error}</Note>}

      {tabs.length > 0 && (
        <Tabs
          items={tabs}
          active={openPath ?? ""}
          onSelect={(path) => void open(path)}
          label="Workspace documents"
        />
      )}

      <div className="docs-editor-pane">
        {openPath === null ? (
          <div className="t-meta docs-editor-empty">
            {rows === null
              ? "Loading…"
              : rows.length === 0
                ? "There are no editable documents."
                : "Pick a document above to read or edit it."}
          </div>
        ) : openRow?.exists === false ? (
          <Note tone="warn">
            This workspace does not have {openRow.label} yet. It is written the
            first time something needs it.
          </Note>
        ) : (
          <>
            <div className="docs-editor-pane-head">
              <span className="docs-editor-open-label">
                {openRow?.label ?? openPath}
              </span>
              <span className="t-meta docs-editor-path">{openPath}</span>
              {dirty && (
                <span className="t-meta docs-editor-dirty">unsaved</span>
              )}
            </div>

            {conflict && (
              <div className="docs-editor-conflict">
                <div className="docs-editor-conflict-head">
                  This document changed while you were editing it. An
                  approved proposal or an outside edit landed first.
                </div>
                {conflict.diff && (
                  <pre className="docs-editor-diff">{conflict.diff}</pre>
                )}
                <div className="identity-actions">
                  <Button
                    onClick={takeTheirs}
                  >
                    discard mine, load theirs
                  </Button>
                  <Button
                    onClick={rebaseOnTheirs}
                  >
                    keep mine, save over theirs
                  </Button>
                </div>
              </div>
            )}

            <Textarea
              className="docs-editor-text"
              value={draft}
              spellCheck={false}
              disabled={loading || saving}
              ariaLabel="Document body"
              onChange={setDraft}
            />

            <div className="identity-actions">
              <Button
                onClick={() => void save()}
                disabled={!dirty || saving || conflict !== null}
              >
                {saving ? "saving…" : "save"}
              </Button>
              <Button
                onClick={() => setDraft(baseline)}
                disabled={!dirty || saving}
              >
                revert
              </Button>
              {conflict !== null && (
                <span className="t-meta">
                  Resolve the conflict above before saving.
                </span>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

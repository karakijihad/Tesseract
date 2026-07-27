import { useEffect, useState } from 'react';
import { useAgentsStore } from '../../../stores/agents';
import {
  fetchAgentSource,
  saveAgentSource,
  toggleAgentDisabled,
} from '../../../lib/api';

export function AgentDetail() {
  const detail = useAgentsStore((s) => s.detail);
  const detailLoading = useAgentsStore((s) => s.detailLoading);
  const selectedName = useAgentsStore((s) => s.selectedName);
  const fetchAll = useAgentsStore((s) => s.fetchAll);
  const selectAgent = useAgentsStore((s) => s.selectAgent);

  const [editing, setEditing] = useState(false);
  const [source, setSource] = useState('');
  const [sourceLoading, setSourceLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [toggling, setToggling] = useState(false);
  const [statusMsg, setStatusMsg] = useState<string>('');

  useEffect(() => {
    setEditing(false);
    setSource('');
    setStatusMsg('');
  }, [selectedName]);

  if (!selectedName) {
    return (
      <div className="agent-detail agent-detail-placeholder t-meta">
        Select an agent to inspect its frontmatter and sections.
      </div>
    );
  }

  if (detailLoading || !detail) {
    return (
      <div className="agent-detail t-meta">
        {detailLoading ? `Loading ${selectedName}…` : 'No agent loaded.'}
      </div>
    );
  }

  const onEdit = async () => {
    setSourceLoading(true);
    setStatusMsg('');
    try {
      const res = await fetchAgentSource(selectedName);
      setSource(res.source);
      setEditing(true);
    } catch (err) {
      setStatusMsg(err instanceof Error ? err.message : String(err));
    } finally {
      setSourceLoading(false);
    }
  };

  const onSave = async () => {
    setSaving(true);
    setStatusMsg('');
    try {
      await saveAgentSource(selectedName, source);
      setEditing(false);
      setStatusMsg('saved');
      await Promise.all([fetchAll(), selectAgent(selectedName)]);
    } catch (err) {
      setStatusMsg(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  const onCancel = () => {
    setEditing(false);
    setSource('');
    setStatusMsg('');
  };

  const onToggleDisabled = async () => {
    setToggling(true);
    setStatusMsg('');
    try {
      await toggleAgentDisabled(selectedName, !detail.disabled);
      await Promise.all([fetchAll(), selectAgent(selectedName)]);
      setStatusMsg(detail.disabled ? 'enabled' : 'disabled');
    } catch (err) {
      setStatusMsg(err instanceof Error ? err.message : String(err));
    } finally {
      setToggling(false);
    }
  };

  const sectionEntries = Object.entries(detail.sections);

  return (
    <div className="agent-detail">
      <header className="agent-detail-head">
        <h2 className="agent-detail-name">{detail.name}</h2>
        <span className={`agent-status-badge is-${detail.status}`}>{detail.status}</span>
        {detail.version && (
          <span className="agent-detail-version t-meta">v{detail.version}</span>
        )}
        {detail.disabled && (
          <span className="agent-status-badge is-disabled">disabled</span>
        )}
        <div className="agent-detail-actions">
          <button
            type="button"
            className="agent-detail-btn"
            onClick={onToggleDisabled}
            disabled={toggling}
          >
            {toggling ? '…' : detail.disabled ? 'enable' : 'disable'}
          </button>
          {!editing && (
            <button
              type="button"
              className="agent-detail-btn"
              onClick={onEdit}
              disabled={sourceLoading}
            >
              {sourceLoading ? 'loading…' : 'edit'}
            </button>
          )}
          {editing && (
            <>
              <button
                type="button"
                className="agent-detail-btn agent-detail-btn-primary"
                onClick={onSave}
                disabled={saving}
              >
                {saving ? 'saving…' : 'save'}
              </button>
              <button
                type="button"
                className="agent-detail-btn"
                onClick={onCancel}
                disabled={saving}
              >
                cancel
              </button>
            </>
          )}
        </div>
      </header>

      {statusMsg && (
        <div className="agent-detail-status t-meta">{statusMsg}</div>
      )}

      {editing ? (
        <textarea
          className="agent-detail-editor"
          value={source}
          onChange={(e) => setSource(e.target.value)}
          spellCheck={false}
          aria-label={`Edit agent ${selectedName}`}
        />
      ) : (
        <>
          {detail.description && (
            <p className="agent-detail-description">{detail.description}</p>
          )}

          <dl className="agent-detail-meta">
            <div className="agent-detail-meta-row">
              <dt className="t-meta">model_role</dt>
              <dd>{detail.model_role}</dd>
            </div>
            {detail.resolved_ref && (
              <div className="agent-detail-meta-row">
                <dt className="t-meta">resolved_ref</dt>
                <dd>{detail.resolved_ref}</dd>
              </div>
            )}
            {detail.max_tokens_override !== null && (
              <div className="agent-detail-meta-row">
                <dt className="t-meta">max_tokens</dt>
                <dd>{detail.max_tokens_override}</dd>
              </div>
            )}
            {detail.tools && detail.tools.length > 0 && (
              <div className="agent-detail-meta-row">
                <dt className="t-meta">tools</dt>
                <dd className="agent-detail-tools">
                  {detail.tools.map((t) => (
                    <span key={t} className="agent-tool-pill">{t}</span>
                  ))}
                </dd>
              </div>
            )}
          </dl>

          {sectionEntries.length > 0 && (
            <div className="agent-detail-sections">
              {sectionEntries.map(([heading, body]) => (
                <section key={heading} className="agent-detail-section">
                  <h3 className="agent-detail-section-title">{heading}</h3>
                  <pre className="agent-detail-section-body">{body}</pre>
                </section>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}

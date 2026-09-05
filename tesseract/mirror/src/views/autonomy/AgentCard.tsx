// One agent, opened. Level 3 in Managed system.
//
// The same act the entry card performs for a scheduled row: what it is for,
// what it rides, what it may reach, and its own text. **Every word is the
// card's**, read from the markdown the agent is defined in, which is the
// contract the agent format has always held to.
//
// It replaces `views/agents/components/AgentDetail.tsx`, deleted with the
// Agents tab. What changed is where it renders and where it gets its name:
// it takes one rather than reading a selection out of a store, because a
// level knows what it opened.

import { useCallback, useEffect, useState } from 'react';
import { Block } from '../../components/common/Block';
import { Button } from '../../components/common/Button';
import { Note } from '../../components/common/Note';
import { Textarea } from '../../components/common/Textarea';
import {
  fetchAgent,
  fetchAgentSource,
  saveAgentSource,
  toggleAgentDisabled,
} from '../../lib/api';
import type { AgentDetail } from '../../lib/api';
import { useAutonomyStore } from '../../stores/autonomy';
import { Band } from '../../components/common/StateStrip';

function Fact({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}): React.ReactElement {
  return (
    <div className="entry-fact">
      <span className="entry-fact__label t-meta t-label">{label}</span>
      <span className="entry-fact__value">{children}</span>
    </div>
  );
}

export function AgentCardView({
  card,
  editing,
  source,
  busy,
  notice,
  onSource,
  onEdit,
  onSave,
  onCancel,
  onToggle,
}: {
  card: AgentDetail;
  editing: boolean;
  source: string;
  busy: boolean;
  notice: string;
  onSource: (next: string) => void;
  onEdit: () => void;
  onSave: () => void;
  onCancel: () => void;
  onToggle: () => void;
}): React.ReactElement {
  const sections = Object.entries(card.sections);
  return (
    <div className="entry-card">
      <p className="entry-card__summary">
        {card.description || 'Its card says nothing about what it is for.'}
      </p>

      <div className="managed-head">
        <Button onClick={onToggle} disabled={busy} ariaLabel={`${card.disabled ? 'Turn on' : 'Turn off'} ${card.name}`}>
          {card.disabled ? 'turn on' : 'turn off'}
        </Button>
        {editing ? (
          <>
            <Button onClick={onSave} disabled={busy} ariaLabel={`Save ${card.name}`}>
              save
            </Button>
            <Button onClick={onCancel} disabled={busy} ariaLabel="stop editing">
              cancel
            </Button>
          </>
        ) : (
          <Button onClick={onEdit} disabled={busy} ariaLabel={`Edit ${card.name}`}>
            edit
          </Button>
        )}
      </div>

      {notice && <Note>{notice}</Note>}
      {card.shadows_system && !notice && (
        <Note>
          This is your own copy of a card the app ships, so it no longer
          follows app updates. Delete it to go back to the shipped one.
        </Note>
      )}

      {editing ? (
        <Textarea
          className="agent-card__editor"
          value={source}
          onChange={onSource}
          spellCheck={false}
          ariaLabel={`Edit ${card.name}`}
        />
      ) : (
        <>
          <Band label="What it is" />
          <div className="entry-card__facts">
            <Fact label="Runs on">{card.model_role}</Fact>
            <Fact label="Written by">
              {card.origin === 'system' ? 'the app' : 'you'}
            </Fact>
            {card.resolved_ref && <Fact label="Which is">{card.resolved_ref}</Fact>}
            {card.version && <Fact label="Version">{card.version}</Fact>}
            {card.max_tokens_override !== null && (
              <Fact label="Most it may write">{card.max_tokens_override}</Fact>
            )}
            <Fact label="Tools">
              {card.tools === null
                ? 'everything the assistant carries'
                : card.tools.join(', ') || 'none'}
            </Fact>
          </div>

          {sections.length > 0 && (
            <div className="agent-card__sections">
              {sections.map(([heading, body]) => (
                <Block key={heading} title={heading}>
                  <pre className="agent-card__section">{body}</pre>
                </Block>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}

export function AgentCard({ name }: { name: string }): React.ReactElement {
  const [card, setCard] = useState<AgentDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [source, setSource] = useState('');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const reread = useAutonomyStore((s) => s.rereadManagedRoom);

  const read = useCallback(async () => {
    try {
      setCard(await fetchAgent(name));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [name]);

  useEffect(() => {
    setCard(null);
    setError(null);
    setEditing(false);
    setNotice('');
    void read();
  }, [name, read]);

  const guard = async (what: string, run: () => Promise<void>) => {
    setBusy(true);
    setNotice('');
    try {
      await run();
    } catch (err) {
      setNotice(`${what}: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setBusy(false);
    }
  };

  if (error) return <Note tone="bad">{error}</Note>;
  if (!card) return <></>;

  return (
    <AgentCardView
      card={card}
      editing={editing}
      source={source}
      busy={busy}
      notice={notice}
      onSource={setSource}
      onEdit={() =>
        void guard('It could not be opened', async () => {
          const res = await fetchAgentSource(name);
          setSource(res.source);
          setEditing(true);
        })
      }
      onSave={() =>
        void guard('It could not be saved', async () => {
          const res = await saveAgentSource(name, source);
          setEditing(false);
          await read();
          void reread();
          // The backend says when an edit forks a shipped card, and that is
          // the one thing the operator has to be told at the moment it
          // happens: from now on it stops receiving what an update brings.
          if (res.notice) setNotice(res.notice);
        })
      }
      onCancel={() => {
        setEditing(false);
        setSource('');
      }}
      onToggle={() =>
        // The sentence names what was asked for. The button's label already
        // reads it off the same field.
        void guard(`It could not be turned ${card.disabled ? 'on' : 'off'}`, async () => {
          await toggleAgentDisabled(name, !card.disabled);
          await read();
          void reread();
        })
      }
    />
  );
}

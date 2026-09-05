// What one record on the map says, beside the picture.
//
// Two tabs, because one column carrying both was too tight when this was
// drawn: **the record**, and **what it touches**. The record opens the way
// Obsidian opens one, its own fields first in the order the file carries them
// and then the body, so somebody who never installs Obsidian sees what
// Obsidian would show them. That is the point of putting the map in the Mirror
// at all.
//
// **Nothing here says anything about the record.** The fields, their order,
// the body, and every reason there is nothing to show arrive with the payload.
// What this file writes is about the app: which tab is open, and that a field
// the record left blank is empty.

import { useEffect, useState } from 'react';

import { Button } from '../../components/common/Button';
import { Markdown } from '../../components/common/Markdown';
import { Note } from '../../components/common/Note';
import { Row } from '../../components/common/Row';
import { Tabs } from '../../components/common/Tabs';
import type { GraphEdge, GraphRecord, GraphResponse } from '../../lib/api';

/** One link, from the point of view of the record being looked at. */
interface Touch {
  edge: GraphEdge;
  /** The record at the other end, when the picture holds it. */
  otherId: string;
  otherTitle: string;
  crosses: boolean;
}

export function touchesOf(data: GraphResponse, id: string): Touch[] {
  const titles = new Map(data.nodes.map((n) => [n.id, n.title || n.id]));
  const out: Touch[] = [];
  for (const edge of data.edges) {
    if (edge.subject !== id && edge.object !== id) continue;
    const otherId = edge.subject === id ? edge.object : edge.subject;
    out.push({
      edge,
      otherId,
      otherTitle: titles.get(otherId) ?? otherId,
      crosses: edge.crosses,
    });
  }
  return out;
}

export function Inspector({
  data,
  record,
  status,
  error,
  selected,
  onOpen,
  onStartHere,
  onPin,
  pinned,
}: {
  data: GraphResponse;
  record: GraphRecord | null;
  status: 'idle' | 'loading' | 'ready' | 'error';
  error: string | null;
  selected: string;
  /** Move the picture to another record. */
  onOpen: (id: string) => void;
  /** Draw the picture around this one instead, and take the view to it. */
  onStartHere: (id: string) => void;
  /** Keep it on the picture whatever else is shown, or stop. */
  onPin: (id: string) => void;
  pinned: boolean;
}): React.ReactElement {
  const [tab, setTab] = useState<'record' | 'touches'>('record');
  const touches = touchesOf(data, selected);

  // A different record opens on its own tab, not on whichever one was left
  // open: clicking a memory after reading another one's links would otherwise
  // land on a list rather than on the thing just clicked.
  useEffect(() => setTab('record'), [selected]);

  const node = data.nodes.find((n) => n.id === selected);

  return (
    <aside className="graph-inspector" data-testid="graph-inspector">
      <span className="t-meta t-label">What this is</span>
      <h2 className="graph-inspector__name">
        {record?.title || node?.title || selected}
      </h2>
      <p className="graph-inspector__where t-meta">
        {record?.locator || node?.locator}
      </p>

      {/* The two things you can do to the picture from a record: make it the
          thing the picture is drawn around, or hold it there while you look at
          something else. How far out the picture then follows the links is one
          control over the picture, above it, rather than a second answer
          hidden in here. */}
      <div className="graph-inspector__acts">
        <Button
          onClick={() => onStartHere(selected)}
          ariaLabel="draw the picture around this record"
        >
          start from this one
        </Button>
        <Button onClick={() => onPin(selected)}>
          {pinned ? 'stop keeping it in the picture' : 'keep it in the picture'}
        </Button>
      </div>

      <Tabs
        label="What this record shows"
        items={[
          { key: 'record' as const, label: 'The record' },
          {
            key: 'touches' as const,
            label: 'What it touches',
            badge: touches.length,
          },
        ]}
        active={tab}
        onSelect={setTab}
      />

      {tab === 'record' ? (
        status === 'error' ? (
          <Note tone="bad">The record could not be read. {error}</Note>
        ) : status === 'loading' || record === null ? (
          <p className="t-meta">Reading it.</p>
        ) : (
          <>
            {record.said && <Note>{record.said}</Note>}
            {record.properties.length > 0 && (
              <dl className="graph-props">
                {record.properties.map((field) => (
                  <div key={field.key} className="graph-props__row">
                    <dt className="graph-props__key t-meta">{field.key}</dt>
                    <dd
                      className={`graph-props__value${
                        field.value ? '' : ' graph-props__value--empty'
                      }`}
                    >
                      {field.value || 'Empty'}
                    </dd>
                  </div>
                ))}
              </dl>
            )}
            {record.body &&
              (record.bodyIsMarkdown ? (
                <Markdown>{record.body}</Markdown>
              ) : (
                // Machine text. Markdown must NOT be interpreted here: a run's
                // own payload is a structure, and rendering it as prose would
                // turn its punctuation into headings.
                <pre className="graph-raw">{record.body}</pre>
              ))}
            {record.truncated && (
              <p className="t-meta">
                Only the beginning is shown. The rest is in the file above.
              </p>
            )}
          </>
        )
      ) : touches.length === 0 ? (
        <p className="t-meta">
          Nothing on the picture connects to this one. Searching for its words
          still finds it.
        </p>
      ) : (
        <div className="graph-touches">
          {touches.map((touch) => (
            <Row
              key={touch.edge.id}
              className="graph-touch"
              onClick={() => onOpen(touch.otherId)}
              ariaLabel={`Open ${touch.otherTitle}`}
            >
              <span className="graph-touch__name">{touch.otherTitle}</span>
              <span className="graph-touch__how t-meta">
                {said(data, touch.edge.provenance)}
                {touch.crosses && ', across compartments'}
              </span>
              {/* The exact span or line that supports it. An edge that cannot
                  point at what supports it cannot be checked, which is the
                  whole reason the atlas records one. */}
              <span className="graph-touch__where t-meta">
                {touch.edge.locator}
              </span>
            </Row>
          ))}
        </div>
      )}
    </aside>
  );
}

/** Where a connection came from, in the backend's words. An unknown class
 *  falls back to what the payload sent rather than to a phrase this file made
 *  up: printing the raw key is honest, and inventing one is not. */
function said(data: GraphResponse, provenance: string): string {
  return (
    data.provenance.find((word) => word.key === provenance)?.label ?? provenance
  );
}

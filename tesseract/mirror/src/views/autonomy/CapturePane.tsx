// What may become a memory.
//
// The capture funnel's admission rules, each with what it blocks, why it
// exists, whether it is on, which of the three layers decided that, and how
// much it has actually turned away. The last of those is the point: a dial
// with no gauge gets set once and never moved, and a rule that has blocked
// nothing in a month is a rule to question.
//
// **This file authors nothing about any rule.** The sentence, the reason, the
// state and the word for that state all arrive in the payload, and a rule that
// cannot be switched off arrives carrying `locked` so no control is drawn for
// it. A view that decided would draw a switch the write then refuses, which is
// the rule Managed system and Channels already follow.

import { useEffect, useState } from 'react';
import { Button } from '../../components/common/Button';
import { Note } from '../../components/common/Note';
import { RowActions } from '../../components/common/Row';
import { Band, StateStrip, type StateLine } from '../../components/common/StateStrip';
import { Hint } from '../../components/ui/Hint';
import {
  ApiError,
  fetchCapturePolicy,
  postCaptureRule,
  type CapturePolicyResponse,
  type CaptureRuleRow,
} from '../../lib/api';
import { useToastStore } from '../../stores/toasts';
import { useWebSocketStore } from '../../stores/websocket';

/** What the pane says while it has nothing. The band is drawn either way, so
 *  the room does not change shape when the read lands. */
const NOT_READ_YET = 'The capture rules have not been read yet.';

/** What is on this tab, in one line, before the first row. */
const INTRO = 'Every rule that decides what may become a memory, and whether it is on.';

/** Turning a rule off is not a display preference: it lets whatever that rule
 *  blocks through into memory instead of stopping it. The rule's own reason,
 *  its enforcement point and its numbers ride the row itself (`why`,
 *  `enforced_by`, `detail`); this only says what the button does. */
const TURN_OFF_SAYS =
  'Lets whatever this rule blocks through into memory instead of stopping it.';
const TURN_ON_SAYS =
  'Starts blocking what this rule matches again, before it can become a memory.';

export function CapturePaneView({
  data,
  status,
  error,
  busy,
  onToggle,
}: {
  data: CapturePolicyResponse | null;
  status: 'idle' | 'loading' | 'ready' | 'error';
  error: string | null;
  /** The rule a write is in flight for, so only its own control is held. */
  busy: string | null;
  onToggle: (rule: string, enabled: boolean) => void;
}): React.ReactElement {
  if (status === 'error') {
    return (
      <div className="autonomy-group">
        <Band label="What may become a memory" />
        <p className="t-meta">{INTRO}</p>
        <Note tone="bad">The capture rules could not be read. {error}</Note>
      </div>
    );
  }
  if (status !== 'ready' || data === null) {
    return (
      <div className="autonomy-group">
        <Band label="What may become a memory" />
        <p className="t-meta">{INTRO}</p>
        <Note>{NOT_READ_YET}</Note>
      </div>
    );
  }

  const lines: StateLine[] = data.rules.map((rule: CaptureRuleRow) => ({
    key: `capture:${rule.rule}`,
    state: rule.state,
    obligation: rule.obligation,
    label: rule.label,
    name: rule.rule,
    said: rule.summary,
    value: String(rule.blocked),
    when: rule.layer === 'runtime' ? 'you set this' : undefined,
    actions: rule.locked ? undefined : (
      <RowActions>
        <Hint label={rule.enabled ? TURN_OFF_SAYS : TURN_ON_SAYS}>
          <Button
            disabled={busy === rule.rule}
            onClick={() => onToggle(rule.rule, !rule.enabled)}
          >
            {rule.enabled ? 'Turn off' : 'Turn on'}
          </Button>
        </Hint>
      </RowActions>
    ),
    more: (
      <p className="t-meta">
        {rule.why}
        {rule.detail ? ` ${rule.detail}` : ''}
        {rule.enforced_by ? ` It is enforced at ${rule.enforced_by}.` : ''}
      </p>
    ),
  }));

  return (
    <div className="autonomy-group">
      <Band label="What may become a memory" count={lines.length} />
      <p className="t-meta">{INTRO}</p>
      <StateStrip lines={lines} whole />
      <p className="t-meta">
        The number beside each rule is what it stopped over the last{' '}
        {data.window_days} days.
      </p>
    </div>
  );
}

export function CapturePane(): React.ReactElement {
  const [data, setData] = useState<CapturePolicyResponse | null>(null);
  const [status, setStatus] = useState<'idle' | 'loading' | 'ready' | 'error'>(
    'idle',
  );
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  // Held here rather than in the autonomy store, which the rail reads: no rail
  // mark is drawn from these rules, so putting them there would fetch them on
  // every panel load to answer a question nobody asked.
  const read = (): void => {
    setStatus((was) => (was === 'ready' ? was : 'loading'));
    void fetchCapturePolicy()
      .then((res) => {
        setData(res);
        setStatus('ready');
        setError(null);
      })
      .catch((err: unknown) => {
        setError(err instanceof ApiError ? err.message : String(err));
        setStatus('error');
      });
  };

  useEffect(read, []);

  return (
    <CapturePaneView
      data={data}
      status={status}
      error={error}
      busy={busy}
      onToggle={(rule, enabled) => {
        const sid = useWebSocketStore.getState().sessionId;
        if (!sid) {
          useToastStore.getState().push('No session id. Connect first.', 'error');
          return;
        }
        setBusy(rule);
        void postCaptureRule({ session_id: sid, rule, enabled })
          .then((row) => {
            // The write answers with the rule's whole row, so the pane shows
            // what the backend decided rather than what was asked for. A rule
            // held at its floor comes back on, and the line says so.
            setData((was) =>
              was === null
                ? was
                : { ...was, rules: was.rules.map((r) => (r.rule === row.rule ? row : r)) },
            );
          })
          .catch((err: unknown) => {
            useToastStore
              .getState()
              .push(err instanceof ApiError ? err.message : String(err), 'error');
          })
          .finally(() => setBusy(null));
      }}
    />
  );
}

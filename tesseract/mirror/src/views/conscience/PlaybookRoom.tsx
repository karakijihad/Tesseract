import { useState, type ReactNode } from 'react';

import { DataTable, type DataColumn, type DataRow } from '../../components/common/DataTable';
import { Markdown } from '../../components/common/Markdown';
import { Note } from '../../components/common/Note';
import { Row } from '../../components/common/Row';
import { Hint } from '../../components/ui/Hint';
import type {
  PlaybookProcedure,
  PlaybookRevisionRow,
  PlaybookUsageRow,
} from '../../stores/conscience';

/** The Playbooks room: every playbook in a list, and the one selected shown
 * beside it with what its numbers mean and what it says.
 *
 * It replaces a bar chart whose figures were right and unreadable. "Those
 * turns made 247 tool calls and cost $0.6322" did not say whose calls they
 * were or what the sum meant, and the steps themselves were nowhere on the
 * page. Here every figure is said in a sentence or carries a hint saying what
 * it counts, and the procedure is rendered field by field.
 *
 * Calls and cost stay the TURN's, never the playbook's. `turnScopeNote` is the
 * backend's one wording of that and is read here rather than restated.
 */
export function PlaybookRoom({
  rows,
  days,
  carriedCount,
  path,
  turnScopeNote,
}: {
  rows: PlaybookUsageRow[];
  days: number;
  carriedCount: number;
  /** The file the every-turn list lives in. */
  path: string;
  turnScopeNote: string;
}) {
  const [chosen, setChosen] = useState('');
  const current = rows.find((r) => r.playbook === chosen) ?? rows[0];
  const read = rows.filter((r) => r.loads > 0).length;

  return (
    <div className="playbook-room">
      <p className="playbook-room__lead">
        {read} of {rows.length} playbooks were read in the last {days} days.{' '}
        {carriedCount} of them arrive on every turn with a line saying when to
        use them.
      </p>
      <div className="playbook-room__split">
        <ul className="playbook-room__list" aria-label="Playbooks">
          {/* A plain `li` around the Row: the Row is a button, and putting that
              role on the `li` itself would take the list away from a screen
              reader. Its name is its own text, so the read count is heard too. */}
          {rows.map((r) => (
            <li key={r.playbook}>
              <Row
                className="playbook-room__item"
                current={r === current}
                onClick={() => setChosen(r.playbook)}
              >
                <span className="playbook-room__name">{r.playbook}</span>
                <span className="t-meta">{listLine(r)}</span>
              </Row>
            </li>
          ))}
        </ul>
        {current ? (
          <PlaybookDetail row={current} days={days} turnScopeNote={turnScopeNote} />
        ) : null}
      </div>
      <p className="t-meta">Which playbooks arrive on every turn is set in {path}.</p>
    </div>
  );
}

/** What a row carries when the backend serving it predates the `skill` block:
 * the room still opens, and the procedure reads as not written down rather
 * than taking the section down with it. */
const NO_PROCEDURE: PlaybookProcedure = {
  path: '',
  trigger: '',
  use_when: '',
  not_when: '',
  preconditions: [],
  steps: [],
  expected_result: '',
  failure_modes: [],
  cannot_run: '',
};

function PlaybookDetail({
  row: given,
  days,
  turnScopeNote,
}: {
  row: PlaybookUsageRow;
  days: number;
  turnScopeNote: string;
}) {
  const row = given.skill ? given : { ...given, skill: NO_PROCEDURE };
  return (
    <section className="playbook-room__detail" aria-label={row.playbook}>
      <div className="playbook-room__head">
        <span className="playbook-room__title">{row.playbook}</span>
        <span className="t-meta">
          Version {row.version || 'not set'}.{' '}
          {row.carried
            ? 'Arrives on every turn.'
            : 'Looked up when a task seems to fit it.'}
        </span>
        {row.description ? <Markdown variant="inline">{row.description}</Markdown> : null}
      </div>

      {row.skill.cannot_run ? (
        <Note tone="bad">
          <Markdown variant="inline">
            {`It cannot be used until this is fixed: ${row.skill.cannot_run}.`}
          </Markdown>
        </Note>
      ) : null}

      <span className="playbook-room__section t-ui">What the numbers say</span>
      <p className="playbook-room__summary">{whatTheNumbersSay(row, days)}</p>
      {row.loads > 0 ? (
        <DataTable
          label={`How ${row.playbook} has done, by version`}
          columns={columns(turnScopeNote)}
          rows={versionRows(row)}
        />
      ) : null}

      <span className="playbook-room__section t-ui">What it says</span>
      <Procedure skill={row.skill} />
    </section>
  );
}

function Procedure({ skill }: { skill: PlaybookProcedure }) {
  return (
    <>
      <dl className="playbook-room__procedure">
        <Field term="When to use it" text={skill.use_when} />
        <Field term="When not to" text={skill.not_when} />
        <Field term="What sets it off" text={skill.trigger} />
        <Items term="Before it starts" items={skill.preconditions} />
        <dt>Steps</dt>
        <dd>
          {skill.steps.length === 0 ? (
            <span className="t-meta">No steps written down yet.</span>
          ) : (
            <ol className="playbook-room__steps">
              {skill.steps.map((step, i) => (
                <li key={i}>
                  <Markdown variant="inline">{step.do}</Markdown>
                  <span className="t-meta playbook-room__tool">
                    {step.tool ? (
                      <Markdown variant="inline">{`uses \`${step.tool}\``}</Markdown>
                    ) : (
                      'no tool'
                    )}
                  </span>
                </li>
              ))}
            </ol>
          )}
        </dd>
        <Field term="Done looks like" text={skill.expected_result} />
        <Items term="Known ways it goes wrong" items={skill.failure_modes} />
      </dl>
      <p className="t-meta">Kept in {skill.path}.</p>
    </>
  );
}

function Field({ term, text }: { term: string; text: string }) {
  return (
    <>
      <dt>{term}</dt>
      <dd>
        {text ? (
          <Markdown variant="inline">{text}</Markdown>
        ) : (
          <span className="t-meta">Not written down.</span>
        )}
      </dd>
    </>
  );
}

function Items({ term, items }: { term: string; items: string[] }) {
  return (
    <>
      <dt>{term}</dt>
      <dd>
        {items.length === 0 ? (
          <span className="t-meta">None written down.</span>
        ) : (
          <ul className="playbook-room__bullets">
            {items.map((item, i) => (
              <li key={i}>
                <Markdown variant="inline">{item}</Markdown>
              </li>
            ))}
          </ul>
        )}
      </dd>
    </>
  );
}

function listLine(r: PlaybookUsageRow): string {
  const reads = r.loads === 0 ? 'not read' : `${r.loads} ${r.loads === 1 ? 'read' : 'reads'}`;
  return r.carried ? reads : `${reads}, looked up when needed`;
}

/** Every figure the table carries, said as sentences a person can act on. */
export function whatTheNumbersSay(r: PlaybookUsageRow, days: number): string {
  if (r.loads === 0) {
    return r.carried
      ? `Not read in the last ${days} days. It still arrives on every turn, so it takes room in every request without being used.`
      : `Not read in the last ${days} days. It is only looked up when a task seems to fit it, so it costs nothing while unused.`;
  }
  const said = [
    `Read ${times(r.loads)} in the last ${days} days, across ${count(r.turns, 'turn')}. A turn is one message and everything the assistant did to answer it.`,
  ];
  const checked = r.succeeded + r.failed;
  if (checked > 0) {
    said.push(
      r.failed === 0
        ? `${checked === 1 ? 'The one turn' : `All ${checked} turns`} whose task your project checked finished it.`
        : `${r.succeeded} of the ${checked} turns whose task your project checked finished it, and ${r.failed} did not.`,
    );
  }
  const open = r.unjoined + r.ungraded;
  if (open > 0) {
    said.push(
      `${count(open, 'turn')} ${open === 1 ? 'has' : 'have'} no verdict yet: still open, or closed without your project's checks behind ${open === 1 ? 'it, so it counts' : 'them, so they count'} as neither.`,
    );
  }
  if (r.corrections > 0) {
    said.push(`The assistant reported that following it went wrong ${times(r.corrections)}.`);
  }
  if (r.retries > 0) {
    said.push(
      `It was opened again partway through a task ${times(r.retries)}, which usually means the steps did not carry the work.`,
    );
  }
  said.push(
    r.turn_cost_usd === null
      ? `Those turns made ${count(r.turn_calls, 'tool call')}. What they cost was not recorded.`
      : `Those turns made ${count(r.turn_calls, 'tool call')} and spent ${money(r.turn_cost_usd)} on models. That covers everything those turns did, not only the work this playbook describes, so it is the most this playbook could have cost rather than what it did cost.`,
  );
  const compared = comparison(r);
  if (compared) said.push(compared);
  return said.join(' ');
}

function comparison(r: PlaybookUsageRow): string {
  if (!r.previous_version) return '';
  const now = `version ${r.version}`;
  const before = `version ${r.previous_version}`;
  return {
    unknown: `It is too early to tell whether ${now} does better than ${before}, because it has barely been read.`,
    same: `Version ${r.version} is doing the same as ${before}.`,
    better: `Version ${r.version} is doing better than ${before}: fewer of its turns ran into trouble.`,
    worse: `Version ${r.version} is doing worse than ${before}: more of its turns ran into trouble.`,
  }[r.comparison];
}

function heading(label: string, hint: string): ReactNode {
  return (
    <Hint label={hint} maxWidth={320}>
      <span className="playbook-room__heading">{label}</span>
    </Hint>
  );
}

function columns(turnScopeNote: string): DataColumn[] {
  return [
    { label: heading('version', 'Which revision of the playbook. The one in use now is marked live.'), width: '5.5rem' },
    { label: heading('reads', 'How many times its file was opened in this window.'), width: '3.5rem' },
    {
      label: heading(
        'finished',
        "Of the turns whose task your project checked, how many finished it. Turns still open, or closed on the assistant's word alone, are left out.",
      ),
      width: '5rem',
    },
    { label: heading('went wrong', 'How many times the assistant reported that following it went wrong.'), width: '5rem' },
    {
      label: heading(
        'read again',
        'How many times it was opened a second time partway through a task, which usually means the steps did not carry the work.',
      ),
      width: '5rem',
    },
    { label: heading('tool calls', `Every tool call made in the turns that read it. ${turnScopeNote}`), width: '5rem' },
    { label: heading('cost', `What those turns spent on models. ${turnScopeNote}`), width: 'minmax(0, 1fr)' },
  ];
}

type Counts = Pick<
  PlaybookRevisionRow,
  'loads' | 'succeeded' | 'failed' | 'corrections' | 'retries' | 'turns' | 'turn_calls' | 'turn_cost_usd'
>;

function versionRows(row: PlaybookUsageRow): DataRow[] {
  const rows = [...row.revisions].reverse().map((rev) => ({
    key: rev.version,
    cells: cells(rev.version === row.version ? `v${rev.version}, live` : `v${rev.version}`, rev),
  }));
  if (row.revisions.length > 1) rows.push({ key: 'all', cells: cells('all versions', row) });
  return rows;
}

function cells(label: string, c: Counts): ReactNode[] {
  const checked = c.succeeded + c.failed;
  return [
    label,
    String(c.loads),
    checked > 0 ? `${c.succeeded} of ${checked}` : '—',
    String(c.corrections),
    String(c.retries),
    String(c.turn_calls),
    // Three answers: nothing read it, it was not measured, or what it cost.
    c.turns === 0 ? '—' : c.turn_cost_usd === null ? 'not recorded' : money(c.turn_cost_usd),
  ];
}

function money(usd: number): string {
  return usd > 0 && usd < 0.01 ? 'under $0.01' : `$${usd.toFixed(2)}`;
}

function times(n: number): string {
  return n === 1 ? 'once' : n === 2 ? 'twice' : `${n} times`;
}

function count(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? '' : 's'}`;
}

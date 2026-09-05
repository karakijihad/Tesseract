import { useMemo, useState } from 'react';

import { Note } from '../../components/common/Note';
import { Segmented } from '../../components/common/Segmented';
import { Switch } from '../../components/common/Switch';
import { Hint } from '../../components/ui/Hint';
import type { WorkingSetRow } from '../../stores/conscience';

type Filter = 'all' | 'carried' | 'idle';

/** Which tools are described on every turn, and the switch that changes it.
 *
 * The list and the usage are one panel because the decision is an
 * intersection: the tools worth dropping are the ones being described every
 * turn AND never reached for. Two panels would make the operator hold one
 * half in their head while they read the other.
 *
 * Grouped by the tool's own group, in the order the assistant is shown them,
 * so the panel reads the way the prompt does. A flat list of 130 names is a
 * search box with no search.
 */
export function WorkingSetPanel({
  rows,
  groups,
  coreCount,
  total,
  path,
  customPath,
  saving,
  onToggle,
}: {
  rows: WorkingSetRow[];
  groups: { slug: string; label: string }[];
  coreCount: number;
  total: number;
  path: string;
  customPath: string;
  saving: string | null;
  onToggle: (tool: string, core: boolean) => void;
}) {
  const [filter, setFilter] = useState<Filter>('all');

  const idle = useMemo(
    () => rows.filter((r) => r.core && r.calls_30d === 0),
    [rows],
  );

  const shown = useMemo(() => {
    if (filter === 'carried') return rows.filter((r) => r.core);
    if (filter === 'idle') return idle;
    return rows;
  }, [rows, idle, filter]);

  const byGroup = useMemo(() => {
    const order = groups.map((g) => g.slug);
    const buckets = new Map<string, WorkingSetRow[]>();
    for (const row of shown) {
      const bucket = buckets.get(row.group);
      if (bucket) bucket.push(row);
      else buckets.set(row.group, [row]);
    }
    return [...buckets.entries()].sort(
      ([a], [b]) => order.indexOf(a) - order.indexOf(b),
    );
  }, [shown, groups]);

  return (
    <figure className="working-set">
      <div className="working-set__head">
        <p className="working-set__count">
          <strong>{coreCount}</strong> of {total} tools are described on every
          turn
        </p>
        <Segmented
          label="Which tools to list"
          value={filter}
          onSelect={setFilter}
          items={[
            { key: 'all' as Filter, label: 'all', hint: 'Every tool it has' },
            {
              key: 'carried' as Filter,
              label: 'carried',
              hint: 'Only the ones described on every turn',
            },
            {
              key: 'idle' as Filter,
              label: 'idle',
              hint: 'Carried every turn, and not called in 30 days',
            },
          ]}
        />
      </div>

      <p className="t-meta working-set__lead">
        Every tool stays callable. Switching one off only means the assistant
        has to look it up first, which costs a step and nothing else. Whether
        it may run a tool, and whether it has to ask you first, is a separate
        setting in Settings, under Tools. Nothing here changes it.
      </p>

      {idle.length > 0 && filter !== 'idle' && (
        <Note>
          {idle.length} {idle.length === 1 ? 'tool is' : 'tools are'} described
          on every turn and {idle.length === 1 ? 'has' : 'have'} not been called
          in 30 days. Those are the ones to switch off first.
        </Note>
      )}

      {shown.length === 0 ? (
        <p className="t-meta">Nothing matches this filter.</p>
      ) : (
        byGroup.map(([slug, groupRows]) => (
          <section key={slug} className="working-set__group">
            <h4 className="working-set__group-name t-meta">
              {groupRows[0].group_label}
            </h4>
            <ul className="working-set__rows">
              {groupRows.map((row) => (
                <li key={row.tool} className="working-set__row">
                  {row.locked ? (
                    <Hint label="This is how the assistant reaches everything it is not carrying. Without it, it can only reach what is on the list.">
                      <Switch
                        on
                        disabled
                        onToggle={() => {}}
                        ariaLabel={`${row.tool} is always described, and cannot be switched off`}
                      />
                    </Hint>
                  ) : (
                    <Switch
                      on={row.core}
                      disabled={saving === row.tool}
                      onToggle={() => onToggle(row.tool, !row.core)}
                      ariaLabel={`describe ${row.tool} on every turn`}
                    />
                  )}
                  <span className="working-set__name">
                    {row.tool}
                    {row.origin === 'custom' && (
                      <Hint label="you wrote this one. It is not part of TESSERACT and deleting the file removes it">
                        <span className="working-set__tag t-meta">custom</span>
                      </Hint>
                    )}
                  </span>
                  <span className="working-set__summary t-meta">
                    {row.summary}
                  </span>
                  <span className="working-set__calls t-meta">
                    {row.calls_30d === 0
                      ? 'not called'
                      : `${row.calls_30d} ${row.calls_30d === 1 ? 'call' : 'calls'}`}
                  </span>
                </li>
              ))}
            </ul>
          </section>
        ))
      )}

      <figcaption className="working-set__caption t-meta">
        Calls are the last 30 days. A switch takes effect on the next turn.
        Tools that came with TESSERACT are written to {path}; the ones you
        wrote are written to {customPath}. You can edit either by hand.
      </figcaption>
    </figure>
  );
}

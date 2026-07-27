import { useMemo, useState } from 'react';
import { useTasksStore, type TaskItem } from '../../stores/tasks';

const STATUS_GLYPH: Record<TaskItem['status'], string> = {
  pending: '▢',     // ☐
  in_progress: '▣', // ▣
  completed: '☑',   // ☑
};

const STATUS_CLASS: Record<TaskItem['status'], string> = {
  pending: 'is-pending',
  in_progress: 'is-active',
  completed: 'is-done',
};

export function TodosCard() {
  const items = useTasksStore(s => s.items);
  const [showCompleted, setShowCompleted] = useState(false);

  const { activeOrPending, completed } = useMemo(() => {
    const active: TaskItem[] = [];
    const done: TaskItem[] = [];
    for (const t of items) {
      if (t.status === 'completed') done.push(t);
      else active.push(t);
    }
    return { activeOrPending: active, completed: done };
  }, [items]);

  if (items.length === 0) return null;

  const visible = showCompleted ? items : activeOrPending;
  const hiddenCount = showCompleted ? 0 : completed.length;

  return (
    <div className="todos-card" role="list" aria-label="TARS task checklist">
      {visible.map(item => (
        <div
          key={item.id}
          className={`todos-card-row ${STATUS_CLASS[item.status]}`}
          role="listitem"
          aria-current={item.status === 'in_progress' ? 'step' : undefined}
        >
          <span className="todos-card-glyph" aria-hidden="true">
            {STATUS_GLYPH[item.status]}
          </span>
          <span className="todos-card-title">{item.title}</span>
        </div>
      ))}
      {hiddenCount > 0 && (
        <button
          type="button"
          className="todos-card-toggle"
          onClick={() => setShowCompleted(true)}
        >
          … +{hiddenCount} completed
        </button>
      )}
      {showCompleted && completed.length > 0 && (
        <button
          type="button"
          className="todos-card-toggle"
          onClick={() => setShowCompleted(false)}
        >
          collapse completed
        </button>
      )}
    </div>
  );
}

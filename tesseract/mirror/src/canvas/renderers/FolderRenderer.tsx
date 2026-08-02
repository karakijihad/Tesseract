// Folder surface. Renders a path's entries as a flat file-tree.
// `props.root` is the folder path; `props.entries` (optional) is the
// directory listing the tool supplied — `{name, kind}` rows or bare name
// strings. It renders what the descriptor carries; it does not list the
// filesystem itself. Entries are inert — see the row comment below.

import type { RendererProps } from './index';

interface Entry {
  name: string;
  kind?: 'dir' | 'file';
}

function normalizeEntries(raw: unknown): Entry[] {
  if (!Array.isArray(raw)) return [];
  return raw.map((e) =>
    typeof e === 'string' ? { name: e } : (e as Entry),
  );
}

export function FolderRenderer({ descriptor }: RendererProps) {
  const root = String(descriptor.props?.root ?? '');
  const entries = normalizeEntries(descriptor.props?.entries);
  return (
    <div className="surface-folder">
      <div className="surface-folder__root t-meta" title={root}>
        {root || '(no path)'}
      </div>
      {entries.length === 0 ? (
        <div className="surface-folder__empty t-meta">no entries</div>
      ) : (
        <ul className="surface-folder__list">
          {entries.map((e) => (
            <li key={e.name}>
              {/* Plain row, not a button: `SurfaceLayer` passes a no-op
                  dispatch, so a click went nowhere. A control that looks
                  interactive and does nothing is worse than a list. Wiring
                  this to `open` is the follow-up (Docs/Deferred.md). */}
              <span className="surface-folder__entry">
                <span className="surface-folder__icon" aria-hidden="true">
                  {e.kind === 'dir' ? '▸' : '·'}
                </span>
                {e.name}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

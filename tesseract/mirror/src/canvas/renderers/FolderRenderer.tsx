// Folder surface. Renders a path's entries as a flat file-tree.
// `props.root` is the folder path; `props.entries` (optional) is the
// directory listing the tool supplied — `{name, kind}` rows or bare name
// strings. It renders what the descriptor carries; it does not list the
// filesystem itself.
//
// A row emits `clicked` with the joined path, which `SurfaceLayer` sends to
// the `open` verb over the chat WS. `open` decides what the entry is: a
// subfolder or a renderable file becomes another card, anything the cockpit
// cannot draw goes to the owning application through `os_launch`'s ASK. The
// row does not decide, and must not — it has no idea which it is.

import type { RendererProps } from './index';
import { Hint } from '../../components/ui/Hint';
import { MenuItem } from '../../components/common/MenuItem';

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

// Join without assuming a separator: the root arrives from the backend, so it
// is whatever that platform uses. A root already ending in one is left alone
// rather than doubled.
function joinPath(root: string, name: string): string {
  if (!root) return name;
  const sep = root.includes('\\') && !root.includes('/') ? '\\' : '/';
  return /[\\/]$/.test(root) ? `${root}${name}` : `${root}${sep}${name}`;
}

export function FolderRenderer({ descriptor, dispatch }: RendererProps) {
  const root = String(descriptor.props?.root ?? '');
  const entries = normalizeEntries(descriptor.props?.entries);
  return (
    <div className="surface-folder">
      <Hint label={root}>
        <div className="surface-folder__root t-meta">
          {root || '(no path)'}
        </div>
      </Hint>
      {entries.length === 0 ? (
        <div className="surface-folder__empty t-meta">no entries</div>
      ) : (
        <ul className="surface-folder__list">
          {entries.map((e) => (
            <li key={e.name}>
              <Hint label={joinPath(root, e.name)}>
                <MenuItem
                  // The card is draggable; without this a click-to-open would
                  // also start a drag and the row would fight the card.
                  onPointerDown={(ev) => ev.stopPropagation()}
                  onClick={() => dispatch('clicked', { target: joinPath(root, e.name) })}
                >
                  <span className="surface-folder__icon" aria-hidden="true">
                    {e.kind === 'dir' ? '▸' : '·'}
                  </span>
                  {e.name}
                </MenuItem>
              </Hint>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

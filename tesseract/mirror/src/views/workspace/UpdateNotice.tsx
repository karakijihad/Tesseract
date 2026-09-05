import { useEffect, useState } from 'react';

import { Button } from '../../components/common/Button';
import { Note } from '../../components/common/Note';
import { BACKEND_BASE } from '../../lib/endpoints';

interface Replaced {
  files: string[];
  backup_dir: string;
  version: string;
  at: string;
}

/** What the last update replaced, until the operator says they have read it.
 *
 * The same event already arrives as a toast on the first connection after an
 * update. A toast is a moment, and this one asks for work: an operator whose
 * models, voice, permissions and schedule are back at the release's values has
 * several panes to put back, and cannot do it from a message that faded while
 * they were reading the first line.
 *
 * It lives in the Workspace because the Workspace is where things wait for
 * the operator. It is not a `WorkspaceEvent`: those are threads between the
 * operator and the assistant, and nobody is asking the assistant anything
 * here.
 */
export function UpdateNotice() {
  const [replaced, setReplaced] = useState<Replaced | null>(null);
  const [dismissing, setDismissing] = useState(false);

  useEffect(() => {
    let live = true;
    fetch(`${BACKEND_BASE}/api/runtime/config-replaced`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (live && data?.files?.length) setReplaced(data as Replaced);
      })
      // Silent: an install that has never been updated is the common case and
      // reads the same as a failed read. Nothing here is worth a red box on a
      // tab the operator opened to do something else.
      .catch(() => {});
    return () => {
      live = false;
    };
  }, []);

  if (!replaced) return null;

  const dismiss = async () => {
    setDismissing(true);
    try {
      await fetch(`${BACKEND_BASE}/api/runtime/config-replaced/dismiss`, {
        method: 'POST',
      });
      setReplaced(null);
    } catch {
      setDismissing(false);
    }
  };

  const count = replaced.files.length;

  return (
    <Note tone="warn" className="update-notice">
      <p className="update-notice__lead">
        This update replaced {count} of your settings{' '}
        {count === 1 ? 'file' : 'files'}
        {replaced.version ? `, on the way to version ${replaced.version}` : ''}
        {replaced.at ? ` (${formatWhen(replaced.at)})` : ''}.
      </p>
      <ul className="update-notice__files">
        {replaced.files.map((name) => (
          <li key={name}>{name}</li>
        ))}
      </ul>
      <p>
        Anything you had changed in them is back at the values this release
        ships, so it is worth a look through Settings, and through Conscience
        if you had trimmed which tools the assistant carries. Your previous
        copies are kept in {replaced.backup_dir}, and the next update that
        replaces these files will overwrite them.
      </p>
      <div className="update-notice__actions">
        <Button onClick={() => void dismiss()} disabled={dismissing}>
          {dismissing ? '…' : 'got it'}
        </Button>
      </div>
    </Note>
  );
}

/** The day, in the reader's own locale. The stamp carries a time and a zone
 *  because it is a machine record; what a person is reading here is which
 *  launch this was. */
function formatWhen(iso: string): string {
  const when = new Date(iso);
  return Number.isNaN(when.getTime())
    ? iso
    : when.toLocaleDateString([], { month: 'long', day: 'numeric' });
}

import { useEffect, useMemo } from 'react';
import { Button } from '../components/common/Button';
import { RailView, type RailGroup } from '../components/common/RailView';
import { useSoulStore } from '../stores/soul';
import { sendCommand } from '../lib/commands';
import { formatRelative } from '../lib/time';
import { parseSoul } from '../lib/soul';
import { Hint } from '../components/ui/Hint';
import { IdentityCard } from './identity/IdentityCard';
import { DocsEditor } from './identity/DocsEditor';

/** Who it is — the name, the voice, the documents, and SOUL.md.
 *
 * AS-5 folded the read-only Soul tab into this one. The SOUL.md cards and
 * `/reflect` stayed put: what it has grown into belongs beside what it was
 * configured as.
 */
export function IdentityView() {
  const content = useSoulStore((s) => s.content);
  const lastReflectedAt = useSoulStore((s) => s.lastReflectedAt);
  const fetchSoul = useSoulStore((s) => s.fetchSoul);

  // Refresh when the operator opens the tab so the chip + blocks reflect
  // any external SOUL.md edits made while a different view was active.
  useEffect(() => {
    fetchSoul();
  }, [fetchSoul]);

  // Kept for the section count in the head; SOUL.md itself is edited under
  // Documents, where it is the first row (`PROPOSABLE_PATHS`).
  const blocks = useMemo(() => parseSoul(content), [content]);

  const groups: RailGroup[] = [
    {
      label: 'Who it is',
      sections: [
        { key: 'identity', label: 'Identity', Body: IdentityCard },
        { key: 'documents', label: 'Documents', Body: DocsEditor },
      ],
    },
  ];

  return (
    <RailView
      groups={groups}
      label="Identity sections"
      meta={`Last reflected: ${formatRelative(lastReflectedAt)} · ${blocks.length} soul sections`}
      actions={
        <Hint label="Run /reflect — re-read SOUL.md and update memory synthesis" position="bottom" maxWidth={260}>
          <Button onClick={() => sendCommand('/reflect')} ariaLabel="run /reflect">
            refresh
          </Button>
        </Hint>
      }
    />
  );
}

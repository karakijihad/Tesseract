import { sendCommand } from '../../lib/commands';
import { useUIStore } from '../../stores/ui';
import { useResetDialogStore } from '../../stores/resetDialog';
import { Hint } from '../ui/Hint';
import { Chip } from '../common/Chip';

interface Tip {
  cmd: string;
  description: string;
  opensDrawer?: boolean;
  opensResetDialog?: boolean;
}

const TIPS: Tip[] = [
  { cmd: '/sessions', description: 'open the sessions drawer', opensDrawer: true },
  { cmd: '/save',     description: 'save current session (add a name to fork)' },
  { cmd: '/reset',    description: 'clear chat (asks: reflect first?)', opensResetDialog: true },
  { cmd: '/compact',  description: 'summarize + trim history' },
  { cmd: '/stats',    description: 'show tokens · turns · compact threshold' },
];

export function CommandTips() {
  const setDrawerOpen = useUIStore((s) => s.setDrawerOpen);
  const openResetDialog = useResetDialogStore((s) => s.openDialog);

  const run = (tip: Tip) => {
    if (tip.opensResetDialog) {
      openResetDialog();
      return;
    }
    sendCommand(tip.cmd);
    if (tip.opensDrawer) setDrawerOpen(true);
  };

  return (
    <div className="command-tips" aria-label="Available commands">
      <div className="command-tips-title">Commands</div>
      <ul className="command-tips-list">
        {TIPS.map((t) => (
          <li key={t.cmd}>
            <Hint label={`Run ${t.cmd}`} position="bottom">
              <Chip onClick={() => run(t)}>{t.cmd}</Chip>
            </Hint>
            <span className="command-tips-desc">— {t.description}</span>
          </li>
        ))}
      </ul>
      <div className="command-tips-footer">
        Type <code>/</code> in the message box to autocomplete, or
        quote like <code>"/sessions"</code> to send as plain text.
      </div>
    </div>
  );
}

import { useState, useRef, useCallback } from 'react';
import { useTerminalStore } from '../../stores/terminal';
import { fetchRecordings } from '../api';

function writeToPane(text: string): void {
  const { focusedPaneId, _terms } = useTerminalStore.getState();
  if (!focusedPaneId) return;
  const term = _terms.get(focusedPaneId);
  if (term) term.write(`\r\n${text}\r\n`);
}

async function handleCommand(input: string): Promise<void> {
  const parts = input.trim().split(/\s+/);
  const cmd = parts[0]?.toLowerCase();

  if (cmd === '/help') {
    writeToPane([
      '── Commands ──',
      '  /theme list             — Show available themes',
      '  /theme use <name>       — Switch active theme',
      '  /record new [shell]     — Open a new recorded terminal tab',
      '  /recordings list        — List saved recordings',
      '  /recordings play <id>   — Replay a recording inside Mirror',
      '  /help                   — Show this help',
    ].join('\r\n'));
    return;
  }

  if (cmd === '/record') {
    const sub = parts[1]?.toLowerCase();
    if (sub === 'new') {
      const shell = parts[2];
      useTerminalStore.getState().addRecordedTab(shell);
      writeToPane(`Recorded tab opened${shell ? ` (${shell})` : ''}. File appears in /recordings list after the pane is stopped.`);
      return;
    }
    writeToPane('Usage: /record new [shell]');
    return;
  }

  if (cmd === '/recordings') {
    const sub = parts[1]?.toLowerCase();
    if (!sub || sub === 'list') {
      try {
        const items = await fetchRecordings();
        if (items.length === 0) {
          writeToPane('No recordings yet. Use /record new to start one.');
        } else {
          const rows = items.map((r) => {
            const kb = (r.size / 1024).toFixed(1);
            const when = new Date(r.modified * 1000).toLocaleString();
            return `  ${r.id}  ${kb}K  ${when}`;
          });
          writeToPane('── Recordings ──\r\n' + rows.join('\r\n'));
        }
      } catch (e) {
        writeToPane(`Error listing recordings: ${e instanceof Error ? e.message : String(e)}`);
      }
      return;
    }
    if (sub === 'play') {
      const id = parts[2];
      if (!id) { writeToPane('Usage: /recordings play <id>'); return; }
      useTerminalStore.getState().openReplay(id);
      writeToPane(`Replaying ${id}. Press Esc to close.`);
      return;
    }
    writeToPane('Usage: /recordings list | /recordings play <id>');
    return;
  }

  if (cmd === '/theme') {
    const { config, activeThemeName, setActiveTheme } = useTerminalStore.getState();
    const sub = parts[1]?.toLowerCase();
    const themes = config?.themes ?? {};
    const names = Object.keys(themes);

    if (!sub || sub === 'list') {
      if (names.length === 0) {
        writeToPane('No themes configured.');
      } else {
        const active = activeThemeName ?? 'mirror';
        writeToPane(
          '── Themes ──\r\n' +
          names.map((n) => `  ${n === active ? '●' : ' '} ${n}`).join('\r\n'),
        );
      }
      return;
    }
    if (sub === 'use') {
      const name = parts[2];
      if (!name) { writeToPane('Usage: /theme use <name>'); return; }
      if (!themes[name] && name !== 'mirror') {
        writeToPane(`Unknown theme: ${name}. Type /theme list to see available.`);
        return;
      }
      setActiveTheme(name);
      writeToPane(`Theme switched to: ${name}`);
      return;
    }
    writeToPane(`Unknown subcommand: ${sub}. Type /theme list or /theme use <name>.`);
    return;
  }

  writeToPane(`Unknown command: ${cmd}. Type /help for available commands.`);
}

const HISTORY_LIMIT = 50;

export function CommandBar() {
  const [value, setValue] = useState('');
  const historyRef = useRef<string[]>([]);
  const indexRef = useRef<number | null>(null);
  const draftRef = useRef<string>('');
  const inputRef = useRef<HTMLInputElement>(null);

  const onSubmit = useCallback(async () => {
    const trimmed = value.trim();
    if (!trimmed) return;
    const hist = historyRef.current;
    if (hist[hist.length - 1] !== trimmed) {
      hist.push(trimmed);
      if (hist.length > HISTORY_LIMIT) hist.shift();
    }
    indexRef.current = null;
    draftRef.current = '';
    setValue('');
    await handleCommand(trimmed);
  }, [value]);

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        onSubmit();
        return;
      }
      const hist = historyRef.current;
      if (e.key === 'ArrowUp') {
        if (hist.length === 0) return;
        e.preventDefault();
        if (indexRef.current === null) {
          draftRef.current = value;
          indexRef.current = hist.length - 1;
        } else if (indexRef.current > 0) {
          indexRef.current -= 1;
        }
        setValue(hist[indexRef.current] ?? '');
      } else if (e.key === 'ArrowDown') {
        if (indexRef.current === null) return;
        e.preventDefault();
        if (indexRef.current < hist.length - 1) {
          indexRef.current += 1;
          setValue(hist[indexRef.current] ?? '');
        } else {
          indexRef.current = null;
          setValue(draftRef.current);
          draftRef.current = '';
        }
      }
    },
    [onSubmit, value],
  );

  return (
    <div className="term-command-bar">
      <span className="term-command-prefix t-meta">{'❯ /'}</span>
      <input
        ref={inputRef}
        className="term-command-input"
        type="text"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={onKeyDown}
        placeholder="schedule list, help..."
        spellCheck={false}
      />
    </div>
  );
}

import { useCallback, useEffect, useRef, useState } from 'react';
import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import { Unicode11Addon } from '@xterm/addon-unicode11';
import { fetchRecordingText } from '../api';
import { useTerminalStore } from '../../stores/terminal';
import { resolveTheme, monoFontStack } from './theme';
import { Hint } from '../../components/ui/Hint';
import { CloseButton } from '../../components/common/CloseButton';

interface AsciicastHeader {
  version: number;
  width: number;
  height: number;
  timestamp?: number;
  env?: Record<string, string>;
}

interface RecordingPlayerProps {
  recordingId: string;
  onClose(): void;
}

function parseCast(text: string): { header: AsciicastHeader; events: Array<[number, string, string]> } {
  const lines = text.split('\n').filter((l) => l.trim().length > 0);
  if (lines.length === 0) {
    throw new Error('empty recording');
  }
  const header = JSON.parse(lines[0]) as AsciicastHeader;
  const events: Array<[number, string, string]> = [];
  for (let i = 1; i < lines.length; i++) {
    try {
      const parsed = JSON.parse(lines[i]);
      if (Array.isArray(parsed) && parsed.length >= 3) {
        events.push([Number(parsed[0]), String(parsed[1]), String(parsed[2])]);
      }
    } catch {
      // skip malformed lines
    }
  }
  return { header, events };
}

export function RecordingPlayer({ recordingId, onClose }: RecordingPlayerProps) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const [status, setStatus] = useState<'loading' | 'playing' | 'done' | 'error'>('loading');
  const [err, setErr] = useState<string>('');
  const cancelRef = useRef<boolean>(false);

  const activeThemeName = useTerminalStore((s) => s.activeThemeName);
  const config = useTerminalStore((s) => s.config);

  useEffect(() => {
    cancelRef.current = false;
    let term: Terminal | null = null;
    const timers: ReturnType<typeof setTimeout>[] = [];

    (async () => {
      try {
        const text = await fetchRecordingText(recordingId);
        if (cancelRef.current) return;
        const { header, events } = parseCast(text);

        const el = hostRef.current;
        if (!el) return;

        const themeName = activeThemeName ?? 'mirror';
        const themeCfg = config?.themes?.[themeName] ?? null;

        term = new Terminal({
          theme: resolveTheme(themeCfg),
          fontFamily: monoFontStack(),
          fontSize: 14,
          lineHeight: 1.25,
          cursorBlink: false,
          cols: header.width || 100,
          rows: header.height || 30,
          scrollback: 10000,
          allowProposedApi: true,
          disableStdin: true,
        });
        const fit = new FitAddon();
        const uni = new Unicode11Addon();
        term.loadAddon(uni);
        term.unicode.activeVersion = '11';
        term.loadAddon(fit);
        term.open(el);
        fit.fit();

        setStatus('playing');

        for (const [t, kind, data] of events) {
          if (cancelRef.current) return;
          if (kind === 'o') {
            timers.push(setTimeout(() => {
              if (cancelRef.current || !term) return;
              term.write(data);
            }, Math.max(0, t * 1000)));
          } else if (kind === 'r') {
            // resize event — e.g. "80x24"
            const match = /^(\d+)x(\d+)$/.exec(data);
            if (match && term) {
              const cols = Number(match[1]);
              const rows = Number(match[2]);
              timers.push(setTimeout(() => {
                if (cancelRef.current || !term) return;
                term.resize(cols, rows);
              }, Math.max(0, t * 1000)));
            }
          }
        }

        // Final status flip
        const lastT = events.length ? events[events.length - 1][0] : 0;
        timers.push(setTimeout(() => {
          if (!cancelRef.current) setStatus('done');
        }, Math.max(0, lastT * 1000) + 50));
      } catch (e) {
        setErr(e instanceof Error ? e.message : String(e));
        setStatus('error');
      }
    })();

    return () => {
      cancelRef.current = true;
      for (const t of timers) clearTimeout(t);
      if (term) term.dispose();
    };
  }, [recordingId, activeThemeName, config]);

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        onClose();
      }
    },
    [onClose],
  );

  return (
    <div className="wt-replay" onKeyDown={onKeyDown} tabIndex={-1}>
      <div className="wt-replay-bar">
        <span className="wt-replay-title">▶ {recordingId}</span>
        <span className="wt-replay-status">
          {status === 'loading' && 'loading…'}
          {status === 'playing' && 'playing'}
          {status === 'done' && 'done'}
          {status === 'error' && `error: ${err}`}
        </span>
        <Hint label="Close (Esc)">
          <CloseButton onClick={onClose} ariaLabel="Close recording" />
        </Hint>
      </div>
      <div className="wt-replay-host" ref={hostRef} />
    </div>
  );
}

import { useCallback, useEffect, useRef, useState } from 'react';
import { useTerminalStore } from '../../stores/terminal';

interface TerminalSearchProps {
  paneId: string;
  onClose(): void;
}

export function TerminalSearch({ paneId, onClose }: TerminalSearchProps) {
  const [value, setValue] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const search = useCallback(
    (dir: 'next' | 'prev') => {
      const addon = useTerminalStore.getState().getSearchAddon(paneId);
      if (!addon || !value) return;
      const opts = { caseSensitive: false, wholeWord: false, regex: false };
      if (dir === 'next') addon.findNext(value, opts);
      else addon.findPrevious(value, opts);
    },
    [paneId, value],
  );

  const close = useCallback(() => {
    const addon = useTerminalStore.getState().getSearchAddon(paneId);
    addon?.clearDecorations();
    onClose();
  }, [paneId, onClose]);

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        search(e.shiftKey ? 'prev' : 'next');
      } else if (e.key === 'Escape') {
        e.preventDefault();
        close();
      }
    },
    [search, close],
  );

  return (
    <div className="wt-search" onClick={(e) => e.stopPropagation()}>
      <input
        ref={inputRef}
        className="wt-search-input"
        type="text"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={onKeyDown}
        placeholder="Find in terminal"
        spellCheck={false}
      />
      <button className="wt-search-btn" onClick={() => search('prev')} title="Previous (Shift+Enter)">↑</button>
      <button className="wt-search-btn" onClick={() => search('next')} title="Next (Enter)">↓</button>
      <button className="wt-search-btn" onClick={close} title="Close (Esc)">×</button>
    </div>
  );
}

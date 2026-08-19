import { useCallback, useEffect, useRef, useState } from 'react';
import { useTerminalStore } from '../../stores/terminal';
import { Hint } from '../../components/ui/Hint';
import { Input } from '../../components/common/Input';
import { CloseButton } from '../../components/common/CloseButton';
import { IconButton } from '../../components/common/IconButton';

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
    // brand-exempt: not a control — the panel floats over a terminal whose
    // canvas takes focus on click, so it holds the click rather than letting
    // the pane underneath steal focus mid-search.
    <div className="wt-search" onClick={(e) => e.stopPropagation()}>
      <Input
        inputRef={inputRef}
        className="wt-search-input"
        value={value}
        onChange={setValue}
        onKeyDown={onKeyDown}
        placeholder="Find in terminal"
        spellCheck={false}
      />
      <Hint label="Previous (Shift+Enter)">
        <IconButton onClick={() => search('prev')} ariaLabel="Previous match">↑</IconButton>
      </Hint>
      <Hint label="Next (Enter)">
        <IconButton onClick={() => search('next')} ariaLabel="Next match">↓</IconButton>
      </Hint>
      <Hint label="Close (Esc)">
        <CloseButton size="inline" onClick={close} ariaLabel="Close search" />
      </Hint>
    </div>
  );
}

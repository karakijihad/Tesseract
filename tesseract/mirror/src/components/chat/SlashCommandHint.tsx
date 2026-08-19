import { forwardRef, useImperativeHandle, useEffect, useMemo, useState } from 'react';
import { lookupCommand, matchingCommands, type SlashCommandDef } from '../../lib/slashCommands';
import { MenuItem } from '../common/MenuItem';

export interface SlashCommandHintHandle {
  stepFocus: (direction: 1 | -1) => void;
  selectFocused: () => SlashCommandDef | null;
}

interface Props {
  inputValue: string;
  onPick: (cmd: SlashCommandDef) => void;
}

type HintMode =
  | { kind: 'list'; query: string }
  | { kind: 'help'; cmdName: string }
  | null;

function parseInput(inputValue: string): HintMode {
  if (!inputValue.startsWith('/')) return null;
  const space = inputValue.indexOf(' ');
  if (space === -1) return { kind: 'list', query: inputValue.slice(1) };
  const head = inputValue.slice(1, space).trim();
  if (!head) return null;
  return { kind: 'help', cmdName: head };
}

export const SlashCommandHint = forwardRef<SlashCommandHintHandle, Props>(
  function SlashCommandHint({ inputValue, onPick }, ref) {
    const mode = parseInput(inputValue);
    const modeKind = mode?.kind ?? 'none';
    const listQuery = mode?.kind === 'list' ? mode.query : '';
    const helpName = mode?.kind === 'help' ? mode.cmdName : '';
    // List of pickable options — only populated in 'list' mode. Help mode
    // surfaces a single non-pickable row so keyboard Tab/Enter falls through
    // to the parent's send/intercept logic naturally.
    const options = useMemo<SlashCommandDef[]>(
      () => (modeKind === 'list' ? matchingCommands(listQuery) : []),
      [modeKind, listQuery],
    );
    const helpDef = useMemo<SlashCommandDef | null>(
      () => (modeKind === 'help' ? lookupCommand(helpName) : null),
      [modeKind, helpName],
    );
    const [focus, setFocus] = useState(0);

    useEffect(() => {
      setFocus(0);
    }, [modeKind, listQuery, helpName]);

    useImperativeHandle(
      ref,
      () => ({
        stepFocus: (direction) => {
          if (options.length === 0) return;
          setFocus((f) => (f + direction + options.length) % options.length);
        },
        selectFocused: () => {
          if (options.length === 0) return null;
          return options[focus] ?? null;
        },
      }),
      [focus, options],
    );

    if (mode === null) return null;

    if (mode.kind === 'help') {
      if (!helpDef) return null;
      return (
        <div className="slash-hint" role="status" aria-label="Slash command help">
          {/* The same row a pick would be, standing still — one command,
              explained, with nothing to click. It wears the shared row so it
              cannot drift from the list above it. */}
          <div className="menu-item slash-hint-row is-help">
            <div className="slash-hint-cmd">
              <span className="slash-hint-name">/{helpDef.name}</span>
              {helpDef.argLabel && <span className="slash-hint-arg">{helpDef.argLabel}</span>}
            </div>
            <div className="slash-hint-doc">
              <span className="slash-hint-summary">{helpDef.summary}</span>
              {helpDef.argHelp && <span className="slash-hint-help t-meta">{helpDef.argHelp}</span>}
            </div>
          </div>
        </div>
      );
    }

    if (options.length === 0) return null;

    return (
      <div className="slash-hint" role="listbox" aria-label="Slash commands">
        {options.map((c, idx) => (
          <MenuItem
            key={`${c.name}-${idx}`}
            role="option"
            focused={idx === focus}
            className="slash-hint-row"
            onClick={() => onPick(c)}
            onMouseEnter={() => setFocus(idx)}
            // The composer keeps focus while the list is walked, so the row
            // must not take it on the way to a click.
            onMouseDown={(e) => e.preventDefault()}
          >
            <div className="slash-hint-cmd">
              <span className="slash-hint-name">/{c.name}</span>
              {c.argLabel && <span className="slash-hint-arg">{c.argLabel}</span>}
            </div>
            <div className="slash-hint-doc">
              <span className="slash-hint-summary">{c.summary}</span>
              {c.argHelp && <span className="slash-hint-help t-meta">{c.argHelp}</span>}
            </div>
          </MenuItem>
        ))}
      </div>
    );
  },
);

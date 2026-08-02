// table surface — renders `props.text` (CSV/TSV) as a grid.
//
// Hand-rolled rather than pulling a parser: the job is splitting delimited
// text with quote handling, and a dependency for that is not worth the bundle.
// Rows are capped and the cap is stated — a table that silently stops at row
// 500 reads as "that's all the data", which is worse than no table.

import { useMemo } from 'react';

import type { RendererProps } from './index';

const MAX_ROWS = 500;

function detectDelimiter(text: string): string {
  const firstLine = text.slice(0, text.indexOf('\n') + 1 || undefined);
  return firstLine.split('\t').length > firstLine.split(',').length ? '\t' : ',';
}

// One pass, quote-aware: a delimiter or newline inside quotes is data, and a
// doubled quote is a literal one.
function parseDelimited(text: string, delimiter: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = '';
  let quoted = false;

  for (let i = 0; i < text.length; i += 1) {
    const char = text[i];

    if (quoted) {
      if (char === '"') {
        if (text[i + 1] === '"') {
          field += '"';
          i += 1;
        } else {
          quoted = false;
        }
      } else {
        field += char;
      }
      continue;
    }

    if (char === '"') {
      quoted = true;
    } else if (char === delimiter) {
      row.push(field);
      field = '';
    } else if (char === '\n') {
      row.push(field);
      rows.push(row);
      row = [];
      field = '';
    } else if (char !== '\r') {
      field += char;
    }
  }

  if (field !== '' || row.length > 0) {
    row.push(field);
    rows.push(row);
  }
  return rows;
}

export function TableRenderer({ descriptor }: RendererProps) {
  const text = descriptor.props?.text;
  const delimiter =
    typeof descriptor.props?.delimiter === 'string' ? descriptor.props.delimiter : null;

  const parsed = useMemo(() => {
    if (typeof text !== 'string' || !text.trim()) return null;
    const rows = parseDelimited(text, delimiter ?? detectDelimiter(text));
    if (!rows.length) return null;
    const width = Math.max(...rows.map((r) => r.length));
    // A ragged row is padded rather than thrown away — a malformed line should
    // cost one blank cell, not the rest of the file.
    return {
      header: rows[0],
      body: rows.slice(1, MAX_ROWS + 1),
      total: rows.length - 1,
      width,
    };
  }, [text, delimiter]);

  if (!parsed) {
    return <div className="surface-table surface-table--empty t-meta">no rows</div>;
  }

  const pad = (row: string[]) =>
    row.length === parsed.width
      ? row
      : [...row, ...Array(parsed.width - row.length).fill('')];

  return (
    <div className="surface-table">
      <div className="surface-table__scroll">
        <table>
          <thead>
            <tr>
              {pad(parsed.header).map((cell, i) => (
                <th key={i}>{cell}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {parsed.body.map((row, r) => (
              <tr key={r}>
                {pad(row).map((cell, c) => (
                  <td key={c}>{cell}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {parsed.total > parsed.body.length ? (
        <div className="surface-table__note t-meta">
          showing the first {parsed.body.length} of {parsed.total} rows
        </div>
      ) : null}
    </div>
  );
}

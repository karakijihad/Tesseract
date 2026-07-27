// Y-2 — code surface. Syntax-highlights `props.text` with highlight.js
// (already a dep, used by rehype-highlight in chat). `props.language` picks
// the grammar; unknown/absent languages auto-detect. Read-only viewer — a
// full Monaco/CodeMirror editor is deferred (the protocol slot is the point
// of Y-2, not in-canvas editing).

import { useMemo } from 'react';
import hljs from 'highlight.js';

import type { RendererProps } from './index';

export function CodeRenderer({ descriptor }: RendererProps) {
  const props = descriptor.props ?? {};
  const code = typeof props.text === 'string' ? props.text : '';
  const language = typeof props.language === 'string' ? props.language : '';

  const html = useMemo(() => {
    if (!code) return '';
    try {
      if (language && hljs.getLanguage(language)) {
        return hljs.highlight(code, { language }).value;
      }
      return hljs.highlightAuto(code).value;
    } catch {
      return escapeHtml(code);
    }
  }, [code, language]);

  return (
    <pre className="surface-code hljs">
      <code
        className={language ? `language-${language}` : undefined}
        dangerouslySetInnerHTML={{ __html: html }}
      />
    </pre>
  );
}

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

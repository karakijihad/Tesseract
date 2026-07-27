import { Children, isValidElement, useEffect, useRef, useState, type ReactNode } from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkBreaks from 'remark-breaks';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import { ChatArtifact, isPreviewableArtifact } from './ChatArtifact';
import { copyToClipboard } from '../../lib/clipboard';
import { ExpandOverlay } from '../common/ExpandOverlay';
import { backendAssetUrl } from '../../lib/endpoints';

interface Props {
  children: string;
}

const components: Components = {
  a({ node: _node, href, children, ...props }) {
    return (
      <a
        {...props}
        href={href}
        target="_blank"
        rel="noopener noreferrer"
        className="linked-anchor"
      >
        {children}
      </a>
    );
  },
  img({ node: _node, alt, src, ...props }) {
    // The model emits `![alt](/api/downloads/...)` referencing artifacts the
    // backend just produced. In dev, frontend (port 1420) and backend (8000)
    // are different origins, so a bare `/api/...` URL would 404 against the
    // Vite origin. `backendAssetUrl` rewrites root-relative paths to the
    // resolved BACKEND_BASE; absolute URLs and data: URIs pass through.
    const resolvedSrc = typeof src === 'string' ? backendAssetUrl(src) : src;
    return (
      <img
        {...props}
        src={resolvedSrc}
        alt={alt ?? ''}
        loading="lazy"
        className="chat-md-image"
      />
    );
  },
  pre({ node: _node, children, ...props }) {
    const code = textFromNode(children).replace(/\n$/, '');
    const language = languageFromCodeNode(children);
    if (isPreviewableArtifact(language)) {
      return <ChatArtifact code={code} language={language} />;
    }
    return (
      <div className="chat-md-codeblock">
        <CodeExpandButton code={code} language={language} />
        <CodeCopyButton code={code} />
        <pre {...props}>{children}</pre>
      </div>
    );
  },
  table({ node: _node, children, ...props }) {
    return (
      <div className="chat-md-table-wrap">
        <table {...props}>{children}</table>
      </div>
    );
  },
};

const rehypePlugins = [[rehypeHighlight, { detect: true, ignoreMissing: true }]] as const;

export function ChatMarkdown({ children }: Props) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm, remarkBreaks]}
      rehypePlugins={rehypePlugins as never}
      components={components}
    >
      {children}
    </ReactMarkdown>
  );
}

function textFromNode(node: ReactNode): string {
  if (node === null || node === undefined || typeof node === 'boolean') return '';
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(textFromNode).join('');
  if (isValidElement(node)) {
    const props = node.props as { children?: ReactNode };
    return textFromNode(props.children);
  }
  return Children.toArray(node).map(textFromNode).join('');
}

function languageFromCodeNode(node: ReactNode): string {
  const first = Array.isArray(node) ? node[0] : node;
  if (!isValidElement(first)) return '';
  const props = first.props as { className?: string };
  const match = props.className?.match(/language-([\w-]+)/);
  return match?.[1] ?? '';
}

function CodeExpandButton({ code, language }: { code: string; language: string }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        className="chat-md-expand"
        onClick={() => setOpen(true)}
        aria-label="Expand code"
      >
        ⤢
      </button>
      <ExpandOverlay
        open={open}
        onClose={() => setOpen(false)}
        title={language ? `Code (${language})` : 'Code'}
        actions={<CodeCopyButton code={code} />}
      >
        <pre><code>{code}</code></pre>
      </ExpandOverlay>
    </>
  );
}

function CodeCopyButton({ code }: { code: string }) {
  const [copied, setCopied] = useState(false);
  const timerRef = useRef<number | null>(null);

  useEffect(() => () => {
    if (timerRef.current !== null) window.clearTimeout(timerRef.current);
  }, []);

  const onCopy = async () => {
    await copyToClipboard(code);
    setCopied(true);
    if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    timerRef.current = window.setTimeout(() => setCopied(false), 1200);
  };

  return (
    <button
      type="button"
      className="chat-md-copy"
      onClick={onCopy}
      aria-label="Copy code"
    >
      {copied ? 'Copied' : 'Copy'}
    </button>
  );
}

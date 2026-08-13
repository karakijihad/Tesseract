import { useEffect, useMemo, useRef, useState, lazy, Suspense } from 'react';
import { copyToClipboard } from '../../lib/clipboard';
import { ExpandOverlay } from '../common/ExpandOverlay';

const LazyMarkdown = lazy(() =>
  import('../common/Markdown').then((m) => ({ default: m.Markdown })),
);

interface Props {
  code: string;
  language: string;
}

type ArtifactMode = 'preview' | 'code';
type ArtifactKind = 'html' | 'svg' | 'markdown';

const HTML_LANGUAGES = new Set(['html', 'htm']);
const SVG_LANGUAGES = new Set(['svg']);
const MARKDOWN_LANGUAGES = new Set(['md', 'markdown', 'mdx']);

export function isPreviewableArtifact(language: string): boolean {
  const n = normalizeLanguage(language);
  return HTML_LANGUAGES.has(n) || SVG_LANGUAGES.has(n) || MARKDOWN_LANGUAGES.has(n);
}

function classifyArtifact(language: string): ArtifactKind {
  const n = normalizeLanguage(language);
  if (SVG_LANGUAGES.has(n)) return 'svg';
  if (MARKDOWN_LANGUAGES.has(n)) return 'markdown';
  return 'html';
}

const ARTIFACT_TITLES: Record<ArtifactKind, string> = {
  html: 'HTML artifact',
  svg: 'SVG artifact',
  markdown: 'Markdown artifact',
};

const ARTIFACT_META: Record<ArtifactKind, string> = {
  html: 'sandboxed preview',
  svg: 'sandboxed preview',
  markdown: 'rendered inline',
};

export function ChatArtifact({ code, language }: Props) {
  const [mode, setMode] = useState<ArtifactMode>('code');
  const [copied, setCopied] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const timerRef = useRef<number | null>(null);
  const kind = classifyArtifact(language);
  const srcDoc = useMemo(
    () => (kind === 'markdown' ? '' : buildSrcDoc(code, kind)),
    [code, kind],
  );

  useEffect(() => () => {
    if (timerRef.current !== null) window.clearTimeout(timerRef.current);
  }, []);

  const copyCode = async () => {
    await copyToClipboard(code);
    setCopied(true);
    if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    timerRef.current = window.setTimeout(() => setCopied(false), 1200);
  };

  return (
    <div className="chat-artifact" data-artifact-kind={kind}>
      <div className="chat-artifact-toolbar">
        <div>
          <div className="chat-artifact-title">{ARTIFACT_TITLES[kind]}</div>
          <div className="chat-artifact-meta">{ARTIFACT_META[kind]}</div>
        </div>
        <div className="chat-artifact-actions" role="group" aria-label="Artifact controls">
          <button
            type="button"
            className={mode === 'preview' ? 'is-active' : ''}
            onClick={() => setMode('preview')}
          >
            Preview
          </button>
          <button
            type="button"
            className={mode === 'code' ? 'is-active' : ''}
            onClick={() => setMode('code')}
          >
            Code
          </button>
          <button type="button" onClick={copyCode} aria-label="Copy artifact code">
            {copied ? 'Copied' : 'Copy'}
          </button>
          <button type="button" onClick={() => setExpanded(true)} aria-label="Expand artifact">
            Expand
          </button>
        </div>
      </div>
      {mode === 'preview' ? (
        kind === 'markdown' ? (
          <div className="chat-artifact-markdown bubble-md" aria-label="Markdown artifact preview">
            <Suspense fallback={<div className="chat-artifact-meta">Rendering…</div>}>
              <LazyMarkdown>{code}</LazyMarkdown>
            </Suspense>
          </div>
        ) : (
          <iframe
            className="chat-artifact-frame"
            sandbox="allow-scripts"
            srcDoc={srcDoc}
            title={`${kind.toUpperCase()} artifact preview`}
          />
        )
      ) : (
        <pre className="chat-artifact-code">
          <code>{code}</code>
        </pre>
      )}
      <ExpandOverlay
        open={expanded}
        onClose={() => setExpanded(false)}
        title={ARTIFACT_TITLES[kind]}
      >
        {kind === 'markdown' ? (
          <div className="bubble-md">
            <Suspense fallback={<div className="chat-artifact-meta">Rendering…</div>}>
              <LazyMarkdown>{code}</LazyMarkdown>
            </Suspense>
          </div>
        ) : (
          <iframe
            sandbox="allow-scripts"
            srcDoc={srcDoc}
            title={`${kind.toUpperCase()} artifact expanded`}
          />
        )}
      </ExpandOverlay>
    </div>
  );
}

function normalizeLanguage(language: string): string {
  return language.trim().toLowerCase();
}

function buildSrcDoc(code: string, kind: ArtifactKind): string {
  if (kind === 'svg') {
    return [
      '<!doctype html>',
      '<html>',
      '<head>',
      '<meta charset="utf-8" />',
      '<meta name="viewport" content="width=device-width, initial-scale=1" />',
      '<base target="_blank" />',
      '<style>html,body{margin:0;min-height:100%;background:#fff;color:#111;font-family:system-ui,sans-serif;}body{display:grid;place-items:center;padding:16px;box-sizing:border-box;}svg{max-width:100%;height:auto;}</style>',
      '</head>',
      '<body>',
      code,
      '</body>',
      '</html>',
    ].join('');
  }

  const hasHtmlShell = /<!doctype\s+html|<html[\s>]/i.test(code);
  const base = '<base target="_blank" />';
  if (hasHtmlShell) {
    return code.includes('<head')
      ? code.replace(/<head([^>]*)>/i, `<head$1>${base}`)
      : code.replace(/<html([^>]*)>/i, `<html$1><head>${base}</head>`);
  }

  return [
    '<!doctype html>',
    '<html>',
    '<head>',
    '<meta charset="utf-8" />',
    '<meta name="viewport" content="width=device-width, initial-scale=1" />',
    base,
    '</head>',
    '<body>',
    code,
    '</body>',
    '</html>',
  ].join('');
}

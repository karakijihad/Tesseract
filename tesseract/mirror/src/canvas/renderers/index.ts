// Y-2 — Surface Protocol renderer registry.
//
// Each surface `type` resolves to a renderer here. An unregistered type
// falls back to the JSON-dump card (non-canvas-crashing, badge "unknown
// type: <type>") per `_shared/surface-protocol.md §Type vocabulary`. The
// 8 reference renderers Y-2 ships cover the canvas core; runtime-object
// types (lane, channel) get their rich renderers in CV-1 / P4-3.

import type { ComponentType } from 'react';

import type { SurfaceDescriptor, OperatorEvent, ReportRender } from '../protocol/types';
import { FolderRenderer } from './FolderRenderer';
import { FileRenderer } from './FileRenderer';
import { WebViewRenderer } from './WebViewRenderer';
import { TerminalRenderer } from './TerminalRenderer';
import { CodeRenderer } from './CodeRenderer';
import { MarkdownRenderer } from './MarkdownRenderer';
import { HtmlRenderer } from './HtmlRenderer';
import { ImageRenderer } from './ImageRenderer';
import { PdfRenderer } from './PdfRenderer';
import { VideoRenderer } from './VideoRenderer';
import { AudioRenderer } from './AudioRenderer';
import { TableRenderer } from './TableRenderer';
import { JsonDumpRenderer } from './JsonDumpRenderer';
import { LaneRenderer } from './LaneRenderer';
import { PulseStreamRenderer } from './PulseStreamRenderer';
import { PulseFilterRenderer } from './PulseFilterRenderer';
import { DelegateTranscriptRenderer } from './DelegateTranscriptRenderer';
import { SessionTranscriptRenderer } from './SessionTranscriptRenderer';

export interface RendererProps {
  descriptor: SurfaceDescriptor;
  // Emit a state-change / interaction event back to the tool layer.
  dispatch: (event: OperatorEvent, detail?: Record<string, unknown>) => void;
  // Say what happened when this renderer tried to draw, so `surface_list` can
  // report *mounted / degraded / errored* instead of *exists*. A renderer that
  // already knows it failed — an unsupported codec, a pdf that would not open,
  // an embedded player the sandbox will paint black — calls this with the same
  // reason it puts on the card. Optional: a renderer mounted outside a
  // SurfaceCard (tests, previews) has nobody to report to. The card reports
  // `mounted` on its own, so silence from a renderer means "no failure known".
  report?: ReportRender;
}

export type RendererComponent = ComponentType<RendererProps>;

export const RENDERERS: Record<string, RendererComponent> = {
  folder: FolderRenderer,
  file: FileRenderer,
  webview: WebViewRenderer,
  browser: WebViewRenderer, // `browser` is a webview with a chrome bar; same renderer for v1.
  url: WebViewRenderer,
  iframe: WebViewRenderer, // advertised in the surface_create vocabulary; same strict-sandbox webview

  terminal: TerminalRenderer,
  code: CodeRenderer,
  markdown: MarkdownRenderer,
  html: HtmlRenderer,
  image: ImageRenderer, // generated/standalone image (e.g. image_generate → props.url)
  pdf: PdfRenderer,
  video: VideoRenderer,
  audio: AudioRenderer,
  table: TableRenderer, // csv/tsv text -> grid
  json: JsonDumpRenderer,
  lane: LaneRenderer, // CV-1 — live Claude/Codex controller lane
  // Y-3 — views-as-canvases (Pulse / Terminal applets).
  'pulse-stream': PulseStreamRenderer,
  'pulse-filters': PulseFilterRenderer,
  // SC-0 — `terminal-host` de-registered: the spatial-cockpit model hosts the
  // whole TerminalView in a panel, not as a surface card. A stray
  // `terminal-host` descriptor now falls back to JsonDumpRenderer (safe) rather
  // than mounting a second TerminalPanes and double-bootstrapping the PTYs.
  'delegate-transcript': DelegateTranscriptRenderer, // D-6 — retires SpawnDrawer
  'session-transcript': SessionTranscriptRenderer, // controller-session / chat transcript card
};

export function getRenderer(type: string): RendererComponent {
  return RENDERERS[type] ?? JsonDumpRenderer;
}

import type {
  AssistantStreamSegment,
  ChatAttachment,
  ChatMessage,
  MessageStats,
  ModelSelectedData,
  RawHistoryEntry,
  ToolCall,
  ToolResult,
} from './types';
import { BACKEND_BASE } from './endpoints';

export interface RehydratedHistory {
  messages: ChatMessage[];
  modelById: Map<string, ModelSelectedData>;
  statsById: Map<string, MessageStats>;
}

function tagsToSegments(content: string): {
  segments: AssistantStreamSegment[];
  joinedText: string;
} {
  // `spoken` is matched so its text is claimed by a segment rather than
  // falling through to the untagged buckets below — left unmatched, the
  // spoken line would be restored as a second answer bubble repeating the
  // reply. It is kept out of `joined` for the same reason: `joinedText`
  // becomes the message content, which is the answer alone.
  const re = /<(intent|spoken|answer)>([\s\S]*?)<\/\1>/g;
  const segments: AssistantStreamSegment[] = [];
  const joined: string[] = [];
  let cursor = 0;
  let match: RegExpExecArray | null;
  while ((match = re.exec(content)) !== null) {
    const between = content.slice(cursor, match.index).trim();
    if (between) {
      segments.push({ kind: 'answer', text: between });
      joined.push(between);
    }
    const kind = match[1] as 'intent' | 'spoken' | 'answer';
    const text = match[2].trim();
    if (text) {
      segments.push({ kind, text });
      if (kind === 'answer') joined.push(text);
    }
    cursor = re.lastIndex;
  }
  const tail = content.slice(cursor).trim();
  if (tail) {
    // A stream cut mid-block leaves an opener with no closing tag, which the
    // closed-tag regex above cannot see. The live parser classifies it by
    // state (`stream_parser.py::_parse_tagged_stream`); without the same
    // handling here the raw `<spoken>` markup would be restored as ordinary
    // answer text — protocol scaffolding rendered to the operator.
    const unclosed = /^<(intent|spoken|answer)>([\s\S]*)$/.exec(tail);
    if (unclosed) {
      const kind = unclosed[1] as 'intent' | 'spoken' | 'answer';
      const text = unclosed[2].trim();
      if (text) {
        segments.push({ kind, text });
        if (kind === 'answer') joined.push(text);
      }
    } else {
      segments.push({ kind: 'answer', text: tail });
      joined.push(tail);
    }
  }
  // Same rule the live store applies at the turn boundary
  // (`conversation.ts::_promoteLoneSpoken`): a reply cut after `</spoken>`
  // before any `<answer>` opened has the spoken block as its only content,
  // and spoken renders nothing. Without this the message survives the turn
  // but comes back empty on the next reload.
  if (!joined.length && segments.some(segment => segment.kind === 'spoken')) {
    const promoted = segments.map(segment =>
      segment.kind === 'spoken' ? { ...segment, kind: 'answer' as const } : segment,
    );
    return {
      segments: promoted,
      joinedText: promoted
        .filter(segment => segment.kind === 'answer')
        .map(segment => segment.text)
        .join('\n\n'),
    };
  }
  return { segments, joinedText: joined.join('\n\n') };
}

function appendToolCallSegments(
  segments: AssistantStreamSegment[],
  toolCalls: ToolCall[],
): AssistantStreamSegment[] {
  if (!toolCalls.length) return segments;
  return [
    ...segments,
    ...toolCalls.map<AssistantStreamSegment>((tc) => ({
      kind: 'tool_call',
      text: '',
      call_id: tc.call_id,
      name: tc.name,
    })),
  ];
}

function randomId(prefix: string): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
    return `${prefix}-${crypto.randomUUID()}`;
  }
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

function parseArguments(arg: string): Record<string, unknown> {
  if (!arg) return {};
  try {
    const parsed = JSON.parse(arg);
    return typeof parsed === 'object' && parsed !== null ? (parsed as Record<string, unknown>) : { value: parsed };
  } catch {
    return { _raw: arg };
  }
}

function entryTimestamp(entry: RawHistoryEntry, approxTimestamp: number): number {
  if (!entry.timestamp) return approxTimestamp;
  const parsed = Date.parse(entry.timestamp);
  return Number.isNaN(parsed) ? approxTimestamp : parsed;
}

/**
 * Walk persisted OpenAI-shape history and rebuild ChatMessages so that one
 * user turn maps to one assistant bubble — same as live streaming. The
 * provider stores each tool-loop iteration as a separate `assistant` entry
 * (intent + tool_calls), with `tool` entries between them carrying results,
 * then a final `assistant` entry holding the answer text. Without merging,
 * each iteration would render as its own bubble; live shows them as one.
 *
 * Also returns `modelById` / `statsById` sidecars rebuilt from `_meta` on
 * the persisted assistant entries — these seed `messageModel` /
 * `messageStats` so the model badge and token-cache pill render on resume
 * the same way they did live.
 */
export function rehydrateHistory(
  raw: RawHistoryEntry[],
  approxTimestamp: number = Date.now(),
): RehydratedHistory {
  const out: ChatMessage[] = [];
  const modelById = new Map<string, ModelSelectedData>();
  const statsById = new Map<string, MessageStats>();
  let openTurnId: string | null = null;

  for (const entry of raw) {
    if (entry.role === 'system') continue;

    if (entry.role === 'user') {
      const { text, attachments } = contentToDisplay(entry.content);
      out.push({
        id: randomId('user'),
        role: 'user',
        content: text,
        attachments: attachments.length > 0 ? attachments : undefined,
        timestamp: entryTimestamp(entry, approxTimestamp),
        status: 'complete',
      });
      openTurnId = null;
      continue;
    }

    if (entry.role === 'tool') {
      const result: ToolResult = {
        call_id: entry.tool_call_id ?? '',
        output: typeof entry.content === 'string' ? entry.content : '',
        is_error: false,
      };
      for (let i = out.length - 1; i >= 0; i--) {
        const msg = out[i];
        if (msg.role !== 'entity' && msg.role !== 'assistant') continue;
        if (!msg.toolCalls?.some((tc) => tc.call_id === result.call_id)) continue;
        out[i] = {
          ...msg,
          toolResults: [...(msg.toolResults ?? []), result],
        };
        break;
      }
      continue;
    }

    if (entry.role === 'assistant') {
      const toolCalls: ToolCall[] = entry.tool_calls?.map((tc) => ({
        call_id: tc.id,
        name: tc.function.name,
        input: parseArguments(tc.function.arguments),
      })) ?? [];
      const rawContent = typeof entry.content === 'string'
        ? entry.content
        : contentToDisplay(entry.content).text;
      const { segments, joinedText } = tagsToSegments(rawContent);
      const newSegments = appendToolCallSegments(segments, toolCalls);

      if (openTurnId === null) {
        const id = randomId('assistant');
        openTurnId = id;
        out.push({
          id,
          role: 'assistant',
          content: joinedText || rawContent,
          segments: newSegments.length > 0 ? newSegments : undefined,
          timestamp: entryTimestamp(entry, approxTimestamp),
          toolCalls: toolCalls.length > 0 ? toolCalls : undefined,
          status: 'complete',
        });
      } else {
        // Merge: append to the in-progress assistant bubble for this turn.
        const idx = out.findIndex((m) => m.id === openTurnId);
        if (idx >= 0) {
          const prev = out[idx];
          const prevSegments = prev.segments ?? [];
          const mergedContent = [prev.content, joinedText].filter(Boolean).join('\n\n');
          out[idx] = {
            ...prev,
            content: mergedContent || rawContent,
            segments: [...prevSegments, ...newSegments],
            toolCalls: [...(prev.toolCalls ?? []), ...toolCalls],
          };
        }
      }

      // Sidecar meta — sum usage across the merged turn (matches live's
      // `setMessageStats` accumulator); take the latest non-null model
      // (matches live's `setMessageModel` overwrite semantics).
      const meta = entry._meta;
      if (meta && openTurnId) {
        if (meta.model && meta.model.provider && meta.model.model) {
          modelById.set(openTurnId, {
            role: meta.model.role,
            provider: meta.model.provider,
            model: meta.model.model,
            tier: meta.model.tier,
          });
        }
        if (meta.usage) {
          const prev = statsById.get(openTurnId);
          statsById.set(openTurnId, {
            input_tokens: (prev?.input_tokens ?? 0) + (meta.usage.input_tokens ?? 0),
            output_tokens: (prev?.output_tokens ?? 0) + (meta.usage.output_tokens ?? 0),
            cached_tokens: (prev?.cached_tokens ?? 0) + (meta.usage.cached_tokens ?? 0),
          });
        }
      }
    }
  }

  return { messages: out, modelById, statsById };
}

/** Back-compat shim — existing call sites (Playwright e2e) only need the
 * messages array; meta sidecars stay empty when no `_meta` is present. */
export function rawHistoryToMessages(
  raw: RawHistoryEntry[],
  approxTimestamp: number = Date.now(),
): ChatMessage[] {
  return rehydrateHistory(raw, approxTimestamp).messages;
}

function contentToDisplay(content: RawHistoryEntry['content']): {
  text: string;
  attachments: ChatAttachment[];
} {
  if (typeof content === 'string') return { text: content, attachments: [] };
  if (!Array.isArray(content)) return { text: '', attachments: [] };
  const textParts: string[] = [];
  const attachments: ChatAttachment[] = [];
  for (const item of content) {
    if (!item || typeof item !== 'object') continue;
    const kind = item.type;
    if ((kind === 'text' || kind === 'input_text') && typeof item.text === 'string') {
      textParts.push(item.text);
      continue;
    }
    if ((kind === 'image' || kind === 'file') && typeof item.attachment_id === 'string') {
      const filename = typeof item.filename === 'string' ? item.filename : 'attachment';
      const mimeType = typeof item.mime_type === 'string' ? item.mime_type : 'application/octet-stream';
      attachments.push({
        id: item.attachment_id,
        session_id: typeof item.session_id === 'string' ? item.session_id : '',
        filename,
        mime_type: mimeType,
        size: typeof item.size === 'number' ? item.size : 0,
        kind: kind === 'image' ? 'image' : (mimeType === 'application/pdf' ? 'pdf' : 'file'),
        url: normalizeAttachmentUrl(typeof item.url === 'string' ? item.url : ''),
        created_at: typeof item.created_at === 'string' ? item.created_at : '',
      });
    }
  }
  return { text: textParts.join('\n'), attachments };
}

function normalizeAttachmentUrl(url: string): string {
  if (!url) return '';
  if (/^https?:\/\//i.test(url)) return url;
  return `${BACKEND_BASE}${url.startsWith('/') ? url : `/${url}`}`;
}

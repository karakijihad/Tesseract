// Y-3 — Pulse filter chips as a Surface Protocol card. The 12 tag chips that
// were the `pulse-filters` row of the old PulseView; filter state now lives
// in the pulse store so this card and the PulseStreamRenderer share it.

import { Hint } from '../../components/ui/Hint';
import { Chip } from '../../components/common/Chip';
import type { PulseTag } from '../../lib/types';
import { ALL_PULSE_TAGS, usePulseStore } from '../../stores/pulse';
import type { RendererProps } from './index';

const TAG_HINTS: Record<PulseTag, string> = {
  triage:  'early session setup, connection + catch-up events',
  tool:    'tool calls: stream_tool_call_end, tool_auto, sandbox',
  memory:  'memory_save / memory_update / memory_forget / memory_search, memory_suggestion',
  agent:   'cli_start / cli_output / cli_end (delegate_*), invoke_agent',
  model:   'model_selected — chat_brain and observer_agent model resolution',
  system:  'session_created / saved / loaded / reset / compact, soul_updated',
  chat:    'stream_text + generic loop traffic not otherwise tagged',
  perm:    'tool_ask, tool_approved, tool_denied, tool_denied_hard',
  route:   'mode_changed (security mode flips)',
  loop:    'loop_start, loop_end, stream_stop — turn lifecycle',
  bg:      'background category: observer_result / observer_unavailable / compaction_trigger',
  other:   'uncategorized: entity signals (filtered at push), terminal bypass, planning, unmapped',
};

export function PulseFilterRenderer(_props: RendererProps) {
  const enabledTags = usePulseStore((s) => s.enabledTags);
  const toggleTag = usePulseStore((s) => s.toggleTag);
  const isTagEnabled = (tag: PulseTag) => enabledTags === null || enabledTags.has(tag);

  return (
    <div className="pulse-filters pulse-filters--card">
      {ALL_PULSE_TAGS.map((tag) => (
        <Hint key={tag} label={`${tag} — ${TAG_HINTS[tag]}`} position="bottom" maxWidth={280}>
          <Chip
            variant="tag"
            className={`ev-tag ${tag}`}
            active={isTagEnabled(tag)}
            onClick={() => toggleTag(tag)}
          >
            {tag}
          </Chip>
        </Hint>
      ))}
    </div>
  );
}

// Y-2 — markdown surface. Reuses the chat markdown renderer (react-markdown
// + remark-gfm + rehype-highlight) so canvas markdown matches chat markdown
// exactly. `props.text` (or `props.markdown`) is the source.

import { ChatMarkdown } from '../../components/chat/ChatMarkdown';
import type { RendererProps } from './index';

export function MarkdownRenderer({ descriptor }: RendererProps) {
  const props = descriptor.props ?? {};
  const source = props.text ?? props.markdown;
  return (
    <div className="surface-markdown">
      <ChatMarkdown>{typeof source === 'string' ? source : ''}</ChatMarkdown>
    </div>
  );
}

// Markdown surface. Renders through the app's one markdown component, so a
// canvas surface, a chat bubble and a journal row cannot drift apart.
// `props.text` (or `props.markdown`) is the source.

import { Markdown } from '../../components/common/Markdown';
import type { RendererProps } from './index';

export function MarkdownRenderer({ descriptor }: RendererProps) {
  const props = descriptor.props ?? {};
  const source = props.text ?? props.markdown;
  return (
    <div className="surface-markdown">
      <Markdown>{typeof source === 'string' ? source : ''}</Markdown>
    </div>
  );
}

import type { ModelSelectedData } from '../../lib/types';
import { Hint } from '../ui/Hint';

interface Props {
  info: ModelSelectedData;
}

export function ModelBadge({ info }: Props) {
  const baseTitle =
    `${info.role} / ${info.tier}` +
    (info.reasoning_effort ? ` / effort: ${info.reasoning_effort}` : '');
  const fallbackTitle = info.is_fallback && info.primary
    ? `\nfell back from ${info.primary.provider}/${info.primary.model}` +
      (info.fallback_reason ? `\nreason: ${info.fallback_reason}` : '')
    : '';
  return (
    <Hint label={baseTitle + fallbackTitle} maxWidth={320}>
      <span className={`model-badge${info.is_fallback ? ' is-fallback' : ''}`}>
        <span className="model-badge-provider">{info.provider}</span>
        <span className="model-badge-sep">/</span>
        <span className="model-badge-model">{info.model}</span>
        {info.reasoning_effort && (
          <>
            <span className="model-badge-sep">/</span>
            <span className="model-badge-effort">{info.reasoning_effort}</span>
          </>
        )}
        {info.is_fallback && (
          <span className="model-badge-fallback t-meta" aria-label="fallback model">
            fallback
          </span>
        )}
      </span>
    </Hint>
  );
}

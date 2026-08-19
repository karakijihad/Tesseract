import { useState } from 'react';
import { Hint } from '../../components/ui/Hint';
import { Chip } from '../../components/common/Chip';

interface Props {
  path: string;
  label?: string;
}

/**
 * Inline monospace pill that shows a file path. Click to copy. Used in
 * workspace event detail bodies so the operator can see *where* a write
 * landed without spawning shell commands or "open in editor" plumbing
 * (deliberately outside the runtime's safe surface).
 */
export function PathPill({ path, label }: Props) {
  const [copied, setCopied] = useState(false);
  if (!path) return null;
  const onClick = async (e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      await navigator.clipboard.writeText(path);
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    } catch {
      // Clipboard API can fail in non-secure contexts; silently degrade.
    }
  };
  return (
    <Hint label={`Click to copy: ${path}`}>
      <Chip
        className="workspace-path-pill"
        active={copied}
        onClick={onClick}
      >
        {label ?? path}
        <span className="workspace-path-pill-hint t-meta">
          {copied ? ' copied' : ' copy'}
        </span>
      </Chip>
    </Hint>
  );
}

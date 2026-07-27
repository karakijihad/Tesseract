/**
 * SOUL.md parsing shared between the SOUL view (renders each block) and
 * the right-panel header chip (only cares about section count).
 *
 * A SoulBlock starts at each `## Heading`; content before the first `## `
 * (e.g. the top-level file header) is dropped. Empty-body blocks are pruned.
 */
export interface SoulBlock {
  heading: string;
  body: string;
}

export function parseSoul(md: string): SoulBlock[] {
  if (!md.trim()) return [];
  const lines = md.split('\n');
  const blocks: SoulBlock[] = [];
  let current: SoulBlock | null = null;
  for (const line of lines) {
    const h2 = line.match(/^##\s+(.+)$/);
    if (h2) {
      if (current) blocks.push(current);
      current = { heading: h2[1].trim(), body: '' };
    } else if (current) {
      current.body += (current.body ? '\n' : '') + line;
    }
  }
  if (current) blocks.push(current);
  return blocks
    .map((b) => ({ heading: b.heading, body: b.body.trim() }))
    .filter((b) => b.body.length > 0);
}

export function countSoulSections(md: string): number {
  return parseSoul(md).length;
}

// Starlight's own loader hardcodes its base to `src/content/docs`. The Guide's
// pages predate the site and are read directly on GitHub, so they stay where
// they are and a glob loader is pointed at them instead of being moved under a
// framework's directory.
//
// README.md is mapped to the site root rather than copied into an index.md:
// two files saying the same thing is the drift this whole subsystem exists to
// prevent, and the repo landing page and the site home want the same words.
import { defineCollection } from 'astro:content';
import { docsSchema } from '@astrojs/starlight/schema';
import { glob } from 'astro/loaders';

export const collections = {
  docs: defineCollection({
    loader: glob({
      base: '.',
      pattern: '{README,start/**/*,anatomy/**/*,mechanisms/**/*,reference/**/*}.md',
      generateId: ({ entry }) =>
        entry === 'README.md' ? 'index' : entry.replace(/\.md$/, ''),
    }),
    schema: docsSchema(),
  }),
};

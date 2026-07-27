import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

// Minimal vitest config for frontend unit tests. Mirrors `vite.config.ts`'s
// React plugin so JSX is handled identically. `jsdom` environment gives us
// a DOM for store-touching tests; standalone helpers (no DOM dependency)
// also work fine under jsdom.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: false,
    include: ['src/**/*.test.{ts,tsx}'],
    exclude: ['node_modules/**', 'e2e/**', 'dist/**'],
  },
});

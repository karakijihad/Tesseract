import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { createReadStream, existsSync, statSync } from "node:fs";
import { resolve } from "node:path";

// @ts-expect-error process is a nodejs global
const host = process.env.TAURI_DEV_HOST;

// https://vite.dev/config/
export default defineConfig(async () => ({
  plugins: [
    react(),
    tailwindcss(),
    // onnxruntime-web (transitive dep of @ricky0123/vad-web) does
    // `import('/vad/ort-wasm-simd-threaded.jsep.mjs')` at runtime against
    // the vendored copy under `public/vad/`. The browser appends `?import`
    // and Vite's transform pipeline rejects it because /public files
    // aren't modules. Serve them raw via a pre-pipeline middleware.
    {
      name: "vad-public-passthrough",
      configureServer(server) {
        server.middlewares.use((req, _res, next) => {
          if (!req.url) return next();
          // Match `/vad/<name>.mjs` with or without ?import / ?t= cache-bust.
          const match = req.url.match(/^\/vad\/([^?]+\.mjs)(\?.*)?$/);
          if (!match) return next();
          const filename = match[1];
          const filePath = resolve(__dirname, "public", "vad", filename);
          if (!existsSync(filePath)) return next();
          const stat = statSync(filePath);
          _res.statusCode = 200;
          _res.setHeader("Content-Type", "application/javascript");
          _res.setHeader("Content-Length", String(stat.size));
          _res.setHeader("Cache-Control", "public, max-age=0, must-revalidate");
          createReadStream(filePath).pipe(_res);
        });
      },
    },
  ],

  // Vite options tailored for Tauri development and only applied in `tauri dev` or `tauri build`
  //
  // 1. prevent Vite from obscuring rust errors
  clearScreen: false,

  build: {
    rollupOptions: {
      output: {
        // Deferred 2026-07-12 bundle-size debt: dynamic imports can't split
        // (e.g. ChatMarkdown is statically imported in 6 places), so split
        // the heavy vendors explicitly instead — each chunk caches
        // independently and the main app chunk drops below the warn line.
        manualChunks: {
          react: ["react", "react-dom"],
          three: ["three"],
          pdfjs: ["pdfjs-dist"],
          xterm: [
            "@xterm/xterm",
            "@xterm/addon-canvas",
            "@xterm/addon-fit",
            "@xterm/addon-search",
            "@xterm/addon-unicode11",
            "@xterm/addon-web-links",
            "@xterm/addon-webgl",
          ],
          markdown: [
            "react-markdown",
            "remark-gfm",
            "remark-breaks",
            "rehype-highlight",
            "highlight.js",
          ],
        },
      },
    },
  },
  // 2. tauri expects a fixed port, fail if that port is not available
  server: {
    port: 1420,
    strictPort: true,
    host: host || false,
    // Operator policy (2026-05-26): the browser must NEVER auto-refresh on any
    // file change. Vite still watches + rebuilds modules; a manual page reload
    // picks up the latest. Re-enabling HMR is an explicit operator decision.
    hmr: false,
    watch: {
      // Vite only needs to watch the Mirror frontend (src/, public/).
      // Everything else under tesseract/ is Python runtime state — when the assistant
      // (or the operator, or a delegated CLI) writes there, an HMR fire
      // would drop the WebSocket and `cleanup_session` would cancel any
      // in-flight turn. So exclude all Python source and runtime dirs.
      // Mirror frontend edits (CSS/HTML/TSX/TS) keep hot-reloading normally.
      ignored: [
        "**/src-tauri/**",
        "**/tesseract/sessions/**",
        "**/tesseract/logs/**",
        "**/tesseract/memory-store/**",
        "**/tesseract/vault/**",
        "**/tesseract/provisional/**",
        "**/tesseract/integrations/**",
        "**/tesseract/missions/**",
        "**/tesseract/config/**",
        "**/tesseract/workspace_events/**",
        "**/tesseract/kernel/**",
        "**/tesseract/brain/**",
        "**/tesseract/orchestrator/**",
        "**/tesseract/scheduler/**",
        "**/tesseract/agents/**",
        "**/tesseract/scripts/**",
        "**/tesseract/tests/**",
        "**/__pycache__/**",
        "**/*.pyc",
        "**/.pytest-tmp/**",
        "**/.pytest_tmp*/**",
        "**/.codex-pytest-temp/**",
      ],
    },
  },
}));

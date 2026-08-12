# `mirror/` — the desktop app

Three layers, each with a different job and a different language.

| Path | What it is |
| --- | --- |
| `server/` | The backend. An aiohttp application exposing the assistant over HTTP and WebSocket, on localhost only. This is where a turn actually runs. |
| `src/` | The frontend. React + TypeScript, built with Vite. Chat, the orb, settings, and the cockpit views. |
| `src-tauri/` | The shell. A Tauri (Rust) wrapper that installs the app, keeps it updated, shows the first-run splash, and launches the backend. |
| `public/` | Static assets the shell loads before the frontend exists — the splash screen among them. |
| `scripts/` | Build-time guards and helpers invoked from `package.json`. |

## How a launch goes

The shell starts first, because on a fresh machine it is the only part that
exists. It checks that the application tree and its dependencies are present,
fetching what is missing and reporting progress to the splash window. Then it
starts the supervisor, which starts the backend. Once the backend answers, the
shell swaps the splash for the main window and the frontend connects.

That order is why the splash is plain HTML with no build step: it has to render
before anything else is installed.

## The shell's Rust modules

| File | Responsibility |
| --- | --- |
| `provision.rs` | First run — fetch the application tree, install Python and dependencies, stream progress. |
| `setup.rs` | The first-run answers: what to call the assistant, which optional components to download. |
| `repo.rs` | Git transport for the application tree. Anonymous; the shell holds no credentials. |
| `update.rs` | Updating the application tree in place. |
| `exe_update.rs` | Updating the installer itself, verified against the SHA-256 published with the release. |
| `app_swap.rs` | Replacing the tree atomically, so a failed update cannot leave a half-written app. |
| `shell_log.rs` | The shell's own log, with credentials and query strings stripped on the way in. |

## Working on it

```bash
pnpm install
pnpm dev          # frontend with hot reload, against a backend you start yourself
pnpm build        # type-check and bundle the frontend
pnpm tauri dev    # the whole app, shell included
```

The frontend is bundled into the installer rather than served over HTTP, so a
frontend change reaches an installed copy through a new installer, not through
an application-tree update.

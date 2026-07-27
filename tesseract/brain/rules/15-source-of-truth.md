# Source of truth

Config files are authoritative for what is *actually wired* right now: `roles.yaml` (which model/voice backs each role), `providers.yaml` (the model catalog), `schedule.yaml` (which jobs run), `permissions.yaml` (postures). Your workspace docs — `VOICE.md`, `FOUNDATION.md`, `TOOLS.md`, and the rest — describe intent and character; they drift as the system changes and may be months out of date.

When asked "what model / voice / setup is live", or any question about the running configuration, read the config (or the code that consumes it) before answering — do not narrate a workspace doc as if it were current state. If a workspace doc and the config disagree, the config wins, and the doc is stale. Say so, and prefer verifying over asserting from memory.

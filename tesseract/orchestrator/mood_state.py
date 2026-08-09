"""In-memory mood state for the entity.

Holds two scalars the assistant controls via `set_mood`:

- `intensity` (0..1) — affective energy
- `valence`   (-1..+1) — cool→warm tone

Drives the orb (entity_signals) only — voice synthesis is decoupled. Mood
auto-decays to neutral at the end of every turn (`ws._run_turn` finally),
so it can't bleed between prompts.

Lives per-session. Not persisted; boot defaults on every session.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MoodState:
    intensity: float = 0.5
    valence: float = 0.0

    def set(self, intensity: float, valence: float) -> None:
        self.intensity = max(0.0, min(1.0, intensity))
        self.valence = max(-1.0, min(1.0, valence))

    def reset(self) -> None:
        """Decay to neutral. Called at end of each turn."""
        self.intensity = 0.5
        self.valence = 0.0

# Verify what you render

When a task is *visual* — put something on the Mirror canvas, play media, show a page or image — the tool returning `ok` (a `surface_id`, "updated", "created") is **not** evidence the operator sees anything. The pixels are the evidence, and you don't see the operator's screen. So:

- **Look with your own eyes before claiming success.** You have a real headless browser: `browser_navigate` to the same URL, then `browser_snapshot` (accessibility tree) or `browser_screenshot`. If it's blank or erroring, you'll see it — and `browser_network_requests` shows failed loads. Only then report "it's up and I confirmed it renders," or "it created but renders blank — here's the error." Never assert a visual worked from a tool `ok` alone.
- **Know what you've spawned.** `surface_list` shows every card on a view. Use it before spawning another (don't create duplicates) and to find the `surface_id` to fix.
- **Clean up with the right verb.** `surface_close` destroys a card. Renaming its title does nothing. If a pane isn't working, close it — don't leave a graveyard of dead cards.
- **One surface, then update it.** To retry, `surface_update` the existing surface's `url`/props — don't spawn a fresh card each attempt.

# Delegate the symptom, not the mess

When you hand a problem to `delegate_auditor`/`delegate_coder`, give them the **actual thing that's wrong** — the root symptom you need solved ("the embedded video renders as a black pane, find why") — not your secondary panic about the mess you made getting there ("help me stop spawning extra panes"). A strong model pointed at the real symptom will find the root cause; pointed at your cleanup worry, it can't help. State the observed failure, what you expected, and what you already ruled out.

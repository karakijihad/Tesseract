# The agent roster

A sub-agent is a markdown card in this directory. The app loads the cards
themselves, not this file: what this file is for is CHOOSING one. `invoke_agent`
takes a name, so anything picking an agent has to read a list somewhere, and
this is that list.

Two rules hold it true:

- **Every row here has a card.** A row naming a file that is not there sends a
  request at an agent that cannot answer.
- **A row names the role its card names.** The card runs on what its own
  frontmatter says, so a roster that disagrees is a claim nobody can check.

`model_role` is either a role name from `config/roles.yaml` or a reference of
the shape `<tier>.<provider>.<model_id>` from `config/providers.yaml`. Read
those files for what exists. Roles backed by a command-line tool cannot be
reached this way; use the delegate tools for those.

A card you write yourself is yours and is not listed here. It appears in the
Managed system room of the Autonomy panel, and in `home/autonomy/WHAT-RUNS.md`,
alongside these with a mark saying which is which.

| name | model_role | description |
| --- | --- | --- |
| agent-self | agents_default | The assistant's own agent for autonomous work. Picked when an item of work names no other agent. Refuses a goal too vague to verify. |
| audit-verifier | agents_default | Post-fix re-auditor. Reads an earlier audit, re-checks the same ground, and says whether the fixes landed. |
| cli-reference | agents_default | Reference for the two coding tools the assistant drives: their commands, hooks and flags. |
| code-auditor | agents_default | Scoped code audit. Takes a path or a subject, reads without writing, and files a structured report. |
| code-fixer | agents_default | Works through an audit report, implements the serious findings, and reports what changed per finding. |
| memory-classifier | chat_brain | Sorts one section of the daily notes into user, feedback, project or reference. Answers in JSON and uses no tools. |
| memory-digest | agents_default | Writes the memory paragraph of the daily brief from what the overnight pass says it learned. |
| mission-digest | agents_default | Writes the work paragraph of the daily brief: what finished or got stuck in the last day. |
| observer | observer_agent | Watches the conversation, terminal output (only with your consent) and memory changes, and SUGGESTS memories rather than writing any. |
| provider-watcher | agents_default | Daily outside-world summary: new models, price changes, retirements, tool releases. |
| repo-auditor | agents_default | Reads the whole codebase for one named risk. Slow, so it runs only when you ask. |
| research-brief | agents_default | Researches a question across the library and the web and returns a short brief with its sources. |
| vault-digest | agents_default | Writes the library paragraph of the daily brief: what was added or rewritten in the last day. |
| vault-librarian | agents_default | Turns raw library sources into linked pages, and answers questions out of them. |
| vault-lint | agents_default | Finds two library pages that disagree about the same thing and says how. You decide what to do about it. |
| vision | channel_vision | Describes a picture, answers a question about one, reads the text in it. Returns words only. |
| workspace-digest | agents_default | Writes the workspace paragraph of the daily brief: what changed there in the last day. |

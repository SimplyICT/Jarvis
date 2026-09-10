---
title: Second brain — how this brain is organised
tags: [meta, jarvis, second-brain]
---

# Second brain — how this brain is organised

This folder is JARVIS's memory. Everything the assistant "knows" about the
business lives here as plain markdown. Nothing is hidden in a database, which
means you can read, edit, and version every fact your assistant will use.

## Structure

- `00-business/` — how the company works: profile, rates, policies
- `01-clients/` — one note per client, with account detail and preferences
- `02-projects/` — one note per engagement
- `03-decisions/` — why we chose something, so nobody re-litigates it
- `04-people/` — staff and partners

Colour groups in the 3D view map to these folders. The graph edges come from
`[[wikilinks]]`, so link liberally — a brain with no links is just a file tree.

## How the assistant uses it

1. You speak a request to the voice shell.
2. The shell asks the brain for the relevant notes (`brain.py context`).
3. Those notes are handed to the agent as grounding.
4. The agent answers, or acts, using your real numbers instead of inventing them.

## The rule that makes this work

**Write facts down where you would want to find them.** If you tell the
assistant a rate over voice and it is not in here, it will not remember it
tomorrow. Say "add that to the brain" and let it write the note.

Related: [[rate-card]], [[write-a-note]].

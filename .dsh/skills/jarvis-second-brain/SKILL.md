---
name: jarvis-second-brain
description: Recall facts from the user's second brain (a vault of markdown notes holding business context, rates, client details, project history and decisions) and write new facts back into it. Use whenever a request depends on the user's own business, clients, pricing, or prior decisions, or when the user says something worth remembering.
whenToUse: The user asks about their business, clients, pricing, invoices, projects, staff, or past decisions; or asks you to remember, note, or add something to the brain; or asks you to draft any document that must match their real numbers and brand.
---

# JARVIS second brain

You have access to the user's **second brain**: a folder of plain markdown notes
that is the single source of truth for anything about their business. It is not
optional context — it is the difference between answering with their real rates
and client history, and confidently inventing numbers.

## Where the brain lives

The vault path is in the `JARVIS_VAULT` environment variable. If it is not set,
ask the user for the folder before doing anything else.

```
$JARVIS_VAULT           the notes folder, e.g. D:\notes
$JARVIS_BRAIN           path to brain.py (this skill's `brain/` directory)
```

## The three operations

Run these with the shell tool. `brain.py` uses only the Python standard library.

### 1. Recall before you answer

Any time a request touches the user's business, **recall first**. Do not answer
from general knowledge.

```bash
python "$JARVIS_BRAIN/brain.py" context "<the user's request>" --vault "$JARVIS_VAULT"
```

This prints a ready-to-use briefing: the most relevant notes, their paths, tags,
and a body excerpt. Treat that output as authoritative and quote its numbers
exactly.

For a quick ranked list rather than a full briefing:

```bash
python "$JARVIS_BRAIN/brain.py" search "<query>" --vault "$JARVIS_VAULT" --limit 5
```

Use `search` when you want to know *which* notes are relevant; use `context`
when you are about to answer or draft something.

### 2. Rebuild after any change

The index is a snapshot. **After you write or edit any note, rebuild it**,
otherwise your next recall will miss what you just saved.

```bash
python "$JARVIS_BRAIN/brain.py" build --vault "$JARVIS_VAULT"
```

### 3. Write facts back

When the user states a durable fact — a new rate, a client preference, a
deadline, a decision — write it into the vault, then rebuild. Never leave it
only in the conversation.

Put the note in the folder that matches its kind:

| Kind of fact | Folder | Example filename |
|---|---|---|
| Company, brand, positioning, policy | `00-business/` | `company-profile.md` |
| A client's account detail and preferences | `01-clients/` | `client-acme.md` |
| An engagement, its scope and status | `02-projects/` | `project-website-rebuild.md` |
| A decision and the reasoning behind it | `03-decisions/` | `decision-move-to-x.md` |
| A person — staff, partner, supplier | `04-people/` | `person-jane-doe.md` |

Notes use this shape:

```markdown
---
title: Client — Acme Corp
tags: [client, acme, invoicing]
---

# Client — Acme Corp

One or two lines of the facts that matter.

## Account detail

- Primary contact: ...
- Seats: ...
- Retainer: ...

## Related

See [[rate-card]] and [[company-profile]].
```

**Link liberally with `[[wikilinks]]`.** The links are the brain's structure —
they are what makes a related note surface. A note with no links is an orphan
you will never find again.

## Rules

- **Quote real numbers.** If the brain says $2,500, say $2,500. If it does not
  say, say you do not know and offer to record it — never estimate a price, a
  seat count, or a client's terms.
- **Recall before drafting.** Before writing any invoice, quote, email, or
  proposal, load `context` for it. Reuse the user's existing formatting and
  brand voice rather than inventing a house style.
- **Confirm before writing to the brain** when the fact is ambiguous or the user
  was clearly thinking aloud. When the user says "remember that" or "add it to
  the brain", just do it.
- **Never invent a note path.** Check with `search` or list the folder first.
- **Report the path** of any note you create or change, so the user can read it.
- Keep notes short and factual. This is memory, not documentation.

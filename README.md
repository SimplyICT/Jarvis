# JARVIS

A personal AI assistant that **knows your business** — a voice shell, a second
brain of plain markdown notes, and an agent that acts on both.

Built on the **DeepSeek Harness**. Zero pip installs, zero npm installs, no API
keys beyond whatever your harness already uses. Python 3 standard library and a
browser.

```
  you speak  ──►  voice shell  ──►  second brain  ──►  agent  ──►  answer
   "Jarvis,       (browser STT)     (markdown +       (dsh        (spoken
    what do we                       BM25 recall)      headless)    aloud)
    charge EDR?"
```

What makes it useful is not the voice. It is that the agent answers with **your
real rate card, your real client terms, and your real project history**, instead
of inventing a plausible number. That is the whole design goal.

---

## What works today

| Piece | Status | Where |
|---|---|---|
| Second-brain memory (index, recall, brief) | **working, 37 tests** | `brain/brain.py` |
| Voice shell (wake word, speech in/out) | **working** | `voice/` |
| Agent bridge to the harness | **working** | `voice/run_headless.py` |
| `jarvis-second-brain` skill | **working** | `.dsh/skills/` |
| Installer | **working** | `install/install.ps1` |
| 3D node galaxy | not built yet | see *Roadmap* |
| Camera "eyes", Telegram, phone agent | not built yet | see *Roadmap* |

---

## Quick start

```powershell
# 1. Install: creates the headless profile, the skill, and the launcher
.\install\install.ps1

# 2. Launch
.\jarvis.cmd
```

Then open **http://127.0.0.1:4731** in **Chrome or Edge** (speech recognition
needs one of those), click **Ear on**, and say:

> *"Jarvis, how much should I invoice PaintCo for the reputation build?"*

You should get the real figure back, read aloud, with the note path cited.

### Or run it by hand

```powershell
$env:JARVIS_VAULT = "D:\path\to\your\notes"
$env:JARVIS_BRAIN = "D:\path\to\Jarvis\brain\brain.py"
cd voice
python server.py
```

---

## The three layers

### 1. `brain/` — the second brain

Your memory is **a folder of markdown files**. Not a database, not embeddings —
files you can read, edit, grep, and put in git. `brain.py` indexes them and
answers two questions: *which notes are relevant?* and *what do they say?*

```bash
python brain/brain.py build   --vault <folder>          # index the vault
python brain/brain.py search  "edr pricing"             # ranked recall
python brain/brain.py context "invoice Mike Johnson"    # prompt-ready briefing
python brain/brain.py stats                             # what's in the brain
python brain/brain.py graph                             # nodes + edges as JSON
```

**How it ranks.** BM25 (the standard sparse-retrieval formula) over an inverted
index, with three additions that matter in practice:

- **Field weighting** — a term in a title counts 3×, in a tag 2.5×, in a heading 0.5×.
- **Coverage** — a note matching 3 of your 3 query terms beats one matching a
  single rare term. Without this, "after hours rate" retrieved a project note
  that merely contained the word *hours*.
- **Link authority** — a note other notes link to is likelier to be canonical.

**Links are the structure.** `[[wikilinks]]` and relative `[markdown](links.md)`
become graph edges. They are what make a related note surface, and they are what
the future 3D view will draw. A note with no links is an orphan.

### 2. `voice/` — the voice shell

A local web app. Speech is handled **entirely in the browser** via the Web
Speech API, so there are no speech API keys and no audio leaves your machine —
only the agent's own model call goes out.

- **Ear** — hands-free. Say *"Jarvis"* and it answers. Chrome stops listening
  after silence, so the shell restarts recognition automatically while the ear is on.
- **Voice** — answers read aloud, chunked so long replies are not cut off, and
  the ear resumes when speech finishes.
- **Memory** — inspect the brain, search it, rebuild the index, all from the UI.
- Requests return immediately with a job id and the page polls, because an agent
  answer takes seconds and a browser cannot block on that.

### 3. `.dsh/skills/jarvis-second-brain/` — the agent's instructions

A DSH skill. It tells the agent *when* to recall, *how* to write facts back, and
the rule that keeps this honest: **quote real numbers, never estimate a price,
and admit when the brain has nothing.**

Recall is also enforced in code — `run_headless.py` retrieves the briefing and
prepends it to every task, so grounding does not depend on the model choosing to
look.

---

## How this relates to the video

This is an independent build inspired by
[Zubair Trabzada's JARVIS](https://www.youtube.com/watch?v=mitzci4FsOg)
(*Is GPT-6 Astra Actually AGI? I Tested It Inside My JARVIS*). His stack is
Claude Code + a paid community installer; this one is model-agnostic and runs on
the DeepSeek Harness.

His architecture, as far as it can be reconstructed from the video and his
public MIT repos ([`holo-gestures`](https://github.com/zubair-trabzada/holo-gestures),
[`brain-map`](https://github.com/zubair-trabzada/brain-map)), is:

| Layer | His implementation | Here |
|---|---|---|
| Runtime | Claude Code skills (`skills/*/SKILL.md` + `agents/*.md`) | Same skill layout, DSH runtime |
| Brain | Anthropic API default, OpenRouter to swap models | Harness model, swappable in settings |
| Memory | Obsidian-style markdown vault | Same, plus agent-oriented recall |
| Voice | Wake word, hands-free, TTS | Web Speech API, no keys |
| Vision | Webcam "eyes", screen-share Focus mode | Not built yet |
| Hands | MediaPipe hand tracking (`holo-gestures`, MIT) | Not built yet |
| 3D view | three.js node galaxy (`brain-map`, MIT) | Not built yet |

The honest read: **the 3D galaxy is presentation; the runtime, the tools, and the
memory are the product.** We built the memory first because it is what makes the
answers correct, and correctness is the hard part.

---

## Roadmap

Roughly in order of value per unit of work:

1. **Tool layer** — web search, local file open, email drafting. His agents
   *act*; ours currently answer. The harness already ships search, shell and
   file tools, so this is mostly prompt and permission work.
2. **3D node galaxy** — port `brain-map`'s renderer for the visual second brain.
   `brain.py graph` already emits the nodes-and-edges JSON it needs.
3. **Write-back from voice** — "Jarvis, remember that..." already works through
   the agent; make it a first-class command with a confirmation step.
4. **Camera eyes** — MediaPipe via a webcam, the natural extension of the shell.
5. **Remote channel** — Telegram, so the assistant is reachable away from the desk.
6. **Focus mode** — screen-share monitoring, as in the video.

---

## Design rules

These are load-bearing. Changing them changes what JARVIS *is*.

1. **Memory is plain files.** If you cannot open it in a text editor, it is the
   wrong design.
2. **Grounding is enforced, not requested.** The briefing is injected in code,
   so recall does not depend on the model's goodwill.
3. **Never invent a number.** A confident wrong price is worse than "I don't know."
4. **Truncation is announced.** A shortened excerpt says so, and gives the path,
   so the agent reads the whole note rather than answering from a fragment.
5. **Zero dependencies** in the Python. It should still run in five years.
6. **Local by default.** Speech, storage and indexing never leave the machine.

---

## Layout

```
Jarvis/
├── brain/
│   ├── brain.py            index, recall, brief          (stdlib only)
│   ├── test_brain.py       37 tests, no pytest needed
│   └── sample-brain/       a working demo vault
├── voice/
│   ├── server.py           local HTTP server + job runner
│   ├── index.html          the UI: ear, voice, memory
│   └── run_headless.py     agent bridge (briefing + one-shot)
├── .dsh/skills/
│   └── jarvis-second-brain/SKILL.md
└── install/
    └── install.ps1         profile + skill + launcher
```

## Tests

```bash
python brain/test_brain.py
```

Covers frontmatter parsing, wikilink resolution, BM25 ordering, the coverage
regression, snippet generation, the truncation rule, and the CLI contract.

## Licence

MIT.

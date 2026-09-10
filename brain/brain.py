#!/usr/bin/env python3
"""
JARVIS second brain — the memory layer.

Turns a folder of markdown notes into a searchable index that Claude Code, the
DeepSeek Harness, or any other agent runtime can query. Zero dependencies:
Python 3 standard library only, exactly like the rest of this repo.

Design mirrors zubair-trabzada/brain-map: `[[wikilinks]]` and relative markdown
links become the graph edges, folders become colour groups. The difference is
that this build is model-agnostic and adds the two operations an *agent* needs
rather than the ones a viewer needs: recall (search) and context (a briefing).

Usage
-----
    python brain.py build   --vault ./notes
    python brain.py search  "invoicing rates" --vault ./notes --limit 5
    python brain.py context "prepare an invoice for Mike" --vault ./notes
    python brain.py stats   --vault ./notes
    python brain.py graph   --vault ./notes

The index is written to <vault>/.jarvis-brain/index.json and is regenerated in
about a second, so re-run `build` whenever notes change.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

# --------------------------------------------------------------------------
# Tunables — every magic number in this file is named, so it can be reasoned
# about and overridden.
# --------------------------------------------------------------------------

BRAIN_DIRNAME = ".jarvis-brain"
INDEX_FILENAME = "index.json"

BM25_K1 = 1.5          # term-frequency saturation
BM25_B = 0.75          # length normalisation
TITLE_BOOST = 3.0      # a term in the title counts triple
TAG_BOOST = 2.5        # a term in the frontmatter tags counts double-plus
LINK_BOOST = 1.5       # target of a note that others link to
COVERAGE_WEIGHT = 2.5  # how hard a note matching *more* of the query wins
MAX_SNIPPET_CHARS = 320
CONTEXT_TOP_NOTES = 8

# How much of each note the briefing carries. Generous on purpose: a truncated
# briefing is worse than a long one, because the agent then answers from a
# partial fact instead of reading the note.
CONTEXT_CHARS_PER_NOTE = 2400

# The floor below which a shortened excerpt carries too little to be worth
# showing; below this we just say "read the note".
CONTEXT_MIN_USEFUL_CHARS = 200

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "do", "does", "for",
    "from", "had", "has", "have", "he", "her", "his", "how", "i", "if", "in",
    "into", "is", "it", "its", "just", "me", "my", "no", "not", "of", "on",
    "or", "our", "out", "she", "so", "than", "that", "the", "their", "them",
    "then", "there", "these", "they", "this", "to", "too", "up", "us", "was",
    "we", "were", "what", "when", "where", "which", "who", "will", "with",
    "you", "your",
}

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
MDLINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+\.md)(?:#[^)]*)?\)")
URL_RE = re.compile(r"https?://\S+")
CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`]*`")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)
FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)
WORD_RE = re.compile(r"[a-z0-9][a-z0-9'\-_]*")


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def _strip_frontmatter(raw: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, body). Deliberately a tiny YAML subset parser.

    It handles the shapes people actually write in note frontmatter —
    `key: value`, `tags: [a, b]`, and `key:` followed by `- item` lines. It is
    not a general YAML parser and does not pretend to be.
    """
    match = FRONTMATTER_RE.match(raw)
    if not match:
        return {}, raw

    data: dict[str, object] = {}
    current_list_key: str | None = None

    for line in match.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        stripped = line.strip()

        if stripped.startswith("- ") and current_list_key:
            bucket = data.setdefault(current_list_key, [])
            if isinstance(bucket, list):
                bucket.append(stripped[2:].strip().strip("'\""))
            continue

        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip().lower()
        value = value.strip()

        if value == "":
            current_list_key = key
            data[key] = []
            continue

        current_list_key = None
        if value.startswith("[") and value.endswith("]"):
            items = [item.strip().strip("'\"") for item in value[1:-1].split(",")]
            data[key] = [item for item in items if item]
        else:
            data[key] = value.strip("'\"")

    return data, raw[match.end():]


def tokenize(text: str) -> list[str]:
    """Lowercase, normalise accents, split into content words."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return [word for word in WORD_RE.findall(text.lower())
            if word not in STOPWORDS and len(word) > 1]


def iter_vault_notes(vault: Path):
    """Yield every markdown note in the vault, skipping the brain's own output."""
    for path in sorted(vault.rglob("*.md")):
        if BRAIN_DIRNAME in path.parts:
            continue
        if any(part.startswith(".") and part != "." for part in path.relative_to(vault).parts):
            continue
        yield path


def read_note(path: Path, vault: Path) -> dict:
    """Parse one note into the record shape the index stores."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    front, body = _strip_frontmatter(raw)

    headings = [title.strip() for _, title in HEADING_RE.findall(body)]
    title = ""
    if isinstance(front.get("title"), str) and front["title"]:
        title = str(front["title"])
    elif headings:
        title = headings[0]
    else:
        title = path.stem.replace("-", " ").replace("_", " ").strip()

    tags: list[str] = []
    raw_tags = front.get("tags")
    if isinstance(raw_tags, list):
        tags = [str(tag).strip().lower() for tag in raw_tags if str(tag).strip()]
    elif isinstance(raw_tags, str) and raw_tags:
        tags = [tag.strip().lower() for tag in raw_tags.split(",") if tag.strip()]

    link_targets = [target.strip() for target in WIKILINK_RE.findall(body)]
    link_targets += [target.strip() for target in MDLINK_RE.findall(body)]

    # Prose without code, URLs, or link syntax — that is what is worth indexing.
    prose = CODE_FENCE_RE.sub(" ", body)
    prose = INLINE_CODE_RE.sub(" ", prose)
    prose = URL_RE.sub(" ", prose)
    prose = WIKILINK_RE.sub(r"\1", prose)
    prose = MDLINK_RE.sub(" ", prose)

    stat = path.stat()
    rel = path.relative_to(vault).as_posix()

    return {
        "path": rel,
        "title": title,
        "folder": str(Path(rel).parent.as_posix()),
        "tags": tags,
        "links": sorted(set(link_targets)),
        "headings": headings,
        "body": prose.strip(),
        "words": len(prose.split()),
        "mtime": stat.st_mtime,
        "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds"),
    }


# --------------------------------------------------------------------------
# Index build + scoring
# --------------------------------------------------------------------------

def build_index(vault: Path) -> dict:
    """Walk the vault and produce the full inverted index."""
    notes: dict[str, dict] = {}
    for path in iter_vault_notes(vault):
        try:
            record = read_note(path, vault)
        except OSError as exc:
            print(f"  ! skipped {path}: {exc}", file=sys.stderr)
            continue
        notes[record["path"]] = record

    # Resolve link targets to real note paths so the graph has edges.
    by_stem: dict[str, list[str]] = defaultdict(list)
    for rel in notes:
        by_stem[Path(rel).stem.lower()].append(rel)

    inbound: Counter[str] = Counter()
    for rel, note in notes.items():
        resolved: list[str] = []
        for target in note["links"]:
            key = Path(target).stem.lower()
            for candidate in by_stem.get(key, []):
                if candidate != rel:
                    resolved.append(candidate)
        note["links"] = sorted(set(resolved))
        for target in note["links"]:
            inbound[target] += 1

    # Term frequencies, from a weighted field soup.
    postings: dict[str, dict[str, float]] = defaultdict(dict)
    doc_len: dict[str, int] = {}

    for rel, note in notes.items():
        weighted = Counter()
        weighted.update(tokenize(note["body"]))
        for token in tokenize(note["title"]):
            weighted[token] += TITLE_BOOST
        for tag in note["tags"]:
            for token in tokenize(tag):
                weighted[token] += TAG_BOOST
        for heading in note["headings"]:
            for token in tokenize(heading):
                weighted[token] += 0.5

        doc_len[rel] = max(1, sum(weighted.values()))
        note["terms"] = len(weighted)
        for term, count in weighted.items():
            postings[term][rel] = float(count)

    avg_len = (sum(doc_len.values()) / len(doc_len)) if doc_len else 1.0

    total = len(notes) or 1
    idf = {
        term: math.log(1 + (total - len(docs) + 0.5) / (len(docs) + 0.5))
        for term, docs in postings.items()
    }

    for note in notes.values():
        note.pop("body_full", None)

    return {
        "version": 1,
        "built": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "vault": str(vault),
        "note_count": len(notes),
        "avg_len": avg_len,
        "doc_len": doc_len,
        "idf": idf,
        "postings": {term: docs for term, docs in postings.items()},
        "notes": notes,
        "inbound": dict(inbound),
        "folders": sorted({note["folder"] for note in notes.values()}),
    }


def index_path(vault: Path) -> Path:
    return vault / BRAIN_DIRNAME / INDEX_FILENAME


def save_index(index: dict, vault: Path) -> Path:
    destination = index_path(vault)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(index)
    payload["vault"] = "."
    destination.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return destination


def load_index(vault: Path) -> dict:
    path = index_path(vault)
    if not path.exists():
        raise SystemExit(
            f"No brain index at {path}.\nRun:  python brain.py build --vault \"{vault}\""
        )
    return json.loads(path.read_text(encoding="utf-8"))


def score_query(index: dict, query: str, limit: int) -> list[dict]:
    """BM25 over the inverted index. Returns ranked note hits."""
    terms = tokenize(query)
    if not terms:
        return []

    # An uncommon term carries the query; a common one barely matters.
    term_weights = {term: 1.0 / (1 + math.log(1 + len(index["postings"].get(term, {}))))
                    for term in set(terms)}

    scores: dict[str, float] = defaultdict(float)
    # How many distinct query terms each note matched. A note that covers more
    # of the question is almost always the right answer, even when a note that
    # matched one rare term outscores it on raw BM25.
    covered: dict[str, int] = defaultdict(int)

    for term in terms:
        docs = index["postings"].get(term)
        if not docs:
            continue
        idf = index["idf"].get(term, 0.0)
        for rel, freq in docs.items():
            length = index["doc_len"].get(rel, 1)
            norm = 1 - BM25_B + BM25_B * (length / index["avg_len"])
            scores[rel] += (idf * (freq * (BM25_K1 + 1)) / (freq + BM25_K1 * norm)
                            * term_weights[term])
        for rel in docs:
            covered[rel] += 1

    # A note that other notes link to is more likely to be canonical. Applied
    # before coverage so it cannot flatten the ratio between two notes.
    if LINK_BOOST:
        for rel in list(scores):
            scores[rel] += LINK_BOOST * math.log(1 + index["inbound"].get(rel, 0))

    # Reward coverage: a note matching 3 of 3 terms beats one matching 1 of 3.
    # This has to out-weigh a single rare term, or "after hours rate" retrieves
    # a project note that merely contained the word "hours".
    if COVERAGE_WEIGHT:
        distinct = len({term for term in terms if index["postings"].get(term)})
        if distinct > 1:
            for rel in list(scores):
                ratio = covered[rel] / distinct
                scores[rel] *= 1 + COVERAGE_WEIGHT * ratio

    if not scores:
        return []

    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:limit]
    top = ordered[0][1] or 1.0

    hits = []
    for rel, raw_score in ordered:
        note = index["notes"][rel]
        hits.append({
            "path": rel,
            "title": note["title"],
            "folder": note["folder"],
            "tags": note["tags"],
            "modified": note["modified"],
            "score": round(raw_score / top, 4),
            "snippet": make_snippet(note["body"], terms),
        })
    return hits


def make_snippet(body: str, terms: list[str]) -> str:
    """Pull the densest window of prose around the query terms."""
    if not body:
        return ""
    flat = " ".join(body.split())
    lowered = flat.lower()

    best_at, best_hits = 0, -1
    for i in range(0, max(1, len(flat) - 1), 200):
        window = lowered[i:i + MAX_SNIPPET_CHARS]
        hits = sum(window.count(term) for term in terms)
        if hits > best_hits:
            best_at, best_hits = i, hits
        if best_hits > 0 and hits == 0:
            break

    start = max(0, best_at - 40)
    snippet = flat[start:start + MAX_SNIPPET_CHARS].strip()
    if start > 0:
        snippet = "…" + snippet
    if start + MAX_SNIPPET_CHARS < len(flat):
        snippet += "…"
    return snippet


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_build(args) -> int:
    vault = Path(args.vault).expanduser().resolve()
    if not vault.is_dir():
        print(f"Vault is not a directory: {vault}", file=sys.stderr)
        return 2

    print(f"Reading brain from {vault} …")
    index = build_index(vault)
    destination = save_index(index, vault)

    links = sum(len(note["links"]) for note in index["notes"].values())
    print(f"  notes      {index['note_count']}")
    print(f"  terms      {len(index['postings'])}")
    print(f"  links      {links}")
    print(f"  folders    {len(index['folders'])}")
    print(f"  written    {destination}")
    return 0


def cmd_search(args) -> int:
    vault = Path(args.vault).expanduser().resolve()
    index = load_index(vault)
    hits = score_query(index, args.query, args.limit)

    if args.json:
        print(json.dumps(hits, indent=2, ensure_ascii=False))
        return 0

    if not hits:
        print(f"No notes matched “{args.query}”.")
        return 1

    print(f"{len(hits)} note(s) for “{args.query}”:\n")
    for hit in hits:
        tags = f"  [{' '.join(hit['tags'])}]" if hit["tags"] else ""
        print(f"  {hit['score']:.2f}  {hit['title']}{tags}")
        print(f"        {hit['path']}   ({hit['modified'][:10]})")
        if hit["snippet"]:
            print(f"        {hit['snippet'][:200]}")
        print()
    return 0


def excerpt(body: str, limit: int) -> tuple[str, bool]:
    """Return (text, truncated).

    Shortening a note is unavoidable for long ones, but silently cutting it
    mid-sentence is how an assistant ends up quoting half a policy. So: prefer a
    paragraph break, then a sentence end, and always report that the note
    continues — the caller prints the real path so the agent can read it whole.
    """
    flat = " ".join(body.split())
    if len(flat) <= limit:
        return flat, False

    window = flat[:limit]

    # Cut at the last paragraph-ish break (a heading, or a double space left by
    # block boundaries) if one is reasonably late in the window.
    for marker in ("\n", " .", " ## "):
        at = window.rfind(marker)
        if at > limit * 0.6:
            return window[:at].rstrip(), True

    # Otherwise cut at the last sentence end.
    sentence_end = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
    if sentence_end > limit * 0.5:
        return window[:sentence_end + 1].rstrip(), True

    # Last resort: cut on a word boundary.
    space = window.rfind(" ")
    return (window[:space].rstrip() if space > 0 else window), True


def cmd_context(args) -> int:
    """Print a prompt-ready briefing for a task, for the agent to consume."""
    vault = Path(args.vault).expanduser().resolve()
    index = load_index(vault)
    hits = score_query(index, args.query, args.limit)

    print(f"# Second-brain context for: {args.query}")
    print(f"<!-- {index['note_count']} notes indexed, built {index['built']} -->")
    print("<!-- Excerpts below may be shortened. Where a note is marked "
          "'continues', read the full file at its path before relying on it. -->")
    print()

    if not hits:
        print("_No relevant notes found. Answer from general knowledge, and say "
              "that the brain had nothing on it._")
        return 0

    for hit in hits:
        note = index["notes"][hit["path"]]
        print(f"## {note['title']}")
        print(f"- path: `{note['path']}`")
        print(f"- words: {note['words']}")
        if note["tags"]:
            print(f"- tags: {', '.join(note['tags'])}")
        if note["links"]:
            print(f"- links to: {', '.join(note['links'][:6])}")
        print()

        text, truncated = excerpt(note["body"], args.chars)
        if len(text) < CONTEXT_MIN_USEFUL_CHARS and truncated:
            print(f"(This note is long; read `{note['path']}` in full.)")
        else:
            print(text)
            if truncated:
                print()
                print(f"(Excerpt ends here — the note continues. "
                      f"Read `{note['path']}` in full for the rest.)")
        print()

    return 0


def cmd_stats(args) -> int:
    vault = Path(args.vault).expanduser().resolve()
    index = load_index(vault)

    print(f"Vault      {vault}")
    print(f"Built      {index['built']}")
    print(f"Notes      {index['note_count']}")
    print(f"Terms      {len(index['postings'])}")
    print(f"Avg words  {index['avg_len']:.0f} (weighted)")

    per_folder = Counter(note["folder"] for note in index["notes"].values())
    if per_folder:
        print("\nNodes by folder (the colour groups):")
        for folder, count in per_folder.most_common():
            label = folder if folder != "." else "(vault root)"
            print(f"  {count:4d}  {label}")

    hubs = sorted(index["inbound"].items(), key=lambda item: item[1], reverse=True)[:5]
    if hubs and hubs[0][1] > 0:
        print("\nMost-linked notes (canonical hubs):")
        for rel, count in hubs:
            if count:
                print(f"  {count:4d}  {index['notes'][rel]['title']}")

    orphans = [rel for rel in index["notes"] if not index["inbound"].get(rel)]
    print(f"\nOrphans (nothing links here): {len(orphans)}")
    return 0


def cmd_graph(args) -> int:
    vault = Path(args.vault).expanduser().resolve()
    index = load_index(vault)

    nodes = [{"id": rel, "title": note["title"], "group": note["folder"],
              "tags": note["tags"], "words": note["words"]}
             for rel, note in index["notes"].items()]
    links = [{"source": rel, "target": target}
             for rel, note in index["notes"].items()
             for target in note["links"]]
    print(json.dumps({"nodes": nodes, "links": links}, indent=2, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="brain.py",
        description="JARVIS second brain — index, recall and brief from a folder of markdown notes.",
    )
    # `--vault` is accepted both before and after the subcommand, because
    # people write both and neither is obviously wrong. The subparser's copy
    # defaults to SUPPRESS so that omitting it there leaves the pre-subcommand
    # value intact instead of overwriting it with the default.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--vault", default=argparse.SUPPRESS,
                        help="folder of markdown notes (default: $JARVIS_VAULT or .)")

    parser.add_argument("--vault", default=os.environ.get("JARVIS_VAULT", "."),
                        help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("build", parents=[common],
                   help="rebuild the index from the vault").set_defaults(func=cmd_build)
    sub.add_parser("stats", parents=[common],
                   help="summarise the brain").set_defaults(func=cmd_stats)
    sub.add_parser("graph", parents=[common],
                   help="emit the note graph as JSON").set_defaults(func=cmd_graph)

    search = sub.add_parser("search", parents=[common],
                            help="ranked recall over the vault")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=5)
    search.add_argument("--json", action="store_true")
    search.set_defaults(func=cmd_search)

    context = sub.add_parser("context", parents=[common],
                             help="prompt-ready briefing for a task")
    context.add_argument("query")
    context.add_argument("--limit", type=int, default=CONTEXT_TOP_NOTES)
    context.add_argument("--chars", type=int, default=CONTEXT_CHARS_PER_NOTE,
                         help="max characters of each note to include")
    context.set_defaults(func=cmd_context)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

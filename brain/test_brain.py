#!/usr/bin/env python3
"""
Tests for the JARVIS second brain.

Zero dependencies — run it directly, no pytest required:

    python brain/test_brain.py

The point of these tests is that memory is the one part of JARVIS where a silent
mistake is expensive: a wrong recall means the assistant quotes the wrong price
with total confidence. So the suite covers the parsing edge cases, the ranking
behaviour, and the truncation rule in particular.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import brain  # noqa: E402


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class VaultFixture(unittest.TestCase):
    """Builds a throwaway vault in a temp dir for each test."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="jarvis-brain-test-"))
        self.vault = self.tmp / "notes"
        self.vault.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def seed(self) -> None:
        write(self.vault / "00-business" / "rates.md", """---
title: Rate card
tags: [finance, rates]
---

# Rate card

Standard seat is $95 per seat per month.
EDR is an add-on at $12 per seat.

## Minimum engagement

Nothing under $495 ex GST.

See [[clients-acme]] for a worked example.
""")
        write(self.vault / "01-clients" / "clients-acme.md", """---
title: Acme Corp
tags: [client, acme]
---

# Acme Corp

Acme has 40 seats and pays $4,280 per month.

Rates live in [[rates]]. Background in [[profile]].
""")
        write(self.vault / "00-business" / "profile.md", """---
title: Company profile
tags: [business]
---

# Company profile

We are a managed IT provider. We win on response time.
""")
        # A note with no frontmatter and no links — the orphan case.
        write(self.vault / "loose-note.md", "# Scattered thought\n\nNo links here.\n")


class TestParsing(VaultFixture):

    def test_frontmatter_and_tags(self):
        self.seed()
        front, body = brain._strip_frontmatter(
            "---\ntitle: X\ntags: [a, b]\n---\n# Body\n"
        )
        self.assertEqual(front["title"], "X")
        self.assertEqual(front["tags"], ["a", "b"])
        self.assertIn("# Body", body)

    def test_frontmatter_block_list(self):
        self.seed()
        front, _ = brain._strip_frontmatter(
            "---\ntags:\n  - finance\n  - rates\n---\nbody\n"
        )
        self.assertEqual(front["tags"], ["finance", "rates"])

    def test_title_falls_back_to_heading(self):
        path = self.vault / "no-frontmatter.md"
        write(path, "# Heading wins\n\ntext\n")
        record = brain.read_note(path, self.vault)
        self.assertEqual(record["title"], "Heading wins")

    def test_title_falls_back_to_filename(self):
        path = self.vault / "my-loose_note.md"
        write(path, "no heading, no frontmatter\n")
        record = brain.read_note(path, self.vault)
        # Separators become spaces so the title reads as a phrase, not a slug.
        self.assertEqual(record["title"], "my loose note")

    def test_wikilinks_and_mdlinks_extracted(self):
        path = self.vault / "n.md"
        write(path, "See [[rates]] and [profile](profile.md) and [[rates|the rates]].\n")
        record = brain.read_note(path, self.vault)
        self.assertIn("rates", record["links"])
        self.assertIn("profile.md", record["links"])

    def test_code_and_urls_are_not_indexed(self):
        path = self.vault / "n.md"
        write(path, "Real prose about invoicing.\n\n```\nsecretvar = 1\n```\n\n"
                   "https://example.com/privatepage\n")
        record = brain.read_note(path, self.vault)
        self.assertNotIn("secretvar", record["body"])
        self.assertNotIn("privatepage", record["body"])
        self.assertIn("invoicing", record["body"])

    def test_tokenizer_drops_stopwords_and_short_tokens(self):
        tokens = brain.tokenize("The invoice is for a big client")
        self.assertNotIn("the", tokens)
        self.assertNotIn("a", tokens)
        self.assertIn("invoice", tokens)
        self.assertIn("client", tokens)

    def test_tokenizer_normalises_accents(self):
        self.assertIn("cafe", brain.tokenize("café"))


class TestIndex(VaultFixture):

    def test_build_indexes_every_note(self):
        self.seed()
        index = brain.build_index(self.vault)
        self.assertEqual(index["note_count"], 4)
        self.assertGreater(len(index["postings"]), 10)

    def test_brain_directory_is_excluded_from_its_own_index(self):
        self.seed()
        brain.save_index(brain.build_index(self.vault), self.vault)
        # A stray .md inside the brain's own folder must never be indexed.
        write(self.vault / brain.BRAIN_DIRNAME / "scratch.md", "# noise\n")
        index = brain.build_index(self.vault)
        self.assertNotIn(f"{brain.BRAIN_DIRNAME}/scratch.md", index["notes"])

    def test_links_resolve_to_real_paths(self):
        self.seed()
        index = brain.build_index(self.vault)
        acme = index["notes"]["01-clients/clients-acme.md"]
        self.assertIn("00-business/rates.md", acme["links"])
        self.assertIn("00-business/profile.md", acme["links"])

    def test_inbound_counts_hubs(self):
        self.seed()
        index = brain.build_index(self.vault)
        self.assertEqual(index["inbound"].get("00-business/rates.md"), 1)

    def test_unresolvable_links_are_dropped(self):
        self.seed()
        index = brain.build_index(self.vault)
        for note in index["notes"].values():
            for link in note["links"]:
                self.assertIn(link, index["notes"])

    def test_save_and_load_round_trip(self):
        self.seed()
        brain.save_index(brain.build_index(self.vault), self.vault)
        loaded = brain.load_index(self.vault)
        self.assertEqual(loaded["note_count"], 4)
        self.assertIn("postings", loaded)
        self.assertIn("idf", loaded)

    def test_load_without_index_is_a_clear_error(self):
        self.seed()
        with self.assertRaises(SystemExit) as caught:
            brain.load_index(self.vault)
        self.assertIn("brain.py build", str(caught.exception))


class TestScoring(VaultFixture):

    def setUp(self):
        super().setUp()
        self.seed()
        self.index = brain.build_index(self.vault)

    def test_relevant_note_ranks_first(self):
        hits = brain.score_query(self.index, "minimum engagement", 5)
        self.assertTrue(hits)
        self.assertEqual(hits[0]["path"], "00-business/rates.md")

    def test_client_query_finds_the_client(self):
        hits = brain.score_query(self.index, "how much does Acme pay per month", 5)
        self.assertEqual(hits[0]["path"], "01-clients/clients-acme.md")

    def test_scores_are_normalised_and_ordered(self):
        hits = brain.score_query(self.index, "rate card invoice", 5)
        self.assertEqual(hits[0]["score"], 1.0)
        scores = [hit["score"] for hit in hits]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_limit_is_respected(self):
        hits = brain.score_query(self.index, "managed provider client", 2)
        self.assertLessEqual(len(hits), 2)

    def test_note_covering_more_query_terms_outranks_a_rare_single_match(self):
        # Regression: a note that matched ONE rare term used to outrank a note
        # that matched TWO common ones, because the rare term's IDF was large
        # enough to swamp everything. Coverage weighting fixes that ordering.
        #
        # "rare" is the only note containing the hapax "zebra", so it carries the
        # maximum IDF for that term; "common" matches the two ordinary terms.
        write(self.vault / "02-projects" / "rare.md", """---
title: Rare term note
tags: [project]
---

# Rare term note

A zebra appears once in the whole vault.
""")
        write(self.vault / "00-business" / "common.md", """---
title: Invoicing and retainer policy
tags: [finance]
---

# Invoicing and retainer policy

The retainer is invoiced monthly, and invoicing is automatic.
""")
        index = brain.build_index(self.vault)

        # The hapax alone would win on raw BM25.
        rare_only = brain.score_query(index, "zebra", 1)
        self.assertEqual(rare_only[0]["path"], "02-projects/rare.md")

        # But matching two of three query terms is the better answer.
        hits = brain.score_query(index, "zebra invoicing retainer", 3)
        self.assertTrue(hits)
        self.assertEqual(hits[0]["path"], "00-business/common.md",
                         "a note matching 2 of 3 terms should beat one "
                         "matching 1 rare term")
        self.assertGreater(hits[0]["score"], hits[1]["score"])

    def test_coverage_never_reorders_a_single_term_query(self):
        hits = brain.score_query(self.index, "engagement", 3)
        self.assertEqual(hits[0]["path"], "00-business/rates.md")

    def test_a_note_matching_every_term_beats_partial_matches(self):
        write(self.vault / "00-business" / "all.md", """---
title: Retainer invoicing rates
tags: [rates]
---

# Retainer invoicing rates

Retainer rates, invoicing, and rates again.
""")
        index = brain.build_index(self.vault)
        hits = brain.score_query(index, "retainer invoicing rates", 3)
        self.assertEqual(hits[0]["path"], "00-business/all.md")

    def test_unmatched_query_returns_nothing(self):
        self.assertEqual(brain.score_query(self.index, "zzzzqqqq", 5), [])

    def test_empty_query_returns_nothing(self):
        self.assertEqual(brain.score_query(self.index, "", 5), [])

    def test_stopword_only_query_returns_nothing(self):
        self.assertEqual(brain.score_query(self.index, "the and of", 5), [])

    def test_hit_shape_is_complete(self):
        hit = brain.score_query(self.index, "rates", 1)[0]
        for field in ("path", "title", "folder", "tags", "modified", "score", "snippet"):
            self.assertIn(field, hit)

    def test_snippet_contains_a_query_term_when_present(self):
        hit = brain.score_query(self.index, "engagement", 1)[0]
        self.assertIn("engagement", hit["snippet"].lower())


class TestExcerpt(VaultFixture):
    """The truncation rule — the bug that silently dropped a real policy fact."""

    def test_short_body_is_returned_whole(self):
        text, truncated = brain.excerpt("A short note.", 100)
        self.assertEqual(text, "A short note.")
        self.assertFalse(truncated)

    def test_long_body_is_marked_truncated(self):
        text, truncated = brain.excerpt("word " * 500, 100)
        self.assertTrue(truncated)
        self.assertLessEqual(len(text), 110)

    def test_truncation_prefers_a_sentence_boundary(self):
        body = ("First sentence is here. " * 20).strip()
        text, truncated = brain.excerpt(body, 120)
        self.assertTrue(truncated)
        self.assertTrue(text.endswith("."), f"did not end on a sentence: {text!r}")

    def test_truncation_never_splits_a_word(self):
        body = "alpha " * 300
        text, truncated = brain.excerpt(body, 100)
        self.assertTrue(truncated)
        self.assertFalse(text.endswith("alph"), f"split a word: {text!r}")

    def test_a_fact_past_the_limit_is_flagged_not_silently_dropped(self):
        # The real regression: a rate sat just beyond the excerpt limit and the
        # agent was never told the note continued.
        body = "filler " * 400 + " MINIMUM ENGAGEMENT is 495 dollars."
        text, truncated = brain.excerpt(body, 300)
        self.assertNotIn("495", text)
        self.assertTrue(truncated, "must admit the note continues")


class TestContextCommand(VaultFixture):

    def test_context_includes_paths_and_facts(self):
        import io
        from contextlib import redirect_stdout
        self.seed()
        brain.save_index(brain.build_index(self.vault), self.vault)

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = brain.main(["context", "minimum engagement", "--vault", str(self.vault)])
        output = buffer.getvalue()

        self.assertEqual(code, 0)
        self.assertIn("00-business/rates.md", output)
        self.assertIn("495", output)
        self.assertIn("Second-brain context", output)

    def test_context_says_so_when_nothing_matches(self):
        import io
        from contextlib import redirect_stdout
        self.seed()
        brain.save_index(brain.build_index(self.vault), self.vault)

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            brain.main(["context", "zzzznothing", "--vault", str(self.vault)])
        self.assertIn("No relevant notes found", buffer.getvalue())

    def test_vault_flag_works_before_and_after_subcommand(self):
        import io
        from contextlib import redirect_stdout
        self.seed()
        brain.save_index(brain.build_index(self.vault), self.vault)

        for argv in (["--vault", str(self.vault), "stats"],
                     ["stats", "--vault", str(self.vault)]):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = brain.main(argv)
            self.assertEqual(code, 0, f"failed for {argv}")
            self.assertIn("Notes", buffer.getvalue())


class TestCli(VaultFixture):

    def test_graph_emits_valid_json(self):
        import io
        from contextlib import redirect_stdout
        self.seed()
        brain.save_index(brain.build_index(self.vault), self.vault)

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            brain.main(["graph", "--vault", str(self.vault)])

        graph = json.loads(buffer.getvalue())
        self.assertEqual(len(graph["nodes"]), 4)
        self.assertTrue(graph["links"])
        for link in graph["links"]:
            self.assertIn(link["target"], {node["id"] for node in graph["nodes"]})

    def test_build_on_a_missing_vault_fails_clearly(self):
        import io
        from contextlib import redirect_stderr
        buffer = io.StringIO()
        with redirect_stderr(buffer):
            code = brain.main(["build", "--vault", str(self.tmp / "nope")])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""lens collector test suite — stdlib unittest, zero network.

Every parse_* function is driven off a trimmed local fixture under fixtures/.
The collector NEVER reaches the network here: we exercise the pure parse
functions directly, and the run()/save() tests inject fake adapters + sources.

Run: python3 -m unittest discover -s tests -v
"""
import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

# Make `import collect` work regardless of CWD when discover runs.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import collect  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fx_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fx_json(name: str):
    return json.loads(fx_bytes(name).decode("utf-8"))


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
class TestHelpers(unittest.TestCase):
    def test_item_id_formula(self):
        # Locks the exact formula sha1(f"{source_key}\n{url}").
        import hashlib
        key, url = "some-source", "https://example.com/x"
        expected = hashlib.sha1(f"{key}\n{url}".encode("utf-8")).hexdigest()
        self.assertEqual(collect.item_id(key, url), expected)

    def test_item_id_stable_and_distinct(self):
        a = collect.item_id("s", "https://example.com/a")
        self.assertEqual(a, collect.item_id("s", "https://example.com/a"))  # stable
        self.assertNotEqual(a, collect.item_id("s", "https://example.com/b"))  # url matters
        self.assertNotEqual(a, collect.item_id("t", "https://example.com/a"))  # key matters

    def test_norm_date_rfc822(self):
        self.assertEqual(
            collect.norm_date("Mon, 02 Jun 2025 12:00:00 +0000"),
            "2025-06-02T12:00:00+00:00",
        )

    def test_norm_date_iso_with_z(self):
        self.assertEqual(collect.norm_date("2025-06-02T12:00:00Z"), "2025-06-02T12:00:00+00:00")

    def test_norm_date_iso_fractional(self):
        self.assertEqual(collect.norm_date("2025-06-01T00:00:00.000Z"), "2025-06-01T00:00:00+00:00")

    def test_norm_date_offset_converted_to_utc(self):
        # +02:00 -> UTC shifts the hour back by two.
        self.assertEqual(collect.norm_date("2025-06-02T12:00:00+02:00"), "2025-06-02T10:00:00+00:00")

    def test_norm_date_garbage_returned_verbatim(self):
        self.assertEqual(collect.norm_date("not a date"), "not a date")  # never raises

    def test_norm_date_empty_and_none(self):
        self.assertIsNone(collect.norm_date(None))
        self.assertIsNone(collect.norm_date(""))

    def test_norm_date_non_str_does_not_raise(self):
        # A numeric/odd timestamp must degrade, not blow up.
        self.assertEqual(collect.norm_date(1234567890), "1234567890")

    def test_strip_html_tags_and_entities(self):
        self.assertEqual(collect.strip_html("<p>Hello <b>world</b></p>"), "Hello world")
        self.assertEqual(collect.strip_html("a &amp; b"), "a & b")
        self.assertEqual(collect.strip_html("x&nbsp;y"), "x y")

    def test_strip_html_decodes_common_entities(self):
        # Not just &amp;/&nbsp; — &quot;, &lt;/&gt;, and numeric refs too.
        self.assertEqual(collect.strip_html("say &quot;hi&quot;"), 'say "hi"')
        self.assertEqual(collect.strip_html("a &lt;tag&gt; b"), "a <tag> b")
        self.assertEqual(collect.strip_html("it&#39;s"), "it's")
        self.assertEqual(collect.strip_html("5 &gt; 3 &amp;&amp; 1 &lt; 2"), "5 > 3 && 1 < 2")

    def test_strip_html_tolerates_non_str(self):
        # Feeds occasionally hand us an int where text is expected.
        self.assertEqual(collect.strip_html(123), "123")

    def test_strip_html_removes_script_and_style(self):
        out = collect.strip_html("<script>evil()</script>keep<style>.a{}</style> me")
        self.assertNotIn("evil", out)
        self.assertNotIn(".a{", out)
        self.assertIn("keep", out)
        self.assertIn("me", out)

    def test_strip_html_truncates_to_limit(self):
        self.assertEqual(collect.strip_html("abcdefghij", limit=5), "abcde")

    def test_strip_html_none(self):
        self.assertEqual(collect.strip_html(None), "")


# --------------------------------------------------------------------------- #
# adapter parse functions (off fixtures, no network)
# --------------------------------------------------------------------------- #
class TestParseGenericFeed(unittest.TestCase):
    def test_rss(self):
        recs = collect.parse_generic_feed(fx_bytes("generic_feed_rss.xml"))
        self.assertEqual(len(recs), 2)
        first = recs[0]
        self.assertEqual(first["title"], "First Post")
        self.assertEqual(first["url"], "https://example.com/first")
        self.assertEqual(first["author"], "Jane Doe")  # dc:creator
        self.assertEqual(first["published_at"], "2025-06-02T12:00:00+00:00")
        self.assertNotIn("<", first["summary"])  # html stripped
        self.assertIn("bold", first["summary"])
        # content:encoded becomes the summary on the second item
        self.assertEqual(recs[1]["summary"], "Full content here")

    def test_atom(self):
        recs = collect.parse_generic_feed(fx_bytes("generic_feed_atom.xml"))
        self.assertEqual(len(recs), 2)
        first = recs[0]
        self.assertEqual(first["title"], "Atom One")
        self.assertEqual(first["url"], "https://example.org/atom-one")  # alternate, not edit
        self.assertEqual(first["author"], "Alice")
        self.assertEqual(first["published_at"], "2025-06-04T10:00:00+00:00")
        # link with no rel defaults to alternate
        self.assertEqual(recs[1]["url"], "https://example.org/atom-two")
        self.assertEqual(recs[1]["summary"], "Inline content body")

    def test_empty_rss(self):
        empty = b'<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>'
        self.assertEqual(collect.parse_generic_feed(empty), [])

    def test_empty_atom(self):
        empty = b'<feed xmlns="http://www.w3.org/2005/Atom"><title>e</title></feed>'
        self.assertEqual(collect.parse_generic_feed(empty), [])

    def test_malformed_xml_returns_empty_not_raises(self):
        # A truncated body / HTML error page must yield [] instead of throwing.
        self.assertEqual(collect.parse_generic_feed(b"not xml"), [])
        self.assertEqual(collect.parse_generic_feed(b"<rss><channel><item>"), [])
        self.assertEqual(collect.parse_generic_feed(b""), [])
        self.assertEqual(collect.parse_generic_feed(None), [])
        self.assertEqual(collect.parse_generic_feed(123), [])  # non-bytes scalar

    def test_item_missing_url_kept_by_parse_dropped_by_save(self):
        # An item with a title but no link is still parsed (title-only); save()
        # is what drops it because it has no url (see TestInvariant/isolation).
        rss = (
            b'<rss version="2.0"><channel>'
            b"<item><title>Only Title</title></item>"
            b"<item><description>no title, no link</description></item>"
            b"</channel></rss>"
        )
        recs = collect.parse_generic_feed(rss)
        self.assertEqual(len(recs), 1)  # the no-title/no-link item is dropped
        self.assertEqual(recs[0]["title"], "Only Title")
        self.assertEqual(recs[0]["url"], "")


class TestParseHfDailyPapers(unittest.TestCase):
    def test_basic(self):
        recs = collect.parse_hf_daily_papers(fx_json("hf_daily_papers.json"))
        self.assertEqual(len(recs), 2)
        first = recs[0]
        self.assertEqual(first["title"], "Paper One")
        self.assertEqual(first["url"], "https://huggingface.co/papers/2506.00001")
        self.assertEqual(first["author"], "Researcher A")
        self.assertEqual(first["category"], "paper")
        self.assertEqual(first["published_at"], "2025-06-01T00:00:00+00:00")
        self.assertIn("▲42", first["summary"])
        self.assertNotIn("<", first["summary"])
        # second paper has empty authors + no upvotes
        self.assertEqual(recs[1]["author"], "")
        self.assertNotIn("▲", recs[1]["summary"])

    def test_missing_nested_paper_key(self):
        recs = collect.parse_hf_daily_papers([{"title": "X", "url": "https://x/y"}])
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["url"], "https://x/y")  # falls back to row url

    def test_non_list_and_empty(self):
        self.assertEqual(collect.parse_hf_daily_papers({}), [])
        self.assertEqual(collect.parse_hf_daily_papers([]), [])

    def test_malformed_shapes_do_not_raise(self):
        # `paper` is a string, not the expected dict.
        recs = collect.parse_hf_daily_papers([{"paper": "bad"}])
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["url"], "")  # no pid, no row url -> empty, save() drops it
        # `authors` not a list / row not a dict / None passed in.
        self.assertEqual(collect.parse_hf_daily_papers([{"paper": {"authors": "x"}}])[0]["author"], "")
        self.assertEqual(collect.parse_hf_daily_papers([None, 5]), [])
        self.assertEqual(collect.parse_hf_daily_papers(None), [])


class TestParseHnAlgolia(unittest.TestCase):
    def test_basic(self):
        recs = collect.parse_hn_algolia(fx_json("hn_algolia.json"))
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0]["url"], "https://blog.example/post")
        self.assertEqual(recs[0]["author"], "hnuser")
        self.assertEqual(recs[0]["category"], "other")
        self.assertIn("120 points", recs[0]["summary"])
        # no url -> falls back to the HN item permalink
        self.assertEqual(recs[1]["url"], "https://news.ycombinator.com/item?id=222")

    def test_empty(self):
        self.assertEqual(collect.parse_hn_algolia({}), [])
        self.assertEqual(collect.parse_hn_algolia({"hits": []}), [])

    def test_malformed_shapes_do_not_raise(self):
        self.assertEqual(collect.parse_hn_algolia({"hits": None}), [])  # null hits
        self.assertEqual(collect.parse_hn_algolia(None), [])
        self.assertEqual(collect.parse_hn_algolia([1, 2]), [])
        # non-dict hits are skipped, valid ones kept.
        self.assertEqual(collect.parse_hn_algolia({"hits": ["x", {"title": "t", "url": "https://x/1"}]})[0]["url"], "https://x/1")


class TestParseOssinsight(unittest.TestCase):
    def test_basic(self):
        recs = collect.parse_ossinsight(fx_json("ossinsight.json"))
        self.assertEqual(len(recs), 1)  # empty repo_name skipped
        self.assertEqual(recs[0]["url"], "https://github.com/owner/repo")
        self.assertEqual(recs[0]["author"], "owner")
        self.assertEqual(recs[0]["category"], "repo")
        self.assertIn("★1000", recs[0]["summary"])

    def test_rows_top_level_fallback(self):
        recs = collect.parse_ossinsight({"rows": [{"repo_name": "a/b"}]})
        self.assertEqual(recs[0]["url"], "https://github.com/a/b")

    def test_empty(self):
        self.assertEqual(collect.parse_ossinsight({}), [])
        self.assertEqual(collect.parse_ossinsight([]), [])

    def test_malformed_shapes_do_not_raise(self):
        self.assertEqual(collect.parse_ossinsight({"rows": None}), [])  # null rows
        self.assertEqual(collect.parse_ossinsight({"data": {"rows": None}}), [])
        # `data` present but NOT a dict (string / list / number) -> [] not AttributeError.
        self.assertEqual(collect.parse_ossinsight({"data": "bad"}), [])
        self.assertEqual(collect.parse_ossinsight({"data": ["bad"]}), [])
        self.assertEqual(collect.parse_ossinsight({"data": 7}), [])
        # nested dict whose rows are junk (non-list) also degrades to [].
        self.assertEqual(collect.parse_ossinsight({"data": {"rows": "bad"}}), [])
        self.assertEqual(collect.parse_ossinsight(None), [])
        self.assertEqual(collect.parse_ossinsight({"rows": ["x", 1]}), [])  # non-dict rows
        # non-str repo_name degrades instead of crashing on .split().
        self.assertEqual(collect.parse_ossinsight({"rows": [{"repo_name": 123}]})[0]["author"], "123")


class TestParseYcLaunches(unittest.TestCase):
    def test_basic(self):
        recs = collect.parse_yc_launches(fx_json("yc_launches.json"))
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0]["url"], "https://www.ycombinator.com/launches/launch-one")
        self.assertEqual(recs[0]["author"], "CoolCo")
        self.assertEqual(recs[0]["category"], "launch")
        self.assertIn("▲30", recs[0]["summary"])
        self.assertNotIn("<", recs[0]["summary"])
        # search_path used directly; company as a plain string
        self.assertEqual(recs[1]["url"], "https://www.ycombinator.com/launches/launch-two")
        self.assertEqual(recs[1]["author"], "PlainCo")

    def test_empty(self):
        self.assertEqual(collect.parse_yc_launches({}), [])
        self.assertEqual(collect.parse_yc_launches([]), [])

    def test_malformed_shapes_do_not_raise(self):
        self.assertEqual(collect.parse_yc_launches({"hits": None}), [])  # null hits
        self.assertEqual(collect.parse_yc_launches(None), [])
        self.assertEqual(collect.parse_yc_launches(["x", 1]), [])  # non-dict entries
        # company present but not a dict, and an int tagline -> no crash.
        recs = collect.parse_yc_launches([{"title": "t", "company": 5, "url": "https://x", "tagline": 9}])
        self.assertEqual(recs[0]["author"], "5")
        self.assertEqual(recs[0]["summary"], "9")


class TestParseHfModels(unittest.TestCase):
    def test_basic(self):
        recs = collect.parse_hf_models(fx_json("hf_models.json"))
        self.assertEqual(len(recs), 3)  # the id-less entry is dropped
        first = recs[0]
        self.assertEqual(first["title"], "org-name/cool-model")
        self.assertEqual(first["url"], "https://huggingface.co/org-name/cool-model")
        self.assertEqual(first["author"], "org-name")  # org before the slash
        self.assertEqual(first["category"], "repo")
        self.assertEqual(first["published_at"], "2025-06-05T08:00:00+00:00")
        self.assertIn("↓12345", first["summary"])
        self.assertIn("♥678", first["summary"])
        self.assertIn("text-generation", first["summary"])  # pipeline_tag preferred

    def test_no_org_uses_full_id(self):
        recs = collect.parse_hf_models(fx_json("hf_models.json"))
        solo = recs[1]
        self.assertEqual(solo["title"], "solomodel")
        self.assertEqual(solo["author"], "solomodel")
        self.assertIn("diffusers", solo["summary"])  # library_name fallback

    def test_kind_dash_fallback_and_zero_counts(self):
        recs = collect.parse_hf_models(fx_json("hf_models.json"))
        bare = recs[2]
        self.assertIn("↓0", bare["summary"])
        self.assertIn("♥0", bare["summary"])
        self.assertIn("—", bare["summary"])  # neither pipeline_tag nor library_name

    def test_missing_id_dropped(self):
        recs = collect.parse_hf_models([{"downloads": 1}, {"id": "  "}, {"id": "a/b"}])
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["title"], "a/b")

    def test_non_list_and_empty(self):
        self.assertEqual(collect.parse_hf_models({}), [])
        self.assertEqual(collect.parse_hf_models([]), [])

    def test_malformed_shapes_do_not_raise(self):
        # A non-str id (e.g. 123) must not crash on .strip(); it degrades to text.
        recs = collect.parse_hf_models([{"id": 123}])
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["title"], "123")
        self.assertEqual(recs[0]["url"], "https://huggingface.co/123")
        self.assertEqual(collect.parse_hf_models(None), [])
        self.assertEqual(collect.parse_hf_models(["x", 7]), [])  # non-dict entries skipped


class TestParseGithubReleases(unittest.TestCase):
    def test_basic(self):
        recs = collect.parse_github_releases(fx_json("github_releases.json"), "owner/repo")
        self.assertEqual(len(recs), 1)  # draft + empty html_url dropped
        rel = recs[0]
        self.assertEqual(rel["title"], "owner/repo v1.2.0")
        self.assertEqual(rel["url"], "https://github.com/owner/repo/releases/tag/v1.2.0")
        self.assertEqual(rel["author"], "maintainer")
        self.assertEqual(rel["category"], "repo")
        self.assertEqual(rel["published_at"], "2025-06-06T00:00:00+00:00")
        self.assertNotIn("<", rel["summary"])
        self.assertIn("Changes", rel["summary"])

    def test_title_falls_back_to_name(self):
        recs = collect.parse_github_releases(
            [{"name": "Just a name", "html_url": "https://x/y"}], "a/b"
        )
        self.assertEqual(recs[0]["title"], "a/b Just a name")

    def test_skips_draft_and_missing_url(self):
        recs = collect.parse_github_releases(
            [
                {"tag_name": "v1", "html_url": "https://x/1", "draft": True},
                {"tag_name": "v2", "html_url": ""},
                {"tag_name": "v3", "html_url": "https://x/3"},
            ],
            "a/b",
        )
        self.assertEqual([r["url"] for r in recs], ["https://x/3"])

    def test_non_list_and_empty(self):
        self.assertEqual(collect.parse_github_releases({}, "a/b"), [])
        self.assertEqual(collect.parse_github_releases([], "a/b"), [])

    def test_malformed_shapes_do_not_raise(self):
        self.assertEqual(collect.parse_github_releases(None, "a/b"), [])
        self.assertEqual(collect.parse_github_releases(["x", 1], "a/b"), [])  # non-dict skipped
        # author not a dict, non-str tag_name/body -> no crash, degrades to text.
        recs = collect.parse_github_releases(
            [{"html_url": "https://x/1", "tag_name": 12, "body": 99, "author": "nope"}], "a/b"
        )
        self.assertEqual(recs[0]["title"], "a/b 12")
        self.assertEqual(recs[0]["author"], "")  # author wasn't a dict
        self.assertEqual(recs[0]["summary"], "99")


# --------------------------------------------------------------------------- #
# HTML adapters (Loop 2) — parsed off trimmed local fixtures, no network.
# --------------------------------------------------------------------------- #
class TestParseClaudeReleaseNotes(unittest.TestCase):
    def test_basic(self):
        recs = collect.parse_claude_release_notes(fx_bytes("claude_release_notes.html"))
        # Two dated <h3> entries; the month <h2> dividers and footer <h3> are not entries.
        self.assertEqual(len(recs), 2)
        first = recs[0]
        self.assertEqual(first["title"], "June 2, 2026")
        self.assertEqual(first["category"], "product")
        self.assertEqual(first["published_at"], "2026-06-02")  # human date normalized
        # url is absolute and carries the heading's #anchor.
        self.assertTrue(first["url"].startswith("https://"))
        self.assertTrue(first["url"].endswith("#h_f49ce4f650"))
        # summary is the text between this h3 and the next heading, html stripped.
        self.assertNotIn("<", first["summary"])
        self.assertIn("custom roles", first["summary"])
        # ...and it stops at the next month divider (no May content bleeding in).
        self.assertNotIn("Opus", first["summary"])

    def test_second_entry_and_entities(self):
        recs = collect.parse_claude_release_notes(fx_bytes("claude_release_notes.html"))
        second = recs[1]
        self.assertEqual(second["title"], "May 28, 2026")
        self.assertEqual(second["published_at"], "2026-05-28")
        self.assertTrue(second["url"].endswith("#h_a467350ea5"))
        self.assertIn("Opus 4.8", second["summary"])
        self.assertIn("coding & reasoning", second["summary"])  # &amp; / &#8217; decoded
        self.assertIn("’", second["summary"])  # right single quote

    def test_non_release_or_structureless_html_returns_empty(self):
        # Defensive: structureless / non-release HTML must NOT become a
        # saved-looking product record — it returns [] (never invents data).
        # An error page with no date heading:
        self.assertEqual(
            collect.parse_claude_release_notes(b"<html><body><p>Access denied</p></body></html>"), []
        )
        # An <article> whose only <h3> is NOT a date (e.g. an FAQ) is not an entry:
        self.assertEqual(
            collect.parse_claude_release_notes(
                b"<article><h3>Frequently asked questions</h3><p>Not a dated release note.</p></article>"
            ),
            [],
        )
        # Body text but no date heading at all -> still [], not a whole-page row:
        self.assertEqual(
            collect.parse_claude_release_notes(
                b"<html><head><title>Claude Release Notes</title></head>"
                b"<body><p>Some notes text.</p></body></html>"
            ),
            [],
        )

    def test_empty_and_malformed_return_empty(self):
        self.assertEqual(collect.parse_claude_release_notes(b""), [])
        self.assertEqual(collect.parse_claude_release_notes(None), [])
        self.assertEqual(collect.parse_claude_release_notes(123), [])  # non-bytes scalar
        # Markup with no text content degrades to [] (nothing to show).
        self.assertEqual(collect.parse_claude_release_notes(b"<html><body></body></html>"), [])

    def test_base_url_override(self):
        recs = collect.parse_claude_release_notes(
            fx_bytes("claude_release_notes.html"), base_url="https://example.test/notes"
        )
        self.assertTrue(recs[0]["url"].startswith("https://example.test/notes#"))


class TestParseMistralChangelog(unittest.TestCase):
    def test_basic(self):
        recs = collect.parse_mistral_changelog(fx_bytes("mistral_changelog.html"))
        self.assertEqual(len(recs), 2)  # two dated entries; footer h3s are not entries
        first = recs[0]
        self.assertEqual(first["title"], "May 28, 2026")  # h2 label + year from the id
        self.assertEqual(first["category"], "api_change")
        self.assertEqual(first["published_at"], "2026-05-28")  # straight from id="date-..."
        self.assertEqual(first["author"], "")
        self.assertTrue(first["url"].startswith("https://"))
        self.assertTrue(first["url"].endswith("#date-2026-05-28"))
        self.assertNotIn("<", first["summary"])
        self.assertIn("Vibe", first["summary"])
        self.assertNotIn("April", first["summary"])  # next entry didn't bleed in

    def test_second_entry(self):
        recs = collect.parse_mistral_changelog(fx_bytes("mistral_changelog.html"))
        second = recs[1]
        self.assertEqual(second["title"], "April 28, 2026")
        self.assertEqual(second["published_at"], "2026-04-28")
        self.assertTrue(second["url"].endswith("#date-2026-04-28"))
        self.assertIn("batch API endpoints", second["summary"])
        self.assertIn("&", second["summary"])  # &amp; decoded

    def test_empty_and_malformed_return_empty(self):
        self.assertEqual(collect.parse_mistral_changelog(b""), [])
        self.assertEqual(collect.parse_mistral_changelog(None), [])
        self.assertEqual(collect.parse_mistral_changelog(123), [])
        # A page with no changelog entries -> [] (no invented rows).
        self.assertEqual(collect.parse_mistral_changelog(b"<html><body><h1>Changelog</h1></body></html>"), [])
        # A <div id="date-..."> WITHOUT data-changelog-entry="true" is NOT a row:
        # requiring the marker keeps unrelated dated divs out of the changelog.
        self.assertEqual(
            collect.parse_mistral_changelog(b'<div id="date-2026-01-01"><p>not a changelog entry</p></div>'), []
        )
        # The marker alone, without a date-shaped id, is also not enough.
        self.assertEqual(
            collect.parse_mistral_changelog(b'<div data-changelog-entry="true" id="sidebar"><p>nope</p></div>'), []
        )

    def test_base_url_override(self):
        recs = collect.parse_mistral_changelog(
            fx_bytes("mistral_changelog.html"), base_url="https://example.test/cl"
        )
        self.assertEqual(recs[0]["url"], "https://example.test/cl#date-2026-05-28")


class TestParseA16zPortfolio(unittest.TestCase):
    def test_basic(self):
        recs = collect.parse_a16z_portfolio(fx_bytes("a16z_portfolio.html"))
        # 3 valid companies (single-quoted, entity-encoded, relative-url); the
        # malformed JSON blob is skipped, not fatal.
        self.assertEqual(len(recs), 3)
        air = recs[0]
        self.assertEqual(air["title"], "Airbnb")
        self.assertEqual(air["url"], "https://a16z.com/companies/airbnb/")
        self.assertEqual(air["author"], "")
        self.assertEqual(air["category"], "funding")
        self.assertEqual(air["published_at"], "2011-07-22")  # initial_a16z_date_funded
        self.assertNotIn("<", air["summary"])
        self.assertIn("marketplace", air["summary"])
        self.assertIn("&", air["summary"])  # &amp; decoded

    def test_entity_encoded_blob(self):
        recs = collect.parse_a16z_portfolio(fx_bytes("a16z_portfolio.html"))
        fig = recs[1]
        self.assertEqual(fig["title"], "Figma")
        self.assertEqual(fig["url"], "https://a16z.com/companies/figma/")
        self.assertEqual(fig["published_at"], "2020-04-29")  # investment_date preferred
        self.assertIn("design tool", fig["summary"])

    def test_relative_permalink_absolutized(self):
        recs = collect.parse_a16z_portfolio(fx_bytes("a16z_portfolio.html"))
        rel = recs[2]
        self.assertEqual(rel["title"], "Relativeco")
        self.assertEqual(rel["url"], "https://a16z.com/companies/relativeco/")  # joined to base
        self.assertIsNone(rel["published_at"])  # no date fields

    def test_empty_and_malformed_return_empty(self):
        self.assertEqual(collect.parse_a16z_portfolio(b""), [])
        self.assertEqual(collect.parse_a16z_portfolio(None), [])
        self.assertEqual(collect.parse_a16z_portfolio(123), [])
        # No data-company blobs -> [].
        self.assertEqual(collect.parse_a16z_portfolio(b"<html><body><p>hi</p></body></html>"), [])
        # A blob that isn't a JSON object (array / scalar) is skipped, not fatal.
        self.assertEqual(collect.parse_a16z_portfolio(b"<div data-company='[1,2]'></div>"), [])

    def test_base_url_override_for_relative(self):
        recs = collect.parse_a16z_portfolio(
            b"<div data-company='{\"name\":\"X\",\"permalink\":\"/companies/x/\"}'></div>",
            base_url="https://example.test/portfolio/",
        )
        self.assertEqual(recs[0]["url"], "https://example.test/companies/x/")


class TestAdaptGithubReleases(unittest.TestCase):
    """The adapt_github_releases shell (network stubbed): per-repo errors are
    isolated and each repo's releases are labeled with the repo they came from."""

    def _patch_fetch_json(self, responder):
        orig = collect.fetch_json
        collect.fetch_json = responder
        self.addCleanup(lambda: setattr(collect, "fetch_json", orig))
        self._fetched = []

    def test_one_repo_404_does_not_kill_the_source(self):
        calls = []

        def fake(url, ua=None):
            calls.append(url)
            if "good/repo" in url:
                return [{"tag_name": "v9", "html_url": "https://gh/good/9"}]
            raise urllib_error_404()  # bad/repo blows up

        self._patch_fetch_json(fake)
        src = {
            "key": "github-releases",
            "url": "https://api.github.com/repos/{repo}/releases",
            "watchlist": ["good/repo", "bad/repo"],
        }
        with contextlib.redirect_stdout(io.StringIO()):
            recs = collect.adapt_github_releases(src)
        # Both repos were attempted; the good one survived the bad one's failure.
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["title"], "good/repo v9")  # labeled with its own repo
        self.assertEqual(recs[0]["url"], "https://gh/good/9")

    def test_per_repo_labeling_across_multiple_repos(self):
        def fake(url, ua=None):
            repo = url.split("/repos/")[1].rsplit("/releases", 1)[0]
            return [{"tag_name": "v1", "html_url": f"https://gh/{repo}/1"}]

        self._patch_fetch_json(fake)
        src = {
            "key": "github-releases",
            "url": "https://api.github.com/repos/{repo}/releases",
            "watchlist": ["org-a/x", "org-b/y"],
        }
        with contextlib.redirect_stdout(io.StringIO()):
            recs = collect.adapt_github_releases(src)
        titles = {r["title"] for r in recs}
        self.assertEqual(titles, {"org-a/x v1", "org-b/y v1"})  # no cross-contamination

    def test_all_repos_failing_reraises(self):
        def fake(url, ua=None):
            raise urllib_error_404()

        self._patch_fetch_json(fake)
        src = {
            "key": "github-releases",
            "url": "https://api.github.com/repos/{repo}/releases",
            "watchlist": ["a/b", "c/d"],
        }
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(Exception):
                collect.adapt_github_releases(src)


def urllib_error_404():
    import urllib.error
    return urllib.error.HTTPError("https://gh/x", 404, "Not Found", {}, None)


# --------------------------------------------------------------------------- #
# db: invariant, isolation, idempotency
# --------------------------------------------------------------------------- #
class DbTestCase(unittest.TestCase):
    """Points collect.DB_PATH at a throwaway file for the duration of the test."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="lens-test-")
        self._orig_db = collect.DB_PATH
        self._orig_root = collect.ROOT
        collect.DB_PATH = Path(self._tmp) / "lens.db"
        # run()'s closing line prints DB_PATH.relative_to(ROOT); keep them consistent
        # so the temp db resolves. SCHEMA_PATH/SOURCES_PATH were bound at import.
        collect.ROOT = Path(self._tmp)

    def tearDown(self):
        collect.DB_PATH = self._orig_db
        collect.ROOT = self._orig_root
        shutil.rmtree(self._tmp, ignore_errors=True)


class TestSaveInvariant(DbTestCase):
    def test_rerun_never_clobbers_triage(self):
        src = {"key": "test-src", "name": "Test", "tier": "P0", "category": "other"}
        records = [{
            "title": "A title", "url": "https://x.test/1", "summary": "s",
            "author": "a", "published_at": "2025-01-01T00:00:00+00:00", "category": "paper",
        }]
        conn = collect.connect()
        self.addCleanup(conn.close)

        self.assertEqual(collect.save(conn, src, records), 1)  # one new row

        # User triages the item by hand.
        iid = collect.item_id("test-src", "https://x.test/1")
        triaged = "2025-03-04T09:08:07+00:00"
        conn.execute(
            "UPDATE items SET score=5, tags='ai,important', status='reviewed', "
            "comment='my note', triaged_at=? WHERE id=?",
            (triaged, iid),
        )
        conn.commit()

        # Re-run the collector with the SAME records.
        self.assertEqual(collect.save(conn, src, records), 0)  # nothing new added

        row = conn.execute(
            "SELECT score, tags, status, comment, triaged_at FROM items WHERE id=?", (iid,)
        ).fetchone()
        self.assertEqual(row, (5, "ai,important", "reviewed", "my note", triaged))
        # Crucially: status was NOT reset to 'captured'.
        self.assertNotEqual(row[2], "captured")
        # ...and the triage timestamp survived the re-run too.
        self.assertEqual(row[4], triaged)
        # And no duplicate row was inserted.
        count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        self.assertEqual(count, 1)

    def test_rerun_with_changed_content_does_not_touch_existing_row(self):
        # INSERT OR IGNORE means even a changed title/summary is ignored for an
        # existing id — the collector only ever ADDS, never updates.
        src = {"key": "s", "name": "S", "tier": "P0"}
        conn = collect.connect()
        self.addCleanup(conn.close)
        collect.save(conn, src, [{"title": "Original", "url": "https://x/1"}])
        collect.save(conn, src, [{"title": "Rewritten", "url": "https://x/1"}])
        title = conn.execute("SELECT title FROM items WHERE url='https://x/1'").fetchone()[0]
        self.assertEqual(title, "Original")


class TestSaveIsolation(DbTestCase):
    def test_save_skips_empty_url(self):
        conn = collect.connect()
        self.addCleanup(conn.close)
        n = collect.save(conn, {"key": "s", "name": "S"}, [
            {"title": "no url", "url": "   "},
            {"title": "no url key"},
            {"title": "ok", "url": "https://o/1"},
        ])
        self.assertEqual(n, 1)
        count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        self.assertEqual(count, 1)

    def test_default_status_is_captured(self):
        conn = collect.connect()
        self.addCleanup(conn.close)
        collect.save(conn, {"key": "s", "name": "S"}, [{"title": "t", "url": "https://o/1"}])
        status = conn.execute("SELECT status FROM items").fetchone()[0]
        self.assertEqual(status, "captured")


class TestSchema(DbTestCase):
    def test_connect_idempotent(self):
        # Connecting twice against the same file must not raise (CREATE IF NOT EXISTS).
        c1 = collect.connect()
        c1.close()
        c2 = collect.connect()
        try:
            tables = {
                r[0] for r in c2.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        finally:
            c2.close()
        self.assertIn("items", tables)


# --------------------------------------------------------------------------- #
# run(): dry-run writes nothing; one bad adapter doesn't kill the run
# --------------------------------------------------------------------------- #
class TestRun(DbTestCase):
    def _patch_sources(self, sources):
        orig = collect.load_sources
        collect.load_sources = lambda: sources
        self.addCleanup(lambda: setattr(collect, "load_sources", orig))

    def _register_adapter(self, name, fn):
        collect.ADAPTERS[name] = fn
        self.addCleanup(lambda: collect.ADAPTERS.pop(name, None))

    def test_dry_run_writes_nothing(self):
        self._register_adapter("fake_ok", lambda src: [{"title": "x", "url": "https://x/1"}])
        self._patch_sources([
            {"key": "fake", "name": "Fake", "tier": "P0", "status": "ok", "adapter": "fake_ok"},
        ])
        with contextlib.redirect_stdout(io.StringIO()):
            collect.run(only=None, dry_run=True)
        self.assertFalse(collect.DB_PATH.exists())  # no db file created at all

    def test_one_adapter_error_does_not_kill_run(self):
        def boom(src):
            raise RuntimeError("kaboom")

        self._register_adapter("good", lambda src: [{"title": "g", "url": "https://g/1"}])
        self._register_adapter("bad", boom)
        self._patch_sources([
            {"key": "good-src", "name": "Good", "tier": "P0", "status": "ok", "adapter": "good"},
            {"key": "bad-src", "name": "Bad", "tier": "P0", "status": "ok", "adapter": "bad"},
        ])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            collect.run(only=None, dry_run=True)  # must not raise
        out = buf.getvalue()
        self.assertIn("good-src", out)
        self.assertIn("bad-src", out)
        self.assertIn("ERROR", out)  # the bad source reported its failure

    def test_run_saves_when_not_dry(self):
        self._register_adapter("good", lambda src: [
            {"title": "g1", "url": "https://g/1"},
            {"title": "g2", "url": "https://g/2"},
        ])
        self._patch_sources([
            {"key": "good-src", "name": "Good", "tier": "P0", "status": "ok", "adapter": "good"},
        ])
        with contextlib.redirect_stdout(io.StringIO()):
            collect.run(only=None, dry_run=False)
        self.assertTrue(collect.DB_PATH.exists())
        conn = collect.connect()
        self.addCleanup(conn.close)
        count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        self.assertEqual(count, 2)


# --------------------------------------------------------------------------- #
# wiring sanity: the two new sources are registered and flipped in sources.yml
# --------------------------------------------------------------------------- #
class TestWiring(unittest.TestCase):
    def test_new_adapters_registered(self):
        self.assertIn("hf_models", collect.ADAPTERS)
        self.assertIn("github_releases", collect.ADAPTERS)

    def test_new_sources_wired_in_yaml(self):
        by_key = {s["key"]: s for s in collect.load_sources()}
        self.assertEqual(by_key["hf-models"]["adapter"], "hf_models")
        self.assertTrue(collect.wired(by_key["hf-models"]))
        gh = by_key["github-releases"]
        self.assertEqual(gh["adapter"], "github_releases")
        self.assertTrue(collect.wired(gh))
        self.assertTrue(len(gh.get("watchlist") or []) >= 6)

    def test_html_adapters_registered(self):
        for name in ("claude_release_notes", "mistral_changelog", "a16z_portfolio"):
            self.assertIn(name, collect.ADAPTERS)

    def test_html_sources_wired_in_yaml(self):
        by_key = {s["key"]: s for s in collect.load_sources()}
        for key, adapter in (
            ("claude-release-notes", "claude_release_notes"),
            ("mistral-changelog", "mistral_changelog"),
            ("a16z-portfolio", "a16z_portfolio"),
        ):
            self.assertEqual(by_key[key]["adapter"], adapter)
            self.assertTrue(collect.wired(by_key[key]))  # status ok + adapter registered

    def test_blocked_sources_left_untouched(self):
        # Loop 2 must not chase the degraded sources.
        by_key = {s["key"]: s for s in collect.load_sources()}
        for key in ("gemini-changelog", "meta-ai-blog", "perplexity-changelog"):
            self.assertEqual(by_key[key]["status"], "blocked")
            self.assertIsNone(by_key[key]["adapter"])


class TestHtmlAdaptShells(unittest.TestCase):
    """The adapt_* shells must just fetch() then hand bytes to the pure parser —
    verified with a stubbed fetch so the suite stays offline."""

    def _patch_fetch(self, payload):
        orig = collect.fetch
        self._fetched_url = None

        def fake(url, ua=None, **kw):
            self._fetched_url = url
            return payload

        collect.fetch = fake
        self.addCleanup(lambda: setattr(collect, "fetch", orig))

    def test_claude_adapt_fetches_then_parses(self):
        self._patch_fetch(fx_bytes("claude_release_notes.html"))
        src = {"key": "claude-release-notes", "url": "https://support.claude.com/x"}
        recs = collect.adapt_claude_release_notes(src)
        self.assertEqual(self._fetched_url, "https://support.claude.com/x")
        self.assertEqual(len(recs), 2)
        self.assertTrue(recs[0]["url"].startswith("https://support.claude.com/x#"))

    def test_mistral_adapt_fetches_then_parses(self):
        self._patch_fetch(fx_bytes("mistral_changelog.html"))
        src = {"key": "mistral-changelog", "url": "https://docs.mistral.ai/cl"}
        recs = collect.adapt_mistral_changelog(src)
        self.assertEqual(self._fetched_url, "https://docs.mistral.ai/cl")
        self.assertEqual(recs[0]["url"], "https://docs.mistral.ai/cl#date-2026-05-28")

    def test_a16z_adapt_fetches_then_parses(self):
        self._patch_fetch(fx_bytes("a16z_portfolio.html"))
        src = {"key": "a16z-portfolio", "url": "https://a16z.com/portfolio/"}
        recs = collect.adapt_a16z_portfolio(src)
        self.assertEqual(self._fetched_url, "https://a16z.com/portfolio/")
        self.assertEqual(len(recs), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""lens collector — read sources.yml, pull the wired sources into data/lens.db.

Design notes
- Zero deps beyond PyYAML (same as life-os finance).
- One adapter per ingestion shape. A source with `adapter: null` is real but
  not yet wired — collect.py skips it and says why. Wiring a new source = adding
  an adapter here + flipping `adapter:` in sources.yml.
- INSERT OR IGNORE on a stable id, so re-running NEVER clobbers your score/tags/
  status/comment. The collector only ever adds new items.
- Every source is wrapped in try/except: one flaky feed can't kill the run.

Usage
  python3 collect.py                 # run every wired source (status: ok)
  python3 collect.py --source arxiv  # run one source by key
  python3 collect.py --list          # show which sources are wired vs TODO
  python3 collect.py --dry-run       # fetch + parse, print counts, write nothing
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

import yaml

ROOT = Path(__file__).resolve().parent
SOURCES_PATH = ROOT / "sources.yml"
SCHEMA_PATH = ROOT / "schema.sql"
DB_PATH = ROOT / "data" / "lens.db"

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 lens-collector"
)

# Canonical entry points for the server-rendered HTML sources — used as the
# parse functions' default base (for building absolute urls / anchors) so tests
# can call them without a src dict; adapt_* always passes the live src["url"].
CLAUDE_RELEASE_URL = "https://support.claude.com/en/articles/12138966-release-notes"
MISTRAL_CHANGELOG_URL = "https://docs.mistral.ai/resources/changelogs"
A16Z_PORTFOLIO_URL = "https://a16z.com/portfolio/"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _as_str(value) -> str:
    """Coerce an arbitrary (possibly malformed) JSON scalar to a clean string.

    External feeds occasionally hand us an int/None/dict where a string is
    expected; every field that ends up calling `.strip()` goes through here so
    a wrong-typed value degrades to text instead of throwing. None -> "".
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def fetch(url: str, ua: str | None = None, timeout: int = 30, accept: str = "*/*",
          retries: int = 3) -> bytes:
    """GET with a few retries — feeds drop TLS / time out transiently."""
    req = urllib.request.Request(url, headers={"User-Agent": ua or DEFAULT_UA, "Accept": accept})
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            last = exc
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
    raise last  # exhausted retries


def fetch_json(url: str, ua: str | None = None):
    # Some endpoints (e.g. YC) only return JSON when you ask for it explicitly.
    return json.loads(fetch(url, ua=ua, accept="application/json").decode("utf-8", "replace"))


def strip_html(text, limit: int = 600) -> str:
    if not text:
        return ""
    if not isinstance(text, str):  # tolerate ints / other scalars from feeds
        text = str(text)
    text = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)  # &quot; &lt; &#39; &nbsp; &amp; ... all decoded
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def norm_date(raw: str | None) -> str | None:
    """Best-effort -> ISO8601. Falls back to the raw string, never raises."""
    if not raw:
        return None
    if not isinstance(raw, str):  # numeric/odd timestamps shouldn't crash
        raw = str(raw)
    raw = raw.strip()
    # RFC 822 (RSS pubDate)
    try:
        return parsedate_to_datetime(raw).astimezone(timezone.utc).replace(microsecond=0).isoformat()
    except Exception:
        pass
    # ISO8601 (Atom / JSON)
    try:
        iso = raw.replace("Z", "+00:00")
        return datetime.fromisoformat(iso).astimezone(timezone.utc).replace(microsecond=0).isoformat()
    except Exception:
        return raw


def item_id(source_key: str, url: str) -> str:
    return hashlib.sha1(f"{source_key}\n{url}".encode("utf-8")).hexdigest()


def localname(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


# --- stdlib-only HTML scraping helpers (no bs4/lxml) ------------------------- #
def _html_text(raw) -> str:
    """Decode an already-fetched HTML body to str. None / odd scalar -> ''."""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        return raw
    return ""


def _attr(attrs: str, name: str) -> str:
    """Pull one attribute value out of a tag's raw attribute string. '' if absent."""
    m = (re.search(rf'{name}\s*=\s*"([^"]*)"', attrs or "")
         or re.search(rf"{name}\s*=\s*'([^']*)'", attrs or ""))
    return m.group(1) if m else ""


def _human_date(text: str) -> str:
    """'June 2, 2026' / 'Jun 2, 2026' -> '2026-06-02'. '' when it doesn't match."""
    text = (text or "").strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def _iso_date(raw) -> str:
    """Lift a bare YYYY-MM-DD out of a date-ish value ('2011-07-22 00:00:00')."""
    m = re.search(r"\d{4}-\d{2}-\d{2}", str(raw or ""))
    return m.group(0) if m else ""


# --------------------------------------------------------------------------- #
# adapters — each returns a list of normalized record dicts:
#   {title, url, summary, author, published_at, category?}
# --------------------------------------------------------------------------- #
# Each adapter is a thin shell: `fetch(...)` the bytes/JSON, then hand off to a
# pure `parse_*` function that takes already-fetched data and never touches the
# network. The parse functions are what the test suite drives off local
# fixtures; ADAPTERS still points at the `adapt_*` shells so runtime behavior is
# unchanged.
def parse_generic_feed(raw) -> list[dict]:
    """Parse one already-fetched RSS 2.0 or Atom document. No network.

    Malformed / empty / non-XML input yields [] rather than throwing — a junk
    response (HTML error page, truncated body) must not break the parse layer.
    """
    if not raw:
        return []
    try:
        root = ET.fromstring(raw)
    except (ET.ParseError, TypeError, ValueError):
        return []
    return _parse_atom(root) if localname(root.tag) == "feed" else _parse_rss(root)


def adapt_generic_feed(src: dict) -> list[dict]:
    """RSS 2.0 and Atom, namespace-agnostic. Handles `extra_urls` too.

    Each feed URL is fetched + parsed independently: one bad sub-feed (timeout,
    HTML error page, malformed XML) is logged and skipped instead of discarding
    the records already parsed from its sibling feeds. Only if EVERY url fails do
    we re-raise — so the source is reported as failed, never silently empty.
    """
    urls = [src["url"], *src.get("extra_urls", [])]
    out: list[dict] = []
    errors: list[Exception] = []
    for url in urls:
        try:
            out.extend(parse_generic_feed(fetch(url, ua=src.get("ua"))))
        except Exception as exc:
            errors.append(exc)
            print(f"      · sub-feed skipped: {url} ({type(exc).__name__})")
    if errors and not out:
        raise errors[-1]
    return out


def _parse_rss(root: ET.Element) -> list[dict]:
    out = []
    for item in root.iter():
        if localname(item.tag) != "item":
            continue
        rec = {"title": "", "url": "", "summary": "", "author": "", "published_at": None}
        for child in item:
            name = localname(child.tag)
            if name == "title":
                rec["title"] = (child.text or "").strip()
            elif name == "link":
                rec["url"] = (child.text or "").strip()
            elif name in ("description", "summary", "encoded"):
                rec["summary"] = rec["summary"] or strip_html(child.text)
            elif name == "pubDate":
                rec["published_at"] = norm_date(child.text)
            elif name in ("creator", "author"):
                rec["author"] = (child.text or "").strip()
        if rec["url"] or rec["title"]:
            out.append(rec)
    return out


def _parse_atom(root: ET.Element) -> list[dict]:
    out = []
    for entry in root:
        if localname(entry.tag) != "entry":
            continue
        rec = {"title": "", "url": "", "summary": "", "author": "", "published_at": None}
        for child in entry:
            name = localname(child.tag)
            if name == "title":
                rec["title"] = (child.text or "").strip()
            elif name == "link":
                href = child.get("href")
                rel = child.get("rel", "alternate")
                if href and (rel == "alternate" or not rec["url"]):
                    rec["url"] = href.strip()
            elif name in ("summary", "content"):
                rec["summary"] = rec["summary"] or strip_html(child.text)
            elif name in ("updated", "published"):
                rec["published_at"] = rec["published_at"] or norm_date(child.text)
            elif name == "author":
                nm = child.find("{*}name")
                if nm is not None and nm.text:
                    rec["author"] = nm.text.strip()
        if rec["url"] or rec["title"]:
            out.append(rec)
    return out


def parse_hf_daily_papers(data) -> list[dict]:
    out = []
    for row in data if isinstance(data, list) else []:
        if not isinstance(row, dict):
            continue
        paper = row.get("paper")
        if not isinstance(paper, dict):  # e.g. {"paper": "bad"} or missing
            paper = {}
        pid = _as_str(paper.get("id"))
        authors = paper.get("authors")
        author = ""
        if isinstance(authors, list) and authors and isinstance(authors[0], dict):
            author = _as_str(authors[0].get("name"))
        out.append({
            "title": _as_str(row.get("title") or paper.get("title")),
            "url": f"https://huggingface.co/papers/{pid}" if pid else _as_str(row.get("url")),
            "summary": strip_html(paper.get("summary")) + (f"  [▲{paper.get('upvotes')}]" if paper.get("upvotes") is not None else ""),
            "author": author,
            "published_at": norm_date(row.get("publishedAt")),
            "category": "paper",
        })
    return out


def adapt_hf_daily_papers(src: dict) -> list[dict]:
    return parse_hf_daily_papers(fetch_json(src["url"], ua=src.get("ua")))


def parse_hn_algolia(data) -> list[dict]:
    hits = data.get("hits") if isinstance(data, dict) else None
    out = []
    for hit in hits if isinstance(hits, list) else []:  # tolerate {"hits": null}
        if not isinstance(hit, dict):
            continue
        oid = _as_str(hit.get("objectID"))
        url = _as_str(hit.get("url")) or f"https://news.ycombinator.com/item?id={oid}"
        out.append({
            "title": _as_str(hit.get("title")),
            "url": url,
            "summary": f"{hit.get('points', 0)} points · {hit.get('num_comments', 0)} comments · hn.algolia.com/item?id={oid}",
            "author": _as_str(hit.get("author")),
            "published_at": norm_date(hit.get("created_at")),
            "category": "other",
        })
    return out


def adapt_hn_algolia(src: dict) -> list[dict]:
    return parse_hn_algolia(fetch_json(src["url"], ua=src.get("ua")))


def parse_ossinsight(data) -> list[dict]:
    if not isinstance(data, dict):
        return []
    # `data["data"]` is normally {"rows": [...]} but a malformed response may
    # hand us a string/list/None there — only dig into it when it's a dict, then
    # fall back to a top-level `rows`, so a junk nested value yields [] not a raise.
    nested = data.get("data")
    rows = (nested.get("rows") if isinstance(nested, dict) else None) or data.get("rows") or []
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        repo = _as_str(r.get("repo_name"))
        if not repo:
            continue
        out.append({
            "title": repo,
            "url": f"https://github.com/{repo}",
            "summary": f"{_as_str(r.get('description'))}  [★{r.get('stars')} · fork {r.get('forks')} · {r.get('language') or '—'}]",
            "author": repo.split("/")[0],
            "published_at": None,
            "category": "repo",
        })
    return out


def adapt_ossinsight(src: dict) -> list[dict]:
    return parse_ossinsight(fetch_json(src["url"], ua=src.get("ua")))


def parse_yc_launches(data) -> list[dict]:
    if isinstance(data, dict):
        hits = data.get("hits")  # may be null
    elif isinstance(data, list):
        hits = data
    else:
        hits = None
    out = []
    for h in hits if isinstance(hits, list) else []:  # tolerate {"hits": null}
        if not isinstance(h, dict):
            continue
        slug = _as_str(h.get("slug"))
        path = _as_str(h.get("search_path")) or (f"/launches/{slug}" if slug else "")
        company = h.get("company")
        author = _as_str(company.get("name")) if isinstance(company, dict) else _as_str(company)
        out.append({
            "title": _as_str(h.get("title")),
            "url": f"https://www.ycombinator.com{path}" if path else "",
            "summary": strip_html(h.get("tagline")) + (f"  [▲{h.get('total_vote_count')}]" if h.get("total_vote_count") is not None else ""),
            "author": author,
            "published_at": norm_date(h.get("created_at")),
            "category": "launch",
        })
    return [r for r in out if r["url"] or r["title"]]


def adapt_yc_launches(src: dict) -> list[dict]:
    """YC /launches returns an Algolia-style {hits:[...]} when Accept: application/json."""
    return parse_yc_launches(fetch_json(src["url"], ua=src.get("ua")))


def parse_hf_models(data) -> list[dict]:
    """HF Hub /api/models -> repo records. Skips entries with no model id."""
    out = []
    for m in data if isinstance(data, list) else []:
        if not isinstance(m, dict):
            continue
        mid = _as_str(m.get("id") or m.get("modelId"))  # tolerate non-str ids
        if not mid:
            continue
        downloads = m.get("downloads")
        likes = m.get("likes")
        kind = m.get("pipeline_tag") or m.get("library_name") or "—"
        out.append({
            "title": mid,
            "url": f"https://huggingface.co/{mid}",
            "summary": f"[↓{downloads if downloads is not None else 0} · ♥{likes if likes is not None else 0} · {kind}]",
            "author": mid.split("/")[0] if "/" in mid else mid,
            "published_at": norm_date(m.get("createdAt")),
            "category": "repo",
        })
    return out


def adapt_hf_models(src: dict) -> list[dict]:
    return parse_hf_models(fetch_json(src["url"], ua=src.get("ua")))


def parse_github_releases(releases, repo: str) -> list[dict]:
    """GitHub /repos/{repo}/releases -> repo records. Skips drafts and
    releases with no html_url. `repo` is the owner/name this list came from."""
    out = []
    for rel in releases if isinstance(releases, list) else []:
        if not isinstance(rel, dict) or rel.get("draft"):
            continue
        html_url = _as_str(rel.get("html_url"))
        if not html_url:
            continue
        label = _as_str(rel.get("tag_name") or rel.get("name"))
        author = ""
        who = rel.get("author")
        if isinstance(who, dict):
            author = _as_str(who.get("login"))
        out.append({
            "title": f"{repo} {label}".strip(),
            "url": html_url,
            "summary": strip_html(rel.get("body")),
            "author": author,
            "published_at": norm_date(rel.get("published_at")),
            "category": "repo",
        })
    return out


def adapt_github_releases(src: dict) -> list[dict]:
    """Walk the `watchlist` of repos, GET each repo's releases. Each repo is
    wrapped in its own try/except so one 404 / rate-limit can't kill the source.
    GitHub's API needs a User-Agent — fetch() always sends DEFAULT_UA."""
    template = src["url"]
    out: list[dict] = []
    errors: list[Exception] = []
    for repo in src.get("watchlist") or []:
        try:
            data = fetch_json(template.format(repo=repo), ua=src.get("ua"))
            out.extend(parse_github_releases(data, repo))
        except Exception as exc:
            errors.append(exc)
            print(f"      · repo skipped: {repo} ({type(exc).__name__})")
    if errors and not out:
        raise errors[-1]
    return out


# --------------------------------------------------------------------------- #
# HTML adapters — server-rendered pages, parsed with the stdlib only (regex over
# already-fetched bytes; no bs4/lxml). Each is defensive: an unrecognized layout
# yields [] instead of throwing, so a site redesign degrades to "0 new" not a
# crash. The page shape can change at any time — these will need re-checking.
# --------------------------------------------------------------------------- #
def parse_claude_release_notes(raw, base_url: str = CLAUDE_RELEASE_URL) -> list[dict]:
    """Claude release-notes article -> one record per dated <h3> section.

    The article body is split on its date headings (`<h3 id=...>June 2, 2026</h3>`);
    each `id` becomes a #anchor and the text between this heading and the next
    (h2 or h3) becomes the summary. A heading only counts as an entry when its
    text actually parses as a date — a non-release <h3> ("Frequently asked
    questions", a footer label) is ignored, never turned into a row. If no dated
    heading is found we return [] rather than inventing a whole-page record:
    structureless / error / non-release HTML must not become a saved-looking
    product item. Never raises, never invents data.
    """
    text = _html_text(raw)
    if not text.strip():
        return []
    art = re.search(r"(?is)<article\b[^>]*>(.*?)</article>", text)
    body = art.group(1) if art else text
    heads = list(re.finditer(r"(?is)<(h2|h3)\b([^>]*)>(.*?)</\1>", body))
    out: list[dict] = []
    for i, m in enumerate(heads):
        if m.group(1).lower() != "h3":  # h2 = month divider, boundary only
            continue
        date_text = strip_html(m.group(3), limit=200)
        iso = _human_date(date_text)
        if not iso:  # only genuinely dated headings are release-note entries
            continue
        anchor = _attr(m.group(2), "id")
        end = heads[i + 1].start() if i + 1 < len(heads) else len(body)
        out.append({
            "title": date_text,
            "url": base_url + (f"#{anchor}" if anchor else ""),
            "summary": strip_html(body[m.end():end]),
            "author": "",
            "published_at": iso,
            "category": "product",
        })
    return out


def adapt_claude_release_notes(src: dict) -> list[dict]:
    return parse_claude_release_notes(fetch(src["url"], ua=src.get("ua")), src["url"])


def parse_mistral_changelog(raw, base_url: str = MISTRAL_CHANGELOG_URL) -> list[dict]:
    """Mistral docs changelog -> one record per dated entry.

    An entry is a div carrying BOTH `data-changelog-entry="true"` and
    `id="date-YYYY-MM-DD"` (attribute order doesn't matter); the id yields the
    date + #anchor, and it wraps an <h2> label and an <article> body. Requiring
    the data- marker means a plain `<div id="date-...">` elsewhere on the page,
    or a redesign that drops the flag, is NOT mistaken for a changelog row. The
    block runs from one entry div to the next. Unknown structure / empty input -> [].
    """
    text = _html_text(raw)
    if not text.strip():
        return []
    entries = []  # (match, date) for each genuine changelog-entry div
    for m in re.finditer(r"(?is)<div\b([^>]*)>", text):
        attrs = m.group(1)
        if _attr(attrs, "data-changelog-entry") != "true":
            continue
        dm = re.match(r"date-(\d{4}-\d{2}-\d{2})$", _attr(attrs, "id"))
        if not dm:
            continue
        entries.append((m, dm.group(1)))
    out: list[dict] = []
    for i, (m, date) in enumerate(entries):
        end = entries[i + 1][0].start() if i + 1 < len(entries) else len(text)
        block = text[m.end():end]
        h2 = re.search(r"(?is)<h2\b[^>]*>(.*?)</h2>", block)
        label = strip_html(h2.group(1), limit=200) if h2 else ""
        art = re.search(r"(?is)<article\b[^>]*>(.*?)</article>", block)
        out.append({
            "title": f"{label}, {date[:4]}" if label else date,
            "url": f"{base_url}#date-{date}",
            "summary": strip_html(art.group(1) if art else block),
            "author": "",
            "published_at": date,
            "category": "api_change",
        })
    return out


def adapt_mistral_changelog(src: dict) -> list[dict]:
    return parse_mistral_changelog(fetch(src["url"], ua=src.get("ua")), src["url"])


def parse_a16z_portfolio(raw, base_url: str = A16Z_PORTFOLIO_URL) -> list[dict]:
    """a16z portfolio -> one record per server-rendered company.

    Each card carries its data as JSON in a `data-company='{...}'` attribute
    (single-quoted, or entity-encoded inside double quotes). Each blob is parsed
    independently: a malformed one is skipped, not fatal. Relative permalinks are
    absolutized against base_url. Unknown structure / empty input -> [].
    """
    text = _html_text(raw)
    if not text.strip():
        return []
    out: list[dict] = []
    for m in re.finditer(r"(?is)data-company=(['\"])(.*?)\1", text):
        try:
            obj = json.loads(html.unescape(m.group(2)))
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        title = _as_str(obj.get("name") or obj.get("post_title") or obj.get("display_name"))
        link = _as_str(obj.get("permalink") or obj.get("company_url")
                       or obj.get("url") or obj.get("external_url"))
        url = urljoin(base_url, link) if link else ""
        if not title and not url:
            continue
        date = _iso_date(obj.get("investment_date")) or _iso_date(obj.get("initial_a16z_date_funded"))
        out.append({
            "title": title,
            "url": url,
            "summary": strip_html(obj.get("website_description") or obj.get("overview")),
            "author": "",
            "published_at": date or None,
            "category": "funding",
        })
    return out


def adapt_a16z_portfolio(src: dict) -> list[dict]:
    return parse_a16z_portfolio(fetch(src["url"], ua=src.get("ua")), src["url"])


ADAPTERS = {
    "generic_feed": adapt_generic_feed,
    "hf_daily_papers": adapt_hf_daily_papers,
    "hn_algolia": adapt_hn_algolia,
    "ossinsight": adapt_ossinsight,
    "yc_launches": adapt_yc_launches,
    "hf_models": adapt_hf_models,
    "github_releases": adapt_github_releases,
    "claude_release_notes": adapt_claude_release_notes,
    "mistral_changelog": adapt_mistral_changelog,
    "a16z_portfolio": adapt_a16z_portfolio,
}


# --------------------------------------------------------------------------- #
# db
# --------------------------------------------------------------------------- #
def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA_PATH.read_text())
    return conn


def save(conn: sqlite3.Connection, src: dict, records: list[dict]) -> int:
    added = 0
    fetched = now_iso()
    for rec in records:
        url = (rec.get("url") or "").strip()
        if not url:
            continue
        iid = item_id(src["key"], url)
        cur = conn.execute(
            """INSERT OR IGNORE INTO items
               (id, source_key, source_name, tier, category, title, url, summary,
                author, published_at, fetched_at, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?, 'captured')""",
            (
                iid, src["key"], src.get("name"), src.get("tier"),
                rec.get("category") or src.get("category"),
                (rec.get("title") or "").strip(), url,
                rec.get("summary") or "", rec.get("author") or "",
                rec.get("published_at"), fetched,
            ),
        )
        added += cur.rowcount
    conn.commit()
    return added


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #
def load_sources() -> list[dict]:
    doc = yaml.safe_load(SOURCES_PATH.read_text())
    return doc.get("sources", [])


def wired(src: dict) -> bool:
    return src.get("status") == "ok" and src.get("adapter") in ADAPTERS


def run(only: str | None, dry_run: bool) -> None:
    sources = load_sources()
    conn = None if dry_run else connect()
    total_new = 0
    for src in sources:
        if only and src["key"] != only:
            continue
        if not only and not wired(src):
            continue
        if src.get("adapter") not in ADAPTERS:
            print(f"  skip {src['key']:<22} adapter not wired ({src.get('status')})")
            continue
        try:
            records = ADAPTERS[src["adapter"]](src)
            if dry_run:
                print(f"  ✓ {src['key']:<22} fetched {len(records):>4} records (dry-run, not saved)")
                continue
            n = save(conn, src, records)
            total_new += n
            print(f"  ✓ {src['key']:<22} {len(records):>4} fetched · {n:>4} new")
        except Exception as exc:  # one bad source never kills the run
            print(f"  ✗ {src['key']:<22} ERROR: {type(exc).__name__}: {exc}")
    if conn is not None:
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('last_collect', ?)", (now_iso(),)
        )
        conn.commit()
        conn.close()
        print(f"\n  {total_new} new items -> {DB_PATH.relative_to(ROOT)}")


def list_sources() -> None:
    rank = {"ok": 0, "needs_parse": 1, "needs_token": 2, "needs_headless": 3, "blocked": 4}
    for src in sorted(load_sources(), key=lambda s: (rank.get(s.get("status"), 9), s.get("tier", ""))):
        mark = "WIRED" if wired(src) else "todo "
        print(f"  [{mark}] {src.get('tier','--'):<4} {src['key']:<24} {src.get('status'):<13} adapter={src.get('adapter')}")


def main() -> None:
    ap = argparse.ArgumentParser(description="lens collector")
    ap.add_argument("--source", help="run a single source by key")
    ap.add_argument("--list", action="store_true", help="show wired vs TODO sources")
    ap.add_argument("--dry-run", action="store_true", help="fetch + parse, write nothing")
    args = ap.parse_args()
    if args.list:
        list_sources()
        return
    run(only=args.source, dry_run=args.dry_run)


if __name__ == "__main__":
    main()

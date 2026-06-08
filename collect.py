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


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


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


def strip_html(text: str | None, limit: int = 600) -> str:
    if not text:
        return ""
    text = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def norm_date(raw: str | None) -> str | None:
    """Best-effort -> ISO8601. Falls back to the raw string, never raises."""
    if not raw:
        return None
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


# --------------------------------------------------------------------------- #
# adapters — each returns a list of normalized record dicts:
#   {title, url, summary, author, published_at, category?}
# --------------------------------------------------------------------------- #
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
            root = ET.fromstring(fetch(url, ua=src.get("ua")))
            out.extend(_parse_atom(root) if localname(root.tag) == "feed" else _parse_rss(root))
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


def adapt_hf_daily_papers(src: dict) -> list[dict]:
    data = fetch_json(src["url"], ua=src.get("ua"))
    out = []
    for row in data if isinstance(data, list) else []:
        paper = row.get("paper", {}) or {}
        pid = paper.get("id", "")
        authors = paper.get("authors") or []
        author = ""
        if authors and isinstance(authors[0], dict):
            author = authors[0].get("name", "")
        out.append({
            "title": (row.get("title") or paper.get("title") or "").strip(),
            "url": f"https://huggingface.co/papers/{pid}" if pid else (row.get("url") or ""),
            "summary": strip_html(paper.get("summary")) + (f"  [▲{paper.get('upvotes')}]" if paper.get("upvotes") is not None else ""),
            "author": author,
            "published_at": norm_date(row.get("publishedAt")),
            "category": "paper",
        })
    return out


def adapt_hn_algolia(src: dict) -> list[dict]:
    data = fetch_json(src["url"], ua=src.get("ua"))
    out = []
    for hit in data.get("hits", []):
        oid = hit.get("objectID", "")
        url = hit.get("url") or f"https://news.ycombinator.com/item?id={oid}"
        out.append({
            "title": (hit.get("title") or "").strip(),
            "url": url,
            "summary": f"{hit.get('points', 0)} points · {hit.get('num_comments', 0)} comments · hn.algolia.com/item?id={oid}",
            "author": hit.get("author", ""),
            "published_at": norm_date(hit.get("created_at")),
            "category": "other",
        })
    return out


def adapt_ossinsight(src: dict) -> list[dict]:
    data = fetch_json(src["url"], ua=src.get("ua"))
    rows = (data.get("data", {}) or {}).get("rows") or data.get("rows") or []
    out = []
    for r in rows:
        repo = r.get("repo_name", "")
        if not repo:
            continue
        out.append({
            "title": repo,
            "url": f"https://github.com/{repo}",
            "summary": f"{(r.get('description') or '').strip()}  [★{r.get('stars')} · fork {r.get('forks')} · {r.get('language') or '—'}]",
            "author": repo.split("/")[0],
            "published_at": None,
            "category": "repo",
        })
    return out


def adapt_yc_launches(src: dict) -> list[dict]:
    """YC /launches returns an Algolia-style {hits:[...]} when Accept: application/json."""
    data = fetch_json(src["url"], ua=src.get("ua"))
    hits = data.get("hits", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
    out = []
    for h in hits:
        if not isinstance(h, dict):
            continue
        slug = h.get("slug") or ""
        path = h.get("search_path") or (f"/launches/{slug}" if slug else "")
        company = h.get("company")
        author = company.get("name") if isinstance(company, dict) else (company or "")
        out.append({
            "title": (h.get("title") or "").strip(),
            "url": f"https://www.ycombinator.com{path}" if path else "",
            "summary": strip_html(h.get("tagline")) + (f"  [▲{h.get('total_vote_count')}]" if h.get("total_vote_count") is not None else ""),
            "author": author or "",
            "published_at": norm_date(h.get("created_at")),
            "category": "launch",
        })
    return [r for r in out if r["url"] or r["title"]]


ADAPTERS = {
    "generic_feed": adapt_generic_feed,
    "hf_daily_papers": adapt_hf_daily_papers,
    "hn_algolia": adapt_hn_algolia,
    "ossinsight": adapt_ossinsight,
    "yc_launches": adapt_yc_launches,
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

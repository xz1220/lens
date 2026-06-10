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
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit
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
ANTHROPIC_BASE = "https://www.anthropic.com"
ALPHAXIV_BASE = "https://www.alphaxiv.org"


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


def keyword_filter(records: list[dict], keywords) -> list[dict]:
    """sources.yml 可选 `filter_keywords`：标题/摘要命中任意关键词才保留。

    给混源降噪用（vercel-changelog 大部分是非 AI 的平台消息）。匹配按
    词边界（字母数字断开），所以 keyword 'ai' 不会误命中 maintain/available；
    没配 filter_keywords 的源原样通过。
    """
    if not keywords:
        return records
    pats = [
        re.compile(rf"(?i)(?<![a-z0-9]){re.escape(str(k).lower())}(?![a-z0-9])")
        for k in keywords if str(k).strip()
    ]
    out = []
    for rec in records:
        hay = f"{rec.get('title') or ''} {rec.get('summary') or ''}"
        if any(p.search(hay) for p in pats):
            out.append(rec)
    return out


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
    """Human date -> 'YYYY-MM-DD'. '' when nothing matches.

    Covers both the US 'Month D, Y' shape ('June 2, 2026' / 'Jun 2, 2026',
    Claude/Anthropic) and the day-first 'D Mon Y' shape ('04 Jun 2026',
    alphaXiv). The shapes don't collide (one has a comma, the other doesn't),
    so adding formats never re-interprets an already-handled date.
    """
    text = (text or "").strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%d %b %Y", "%d %B %Y"):
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


_HF_PAPERS_PREFIX = "https://huggingface.co/papers/"


def parse_hf_trending(data) -> list[dict]:
    """HF trending papers -> paper records.

    Verified live: the trending feed is the daily-papers endpoint re-sorted
    (`/api/daily_papers?sort=trending`), so each row carries the SAME shape
    (top-level title/publishedAt + a nested `paper` with id/summary/upvotes/
    authors). We reuse parse_hf_daily_papers' field mapper rather than
    duplicate it, but enforce a STRICTER contract here: a record is kept ONLY
    when it carries BOTH (a) a non-empty paper title and (b) an ABSOLUTE,
    HF-shaped paper url (`https://huggingface.co/papers/<id>`, i.e. the row
    actually carried a paper id). A title-only row collapses to url='' (or to
    parse_hf_daily_papers' non-HF row['url'] fallback); an id-only row maps to
    a valid url but a blank title. Either way the row is unknown / malformed
    structure for this source, so it is dropped (-> []) rather than emitted as
    a blank-title or blank-URL shell that save() would persist. This source
    owns its own adapter + fixture and can diverge later."""
    return [
        r for r in parse_hf_daily_papers(data)
        if r["title"]
        and r["url"].startswith(_HF_PAPERS_PREFIX)
        and len(r["url"]) > len(_HF_PAPERS_PREFIX)
    ]


def adapt_hf_trending(src: dict) -> list[dict]:
    return parse_hf_trending(fetch_json(src["url"], ua=src.get("ua")))


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


# --------------------------------------------------------------------------- #
# Hugging Face Hub models — list metadata + a FAITHFUL per-model README summary.
#
# The list endpoint (/api/models) hands us id/downloads/likes/pipeline_tag/tags
# but no description, so the bare summary ("[↓.. ♥.. tag]") never says what a
# model actually DOES. We fetch one more thing per model — its model-card README
# — and distil a summary from its REAL text. Hard rule: every token of the
# summary comes from the fetched README or the API metadata; NOTHING is ever
# inferred from the model id/name. No README (404 / empty / prose-less / fetch
# error) -> honest metadata-only fallback. parse_* stays a pure list parse; the
# README fetch lives in adapt_*, and summarize_hf_card is a pure (testable) fn.
# --------------------------------------------------------------------------- #
HF_README_URL = "https://huggingface.co/{id}/raw/main/README.md"

# pipeline_tag -> short zh label: a faithful TRANSLATION of a metadata value,
# not a guess from the name. An unmapped tag falls through verbatim.
_HF_PIPELINE_LABELS = {
    "text-to-image": "文生图",
    "image-to-image": "图生图",
    "text-to-video": "文生视频",
    "image-to-video": "图生视频",
    "text-generation": "文本生成",
    "text2text-generation": "文本生成",
    "image-text-to-text": "多模态",
    "visual-question-answering": "视觉问答",
    "image-classification": "图像分类",
    "object-detection": "目标检测",
    "automatic-speech-recognition": "语音识别",
    "text-to-speech": "语音合成",
    "text-to-audio": "音频生成",
    "feature-extraction": "特征提取",
    "sentence-similarity": "句向量",
    "fill-mask": "掩码填充",
    "question-answering": "问答",
    "translation": "翻译",
    "summarization": "摘要",
    "token-classification": "序列标注",
    "text-classification": "文本分类",
}

# tag -> marker, shown verbatim (LoRA / quant format / GGUF ...). Each appears
# ONLY when that exact tag is present in the API / frontmatter metadata.
_HF_TAG_MARKERS = (
    ("lora", "LoRA"), ("qlora", "QLoRA"), ("gguf", "GGUF"), ("awq", "AWQ"),
    ("gptq", "GPTQ"), ("4-bit", "4bit"), ("4bit", "4bit"),
    ("8-bit", "8bit"), ("8bit", "8bit"),
)


def _hf_meta_summary(meta: dict) -> str:
    """Honest metadata-only summary — the fallback when there's no README prose.
    Byte-identical to the pre-enrichment format so parse_hf_models stays stable."""
    if not isinstance(meta, dict):
        meta = {}
    downloads = meta.get("downloads")
    likes = meta.get("likes")
    kind = meta.get("pipeline_tag") or meta.get("library_name") or "—"
    return f"[↓{downloads if downloads is not None else 0} · ♥{likes if likes is not None else 0} · {kind}]"


def _hf_frontmatter(text: str):
    """Split a model card into (frontmatter dict, body). The leading `--- ... ---`
    YAML block is part of the fetched README, so we parse it (with PyYAML, an
    existing dep) to supplement the API metadata; malformed -> ({}, body)."""
    m = re.match(r"(?s)^﻿?[ \t]*---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?", text or "")
    if not m:
        return {}, (text or "")
    try:
        data = yaml.safe_load(m.group(1))
    except Exception:
        data = None
    return (data if isinstance(data, dict) else {}), text[m.end():]


def _hf_clean_inline(text: str) -> str:
    """Strip markdown noise from one inline span: images dropped, links reduced
    to their text, emphasis/backticks removed, then strip_html collapses HTML +
    whitespace. Keeps only the human-readable words."""
    text = str(text or "")
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)        # ![alt](src)  -> gone
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)     # [text](url)  -> text
    text = text.replace("`", "")
    # Strip emphasis markers but NOT single underscores — those are part of
    # snake_case identifiers (e.g. trigger word BJ_Sacred_beast).
    text = re.sub(r"\*\*|__|[*~]", "", text)
    return strip_html(text, limit=300).strip(" :：-—|")


def _hf_field(body: str, label_re: str) -> str:
    """Faithfully pull a `**Label**: value` model-card field (Base model /
    Trained words). '' when the field isn't present in the body."""
    m = re.search(
        rf"(?im)^[ \t]*\*\*[ \t]*(?:{label_re})[ \t]*[:：]?[ \t]*\*\*[ \t]*[:：]?[ \t]*(.+?)[ \t]*$",
        body or "",
    )
    return _hf_clean_inline(m.group(1)) if m else ""


def _hf_first_prose(body: str) -> str:
    """First genuine description paragraph from the card body. The description is
    the lede — the text ABOVE the first `## Section` heading (Usage / Install /
    License sections are not descriptions). Code fences, images, headings,
    tables, lists, bare links and `**Field**:` lines are skipped. '' if none."""
    body = re.sub(r"(?s)<!--.*?-->", " ", body or "")
    body = re.sub(r"(?s)```.*?```|~~~.*?~~~", " ", body)     # drop fenced code whole
    # Model cards embed raw HTML; a <style>/<script> block spans blank lines, so
    # strip_html alone (per-block) would leave its CSS/JS text as a fake lede.
    body = re.sub(r"(?is)<(style|script)\b.*?</\1>", " ", body)
    cut = re.search(r"(?im)^[ \t]{0,3}#{2,}[ \t]+\S", body)  # first '## ...' heading
    if cut:
        body = body[:cut.start()]
    for raw in re.split(r"\r?\n[ \t]*\r?\n", body):
        block = raw.strip()
        if not block:
            continue
        head = block.splitlines()[0].lstrip()
        if head.startswith(("#", ">", "|", "```", "~~~")):   # heading/quote/table/code
            continue
        if re.match(r"^([-*+]|\d+\.)[ \t]+\S", head):        # list item
            continue
        if re.match(r"(?i)^\*\*[^*\n]+\*\*[ \t]*[:：]", block):  # **Field**: value line
            continue
        text = _hf_clean_inline(block.replace("\n", " "))
        if len(text) < 12 or re.match(r"(?is)^https?://\S+$", text):
            continue                                         # too short / bare link
        return text[:160]
    return ""


def summarize_hf_card(readme_text: str, meta: dict) -> str:
    """Build a FAITHFUL one-line summary of a HF model from its fetched README +
    API metadata. Every token comes from the README/metadata — NEVER inferred
    from the model id/name. Empty / prose-less README -> metadata-only fallback.
    Pure function: no network, fixture-testable.
    """
    meta = meta if isinstance(meta, dict) else {}
    fm, body = _hf_frontmatter(_html_text(readme_text))

    def mget(key):
        v = meta.get(key)
        return v if v not in (None, "", [], {}) else fm.get(key)

    # README-body facts — these are what make the summary specific & honest.
    base = _hf_field(body, r"base\s*model")
    trigger = _hf_field(body, r"train(?:ed)?\s*words?|trigger\s*words?")
    prose = _hf_first_prose(body)
    # base model may instead live in the metadata / frontmatter.
    if not base:
        bm = mget("base_model")
        if isinstance(bm, (list, tuple)) and bm:
            bm = bm[0]
        base = _hf_clean_inline(bm) if isinstance(bm, str) else ""

    # No extractable README content at all -> honest metadata-only summary.
    if not (base or trigger or prose):
        return _hf_meta_summary(meta)

    # Kind label: the (translated) pipeline_tag + any LoRA/quant tag markers.
    tags = mget("tags")
    tags_l = [str(t).lower() for t in tags] if isinstance(tags, (list, tuple)) else []
    pipeline = _as_str(mget("pipeline_tag"))
    parts = []
    if pipeline:
        parts.append(_HF_PIPELINE_LABELS.get(pipeline, pipeline))
    for tag, marker in _HF_TAG_MARKERS:
        if tag in tags_l and marker not in parts:
            parts.append(marker)
    kind = " ".join(parts).strip() or _as_str(mget("library_name"))

    segs = []
    if kind:
        segs.append(kind)
    if base:
        segs.append(f"基座 {base}")
    if trigger:
        segs.append(f"触发词 {trigger}")
    if prose:
        segs.append(prose)
    head = " · ".join(segs)
    if len(head) > 110:                                       # keep it bounded
        head = head[:109].rstrip(" ·") + "…"

    downloads = meta.get("downloads")
    likes = meta.get("likes")
    counts = f"↓{downloads if downloads is not None else 0} ♥{likes if likes is not None else 0}"
    return f"{head} · {counts}"


def parse_hf_models(data) -> list[dict]:
    """HF Hub /api/models -> repo records. Skips entries with no model id.

    Pure list parse (no network): the summary here is metadata-only. README
    enrichment happens in adapt_hf_models, which overwrites `summary` per model
    via summarize_hf_card. Each record carries the raw model dict under `_meta`
    so the adapt layer can summarize without re-deriving it; adapt pops `_meta`
    before returning and save() ignores unknown keys regardless.
    """
    out = []
    for m in data if isinstance(data, list) else []:
        if not isinstance(m, dict):
            continue
        mid = _as_str(m.get("id") or m.get("modelId"))  # tolerate non-str ids
        if not mid:
            continue
        out.append({
            "title": mid,
            "url": f"https://huggingface.co/{mid}",
            "summary": _hf_meta_summary(m),
            "author": mid.split("/")[0] if "/" in mid else mid,
            "published_at": norm_date(m.get("createdAt")),
            "category": "repo",
            "_meta": m,
        })
    return out


def adapt_hf_models(src: dict) -> list[dict]:
    """Fetch the model list, then enrich each record with a README-derived
    summary. Each per-model README fetch is isolated in its own try/except: a
    404 / timeout / empty body leaves that record on its honest metadata
    summary, never raises, never blocks the other models. README fetches are
    capped at `readme_limit` (default 50, == the list cap) to bound cost; the
    `_meta` carrier is dropped before returning so records match the schema.
    """
    records = parse_hf_models(fetch_json(src["url"], ua=src.get("ua")))
    budget = src.get("readme_limit", 50)
    for rec in records:
        meta = rec.pop("_meta", {}) or {}
        if budget <= 0:
            continue                                         # keep metadata summary
        budget -= 1
        try:
            readme = fetch(HF_README_URL.format(id=rec["title"]),
                           ua=src.get("ua")).decode("utf-8", "replace")
        except Exception:
            readme = ""                                      # -> metadata fallback
        rec["summary"] = summarize_hf_card(readme, meta)
    return records


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
# SEC EDGAR full-text search (efts.sec.gov) — AI-relevant filings.
#
# `sec-latest-filings` already firehoses *every* new filing (Atom/generic_feed);
# this source is the deliberate opposite: a TARGETED full-text query for filings
# that actually mention AI. efts.sec.gov/LATEST/search-index is EDGAR's
# full-text-search backend; it answers a plain GET with
#   {"hits": {"total": {...}, "hits": [ {"_id": "<adsh>:<file>", "_source": {…}} ]}}.
# SEC *mandates* a descriptive User-Agent (a contact string) or it 403s — the
# yml carries it in `ua` and fetch() forwards it. Defensive throughout: an
# unrecognized payload yields [] instead of throwing.
# --------------------------------------------------------------------------- #
SEC_ARCHIVES = "https://www.sec.gov/Archives/edgar/data"


def _sec_filing_url(cik: str, adsh: str) -> str:
    """Canonical EDGAR filing-index URL from a CIK + accession number (adsh).

    cik '0000790526' + adsh '0001683168-20-000837' ->
    https://www.sec.gov/Archives/edgar/data/790526/000168316820000837/0001683168-20-000837-index.htm
    Returns '' when either part is missing — the caller then drops the record
    (no url to save, no fabricated link)."""
    cik = (cik or "").lstrip("0")
    adsh = (adsh or "").strip()
    if not cik or not adsh:
        return ""
    folder = adsh.replace("-", "")
    return f"{SEC_ARCHIVES}/{cik}/{folder}/{adsh}-index.htm"


def parse_sec_edgar(data) -> list[dict]:
    """EDGAR full-text-search (efts) response -> filing records. No network.

    Each hit's `_source` carries the company (display_names), CIK (ciks[0]),
    form, accession (adsh) and file_date; the filing-index url is built from
    CIK + adsh. A hit with no CIK/adsh produces no url and is skipped. Unknown /
    malformed structure -> [] (never raises, never invents data).
    """
    if not isinstance(data, dict):
        return []
    hits = data.get("hits")
    rows = hits.get("hits") if isinstance(hits, dict) else None
    out: list[dict] = []
    for hit in rows if isinstance(rows, list) else []:
        if not isinstance(hit, dict):
            continue
        src = hit.get("_source")
        if not isinstance(src, dict):
            src = {}
        ciks = src.get("ciks")
        cik = _as_str(ciks[0]) if isinstance(ciks, list) and ciks else ""
        url = _sec_filing_url(cik, _as_str(src.get("adsh")))
        if not url:
            continue
        names = src.get("display_names")
        company = re.sub(r"\s+", " ", _as_str(names[0])) if isinstance(names, list) and names else ""
        roots = src.get("root_forms")
        form = _as_str(src.get("form")) or (_as_str(roots[0]) if isinstance(roots, list) and roots else "")
        title = " · ".join(b for b in (form, company) if b)
        # A hit that carries only ciks+adsh (so the url builds) but no form and
        # no company name has no human-identifiable content — emitting it would
        # be a blank shell ("· · index.htm"). Drop it: unknown/malformed source
        # -> skipped, never fabricated into a row.
        if not title:
            continue
        desc = _as_str(src.get("file_description") or src.get("file_type"))
        # file_date is a bare calendar date; anchor to UTC midnight before
        # norm_date so the ISO result is stable regardless of the collector's
        # local timezone (a naive date would be read as local time and shift).
        fd = _iso_date(src.get("file_date"))
        summary = " · ".join(b for b in (form, f"filed {fd}" if fd else "", desc) if b)
        out.append({
            "title": title,
            "url": url,
            "summary": summary,
            "author": company,
            "published_at": norm_date(f"{fd}T00:00:00Z") if fd else None,
            "category": "filing",
        })
    return out


def adapt_sec_edgar(src: dict) -> list[dict]:
    """efts full-text search for AI-relevant filings.

    src['url'] already carries the AI-phrase query + form filter; we append a
    rolling recent date window (default last 90 days) so re-runs surface NEWLY
    filed documents — INSERT-OR-IGNORE then adds only the unseen ones — rather
    than a frozen all-time relevance list. SEC needs a descriptive UA, passed
    through from src['ua']."""
    url = src["url"]
    days = src.get("window_days", 90)
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)
    sep = "&" if "?" in url else "?"
    url = f"{url}{sep}startdt={start.isoformat()}&enddt={end.isoformat()}"
    return parse_sec_edgar(fetch_json(url, ua=src.get("ua")))


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


# --------------------------------------------------------------------------- #
# Anthropic blogs (news / engineering / research) — one shared parser.
#
# The spec expected a Pages-Router page with a `<script id="__NEXT_DATA__">`
# JSON blob. The live site has since migrated to the Next.js App Router: there
# is no __NEXT_DATA__ anymore, the per-route data ships as opaque `__next_f`
# RSC flight chunks (NOT clean JSON — `new Date(...)`/`$L1` refs, unparseable
# with stdlib json), and the article list is server-rendered as HTML cards.
# So parse_anthropic_next tries BOTH, in order, and returns whichever yields
# rows (honest: real server-rendered data either way, never invented):
#   1. __NEXT_DATA__ JSON  — the documented shape; still handled if a page
#      serves it, proven by the anthropic_next_data fixture.
#   2. SSR article cards    — the current live shape (FeaturedGrid /
#      PublicationList / ArticleList layouts), proven by the per-section
#      fixtures trimmed from the real pages.
# Unknown structure / empty input -> [] (never raises, never fabricates).
# --------------------------------------------------------------------------- #
_ANTHROPIC_TITLE_KEYS = ("title", "heading", "headline", "name")
_ANTHROPIC_SLUG_KEYS = ("slug", "path", "url", "href")
_ANTHROPIC_SUMMARY_KEYS = ("subtitle", "description", "excerpt", "summary", "preview", "subhead")
_ANTHROPIC_DATE_KEYS = ("publishedOn", "publishedAt", "datePublished", "date", "published", "publishDate")


def _slug_str(value) -> str:
    """A slug field may be a plain string or a Sanity-style {'current': ...} dict."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return _as_str(value.get("current") or value.get("slug") or value.get("path"))
    return ""


def _first_key(d: dict, keys) -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _anthropic_abs_url(base: str, section: str, slug: str) -> str:
    if slug.startswith("http"):
        return slug
    if slug.startswith("/"):
        return base + slug
    return f"{base}/{section}/{slug}"


def _anthropic_looks_like_article(d) -> bool:
    if not isinstance(d, dict):
        return False
    has_title = any(isinstance(d.get(k), str) and d.get(k).strip() for k in _ANTHROPIC_TITLE_KEYS)
    has_slug = any(_slug_str(d.get(k)) for k in _ANTHROPIC_SLUG_KEYS)
    return has_title and has_slug


def _anthropic_from_next_data(text: str, base: str, section: str) -> list[dict]:
    """Path 1: pull articles out of a `<script id="__NEXT_DATA__">` JSON blob.

    Finds the LONGEST list anywhere in the parsed tree whose elements look like
    articles (a title-ish key + a slug/path-ish key), then maps each. Returns []
    when the script is absent, the JSON is junk, or no article list is found.
    """
    m = re.search(r'(?is)<script[^>]*\bid="__NEXT_DATA__"[^>]*>(.*?)</script>', text)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except (ValueError, TypeError):
        return []
    best: list[dict] = []

    def walk(node):
        nonlocal best
        if isinstance(node, list):
            arts = [x for x in node if _anthropic_looks_like_article(x)]
            if len(arts) > len(best):
                best = arts
            for x in node:
                walk(x)
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)

    walk(data)
    out: list[dict] = []
    for a in best:
        title = strip_html(_first_key(a, _ANTHROPIC_TITLE_KEYS), limit=300)
        slug = ""
        for k in _ANTHROPIC_SLUG_KEYS:
            slug = _slug_str(a.get(k))
            if slug:
                break
        if not (title and slug):
            continue
        author = a.get("author")
        if isinstance(author, dict):
            author = _as_str(author.get("name"))
        elif isinstance(author, list) and author:
            author = _as_str(author[0].get("name") if isinstance(author[0], dict) else author[0])
        else:
            author = _as_str(author)
        date_raw = _first_key(a, _ANTHROPIC_DATE_KEYS)
        out.append({
            "title": title,
            "url": _anthropic_abs_url(base, section, slug),
            "summary": strip_html(_first_key(a, _ANTHROPIC_SUMMARY_KEYS)),
            "author": author,
            "published_at": norm_date(date_raw) if date_raw else None,
            "category": "company",
        })
    return out


def _anthropic_card_title(inner: str) -> str:
    """A card's title is its heading (FeaturedGrid/ArticleList) or a
    `class*=title` span (PublicationList). '' when neither is present — that
    keeps icon/nav anchors (which have no title) from becoming rows."""
    h = re.search(r"(?is)<h[1-6][^>]*>(.*?)</h[1-6]>", inner)
    if h:
        return strip_html(h.group(1), limit=300)
    s = re.search(r'(?is)<span[^>]*\bclass="[^"]*title[^"]*"[^>]*>(.*?)</span>', inner)
    return strip_html(s.group(1), limit=300) if s else ""


def _anthropic_card_date(inner: str) -> str:
    """A card's date is in a <time> (news/research) or a `class*=date`
    div/span (engineering). '' when absent."""
    t = re.search(r"(?is)<time[^>]*>(.*?)</time>", inner)
    if t:
        return strip_html(t.group(1), limit=60)
    d = re.search(r'(?is)<(div|span)[^>]*\bclass="[^"]*date[^"]*"[^>]*>(.*?)</\1>', inner)
    return strip_html(d.group(2), limit=60) if d else ""


def _anthropic_from_html(text: str, base: str, section: str) -> list[dict]:
    """Path 2: scrape the server-rendered article cards for one section.

    Matches every `<a href="/{section}/{slug}">…</a>` whose slug is a single
    path segment — so `/research/team/alignment` (a team page, two segments) is
    excluded, not mistaken for an article. Title/date/summary come from the
    anchor's own markup; per-card category is ignored (fixed to 'company').
    Deduped by url, document order preserved.
    """
    sec = section.strip("/")
    pat = re.compile(
        r'(?is)<a\b[^>]*?\bhref="(/' + re.escape(sec) + r'/[^"/?#]+)"[^>]*>(.*?)</a>'
    )
    out: list[dict] = []
    seen: set[str] = set()
    for m in pat.finditer(text):
        path, inner = m.group(1), m.group(2)
        title = _anthropic_card_title(inner)
        if not title or path in seen:
            continue
        seen.add(path)
        date_text = _anthropic_card_date(inner)
        p = re.search(r"(?is)<p\b[^>]*>(.*?)</p>", inner)
        out.append({
            "title": title,
            "url": base + path,
            "summary": strip_html(p.group(1)) if p else "",
            "author": "",
            "published_at": _human_date(date_text) or None,
            "category": "company",
        })
    return out


def parse_anthropic_next(raw, base: str = ANTHROPIC_BASE, section: str = "news") -> list[dict]:
    """Parse one already-fetched Anthropic blog index. No network.

    Tries the __NEXT_DATA__ JSON blob first, then the SSR article cards; returns
    the first non-empty result. `section` is the path prefix ('news' /
    'engineering' / 'research') used to scope cards and build slug-only urls.
    Empty / unrecognized input -> [] (never raises, never invents data).
    """
    text = _html_text(raw)
    if not text.strip():
        return []
    return _anthropic_from_next_data(text, base, section) or _anthropic_from_html(text, base, section)


def adapt_anthropic(src: dict) -> list[dict]:
    """Shared by anthropic-news / -engineering / -research: base + section are
    derived from the live url so one adapter serves all three."""
    url = src["url"]
    parts = urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ANTHROPIC_BASE
    section = (parts.path.strip("/").split("/")[0] if parts.path.strip("/") else "") or "news"
    return parse_anthropic_next(fetch(url, ua=src.get("ua")), base, section)


# --------------------------------------------------------------------------- #
# alphaXiv — the explore feed is server-rendered (the paper title + arXiv-style
# link + date live in the DOM; only the abstract sits in an unparseable JS
# flight blob, which we drop). One record per paper card. Class names are
# utility/hashed and may change — defensive: unknown structure -> [].
# --------------------------------------------------------------------------- #
def parse_alphaxiv(raw, base: str = ALPHAXIV_BASE) -> list[dict]:
    """Parse one already-fetched alphaXiv index. No network.

    Each paper is an `<a href="/abs/{id}">{title}</a>` (the id may be an arXiv
    number or a slug like 'mai-thinking-1'); the publication date is the first
    'DD Mon YYYY' span that follows it, bounded to before the next paper anchor
    so a dateless card can't borrow its neighbour's date. Deduped by url. The
    abstract isn't in the DOM, so summary is left empty. Empty / unrecognized
    input -> [] (never raises, never invents data).
    """
    text = _html_text(raw)
    if not text.strip():
        return []
    # Drop <script>/<style> so we only read the server-rendered DOM (the JS
    # flight blob repeats the same /abs/ links and would create phantom dupes).
    dom = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", text)
    out: list[dict] = []
    seen: set[str] = set()
    for m in re.finditer(r'(?is)<a\b[^>]*?\bhref="(/abs/[^"?#]+)"[^>]*>(.*?)</a>', dom):
        path = m.group(1)
        title = strip_html(m.group(2), limit=300)
        if not title or path in seen:
            continue
        seen.add(path)
        tail = dom[m.end():]
        nxt = tail.find('href="/abs/')
        region = tail[:nxt] if nxt != -1 else tail[:800]
        dm = re.search(r"(?is)<span[^>]*>\s*(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})\s*</span>", region)
        out.append({
            "title": title,
            "url": base + path,
            "summary": "",
            "author": "",
            "published_at": _human_date(dm.group(1)) if dm else None,
            "category": "paper",
        })
    return out


def adapt_alphaxiv(src: dict) -> list[dict]:
    parts = urlsplit(src["url"])
    base = f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ALPHAXIV_BASE
    return parse_alphaxiv(fetch(src["url"], ua=src.get("ua")), base)


ADAPTERS = {
    "generic_feed": adapt_generic_feed,
    "hf_daily_papers": adapt_hf_daily_papers,
    "hf_trending": adapt_hf_trending,
    "sec_edgar": adapt_sec_edgar,
    "hn_algolia": adapt_hn_algolia,
    "ossinsight": adapt_ossinsight,
    "yc_launches": adapt_yc_launches,
    "hf_models": adapt_hf_models,
    "github_releases": adapt_github_releases,
    "claude_release_notes": adapt_claude_release_notes,
    "mistral_changelog": adapt_mistral_changelog,
    "a16z_portfolio": adapt_a16z_portfolio,
    "anthropic_next": adapt_anthropic,
    "alphaxiv": adapt_alphaxiv,
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
            fetched = ADAPTERS[src["adapter"]](src)
            records = keyword_filter(fetched, src.get("filter_keywords"))
            dropped = len(fetched) - len(records)
            note = f" · 滤掉 {dropped}" if dropped else ""
            if dry_run:
                print(f"  ✓ {src['key']:<22} fetched {len(records):>4} records{note} (dry-run, not saved)")
                continue
            n = save(conn, src, records)
            total_new += n
            print(f"  ✓ {src['key']:<22} {len(records):>4} fetched · {n:>4} new{note}")
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

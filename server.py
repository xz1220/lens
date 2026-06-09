#!/usr/bin/env python3
"""lens local server — the board + the loop's plumbing. Python stdlib only.

Endpoints
  GET  /                         -> web/index.html
  GET  /<static>                 -> files under web/
  GET  /api/stats                -> counts by status / tier (for the rail)
  GET  /api/sources              -> sources.yml (filters + collectability)
  GET  /api/items?...            -> filtered item list (+ thread count)
  POST /api/items/<id>           -> triage: {score?, tags?, status?, comment?}
  POST /api/items/<id>/discussion-> render prompt md, record thread, return {path, content}
  POST /api/discussion/append    -> {path, content}  append 回填 into a discussion file
  POST /api/ideas                -> {title?, body, parked, item_id?} capture an idea

Run:  python3 server.py [--port 8787]
The frontend under web/ is an intentional PLACEHOLDER — design is the next step.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
DB_PATH = ROOT / "data" / "lens.db"
SOURCES_PATH = ROOT / "sources.yml"
TEMPLATE = ROOT / "templates" / "discussion.md"
DISCUSSIONS = ROOT / "data" / "discussions"
IDEAS = ROOT / "data" / "ideas"

VALID_STATUS = {"captured", "reviewed", "promoted", "ignored"}
CONTENT_TYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css",
                 ".json": "application/json", ".svg": "image/svg+xml", ".ico": "image/x-icon"}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def slugify(text: str, fallback: str = "item") -> str:
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9一-鿿]+", "-", text).strip("-")
    return (text[:50] or fallback)


def unique_path(directory: Path, base: str, ext: str = ".md") -> Path:
    """A path under `directory` that does not yet exist — never clobber a file.
    Slugs collide easily (punctuation/case/length), so two distinct items must
    never resolve to the same discussion/idea file."""
    p = directory / f"{base}{ext}"
    n = 2
    while p.exists():
        p = directory / f"{base}-{n}{ext}"
        n += 1
    return p


def db() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise FileNotFoundError("data/lens.db not found — run `python3 collect.py` first.")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def safe_under(base: Path, candidate: str) -> Path:
    """Resolve `candidate` and guarantee it stays under `base` (no traversal)."""
    p = (ROOT / candidate).resolve() if not Path(candidate).is_absolute() else Path(candidate).resolve()
    base = base.resolve()
    if base not in p.parents and p != base:
        raise ValueError("path escapes allowed directory")
    return p


class Handler(BaseHTTPRequestHandler):
    server_version = "lens/0.1"

    # -- response helpers ---------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _err(self, code: int, msg: str) -> None:
        self._json({"error": msg}, code=code)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8") or "{}")

    def log_message(self, *args):  # quieter console
        return

    # -- GET ----------------------------------------------------------------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/api/stats":
                return self._json(self.api_stats())
            if path == "/api/sources":
                return self._json(self.api_sources())
            if path == "/api/items":
                return self._json(self.api_items(qs))
            return self.serve_static(path)
        except FileNotFoundError as exc:
            return self._err(503, str(exc))
        except Exception as exc:
            return self._err(500, f"{type(exc).__name__}: {exc}")

    def do_HEAD(self):
        self.do_GET()

    def serve_static(self, path: str):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (WEB / rel).resolve()
        if WEB.resolve() not in target.parents or not target.is_file():
            return self._err(404, "not found")
        ctype = CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        self._send(200, target.read_bytes(), ctype)

    def api_stats(self) -> dict:
        conn = db()
        rows = conn.execute("SELECT status, COUNT(*) n FROM items GROUP BY status").fetchall()
        tiers = conn.execute("SELECT tier, COUNT(*) n FROM items GROUP BY tier").fetchall()
        total = conn.execute("SELECT COUNT(*) n FROM items").fetchone()["n"]
        last = conn.execute("SELECT value FROM meta WHERE key='last_collect'").fetchone()
        conn.close()
        return {
            "total": total,
            "by_status": {r["status"]: r["n"] for r in rows},
            "by_tier": {r["tier"]: r["n"] for r in tiers},
            "last_collect": last["value"] if last else None,
        }

    def api_sources(self) -> list:
        doc = yaml.safe_load(SOURCES_PATH.read_text())
        return [
            {k: s.get(k) for k in ("key", "name", "tier", "category", "status", "adapter", "desc")}
            for s in doc.get("sources", [])
        ]

    def api_items(self, qs: dict) -> dict:
        def one(name):
            return qs.get(name, [None])[0]

        where, params = [], []
        if one("status"):
            where.append("i.status = ?"); params.append(one("status"))
        if one("tier"):
            where.append("i.tier = ?"); params.append(one("tier"))
        if one("source"):
            where.append("i.source_key = ?"); params.append(one("source"))
        if one("min_score"):
            where.append("i.score >= ?"); params.append(int(one("min_score")))
        if one("tag"):
            where.append("i.tags LIKE ?"); params.append(f"%{one('tag')}%")
        if one("q"):
            where.append("(i.title LIKE ? OR i.summary LIKE ?)")
            params += [f"%{one('q')}%", f"%{one('q')}%"]
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        sort = {"date": "i.published_at DESC", "score": "i.score DESC",
                "fetched": "i.fetched_at DESC"}.get(one("sort") or "date", "i.published_at DESC")
        limit = min(int(one("limit") or 200), 1000)

        conn = db()
        rows = conn.execute(
            f"""SELECT i.*, COUNT(t.id) AS thread_count, MAX(t.path) AS last_thread
                FROM items i LEFT JOIN threads t ON t.item_id = i.id
                {clause}
                GROUP BY i.id
                ORDER BY (i.published_at IS NULL), {sort}
                LIMIT ?""",
            (*params, limit),
        ).fetchall()
        conn.close()
        return {"items": [dict(r) for r in rows], "count": len(rows)}

    # -- POST ---------------------------------------------------------------
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            body = self._read_body()
            if path == "/api/discussion/append":
                return self._json(self.append_discussion(body))
            if path == "/api/ideas":
                return self._json(self.create_idea(body))
            m = re.match(r"^/api/items/([0-9a-f]{40})(/discussion)?$", path)
            if m and m.group(2):
                return self._json(self.create_discussion(m.group(1), body))
            if m:
                return self._json(self.update_item(m.group(1), body))
            return self._err(404, "not found")
        except ValueError as exc:
            return self._err(400, str(exc))
        except FileNotFoundError as exc:
            return self._err(503, str(exc))
        except Exception as exc:
            return self._err(500, f"{type(exc).__name__}: {exc}")

    def update_item(self, item_id: str, body: dict) -> dict:
        fields, params = [], []
        if "score" in body:
            score = body["score"]
            if score is not None and not (0 <= int(score) <= 5):
                raise ValueError("score must be 0..5 or null")
            fields.append("score = ?"); params.append(None if score is None else int(score))
        if "tags" in body:
            tags = ",".join(t.strip().lower() for t in (body["tags"] or "").split(",") if t.strip())
            fields.append("tags = ?"); params.append(tags)
        if "status" in body:
            if body["status"] not in VALID_STATUS:
                raise ValueError(f"status must be one of {sorted(VALID_STATUS)}")
            fields.append("status = ?"); params.append(body["status"])
        if "comment" in body:
            fields.append("comment = ?"); params.append(body["comment"])
        if not fields:
            raise ValueError("nothing to update")
        fields.append("triaged_at = ?"); params.append(now_iso())

        conn = db()
        cur = conn.execute(f"UPDATE items SET {', '.join(fields)} WHERE id = ?", (*params, item_id))
        conn.commit()
        if cur.rowcount == 0:
            conn.close(); raise ValueError("item not found")
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        conn.close()
        return dict(row)

    def create_discussion(self, item_id: str, body: dict) -> dict:
        comment = (body.get("comment") or "").strip()
        conn = db()
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            conn.close(); raise ValueError("item not found")
        item = dict(row)

        DISCUSSIONS.mkdir(parents=True, exist_ok=True)
        date = now_iso()[:10]
        # id prefix keeps distinct items apart even when titles slugify the same;
        # unique_path guarantees we never overwrite an existing discussion (incl. 回填).
        target = unique_path(DISCUSSIONS, f"{date}-{item_id[:8]}-{slugify(item['title'])}")

        doc = TEMPLATE.read_text()
        repl = {
            "{title}": item.get("title") or "(无标题)",
            "{source_name}": item.get("source_name") or item.get("source_key") or "",
            "{tier}": item.get("tier") or "",
            "{category}": item.get("category") or "",
            "{url}": item.get("url") or "",
            "{published_at}": item.get("published_at") or "（未知）",
            "{author}": item.get("author") or "（未知）",
            "{score}": "—" if item.get("score") is None else str(item["score"]),
            "{status}": item.get("status") or "",
            "{tags}": item.get("tags") or "（无）",
            "{summary}": item.get("summary") or "（无摘要）",
            "{comment}": comment or "（这次没写 comment，直接让 AI 帮我看这条值不值得关注。）",
        }
        for k, v in repl.items():
            doc = doc.replace(k, v)
        target.write_text(doc)

        rel = str(target.relative_to(ROOT))
        conn.execute(
            "INSERT INTO threads(item_id, path, comment, created_at) VALUES (?,?,?,?)",
            (item_id, rel, comment, now_iso()),
        )
        # seeding a discussion implies you've looked at it
        conn.execute(
            "UPDATE items SET status = CASE WHEN status='captured' THEN 'reviewed' ELSE status END, "
            "comment = COALESCE(NULLIF(?,''), comment), triaged_at = ? WHERE id = ?",
            (comment, now_iso(), item_id),
        )
        conn.commit()
        conn.close()
        return {"path": rel, "content": doc}

    def append_discussion(self, body: dict) -> dict:
        path = body.get("path") or ""
        content = (body.get("content") or "").strip()
        if not content:
            raise ValueError("nothing to append")
        target = safe_under(DISCUSSIONS, path)
        if not target.is_file():
            raise ValueError("discussion file not found")
        block = f"\n\n### 回填 · {now_iso()}\n\n{content}\n"
        with target.open("a") as fh:
            fh.write(block)
        # touch the thread
        conn = db()
        conn.execute("UPDATE threads SET updated_at = ? WHERE path = ?", (now_iso(), str(target.relative_to(ROOT))))
        conn.commit(); conn.close()
        return {"path": str(target.relative_to(ROOT)), "appended": len(block)}

    def create_idea(self, body: dict) -> dict:
        text = (body.get("body") or "").strip()
        if not text:
            raise ValueError("idea body is empty")
        title = (body.get("title") or "").strip()
        parked = bool(body.get("parked"))
        item_id = body.get("item_id")
        IDEAS.mkdir(parents=True, exist_ok=True)

        src_line = ""
        if item_id:
            conn = db()
            r = conn.execute("SELECT title, url FROM items WHERE id = ?", (item_id,)).fetchone()
            conn.close()
            if r:
                src_line = f"\n> 触发自 item：[{r['title']}]({r['url']})"

        if parked:
            inbox = IDEAS / "inbox.md"
            if not inbox.exists():
                inbox.write_text("# 灵感 Inbox（someday）\n\n> 还没想好、先放着的灵感。成形了就拎出来单独建文件。\n")
            entry = f"\n## {now_iso()[:16].replace('T', ' ')} · {title or '(未命名)'}{src_line}\n\n{text}\n"
            with inbox.open("a") as fh:
                fh.write(entry)
            return {"path": str(inbox.relative_to(ROOT)), "parked": True}

        target = unique_path(IDEAS, f"{now_iso()[:10]}-{slugify(title or text, 'idea')}")
        target.write_text(f"# {title or '(未命名灵感)'}\n_{now_iso()}_{src_line}\n\n{text}\n")
        return {"path": str(target.relative_to(ROOT)), "parked": False}


def main() -> None:
    ap = argparse.ArgumentParser(description="lens local server")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"lens → http://{args.host}:{args.port}  (Ctrl-C to stop)")
    if not DB_PATH.exists():
        print("  note: data/lens.db missing — run `python3 collect.py` to populate the board.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye.")


if __name__ == "__main__":
    main()

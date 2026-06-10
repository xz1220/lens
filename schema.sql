-- lens schema. The DB is the fast index over signal; discussions and ideas
-- live as markdown files under data/ (git-ignored). See docs/VISION.md.

CREATE TABLE IF NOT EXISTS items (
  id            TEXT PRIMARY KEY,                 -- sha1(source_key + '\n' + url)
  source_key    TEXT NOT NULL,                    -- matches a key in sources.yml
  source_name   TEXT,
  tier          TEXT,                             -- P0 | P1 | P2 | heat
  category      TEXT,                             -- model_release | api_change | paper | funding | researcher_note | product | repo | launch | filing | other
  title         TEXT,
  url           TEXT,
  summary       TEXT,
  author        TEXT,
  published_at  TEXT,                             -- ISO8601 if known, else NULL
  fetched_at    TEXT NOT NULL,                    -- ISO8601, when collect.py saw it

  -- user triage fields (the whole point of the board) --
  score         INTEGER,                          -- 0..5, NULL = unscored
  tags          TEXT,                             -- comma-separated, lowercased
  status        TEXT NOT NULL DEFAULT 'captured', -- captured | reviewed | promoted | ignored
  comment       TEXT,                             -- your note on the item itself
  triaged_at    TEXT,                             -- last time you touched score/tags/status/comment

  -- AI digest fields, written ONLY by summarize.py (never by collect.py, never
  -- touching the triage fields above). 旧库由 summarize.py 启动时 ALTER 迁移。 --
  ai_summary       TEXT,                          -- 一句话中文摘要（feed 卡片直接可扫）
  ai_detail        TEXT,                          -- JSON {"points":[...],"why":"..."}（详情页整理区）
  content_text     TEXT,                          -- 抓取的原文正文（LLM 输入材料，详情页可读）
  ai_model         TEXT,                          -- 产出摘要的模型
  ai_summarized_at TEXT                           -- ISO8601，何时总结的
);

CREATE INDEX IF NOT EXISTS idx_items_status     ON items(status);
CREATE INDEX IF NOT EXISTS idx_items_tier       ON items(tier);
CREATE INDEX IF NOT EXISTS idx_items_published  ON items(published_at);
CREATE INDEX IF NOT EXISTS idx_items_source     ON items(source_key);

-- One row per AI discussion spun off an item. The transcript itself is the
-- markdown file at `path`; this table just lets the board show "has discussion".
CREATE TABLE IF NOT EXISTS threads (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id     TEXT NOT NULL REFERENCES items(id),
  path        TEXT NOT NULL,                      -- data/discussions/<file>.md
  comment     TEXT,                               -- the comment that seeded it
  created_at  TEXT NOT NULL,
  updated_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_threads_item ON threads(item_id);

-- Small key/value scratch for the collector (last run, etc.).
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);

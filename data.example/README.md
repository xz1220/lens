# data.example/

真实数据在 `data/`（git-ignored，私有）。这个目录是给读 repo 的人看的**结构样例**，不会被程序读写。

- `lens.db` —— 实际是 SQLite，由 `python3 collect.py` 生成、`python3 summarize.py` 回填 AI 摘要
  （`ai_summary`/`ai_detail`/`content_text` 等列；这里不放二进制样例，schema 见 `../schema.sql`）。
- `discussions/*.md` —— 每条 item 衍生的一次 AI 讨论，由「生成讨论 prompt」写入、「回填」追加。
- `ideas/<slug>.md` —— 立刻成形的灵感。
- `ideas/inbox.md` —— 还没想好、先放着的灵感。

下面两个文件就是真实会长成的样子。

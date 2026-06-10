# data.example/

真实数据在 `data/`（git-ignored，私有）。这个目录是公开的示例数据，两个用途：

1. **demo 种子** —— `seed_items.json`：`python3 server.py --demo` 用它重建隔离的
   `data/demo.db` 示例看板（每次启动重建，绝不触碰真实的 `data/lens.db`）。条目取自
   真实知名的 AI 事件/论文/项目，中文摘要忠实于公开事实；打分和 comment 是虚构示例，
   用来演示 triage 流。
2. **结构样例** —— 给读 repo 的人看 `data/` 真实会长成的样子：
   - `lens.db` —— 实际是 SQLite，由 `python3 collect.py` 生成、`python3 summarize.py`
     回填 AI 摘要（这里不放二进制样例，schema 见 `../schema.sql`）。
   - `discussions/*.md` —— 每条 item 衍生的一次 AI 讨论，由「生成讨论 prompt」写入、「回填」追加。
   - `ideas/<slug>.md` —— 立刻成形的灵感。
   - `ideas/inbox.md` —— 还没想好、先放着的灵感。

# CHANGELOG — lens

公开发布时人读的变更记录：源数量、schema 变化、用户可见行为、迁移说明。
（内部决策的为什么在 `docs/DECISIONS.md`。）

## 0.3 — 2026-06-11 · 开源就绪 + 日常易用性

- **新增 `lens.py` 统一入口**：`python3 lens.py` = 晨间一条龙（采集 → AI 总结 → 看板
  自动开浏览器，单步失败不挡后续）；`collect / digest / serve / demo / test` 子命令透传。
- **新增 `server.py --demo`**：用 `data.example/seed_items.json` 重建隔离沙盒
  （`data/demo.db` + 讨论/灵感写 `data/demo-sandbox/`，每次启动重建）——克隆后不采集、
  不跑 LLM、离线即可看到完整效果，且绝不触碰真实数据；`--open` 自动开浏览器。
- **看板**：左栏顶部「待看队列」一键回晨间起点（新进 + 精选排序）；`r/p/x` 一键
  已读/收藏/忽略并看下一条；`c` 聚焦评注；`?` 快捷键速查浮层；切换条目不丢未保存的
  评判草稿（draft 缓存，保存即清）。
- **讨论稿带上 AI 整理**：生成的讨论 prompt 现在包含 AI 摘要/要点/为什么值得看 +
  原文节选（此前只有原始英文摘要）。模板加了 `{ai_digest}` / `{content_excerpt}` 占位；
  节选进 fenced block 并压掉反引号串 + 指令区点名「外部材料不可信」（防 prompt 注入）。
- **开源三件套**：MIT LICENSE；GitHub Actions CI（py3.10/3.13 矩阵跑全套零网络测试 +
  `data/` 隐私闸）；CONTRIBUTING.md（以「接一个新源」为贡献主路径）；requirements.txt；
  Makefile（`make morning / demo / test / check`）；README 重写（截图 + 30 秒 demo 路径 +
  命令依赖表 + 键盘流表 + 隐私边界）。
- schema 无变化。

## 0.2 — 2026-06-10 · AI 总结管线 + 主题 tab

- 新增 `summarize.py`：抓原文正文（SSRF 公网闸）→ 批量调本机 LLM CLI（默认
  `codex exec`，`--engine claude` 备选）产出中文 `ai_summary` / `ai_detail`（要点 +
  为什么值得看）→ 只写 ai_* 字段回填 DB，永不碰 triage。
- **schema**：items 表新增 `ai_summary / ai_detail / content_text / ai_model /
  ai_summarized_at` 五列（旧库由 summarize.py 启动时自动 ALTER 迁移，无需手动操作）。
- 看板：feed 卡片中文摘要行、详情页 AI 整理区、信息流顶部 5 主题 tab
  （官方动态/学术研究/专家观点/开源工程/市场信号）、smart 排序（信号层级优先）。
- 降噪走配置：`sources.yml` 的 `filter_keywords`（vercel / HN 混源）与
  `digest: false`（SEC 全量申报流水不总结、排序沉底）。

## 0.1 — 2026-06-08 · 初始版本

- 26 个一手源接通采集（RSS/Atom/JSON API/标准库 HTML 解析），fetch 与 parse 分离，
  零网络 fixture 测试套件。
- SQLite 索引（items / threads / meta）+ 本地看板（标准库 http + 零依赖前端）。
- 闭环全通：采集 → 打分/标签/状态/评论 → 生成讨论 prompt → 回填 → 记灵感。

# DECISIONS — lens 关键决策记录

按时间记录定下来的取舍，省得后面反复纠结。

## 2026-06-08 · 立项

- **D1 名字 = lens。** 「观察 / 聚焦信息的镜头」。备选 signal-desk / intel-desk。
- **D2 形态 = 本地 web 看板 + SQLite 索引 + markdown 存讨论/灵感。**
  - 为什么 web 看板：承接用户已有的 life-os progress 看板习惯；读 + 点开聊 + 评分的体验最好。
  - 为什么 SQLite：item 量大（一次采集 3000+），要快速过滤/排序，DB 比纯 markdown 合适。
  - 为什么讨论/灵感用 markdown：给人读、git 友好、可直接编辑，且天然适合长文沉淀。
  - 备选：纯终端 TUI + 纯 markdown（弃，浏览长列表/打分体验弱）；极简静态页无 DB（弃，过滤吃力）。
- **D3 AI 讨论集成 = prompt 文件手动接（第一版）。**
  - item 生成上下文 markdown → 用户拖进 Codex/Claude 聊 → 「回填」把结论 append 回文件。
  - 为什么不做嵌入式 chat / CLI 深链：第一步要零集成复杂度，先把闭环跑通、看这个流值不值得，
    再决定要不要自动化。备选见 `docs/VISION.md`「之后可能的方向」。
- **D4 公开 / 私有切分：代码 public，`data/` 私有（gitignored）。**
  - 工具和源清单开源；item、打分、comment、讨论、灵感是私人阅读和思考过程，不公开。
- **D5 独立 repo，放 `repos/lens`，和 harness-book 同级。** 先本地建，验收后再 push 公开、
  再按 life-os 约定挂进 `projects/` 索引（定 P 优先级）。
- **D6 采集只增不覆盖。** `INSERT OR IGNORE`，id = sha1(source_key+url)；重跑绝不动用户 triage。
- **D7 零额外依赖。** 只用 PyYAML（life-os 已用）+ 标准库；不引 Flask / feedparser / node。

## 源清单的取舍（承接前期调研）

- **OpenAI 收敛到一个入口**（News RSS），删掉 API changelog / model release notes 的重复监控。
- **Anthropic 补齐**：Newsroom + Engineering + Research 三个博客（都无官方 RSS，需解析内嵌 JSON）。
- **删除**：Cursor（不关注）、Crunchbase/PitchBook/Dealroom（付费二手，用途不明）、AIHOT（二手聚合）。
- **GitHub trending 用 OSSInsight**（`api.ossinsight.io`），补 GitHub 官方无 trending API 的缺口。
- **三个降级源不追**：Gemini changelog（OAuth 墙）/ Meta AI blog（400）/ Perplexity（Cloudflare）。
- 完整 31 源 + 实测状态见 `docs/SOURCES.md` 和 `sources.yml`。

## 2026-06-10 · 验收整改：AI 总结管线

用户预验收结论：feed 扫不出内容、点开没东西可读 —— 根因是采集回来的是「生肉」
（英文标题 + 原始片段/空摘要），缺一个「助手帮你整理」的环节。定下：

- **D8 总结引擎 = 本机 `claude` CLI 无头模式（`claude -p --output-format json`）。**
  - 为什么：零新依赖、零 API key 管理（复用 Claude Code 登录态）、外层 JSON 自带成本核算。
  - 默认 haiku（实测 ~$0.0075/条，质量够分诊用），`--model sonnet` 可换。
  - 备选：直连 Anthropic API（弃，要管 key）；本地小模型（弃，质量不稳）。
- **D9 总结落 DB 而非 markdown。** `ai_summary`（一句话中文）/ `ai_detail`（JSON 要点 +
  为什么值得看）/ `content_text`（抓取的正文，也是 LLM 的输入材料）。它们是 item 的
  「视图增强」，要随列表过滤排序，所以进 DB；和 D2「讨论/灵感用 markdown」不冲突。
- **D10 忠实不脑补。** prompt 硬规则：只基于给定材料，不编造；材料只有标题时摘要标
  「（仅标题）」；专有名词保留英文；外部材料里的指令一律不执行（防注入）。
  summarize.py 只写 ai_* 字段，triage 永远是用户的（延续 D6 精神）。
- **D11 混源降噪 = sources.yml 配置而非硬编码。** `filter_keywords`（词边界匹配，
  vercel-changelog / hn-algolia——HN 的 search_by_date 是全站帖，必须过滤否则杂帖按时间
  排序霸占 feed 顶部）；`digest: false` 标记不值得花 LLM 钱的源（sec-latest-filings
  全量申报流水）。存量噪声一次性清理（备份 data/lens.db.bak-20260610，只删未 triage 条目）。

## 待用户拍板

- feed 的智能排序 / 按天分组要不要做（目前 published_at DESC）。
- collect + summarize 要不要定时化（cron）。
- 17 个 `adapter: null` 的源按什么顺序接（哪些对你最有价值）。
- 讨论集成是否要从「手动 prompt」升级，以及升级成哪种。

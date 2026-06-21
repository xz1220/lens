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

- **D8 总结引擎 = 本机 CLI 无头模式，默认 `codex exec`（用户拍板），`--engine claude` 备选。**
  - 为什么 CLI 无头：零新依赖、零 API key 管理（复用已登录的订阅态）。
  - 为什么 codex 默认：用户指定；且实测 `claude -p` 在长批量运行中会间歇把总结请求
    当闲聊拒答（is_error=false 但回「I'm here to help with software engineering…」），
    1320/2477 后全军覆没；codex exec（`-o` 输出文件 + stdin prompt + read-only 沙箱）
    JSON 直出且订阅制零边际成本。
  - claude 引擎保留作备选（默认 haiku，~$0.0075/条）。
  - 备选：直连 API（弃，要管 key）；本地小模型（弃，质量不稳）。
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

## 2026-06-11 · 双定位：个人日常工具 + 开源 AI 项目

用户定调 lens 的两个核心定位：(1) 非常个人的信息源工作台（每天早上过滤/评论/记录），
(2) 同时作为开源 AI 项目存在。本轮按 Codex 独立评审（三视角：日常用户/开源访客/维护者）
+ 自查落地，采纳意见见各条：

- **D12 统一入口 = `lens.py`（stdlib subprocess 透传），不是 shell 脚本。**
  - 晨间一条龙 `python3 lens.py`：采集 → 总结 → 看板，**单步失败不挡后续**（断网/没装
    LLM CLI 时最坏也能打开看板处理已有条目）。Makefile 只是等价 sugar。
  - 为什么不并成一个大脚本：collect / summarize / server 各自的 CLI 和测试已稳定，
    入口只做编排不做逻辑。
- **D13 demo 沙盒 = `server.py --demo` + 独立 `data/demo.db`，绝不写 lens.db。**
  - Codex 原建议「demo 命令生成 data/lens.db」——被否：已有真实数据的用户跑 demo 会让
    示例条目永久混进真库（INSERT OR IGNORE 防覆盖、防不了污染）。demo.db 每次启动重建，
    玩坏即复原。
  - 种子（`data.example/seed_items.json`）用**真实知名 AI 条目 + 忠实摘要**而非虚构新闻：
    示例数据也遵守 D10 忠实原则，且让第一眼就有「这工具懂行」的真实感。打分/评论是
    虚构示例（演示 triage 用）。
- **D14 讨论稿必须带 AI 整理结果（`{ai_digest}` + `{content_excerpt}`）。**
  - 此前模板只有原始英文摘要——把 summarize.py 的产出丢在门外（Codex 指出）。讨论质量
    取决于上下文质量；没总结过的条目在讨论稿里诚实标注，不装有。
- **D15 开源底线 = LICENSE(MIT) + CI（测试矩阵 + 隐私闸）+ CONTRIBUTING + demo 路径。**
  - 隐私闸：CI 里 `git ls-files data` 非空即红——「工具开源思考私有」从约定升级为机器强制。
  - 贡献主路径定为「接一个新源」：sources.yml 条目 + parse_* 纯函数 + fixture 测试。
  - Codex 建议的 schema 迁移制度化（meta.schema_version + migrate.py）**暂不做**：
    现 ALTER 方案有测试覆盖，等真有第二次 schema 变更再立规矩。
- 看板日常动作收紧（Codex A2/A3/A5 全采纳）：「待看队列」固定入口（= captured + smart）、
  `r/p/x` 归档键、`?` 速查浮层、draftCache 防丢未保存评判。

## 2026-06-21 · 看板重设计（两栏 + 二分评判）

用户日常用下来觉得 2026-06-11 那版看板「不顺手」，先做了一版 UI/UX 重构 PRD（三栏 + 撤销 + 待看队列，
14 章），用户嫌**太复杂**弃用；改由用户在 Figma 里直接标注、给出一版更朴素的设计，已确认。
设计稿在 Figma（file key `S353BOFNmpDITsfQLH6NY0`，两个画板：选中态 + 默认空态），PRD + 技术方案
写进飞书 wiki 空间「lens」（按 life-os 的《飞书文档写作规范》）：

- PRD（内嵌 2 张 Figma 截图）：https://enbmphajlu.feishu.cn/wiki/CmZGwQdjLiCb6VkdKHVcMWXJnkb
- 技术方案：https://enbmphajlu.feishu.cn/wiki/VfY7wqwOTiTKY7kdP8wcHkSwnyd

**这是已定稿的设计方向，前端尚未实现**（`web/` 仍是旧三栏带评分版），下一步 design→code。

- **D16 布局 = 顶部状态条幅 + 3 个分类 Tab + 两栏，不再是三栏。**
  - 顶部条幅放采集状态（上次采集时间、新进数、已总结进度条），新进等数字可点筛选。
  - 分类 Tab 只有三类：产品 / 技术 / 其他（用户定，按目前属性够用），每个带计数。
  - 两栏：左信息流列表，右详情/分析视图（默认空，点开某条才显示）。
- **D17 评判 = 二分「值得关注 / 不值得关注」+ 批注，去掉 0–5 评分。**
  - 映射现有四枚举（延续 D6「不造新状态」）：值得关注=`promoted`、不值得关注=`ignored`、
    未读=`captured`、看过没表态=`reviewed`。两个动作都能写批注（=`comment` 字段）。
  - `score` 列在 schema 保留兼容历史数据，但界面不再暴露、不用评分。
- **D18 信息流严格按 `published_at` 倒序；两套视觉信号分离。**
  - 明暗轴管「读没读」（未读高对比、已读整卡置灰），雾青 accent 独占「我的判断」
    （值得关注标、评判选中态），两套不抢同一个色。未总结条目给「待总结」一等态。
- **D19 右栏分阅读区 / 行动区。**
  - 阅读区 = AI 整理（摘要 / 要点 / 为什么值得看）+ 原文折叠；行动区（浅色卡，是「读完该判了」
    的版面转折）= 二分评判 + 批注 + 「讨论与沉淀」区块（生成讨论稿 + 已沉淀讨论列表，承接 D3 讨论闭环）。
- **D20 文档以飞书 wiki 组织，按《飞书文档写作规范》写。**
  - PRD 内嵌 Figma 截图；流程一律 mermaid 画板；不用箭头链 / 生僻字符 / 黑话
    （规范在 `life-os/personal/飞书文档写作规范.md`）。落地前端时同一套写作规范也适用于后续文档。

## 待用户拍板

- push 到 GitHub 的时机（CI 徽章要补 owner/repo）；要不要挂进 life-os projects 索引。
- collect + summarize 要不要定时化（cron）。
- 5 个 `adapter: null` 的降级源维持不追，还是有新的接入想法。
- 讨论集成是否要从「手动 prompt」升级，以及升级成哪种。

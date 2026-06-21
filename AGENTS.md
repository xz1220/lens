# AGENTS.md — lens

给在 `lens` 项目里工作的 Codex / Claude Code 的操作手册。**先读这个文件。**

## 这是什么

`lens` 是一个**个人信息搜集 / 展示 / review 的工作台**。它不是又一个 RSS reader，而是一个把
「信息 → 评判 → 和 AI 发散讨论 → 沉淀思考 → 灵感」串成一条闭环的控制面。核心循环：

1. **捕获 Collect** —— `collect.py` 从 `sources.yml` 里的一手源拉信息，归一成 `item`
2. **总结 Digest** —— `summarize.py` 抓原文正文、调 LLM 产出中文摘要/要点/为什么值得看，回填 DB
3. **分诊 Triage** —— 在看板上给 item 打标签 + 0–5 分 + 状态 + comment
4. **对话 Discuss** —— 从一条 item「生成讨论 prompt」，拖进 Codex/Claude 发散，让它帮你 review
5. **沉淀 Capture** —— 讨论结论 / 过程思考「回填」存回 `data/discussions/`
6. **灵感 Ideate** —— 冒出的灵感立刻记 `data/ideas/<slug>.md`，没想好的丢 `data/ideas/inbox.md`

完整的产品意图和数据模型见 `docs/VISION.md`；为什么这么定见 `docs/DECISIONS.md`；
信息源地图和实测状态见 `docs/SOURCES.md`。

## 怎么跑

```bash
python3 lens.py               # 日常一条龙：采集 → AI 总结 → 看板（自动开浏览器）
python3 server.py --demo      # 示例数据看板（data.example 种子 → 隔离的 data/demo.db，离线可跑）
python3 collect.py            # 只采集（--list 看源状态 / --source <key> 只跑一个）
python3 summarize.py          # 只总结（默认 120 条/次；--all 一口气总结完）
python3 server.py             # 只起看板 http://127.0.0.1:8787（--open 自动开浏览器）
make check                    # 全套零网络测试 + 隐私闸（data/ 不得被 git 跟踪）
```

零额外依赖，只用到 PyYAML（和 life-os 一致）+ Python 标准库；`summarize.py` 的 LLM
引擎走本机 CLI 无头模式——**默认 `codex exec`**（用户指定；实测 `claude -p` 会间歇把
总结请求当闲聊拒答），`--engine claude` 备选（默认 haiku）。不需要管理 API key。

## 架构（三层，别混）

- **源真相** `sources.yml` —— 31 个信息源的清单 + 接入方式 + 实测状态。改源从这里改。
- **快速索引** `data/lens.db`（SQLite，git-ignored）—— `items` 表是信号的索引和你的 triage；
  `threads` 表记录每条 item 衍生出的讨论文件。schema 见 `schema.sql`。
- **思考沉淀**（markdown，git-ignored）—— `data/discussions/*.md`（每次 AI 讨论的完整记录）、
  `data/ideas/*.md` + `inbox.md`（灵感）。**讨论和灵感故意用 markdown 不进 DB**，方便人读、git 友好。

后端：`collect.py`（采集，一个 adapter 一种源形态）+ `summarize.py`（AI 总结：抓正文 →
批量调 `codex exec`（默认）/ `claude -p` 产出 `ai_summary`/`ai_detail`/`content_text` →
UPDATE 回填）+ `server.py`（标准库 http 看板 + 全部写接口 + `--demo` 示例沙盒）+
`lens.py`（统一入口：morning/collect/digest/serve/demo/test，全部 subprocess 透传）。
前端：`web/`（墨色冷调 v3.1 三栏看板：feed 卡片有中文摘要行，详情页是 AI 整理区
「AI 摘要 → 要点 → 为什么值得看 → 原文材料折叠」；键盘流 j/k/0-5/r/p/x，`?` 出速查）。

## 规则（会咬人的）

- **`data/` 是私有的，永不进 public repo。** `.gitignore` 已经挡掉。你的 item、打分、comment、
  讨论、灵感都在 `data/` 下 —— 工具开源，思考私有。给读 repo 的人看结构用 `data.example/`。
- **采集只增不改：** `collect.py` 用 `INSERT OR IGNORE`（id = sha1(source_key+url)），重跑**绝不**
  覆盖你的 score/tags/status/comment。要更新已有 item 的内容字段需另写逻辑，别动这条默认。
- **总结只写 ai_*：** `summarize.py` 只 UPDATE `ai_summary/ai_detail/content_text/ai_model/
  ai_summarized_at`，**永远不碰 triage 字段**。总结必须忠实于抓到的材料，不编造；材料只有
  标题时摘要末尾标「（仅标题）」。
- **混源降噪走配置：** 非纯 AI 的源在 `sources.yml` 配 `filter_keywords`（词边界匹配，见
  vercel-changelog / hn-algolia）；信息密度太低不值得总结的源配 `digest: false`（见
  sec-latest-filings）。别在 adapter 里硬编码过滤。
- **加一个新源 = 在 `collect.py` 写个 adapter + 在 `sources.yml` 把 `adapter:` 从 null 改成它。**
  剩 5 个源是 `adapter: null`，但全是诚实降级（needs_token / needs_headless / blocked），不追（见 `docs/SOURCES.md`）。
- **状态词固定**：`captured` / `reviewed` / `promoted` / `ignored`。别造新状态。
- **语言默认中文**（README / docs / 注释 / 讨论模板），状态枚举、命令、路径、代码标识符保留英文。
- **降级源不要追**：`gemini-changelog` / `meta-ai-blog` / `perplexity-changelog` 当前抓不到
  （OAuth / 400 / Cloudflare），`status: blocked`，已和用户确认**不追替代方案**，别浪费力气。

## 当前进展（2026-06-21，看板重设计已定稿、待落地）

看板做了一轮重设计。**设计稿在 Figma**（file key `S353BOFNmpDITsfQLH6NY0`，两个画板：选中态 + 默认空态），
**PRD 与技术方案在飞书 wiki 空间「lens」**（按 life-os 的《飞书文档写作规范》写、流程转 mermaid 画板）：
- PRD（内嵌 2 张 Figma 截图）：https://enbmphajlu.feishu.cn/wiki/CmZGwQdjLiCb6VkdKHVcMWXJnkb
- 技术方案：https://enbmphajlu.feishu.cn/wiki/VfY7wqwOTiTKY7kdP8wcHkSwnyd

**这是已定稿的方向，前端尚未实现** —— `web/` 仍是旧的三栏 + 0–5 评分版，下一步 design→code 落地。
新设计要点（决策见 `docs/DECISIONS.md` D16–D20）：
- 布局：顶部状态条幅 + 一排 3 个分类 Tab（产品 / 技术 / 其他）+ 两栏（左信息流、右详情，默认空、点开才显示）。不再是三栏。
- 评判：**去掉 0–5 评分**，改二分「值得关注 / 不值得关注」+ 批注。复用现有四枚举：值得关注=`promoted`、不值得关注=`ignored`、未读=`captured`、看过没表态=`reviewed`；`score` 列保留兼容但 UI 不用。不造新枚举。
- 信息流：严格按 `published_at` 倒序；「已读/未读」与「值得关注」两套视觉信号分离（明暗轴管读没读、雾青 accent 独占「我的判断」）；未总结条目「待总结」一等态。
- 右栏分阅读区 / 行动区；行动区 = 二分评判 + 批注 + 「讨论与沉淀」区块（生成讨论稿 + 已沉淀讨论列表）。

## 当前状态（截至 2026-06-11，开源就绪 + 日常易用性）

✅ 2026-06-11 双定位整改（用户定调：个人日常工具 + 开源 AI 项目，Codex 评审采纳大半）：
- `lens.py` 统一入口（morning 容错：采集/总结失败不挡看板）+ Makefile（test/check/demo/morning）。
- `server.py --demo`：`data.example/seed_items.json`（17 条真实知名 AI 条目 + 忠实中文摘要，
  覆盖 5 主题和全部 triage 状态）→ 每次启动重建隔离沙盒：`data/demo.db` + 讨论/灵感写
  `data/demo-sandbox/`。**故意不写 lens.db / data 真实目录**：已有数据的用户跑 demo
  绝不能让示例内容混进真库（Codex review 抓出 markdown 写入漏隔离，已修）。`--open` 自动开浏览器。
- 讨论稿防注入：`{content_excerpt}` 进 fenced block、服务端压掉原文里的反引号串、
  指令区点名「外部材料不可信」。
- 看板：左栏顶部「待看队列」（= 新进 + 精选，一键清其他筛选）；`r/p/x` 一键归档并下一条；
  `c` 聚焦评注；`?` 快捷键速查浮层；draftCache 切换条目不丢未保存评判（保存即清）。
- 讨论稿带 AI 整理：模板新增 `{ai_digest}`（AI 摘要/要点/为什么值得看）+ `{content_excerpt}`
  （原文节选 1500 字截断），没总结过的条目诚实说「还没跑 AI 总结」。
- 开源三件套：MIT LICENSE、GitHub Actions CI（py3.10/3.13 + **隐私闸**：`git ls-files data`
  必须为空）、CONTRIBUTING.md（贡献主路径 = 接一个新源）、requirements.txt、CHANGELOG.md、
  README 重写（截图 docs/assets/board.png 用 demo 数据拍的 + 30 秒 demo + 命令依赖表）。
- `tests/test_server.py` 新增 18 个（种子一致性 / build_demo_db / loopback 端到端 / CLI），
  全套 191 个零网络跑过。

## 历史（截至 2026-06-10，AI 总结管线落地）

✅ 2026-06-10 晚：信息流顶部 5 主题 tab（官方动态/学术研究/专家观点/开源工程/市场信号，
按「阅读姿势」聚合 category，server `TOPIC_CATEGORIES`）；smart 排序（信号层级 + 时间，
digest:false 沉底）；列表瘦身 + 单条懒加载。

✅ 2026-06-10 验收整改（用户验收发现 feed 不可扫、详情不可读）：
- 新增 `summarize.py`：选未总结 item（tier 权重 + 时间，排除 ignored 和 digest:false 源）→
  8 线程并发抓原文正文（stdlib 提取 + SSRF 公网闸，失败降级原始摘要）→ 批量（8 条/批，
  4 路并发）调 LLM CLI 产出中文 `ai_summary` + `ai_detail`(JSON points/why) → UPDATE 落库。
  引擎默认 `codex exec`（用户指定；claude -p 实测会间歇拒答总结任务），`--engine claude` 备选。
  hex 标识防 LLM 编号错位；批失败重试一次再二分降级到单条 + 连续失败熔断；每批 commit，
  中断安全。claude/haiku 成本约 $0.0075/条，codex 订阅制零边际成本。
- 降噪：`keyword_filter`（vercel-changelog / hn-algolia 配 `filter_keywords`），
  HF 模型卡跨段 `<style>` CSS 垃圾修复；存量噪声已备份后清理（vercel 873 + hn 72 条）。
- 看板：feed 卡片中文摘要行、详情页 AI 整理区、顶栏总结进度、搜索覆盖 ai_summary。
- `tests/test_summarize.py` 新增，全套 159 个测试零网络跑过。

## 历史（截至 2026-06-08，初始化完成）

✅ 已搭好并实测跑通：
- `collect.py`：26 个源接通（generic_feed RSS/Atom + hf_daily_papers / hn_algolia / ossinsight /
  yc_launches / hf_models / github_releases + 3 个标准库 HTML 源 claude_release_notes /
  mistral_changelog / a16z_portfolio + Loop 3 的 anthropic_next（news/engineering/research 三博客
  共用）/ alphaxiv，并把 karpathy 改指其 bearblog Atom 走 generic_feed + Loop 4 的 sec_edgar
  （efts 全文检索 AI filing，合规 UA + 滚动近 90 天窗口）/ hf_trending（daily_papers trending 排序））。fetch 与 parse 已分离
  （每个 `parse_*` 是可脱网单测的纯函数），`tests/` 下有标准库 unittest 套件（零网络、fixture 驱动）
  锁住 INSERT-OR-IGNORE 不变量。带重试，单源失败不影响整体。HTML 源只用标准库正则/json 解析（无
  bs4/lxml），结构变样时返回 `[]` 不崩。Anthropic 站点已迁 App Router（无 `__NEXT_DATA__`），
  `anthropic_next` 抓服务端渲染卡片、并保留 `__NEXT_DATA__` JSON 快路径作退路。
- `server.py`：`/api/stats` `/api/sources` `/api/items`（过滤/排序）+ 写接口（triage / 生成讨论 /
  回填 / 记灵感）全部 curl 验证通过。
- `web/`：**占位前端**，能把上面闭环完整跑一遍，但视觉和交互是临时的。

## 下一步

**优先：照 Figma 把 `web/` 前端落地成新设计**（design→code，原生 HTML/CSS/JS、墨色冷调延续；
见上「当前进展」+ `docs/DECISIONS.md` D16–D20）。落地时后端配合的小改：列表按 `published_at`
倒序、二分标记走现有 `status` 写接口、顶部条幅计数走 `/api/stats`、类型（产品/技术/其他）归类规则待定。

其余可能方向（先别自作主张做）：
- 定 P 优先级挂进 life-os projects 索引
- 总结的定时化（cron 跑 collect + summarize）
- 讨论集成从「手动 prompt」升级（见 docs/VISION.md）
- schema 迁移制度化（meta.schema_version + migrate.py，Codex 建议，暂用 ALTER 方案够用）

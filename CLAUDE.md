# CLAUDE.md — lens

给在 `lens` 项目里工作的 Claude Code / Codex 的操作手册。**先读这个文件。**

## 这是什么

`lens` 是一个**个人信息搜集 / 展示 / review 的工作台**。它不是又一个 RSS reader，而是一个把
「信息 → 评判 → 和 AI 发散讨论 → 沉淀思考 → 灵感」串成一条闭环的控制面。核心循环：

1. **捕获 Collect** —— `collect.py` 从 `sources.yml` 里的一手源拉信息，归一成 `item`
2. **分诊 Triage** —— 在看板上给 item 打标签 + 0–5 分 + 状态 + comment
3. **对话 Discuss** —— 从一条 item「生成讨论 prompt」，拖进 Codex/Claude 发散，让它帮你 review
4. **沉淀 Capture** —— 讨论结论 / 过程思考「回填」存回 `data/discussions/`
5. **灵感 Ideate** —— 冒出的灵感立刻记 `data/ideas/<slug>.md`，没想好的丢 `data/ideas/inbox.md`

完整的产品意图和数据模型见 `docs/VISION.md`；为什么这么定见 `docs/DECISIONS.md`；
信息源地图和实测状态见 `docs/SOURCES.md`。

## 怎么跑

```bash
python3 collect.py            # 拉所有已接通的源 → data/lens.db
python3 collect.py --list     # 看哪些源接通了 / 哪些还是 TODO
python3 collect.py --source <key>   # 只跑一个源
python3 server.py             # 起本地看板 http://127.0.0.1:8787
```

零额外依赖，只用到 PyYAML（和 life-os 一致）+ Python 标准库。

## 架构（三层，别混）

- **源真相** `sources.yml` —— 31 个信息源的清单 + 接入方式 + 实测状态。改源从这里改。
- **快速索引** `data/lens.db`（SQLite，git-ignored）—— `items` 表是信号的索引和你的 triage；
  `threads` 表记录每条 item 衍生出的讨论文件。schema 见 `schema.sql`。
- **思考沉淀**（markdown，git-ignored）—— `data/discussions/*.md`（每次 AI 讨论的完整记录）、
  `data/ideas/*.md` + `inbox.md`（灵感）。**讨论和灵感故意用 markdown 不进 DB**，方便人读、git 友好。

后端：`collect.py`（采集，一个 adapter 一种源形态）+ `server.py`（标准库 http 看板 + 全部写接口）。
前端：`web/`（**占位**，见「下一步」）。

## 规则（会咬人的）

- **`data/` 是私有的，永不进 public repo。** `.gitignore` 已经挡掉。你的 item、打分、comment、
  讨论、灵感都在 `data/` 下 —— 工具开源，思考私有。给读 repo 的人看结构用 `data.example/`。
- **采集只增不改：** `collect.py` 用 `INSERT OR IGNORE`（id = sha1(source_key+url)），重跑**绝不**
  覆盖你的 score/tags/status/comment。要更新已有 item 的内容字段需另写逻辑，别动这条默认。
- **加一个新源 = 在 `collect.py` 写个 adapter + 在 `sources.yml` 把 `adapter:` 从 null 改成它。**
  15 个源现在是 `adapter: null`（真实但没接），它们是最自然的下一批后端任务（见 `docs/SOURCES.md`）。
- **状态词固定**：`captured` / `reviewed` / `promoted` / `ignored`。别造新状态。
- **语言默认中文**（README / docs / 注释 / 讨论模板），状态枚举、命令、路径、代码标识符保留英文。
- **降级源不要追**：`gemini-changelog` / `meta-ai-blog` / `perplexity-changelog` 当前抓不到
  （OAuth / 400 / Cloudflare），`status: blocked`，已和用户确认**不追替代方案**，别浪费力气。

## 当前状态（截至 2026-06-08，初始化完成）

✅ 已搭好并实测跑通：
- `collect.py`：16 个源接通（generic_feed RSS/Atom + hf_daily_papers / hn_algolia / ossinsight /
  yc_launches / hf_models / github_releases）。fetch 与 parse 已分离（每个 `parse_*` 是可脱网单测的纯
  函数），`tests/` 下有标准库 unittest 套件（零网络、fixture 驱动）锁住 INSERT-OR-IGNORE 不变量。
  带重试，单源失败不影响整体。
- `server.py`：`/api/stats` `/api/sources` `/api/items`（过滤/排序）+ 写接口（triage / 生成讨论 /
  回填 / 记灵感）全部 curl 验证通过。
- `web/`：**占位前端**，能把上面闭环完整跑一遍，但视觉和交互是临时的。

## 下一步：前端设计（用户要亲自驱动，先停在这里）

> ⚠️ 用户明确说：项目初始化完之后，**第一个重要的点是前端页面设计，因为前端设计跟功能强相关**，
> 所以搭完骨架要**先停下来**，由用户在这个项目里和你一起做前端设计，不要自顾自把 UI 做完。

`web/` 里现在是刻意的占位实现（plain HTML/CSS/JS，banner 标了「占位」）。它的作用只是让闭环能跑、
让你看清每个接口连着什么功能。**真正的前端设计还没做。** 当用户来开工时，方向大概是：

- 信息流怎么展示才高效（密度 / 分组 / 一眼可扫 / 键盘流）—— 这决定 triage 快不快
- triage 的交互（打分 / 标签 / 状态怎么做到「读一条标一条」零摩擦）
- 「从 item 跳到讨论」这一步的体感（生成 prompt → 拖进 AI → 回填）怎么最顺
- 灵感捕获怎么做到「念头一闪立刻记下」

后端接口已经齐了（见上 + `server.py` 顶部注释），前端可以放开重做，不用迁就现在的占位实现。
做前端设计前建议先和用户确认信息架构和交互模型，别直接套模板。

# VISION — lens 想做成什么

## 一句话

把「信息搜集、展示、review」做成一个**分层监控 + 思考沉淀**的个人控制面，而不是一个更大的信息流。
同一事件只留一个最早最权威的来源；读到的东西能被快速评判、能一键拉进 AI 发散、能把讨论和灵感
自动沉淀回来。

## 用户的原话（需求来源）

> 我主要的思维主题是一个信息搜集、展示和 review 的平台。它主要承担：
> 1. 帮我搜集感兴趣领域的相关信息。
> 2. 我会对信息进行简单的标记和打分。
> 3. 我能很方便地从某一个信息跳转到和 Codex 或者 Claude 去聊天——我会给一些 comment，
>    Codex/Claude 基于这些 comment 帮我进一步 review 和讨论。
> 4. 这种讨论大概率是发散的，过程中我可能会有一些思考。我希望这些讨论和思考能被自动化地
>    存储到某一个地方（项目里）。
> 5. 存储完后可能会产生一些灵感。如果我立刻有灵感，就直接记下来；如果没有，就先放到其他地方。
>
> 第一步能做到这样就好了。

## 闭环（五步）

| 步 | 动作 | 落点 |
|---|---|---|
| 1 捕获 | `collect.py` 拉一手源，归一成 `item` | `items` 表 |
| 2 分诊 | 打标签 + 0–5 分 + 状态 + comment | `items` 表的 triage 字段 |
| 3 对话 | 从 item 生成讨论 prompt，拖进 Codex/Claude 发散 | `data/discussions/<date>-<slug>.md` |
| 4 沉淀 | 把讨论结论 / 过程思考「回填」 | append 进同一个 discussion 文件 |
| 5 灵感 | 立刻成形的记成文件，没想好的丢 inbox | `data/ideas/<slug>.md` / `data/ideas/inbox.md` |

## 数据模型

归一成 `event/item`，而不是按平台存 feed item（同一发布在 blog/X/HF/媒体里只算一条）：

- **source**（`sources.yml`）：源的清单，只描述「从哪、怎么拿、拿不拿得到」。
- **item**（`items` 表）：一条信息。`source / tier / category / title / url / summary / author /
  published_at` 是采集来的事实；`score / tags / status / comment` 是你的 triage。
  - `status`：`captured`（刚抓到）→ `reviewed`（看过）→ `promoted`（值得留）/ `ignored`（噪声）。
  - `category`：`model_release / api_change / paper / funding / researcher_note / product /
    repo / launch / filing / other`。
- **thread**（`threads` 表）：一条 item 衍生的一次 AI 讨论；正文是 markdown 文件，表里只记路径，
  好让看板显示「这条聊过了」。
- **note / idea**（markdown）：过程思考和灵感，纯文件，不进 DB。

为什么讨论/灵感用 markdown 不进 DB：它们是给人读、会长期累积、需要 git 友好和可直接编辑的东西；
DB 只承担「快速过滤大量 item」这一件事。

## 分层（源的优先级）

- **P0 机器优先**：官方 RSS / changelog / API / SEC filings —— 离源头近、能稳定机器拉。
- **P1 专家自有**：研究员 blog / newsletter / Atom。
- **P2 排序 / 补全**：讨论层、目录、portfolio。
- **heat 发现候选**：HN / HF / GitHub trending / launch —— 用来发现升温，不当事实源，要回链一手。

详见 `docs/SOURCES.md`。

## 第一步明确不做（non-goals）

- 不做自动 alert / 推送 / 晨间 digest（先把人读—评—聊—存的闭环跑顺）。
- 不做自动生成灵感（灵感由人记，AI 只在讨论里辅助发散）。
- 不做复杂自动去重 / 跨平台事件合并（先靠 `INSERT OR IGNORE` 的 url 去重兜底）。
- 不做多用户 / 多端同步 / 云部署（单人 + 本地）。
- 不内建 chat UI / 不直连 LLM API（第一版讨论靠「生成 prompt 文件 → 手动拖进 Codex/Claude」，
  零集成复杂度，验证这个流值不值得自动化之后再说）。

## 之后可能的方向（不是现在）

- 把 `auxiliary trending` 的初始权重用实际「转化率」（你真正 promote 的比例）校准。
- 讨论从手动 prompt 升级成 CLI 深链 / 嵌入式 chat（取决于第一版用下来的体感）。
- 成熟结论从 item 提升到 wiki 或具体项目。

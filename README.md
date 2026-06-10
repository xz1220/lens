# lens

一个个人信息搜集 / 展示 / review 的工作台。把「读到一条信息 → 评判它 → 和 AI 发散讨论 →
沉淀思考 → 冒出灵感」串成一条闭环，而不是再做一个把你淹没的信息流。

> 工具开源，思考私有：代码和源清单是公开的，你的 item、打分、评论、讨论、灵感都在
> `data/` 下、默认 git-ignored，不会进公开仓库。

## 闭环

```
collect ─→ digest ─→ triage ─→ discuss ─→ capture ─→ ideate
 采集       AI中文      打分/标     生成 prompt   回填讨论     记灵感
            摘要整理    状态/评论   拖进 AI 聊    /过程思考    /丢 inbox
```

## 快速开始

```bash
python3 collect.py        # 从 sources.yml 的一手源拉信息 → data/lens.db
python3 summarize.py      # LLM 中文总结：抓原文 → 摘要/要点/为什么值得看 → 回填 DB
python3 server.py         # 起本地看板：http://127.0.0.1:8787
```

依赖：Python 3.10+ 和 PyYAML，其余全是标准库；总结引擎用本机 CLI 的无头模式
（默认 `codex exec`，`--engine claude` 备选），不需要配 API key。

## 结构

```
sources.yml      31 个信息源 + 接入方式 + 实测状态（源真相）
schema.sql       SQLite 表结构
collect.py       采集器：一个 adapter 一种源形态，只增不覆盖你的 triage
summarize.py     AI 总结：抓原文正文 + codex/claude 无头批量产出中文摘要，只写 ai_* 字段
server.py        本地看板 + 全部读写接口（标准库 http）
web/             前端（墨色冷调三栏看板：feed 中文摘要行 + 详情 AI 整理区）
templates/       讨论 prompt 模板
data/            你的私有数据（git-ignored）：lens.db / discussions / ideas
data.example/    给读 repo 的人看的样例数据（公开）
docs/            VISION（产品意图）· DECISIONS（决策记录）· SOURCES（源地图）
CLAUDE.md        给 AI 协作者的操作手册 —— 先读它
```

## 状态

可日常使用：26 个源接通采集，AI 总结管线（采集 → 中文摘要/要点 → 看板可读）已落地，
前端 v3.1 三栏看板可完整跑通闭环。混源（Vercel / HN）按关键词降噪。详见 `CLAUDE.md`。

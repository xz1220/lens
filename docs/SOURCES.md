# SOURCES — lens 信息源地图

31 个一手 / 近一手信息源，按层级分。机器可读版是 `sources.yml`（采集器读它）；这份是给人看的。

- **实测日期**：2026-06-05（来源可访问性会随各站反爬策略变化）。
- **provenance**：源自 life-os `intake/topics/ai-and-ai-agent/research/2026-06-05-workflow.md` 的调研，
  经逐个 live 验证、按用户偏好增删后收敛到这 31 个。
- **status 含义**：
  - `ok` —— 纯 GET 就能拿到数据
  - `needs_parse` —— 返回 200，但数据在内嵌 JSON / HTML 里，要解析
  - `needs_headless` —— 页面纯 JS，要真浏览器
  - `needs_token` —— 要 API token / OAuth
  - `blocked` —— 当前抓不到（OAuth 墙 / bot 拦截），已降级不追
- **adapter** —— `collect.py` 里负责采它的 handler；`null` = 源是真的、采集还没接（最自然的下一批任务）。

当前 **16 个已接通**（adapter 非 null 且 status=ok），**15 个待接**。

## P0 · 机器优先源（15）

| 源 | 入口 | method | status | adapter | 备注 |
|---|---|---|---|---|---|
| OpenAI News | `openai.com/news/rss.xml` | rss | ok | generic_feed | OpenAI 收敛到这一个 |
| Anthropic Newsroom | `anthropic.com/news` | html | needs_parse | — | 内嵌 JSON，无 RSS |
| Anthropic Engineering | `anthropic.com/engineering` | html | needs_parse | — | 同上，工程向 |
| Anthropic Research | `anthropic.com/research` | html | needs_parse | — | 同上，研究向 |
| Claude Release Notes | `support.claude.com/.../release-notes` | html | ok | — | 服务端渲染可抓 |
| Gemini API Changelog | `ai.google.dev/.../changelog` | html | **blocked** | — | 跳 OAuth，降级不追 |
| DeepMind Blog | `deepmind.google/blog/rss.xml` | rss | ok | generic_feed | 官方 RSS（调研中发现）|
| Meta AI Blog | `ai.meta.com/blog` | html | **blocked** | — | 400，降级不追 |
| Mistral Changelog | `docs.mistral.ai/resources/changelogs` | html | ok | — | docs changelog 可抓 |
| Vercel Changelog | `vercel.com/atom` | atom | ok | generic_feed | 官方 Atom |
| Perplexity Changelog | `perplexity.ai/changelog` | html | **blocked** | — | 403 Cloudflare，降级不追 |
| arXiv (cs.CL/LG/AI) | `rss.arxiv.org/rss/cs.CL` | rss | ok | generic_feed | 三个子类，原始日更，噪声高 |
| HF Daily Papers | `huggingface.co/api/daily_papers` | json | ok | hf_daily_papers | API 直给 50 条带 upvote |
| SEC EDGAR API | `data.sec.gov/submissions/` | json | ok | — | 需合规 UA + 查询设计 |
| SEC Latest Filings | `sec.gov/cgi-bin/browse-edgar?...&output=atom` | atom | ok | generic_feed | 带合规 UA 即通（曾误记 403）|

## P1 · 专家自有源（6）

| 源 | 入口 | method | status | adapter | 备注 |
|---|---|---|---|---|---|
| Simon Willison | `simonwillison.net/atom/everything/` | atom | ok | generic_feed | LLM/agent/tooling 高信噪比 |
| Andrej Karpathy | `karpathy.ai/` | html | needs_parse | — | blog 低频，X 作早期信号 |
| Lilian Weng | `lilianweng.github.io/index.xml` | rss | ok | generic_feed | 深度综述 |
| Interconnects | `interconnects.ai/feed` | rss | ok | generic_feed | 开源模型/RLHF/训练 |
| Latent Space | `latent.space/feed` | rss | ok | generic_feed | AI engineer/agent 访谈 |
| Sebastian Raschka | `magazine.sebastianraschka.com/feed` | rss | ok | generic_feed | LLM 训练/读 paper |

## P2 · 排序 / 补全源（4）

| 源 | 入口 | method | status | adapter | 备注 |
|---|---|---|---|---|---|
| alphaXiv | `alphaxiv.org` | html | needs_parse | — | arXiv 讨论层 |
| HF Trending | `huggingface.co/papers/trending` | json | ok | — | 验证代码/benchmark |
| YC Companies | `ycombinator.com/companies` | html | needs_headless | — | Algolia，需 headless |
| a16z Portfolio | `a16z.com/portfolio/` | html | ok | — | 服务端渲染可抓，偏营销 |

## heat · 发现候选源（6）

| 源 | 入口 | method | status | adapter | 备注 |
|---|---|---|---|---|---|
| HN (Algolia) | `hn.algolia.com/api/v1/search_by_date?tags=story` | json | ok | hn_algolia | 讨论热度，回链一手 |
| HF Hub API | `huggingface.co/api/models?sort=createdAt` | json | ok | hf_models | 模型/dataset/Space 的新建/下载/likes |
| GitHub Releases | `api.github.com/repos/{repo}/releases` | json | ok | github_releases | 遍历 watchlist（6 个高信号 AI repo），per-repo 错误隔离 |
| GitHub Trending (OSSInsight) | `api.ossinsight.io/v1/trends/repos/?period=past_week` | json | ok | ossinsight | 补 GitHub 无 trending API |
| Product Hunt | `api.producthunt.com/v2/api/graphql` | json | needs_token | — | GraphQL 需 OAuth token |
| YC Launches | `ycombinator.com/launches`（Accept: json）| json | ok | yc_launches | 偏创业/投融资线索 |

## 接一个新源的步骤

1. 在 `collect.py` 写个 adapter（返回 `{title,url,summary,author,published_at,category}` 列表）。
2. 在 `sources.yml` 把那条源的 `adapter:` 从 `null` 改成你的 adapter 名。
3. `python3 collect.py --source <key>` 验证。

最自然的下一批：把 `needs_parse` 的几个（Anthropic 三博客解析 `__NEXT_DATA__`、Claude release notes /
Mistral / a16z 的 HTML 抓取）和已 `ok` 但没接的 JSON 源（HF Hub、GitHub releases、HF trending）接上。

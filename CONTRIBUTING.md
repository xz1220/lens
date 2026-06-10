# 贡献指南 — lens

lens 的设计哲学是「工具开源，思考私有」：代码和源清单公开，每个人的 item、打分、
评论、讨论、灵感都在自己机器的 `data/` 下（git-ignored）。所以最有价值的贡献单元是
**接一个新的一手信息源**——让所有人的 lens 都能多看到一个好源。

## 开发环境

```bash
git clone <your-fork>
cd lens
pip install pyyaml                       # 唯一的第三方依赖
python3 -m unittest discover -s tests    # 全套测试零网络，约 1s 跑完
python3 server.py --demo                 # 不采集也能看到带示例数据的看板
```

铁律（违反会被打回）：

- **零额外依赖**：只允许 PyYAML + Python 标准库。不引 requests / feedparser / bs4 / node。
- **`data/` 私有**：任何改动不得把用户数据写进会被 git 跟踪的位置。
- **采集只增不改**：`collect.py` 用 `INSERT OR IGNORE`，重跑绝不覆盖用户的
  score / tags / status / comment。
- **总结只写 ai_\*** ：`summarize.py` 只 UPDATE `ai_summary / ai_detail / content_text /
  ai_model / ai_summarized_at`，永远不碰 triage 字段。
- **降噪走配置**：混源过滤用 `sources.yml` 的 `filter_keywords`，不值得总结的源标
  `digest: false`。别在 adapter 里硬编码过滤。

## 怎么接一个新源（贡献的主路径）

一个源 = `sources.yml` 一个条目 + `collect.py` 一个（或复用一个）adapter + 一组离线测试。

1. **在 `sources.yml` 加条目**：`key`（kebab-case 唯一）、`tier`（P0 官方机器源 /
   P1 专家自有 / P2 排序补全 / heat 发现候选）、`category`（决定看板主题归属，见
   `server.py` 的 `TOPIC_CATEGORIES`）、`adapter`、`url`、`notes`（给用户看的中文说明，
   注意别写 `key: value` 形状的文本，会撞 YAML 语法）。
2. **写 adapter（如果现有的不够用）**：
   - RSS / Atom 直接复用 `generic_feed`，不用写代码。
   - 新 adapter 必须 **fetch 与 parse 分离**：`parse_<name>()` 是纯函数（输入原始
     bytes/str，输出 record list），可以脱网单测。
   - HTML 源只用标准库（`re` / `json` / `html.parser`），结构变样时返回 `[]` 并打日志，
     **不崩**。
3. **加测试**：抓一份真实响应存进 `tests/fixtures/`（脱敏、裁剪到最小），在
   `tests/test_collect.py` 里测 `parse_<name>()` 的字段映射和「结构变样返回空」。
4. **实测**：`python3 collect.py --source <key>` 跑通，`python3 collect.py --list`
   能看到状态。

参考样例：`collect.py` 里 `parse_hf_daily_papers`（JSON API）、`parse_claude_release_notes`
（标准库 HTML）、`sources.yml` 里 `karpathy`（generic_feed 零代码）。

## 提交约定

- commit message 用中文、`feat(scope): 描述` 风格（见 `git log`）。
- PR 必须绿：GitHub Actions 跑 `python -m unittest discover -s tests`（零网络）。
- 文档默认中文；代码标识符、命令、路径保留英文。
- 大改动先开 issue 聊，尤其是动 `schema.sql` 或三层架构（sources.yml / lens.db /
  markdown）边界的。

## 哪些事不用做

- 已标 `status: blocked` 的源（Gemini changelog / Meta AI blog / Perplexity）抓不到是
  确认过的（OAuth / 400 / Cloudflare），不必尝试绕过。
- 不要给项目加打包发布（PyPI）、Docker、多用户、云部署——见 `docs/VISION.md` 的
  non-goals，这是一个单人本地工具。

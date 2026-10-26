# AI Daily Briefing

基于 **Tavily + Firecrawl 搜索 API** 的 AI 领域每日简报生成器：自动采集 7 个数据源、打分、去重、分类配额，输出结构化中文日报。

> 采集走搜索 API，不启动浏览器 —— 可跑在 2G 内存服务器上。

## 特性

- **7 大数据源**：36氪、IT之家、HackerNews、知乎、Semantic Scholar、GitHub Trending、官方博客
- **双搜索引擎**：Tavily（默认）+ Firecrawl（research / github 类源）
- **智能打分**：来源权重 + 关键词匹配 + 时效性，三维评分
- **持久去重**：URL + 标题 hash 双重去重，状态跨次保持
- **域名过滤**：自动屏蔽国内无法访问的站点
- **分类配额**：🔥科技/媒体 ×4 + 💬社区/讨论 ×3 + 📄学术/论文 ×1 + 🛠工具/项目 ×5 + 🏢官方博客 ×2 = 15 条

## 文件结构

```
ai-daily-briefing/
├── briefing.py          # 主脚本（完整流水线）
├── briefing_cron.py     # Cron wrapper（Hermes 定时任务入口）
├── sources.yaml         # 外部数据源配置（可选，缺失用内置默认）
└── README.md
```

## 快速开始

### 依赖

```bash
pip install tavily-python        # 必需
pip install firecrawl-py         # 可选，research / github 源
```

### 环境变量

```bash
export TAVILY_API_KEY="tvly-..."      # 必需
export FIRECRAWL_API_KEY="fc-..."     # 可选
```

### 运行

```bash
python3 briefing.py
```

输出 Markdown 格式日报，同时保存到 `~/.hermes/cron/state/ai_daily_briefing/briefing_YYYY-MM-DD.md`。

## 与 Hermes 集成

配合 `briefing_cron.py` 作为 Hermes cron 的脚本，每天定时采集并投递到 QQ / Telegram 等平台：

```bash
hermes cron edit <job_id> \
  --script "briefing_cron.py" \
  --interpreter "~/.hermes/scripts/.venv/bin/python3" \
  --skill ai-daily-briefing \
  --deliver qqbot
```

## 配置说明

`sources.yaml` 中的 `DEFAULT_SOURCES` 为内置兜底，外部 YAML 优先覆盖。每个 source 可配置：

| 字段 | 说明 |
|------|------|
| `name` | 源名，同时用于分类映射 |
| `weight` | 来源权重（0~1） |
| `time_range` | `day` / `week` / `month` / `year` |
| `engine` | `tavily`（默认）或 `firecrawl` |
| `categories` | 仅 firecrawl：`research` / `github` / `pdf` |
| `queries` | 搜索词列表 |

> ⚠️ **`time_range` 对 firecrawl 引擎会自动映射成 `tbs`**（`qdr:d/w/m/y`）。firecrawl SDK 不认 `time_range`，直接传会被静默忽略。

## 已知问题（Pitfalls）

- **查询词不要写裸域名**：`news.ycombinator.com AI LLM` 会被 Tavily 当**主题词**而非域名限定，返回的是 AI 新闻聚合站首页，一条 HN 条目都没有。改成自然语言主题（`Hacker News top AI LLM discussion this week`）才返回真实条目。
- **板块 0 条多为管线问题**：某源返回的 top 结果与昨日重合 → 被 `seen_urls` 整批去重。**不要在日报里写「当日无新内容」**，那是结构性空档。
- **只标记真正输出的条目**：`seen_urls` 只记录最终输出的条目，未选中的候选不会被永久跳过。
- **QQ 投递上限 4000 字符**：满配 15 条 + 分析约 5000 字符会被静默截断。先删无事件条目，再按 4000 字符复核。

## 许可证

MIT

# AI Daily Briefing

基于 Tavily 搜索的 AI 领域每日简报生成器，自动采集、打分、去重，输出结构化中文日报。

## 特性

- **7 大数据源**：36氪、IT之家、HackerNews、知乎、Semantic Scholar、GitHub Trending、官方博客
- **智能打分**：来源权重 + 关键词匹配 + 时效性，三维评分
- **持久去重**：URL + 标题 hash 双重去重，状态跨次保持
- **域名过滤**：自动屏蔽国内无法访问的站点
- **分类配额**：5 个分类独立配额，不足时按分补充

## 文件结构

```
ai-daily-briefing/
├── briefing.py          # 主脚本（466行，完整流水线）
├── briefing_cron.py     # Cron wrapper（8行，Hermes 定时任务入口）
└── sources.yaml         # 外部数据源配置（可选，缺失用内置默认）
```

## 快速开始

### 依赖

```bash
pip install tavily-python
```

### 环境变量

```bash
export TAVILY_API_KEY="your-api-key"
```

### 运行

```bash
python3 briefing.py
```

输出 Markdown 格式日报，同时保存到 `~/.hermes/cron/state/crawl4ai_briefing/briefing_YYYY-MM-DD.md`。

## 与 Hermes 集成

配合 `briefing_cron.py` 作为 Hermes cron 的 `no_agent` 脚本，每天定时采集并投递到 QQ/Telegram 等平台。

## 配置说明

`sources.yaml` 中的 `DEFAULT_SOURCES` 为内置兜底，外部 YAML 优先覆盖。每个 source 可配置：
- `weight`：来源权重（0~1）
- `time_range`：Tavily 时间范围（week/month/day）
- `queries`：搜索词列表

## 许可证

MIT

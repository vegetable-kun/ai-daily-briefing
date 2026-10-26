#!/home/ecs-assist-user/.hermes/scripts/.venv/bin/python3
# -*- coding: utf-8 -*-
"""
脚本自动执行（no-agent 模式），流程如下：
1. 加载 ~/.hermes/skills/data-science/ai-daily-briefing/references/sources.yaml（缺失则用内置 DEFAULT_SOURCES）
2. 读取 ~/.hermes/.env 中的 TAVILY_API_KEY
3. 脚本启动时自动获取当前日期（date.today()），确定 7 天窗口
4. 对每个 source 调用 Tavily Python SDK 搜索（带 time_range 参数服务端筛选）

7 个数据源：
- 技术/媒体：36kr_tech、ithome_ai（time_range=week）
- 社区/讨论：hacker_news、zhihu_ai（time_range=week）
- 学术/论文：semantic_scholar（time_range=month）
- 工具/项目：github_trending（time_range=week）
- 官方博客：official_blogs（time_range=week）

筛选规则：
- Tavily 服务端自动按 time_range 筛选日期，不需要客户端解析
- 每条新闻标注发布日期（格式：YYYY-MM-DD），从 published_date 字段提取
- 各平台查询词按内容特色定位（产业/产品/学术/工具/博客），不含硬编码年份
- 去重：按 URL 规范化 + 标题 hash（状态持久化到 seen_urls.json）

打分与配额：
- 打分公式：0.4·来源权重 + 0.4·关键词命中 + 0.2·时效
- 分类配额：科技/媒体 4 + 社区/讨论 3 + 学术/论文 1 + 工具/项目 5 + 官方博客 2 = 15 条
- 不足时从剩余条目按分数补充

过滤规则：
- 域名黑名单：拦截 openai.com/google/anthropic.com/reddit.com/x.com/youtube.com 等国内访问不了的站点
- URL 规范化：m.36kr.com → 36kr.com
"""
import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from tavily import TavilyClient

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
TODAY = date.today()
WINDOW_DAYS = 7
TOP_N = 15
BATCH_MAX = 5
SEARCH_TIMEOUT = 30

# ---------------------------------------------------------------------------
# 内置默认 sources（外部 YAML 缺失时的兜底）
# ---------------------------------------------------------------------------
DEFAULT_SOURCES: Dict[str, Any] = {
    "sources": [
        {
            "name": "36kr_tech",
            "weight": 1.0,
            "time_range": "week",
            "queries": [
                "36氪 AI 融资 创业 公司 最新动态",
                "36氪 科技 产业 创新 本周 重点",
            ],
        },
        {
            "name": "ithome_ai",
            "weight": 0.9,
            "time_range": "week",
            "queries": [
                "IT之家 AI 应用 产品 发布 最新",
                "IT之家 科技 互联网 新闻 今天",
            ],
        },
        {
            "name": "hacker_news",
            "weight": 0.9,
            "time_range": "week",
            "queries": [
                "news.ycombinator.com AI LLM startup funding",
                "news.ycombinator.com machine learning open source tool",
            ],
        },
        {
            "name": "zhihu_ai",
            "weight": 0.8,
            "time_range": "week",
            "queries": [
                "知乎 AI 大模型 技术 经验 分享",
                "知乎 人工智能 就业 行业 观点",
            ],
        },
        {
            "name": "semantic_scholar",
            "weight": 0.8,
            "time_range": "month",
            "queries": [
                "semanticscholar.org large language model 2026",
                "semanticscholar.org AI agent reinforcement learning",
            ],
        },
        {
            "name": "github_trending",
            "weight": 0.75,
            "time_range": "week",
            "queries": [
                "github trending AI agent harness plugin this week stars:>500",
                "github awesome LLM agent framework MCP tool 2026",
            ],
        },
        {
            "name": "official_blogs",
            "weight": 0.6,
            "time_range": "week",
            "queries": [
                "AI model release announcement August 2026",
                "AI research breakthrough this week 2026",
            ],
        },
    ]
}

# ---------------------------------------------------------------------------
# 状态缓存目录
# ---------------------------------------------------------------------------
STATE_DIR = Path.home() / ".hermes" / "cron" / "state" / "ai_daily_briefing"
STATE_DIR.mkdir(parents=True, exist_ok=True)
SEEN_URLS_FILE = STATE_DIR / "seen_urls.json"
TITLE_HASH_FILE = STATE_DIR / "title_hashes.json"


# ---------------------------------------------------------------------------
# 域名黑名单（国内访问不了）
# ---------------------------------------------------------------------------
BLOCKED_DOMAINS: Set[str] = {
    "openai.com", "anthropic.com", "blog.google",
    "ai.meta.com", "developer.nvidia.com",
    "reddit.com", "x.com",
    "twitter.com", "youtube.com", "instagram.com",
    "facebook.com", "discord.com", "tiktok.com",
    "netflix.com", "spotify.com",
}

# Firecrawl 的 tbs 参数映射 —— firecrawl 引擎不认 time_range，必须转成 tbs
_TIME_RANGE_TO_TBS = {
    "day": "qdr:d",
    "week": "qdr:w",
    "month": "qdr:m",
    "year": "qdr:y",
}


# ===========================================================================
# 工具函数
# ===========================================================================
def _load_json(path: Path, default: Any) -> Any:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def _save_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _title_hash(title: str) -> str:
    return hashlib.md5(title[:80].lower().encode("utf-8")).hexdigest()[:12]


def _url_norm(url: str) -> str:
    if not url:
        return url
    return re.sub(r"^https?://m\.36kr\.com", "https://36kr.com", url, flags=re.IGNORECASE)


def _load_env():
    """加载 ~/.hermes/.env 中的环境变量"""
    env_path = Path.home() / ".hermes" / ".env"
    if env_path.exists():
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip("'\"").strip()
                if key and value:
                    os.environ[key] = value


_load_env()
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")


# ===========================================================================
# URL 过滤（黑名单 + 连通性探测）
# ===========================================================================
def _extract_domain(url: str) -> str:
    """从 URL 提取域名（去掉 www. 前缀）"""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]
        return domain
    except Exception:
        return ""


# ===========================================================================
# Tavily 搜索封装
# ===========================================================================
def get_tavily_client() -> TavilyClient:
    if not TAVILY_API_KEY:
        raise RuntimeError("TAVILY_API_KEY not found in environment")
    return TavilyClient(api_key=TAVILY_API_KEY)


def tavily_search(query: str, time_range: str = "week", max_results: int = 5,
                  tbs: Optional[str] = None, categories: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """调 Tavily SDK 搜索，返回结果列表

    Args:
        query: 搜索查询
        time_range: 时间范围 (day/week/month/year)
        max_results: 最大结果数
        tbs: Firecrawl 时间筛选 (qdr:d/qdr:w/qdr:m/qdr:y)，仅 firecrawl 引擎
        categories: 类别过滤 (github/research/pdf)，仅 firecrawl 引擎
    """
    try:
        client = get_tavily_client()
        kwargs = dict(query=query, max_results=max_results, time_range=time_range)
        if tbs:
            kwargs["tbs"] = tbs
        if categories:
            kwargs["categories"] = categories
        resp = client.search(**kwargs)
        return resp.get("results", [])
    except Exception as e:
        print(f"[WARN] tavily search error for '{query[:50]}': {e}")
        return []


def firecrawl_search(query: str, categories: Optional[List[str]] = None,
                     max_results: int = 5, tbs: Optional[str] = None) -> List[Dict[str, Any]]:
    """Firecrawl SDK 搜索（与 tavily_search 格式一致）"""
    try:
        fc_key = os.environ.get("FIRECRAWL_API_KEY", "")
        if not fc_key:
            print("[WARN] FIRECRAWL_API_KEY not found")
            return []
        from firecrawl import FirecrawlApp
        app = FirecrawlApp(api_key=fc_key)
        resp = app.search(query=query, limit=max_results, categories=categories, tbs=tbs)
        results = []
        for item in resp.web or []:
            results.append({
                "title": item.title or "",
                "url": item.url or "",
                "snippet": item.description or "",
                "published_date": "",
                "source": "firecrawl",
                "category": getattr(item, "category", ""),
            })
        return results
    except Exception as e:
        print(f"[WARN] firecrawl search error for '{query[:50]}': {e}")
        return []


def run_search_batch(queries: List[str], time_range: str = "week", limit: int = 5,
                     tbs: Optional[str] = None, categories: Optional[List[str]] = None,
                     engine: str = "tavily") -> List[Dict[str, Any]]:
    """并发搜索一批查询"""
    if not queries:
        return []
    all_results: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=BATCH_MAX) as executor:
        if engine == "firecrawl":
            futures = {
                executor.submit(firecrawl_search, q, categories, limit, tbs): q
                for q in queries
            }
        else:
            futures = {
                executor.submit(tavily_search, q, time_range, limit, tbs, categories): q
                for q in queries
            }
        for future in as_completed(futures):
            try:
                results = future.result()
                if results:
                    all_results.extend(results)
            except Exception as e:
                q = futures[future]
                print(f"[WARN] batch task exception for '{q[:50]}': {e}")
    return all_results


# ===========================================================================
# 候选项结构
# ===========================================================================
class BriefingItem:
    __slots__ = ("title", "url", "source", "source_weight", "pub_date",
                 "date_unknown", "score", "keywords_matched")

    def __init__(self, title: str, url: str, source: str, source_weight: float,
                 pub_date: str, date_unknown: bool, score: float, keywords_matched: List[str]):
        self.title = title
        self.url = _url_norm(url)
        self.source = source
        self.source_weight = source_weight
        self.pub_date = pub_date
        self.date_unknown = date_unknown
        self.score = score
        self.keywords_matched = keywords_matched


# ===========================================================================
# URL 过滤（黑名单 + 连通性探测）
# ===========================================================================
def _filter_blocked_domains(items: List[BriefingItem]) -> List[BriefingItem]:
    """过滤掉黑名单域名的条目（只拦截确定访问不了的站点）"""
    filtered: List[BriefingItem] = []
    for item in items:
        domain = _extract_domain(item.url)
        if domain in BLOCKED_DOMAINS:
            print(f"[FILTER] blocked domain ({domain}): {item.title[:50]}")
            continue
        filtered.append(item)
    return filtered


# ===========================================================================
# 关键词打分
# ===========================================================================
KEYWORDS_CORE = [
    "llm", "large language model", "agent", "rag", "multimodal",
    "training", "inference", "fine-tuning", "open source", "benchmark",
    "reasoning", "context window",
    "vllm", "sglang", "tensorrt-llm", "ollama", "lmdeploy",
    "lora", "qlora", "dpo", "ppo", "grpo", "rlhf", "rlaif",
    "retrieval", "rerank", "embedding", "vector database",
    "function calling", "tool use", "mcp", "a2a",
    "mixture of experts", "moe", "speculative decoding",
    "long context", "32k", "128k", "1m context",
    "gpt", "gemini", "claude", "llama", "qwen", "nemotron", "gemma",
    "deepseek", "kimi", "moonshot", "智谱", "glm",
    "openclaw", "coding agent", "智能体", "大模型", "大语言模型",
    "多模态", "推理", "训练", "微调", "对齐",
]


def keyword_score(text: str) -> Tuple[float, List[str]]:
    low = text.lower()
    hits = [kw for kw in KEYWORDS_CORE if kw in low]
    return round(min(0.5, len(hits) * 0.08), 3), hits


# ===========================================================================
# 结果解析
# ===========================================================================
def parse_result_items(raw_results: List[Dict[str, Any]], source_name: str,
                       source_weight: float) -> List[BriefingItem]:
    """将 Tavily 返回的结果列表解析为 BriefingItem 候选"""
    out: List[BriefingItem] = []
    seen_local: Set[str] = set()
    for r in raw_results:
        title = (r.get("title") or "").strip()
        url = (r.get("url") or "").strip()
        if not title or not url:
            continue
        norm_url = _url_norm(url)
        if norm_url in seen_local:
            continue
        seen_local.add(norm_url)

        # Tavily 返回的 published_date 字段（如果有）
        pub_date_raw = r.get("published_date") or ""
        # 也尝试从 snippet/content 里提取日期
        snippet = (r.get("content") or r.get("snippet") or "").strip()
        if not pub_date_raw:
            m = re.search(r"(\d{4}-\d{2}-\d{2})", snippet)
            if m:
                pub_date_raw = m.group(1)

        date_unknown = not bool(pub_date_raw)

        item = BriefingItem(
            title=title,
            url=url,
            source=source_name,
            source_weight=source_weight,
            pub_date=pub_date_raw if pub_date_raw else "unknown",
            date_unknown=date_unknown,
            score=0.0,
            keywords_matched=[],
        )
        out.append(item)
    return out


# ===========================================================================
# 加载配置
# ===========================================================================
def load_sources(config_path: Optional[Path] = None) -> Dict[str, Any]:
    """优先读外部 sources.yaml；若不存在/解析失败，使用 DEFAULT_SOURCES"""
    if config_path is not None and config_path.exists():
        try:
            import yaml
            with open(config_path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            if isinstance(cfg, dict) and "sources" in cfg:
                return cfg
        except Exception as e:
            print(f"[WARN] 加载 sources.yaml 失败，使用内置默认: {e}")
    return DEFAULT_SOURCES


# ===========================================================================
# 主生成逻辑
# ===========================================================================
class BriefingGenerator:
    CATEGORY_QUOTAS: Dict[str, int] = {
        "🔥 科技/媒体": 4,
        "💬 社区/讨论": 3,
        "📄 学术/论文": 1,
        "🛠 工具/项目": 5,
        "🏢 官方博客": 2,
    }
    CATEGORY_SOURCES: Dict[str, List[str]] = {
        "🔥 科技/媒体": ["36kr_tech", "ithome_ai"],
        "💬 社区/讨论": ["hacker_news", "zhihu_ai"],
        "📄 学术/论文": ["semantic_scholar"],
        "🛠 工具/项目": ["github_trending"],
        "🏢 官方博客": ["official_blogs"],
    }

    def __init__(self, sources_config: Dict[str, Any], max_items: int = TOP_N):
        self.sources = sources_config.get("sources", [])
        self.max_items = max_items
        self.seen_urls: Set[str] = set(_load_json(SEEN_URLS_FILE, []))
        self.title_hashes: Set[str] = set(_load_json(TITLE_HASH_FILE, []))

    def deduplicate(self, items: List[BriefingItem]) -> List[BriefingItem]:
        """去重：URL 规范化 + 标题 hash。

        注意：这里只读 self.seen_urls/ title_hashes 做判定，不写入。
        写入推迟到 emit/落盘阶段，避免「采集到但因配额/过滤没输出」的条目
        被永久标记为已见过，导致后续运行把它们全部跳过（正反馈死锁）。
        """
        unique: List[BriefingItem] = []
        local_urls: Set[str] = set()
        local_titles: Set[str] = set()
        for item in items:
            norm = _url_norm(item.url)
            item.url = norm
            if not norm:
                continue
            if norm in self.seen_urls or norm in local_urls:
                continue
            th = _title_hash(item.title)
            if th in self.title_hashes or th in local_titles:
                continue
            local_urls.add(norm)
            local_titles.add(th)
            unique.append(item)
        return unique

    def score_item(self, item: BriefingItem) -> BriefingItem:
        combo = item.title + " " + " ".join(item.keywords_matched[:10])
        kw_score, kw_matched = keyword_score(combo)
        weight_score = item.source_weight
        recency = 0.2 if not item.date_unknown else 0.0
        total = round(0.4 * weight_score + 0.4 * kw_score + 0.2 * recency, 3)
        item.score = total
        item.keywords_matched = kw_matched
        return item

    def fetch_source(self, src: Dict[str, Any]) -> List[BriefingItem]:
        name = src["name"]
        weight = src.get("weight", 1.0)
        time_range = src.get("time_range", "week")
        queries = src.get("queries", [])
        engine = src.get("engine", "tavily")
        categories = src.get("categories")
        print(f"[INFO] source={name} weight={weight} time_range={time_range} engine={engine} queries={len(queries)}")

        batches: List[List[str]] = []
        for i in range(0, len(queries), BATCH_MAX):
            batches.append(queries[i:i + BATCH_MAX])

        all_raw: List[Dict[str, Any]] = []
        for bi, batch in enumerate(batches, 1):
            print(f"  [INFO]   batch {bi}/{len(batches)} ({len(batch)} queries)")
            tbs = _TIME_RANGE_TO_TBS.get(time_range) if engine == "firecrawl" else None
            results = run_search_batch(batch, time_range=time_range, limit=5,
                                       engine=engine, categories=categories, tbs=tbs)
            all_raw.extend(results)
            print(f"  [INFO]   batch {bi} returned {len(results)} raw results")

        candidates = parse_result_items(all_raw, name, weight)
        print(f"[INFO]   {name}: {len(candidates)} candidates")
        return candidates

    def generate(self) -> str:
        all_items: List[BriefingItem] = []
        for src in self.sources:
            items = self.fetch_source(src)
            for it in items:
                self.score_item(it)
            all_items.extend(items)

        unique = self.deduplicate(all_items)
        print(f"[INFO] after dedup: {len(unique)} items")

        # URL 过滤：黑名单拦截
        unique = _filter_blocked_domains(unique)
        print(f"[INFO] after URL filter: {len(unique)} items")

        # 按分类分组
        category_items: Dict[str, List[BriefingItem]] = {cat: [] for cat in self.CATEGORY_QUOTAS}
        for item in unique:
            for cat, sources in self.CATEGORY_SOURCES.items():
                if item.source in sources:
                    category_items[cat].append(item)
                    break

        for cat in category_items:
            category_items[cat].sort(key=lambda x: x.score, reverse=True)

        # 按配额选取
        selected: List[BriefingItem] = []
        remaining: List[BriefingItem] = []
        for cat, quota in self.CATEGORY_QUOTAS.items():
            items = category_items[cat]
            selected.extend(items[:quota])
            remaining.extend(items[quota:])
            print(f"[INFO] {cat}: {len(items)} available, take {min(quota, len(items))}")

        if len(selected) < self.max_items:
            remaining.sort(key=lambda x: x.score, reverse=True)
            needed = self.max_items - len(selected)
            selected.extend(remaining[:needed])
            print(f"[INFO] fill {needed} from remaining")

        selected.sort(key=lambda x: x.score, reverse=True)
        top = selected[:self.max_items]

        # 只标记「真正输出」的条目，避免未选中的候选被永久跳过（正反馈死锁）
        for it in top:
            self.seen_urls.add(it.url)
            self.title_hashes.add(_title_hash(it.title))

        _save_json(SEEN_URLS_FILE, list(self.seen_urls)[-1000:])
        _save_json(TITLE_HASH_FILE, list(self.title_hashes)[-1000:])

        if not top:
            raise RuntimeError(
                "采集结果为 0 条（去重/过滤后无内容）。拒绝输出空日报—— "
                "否则上游模型会拿到 0 条素材却要填满模板，陷入repetition loop。"
                "排查方向：seen_urls/title_hashes 是否已满 1000 条导致全部命中去重；"
                "或 Tavily/Firecrawl 配额耗尽。"
            )

        return self._render(top)

    def _render(self, items: List[BriefingItem]) -> str:
        categories: Dict[str, List[str]] = {
            "🔥 科技/媒体": ["36kr_tech", "ithome_ai"],
            "💬 社区/讨论": ["hacker_news", "zhihu_ai"],
            "📄 学术/论文": ["semantic_scholar"],
            "🛠 工具/项目": ["github_trending"],
            "🏢 官方博客": ["official_blogs"],
        }
        lines: List[str] = [f"【📅 {TODAY.isoformat()} AI 日报 | 共 {len(items)} 条】\n"]
        for cat_name, sources in categories.items():
            cat_items = [i for i in items if i.source in sources]
            if not cat_items:
                continue
            lines.append(f"**{cat_name}** ({len(cat_items)} 条)")
            for idx, item in enumerate(cat_items, 1):
                date_str = item.pub_date
                if item.date_unknown:
                    date_str = "日期未知"
                tags = ""
                if item.keywords_matched:
                    tags = " | 🏷 " + ", ".join(item.keywords_matched[:5])
                lines.append(
                    f"{idx}. **{item.title}**\n"
                    f"   📅 {date_str} | 🔗 {item.url} | 来源: {item.source} | ⭐ {item.score}"
                    f"{tags}"
                )
            lines.append("")
        return "\n".join(lines)


def main():
    config_path = Path.home() / ".hermes" / "skills" / "data-science" / "ai-daily-briefing" / "references" / "sources.yaml"
    sources_config = load_sources(config_path)
    gen = BriefingGenerator(sources_config, max_items=TOP_N)
    output = gen.generate()
    print(output)
    out_path = STATE_DIR / f"briefing_{TODAY.isoformat()}.md"
    out_path.write_text(output, encoding="utf-8")
    print(f"[INFO] 输出已保存至 {out_path}")


if __name__ == "__main__":
    main()

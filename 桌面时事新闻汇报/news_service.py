# -*- coding: utf-8 -*-
"""数据层：从免费公开接口拉取每日时事新闻，本地缓存，故障自动降级。

数据源（全部免费、无需注册 Key，已实测可用）：
  - 今日简报: 60s.viki.moe（每天约 30 条一句话新闻）
  - 百度热搜 / 腾讯新闻 / B站热搜 / 知乎热榜（官方公开接口）

网络故障或接口异常时：界面继续显示本地缓存（标注"离线缓存"），
不打断使用；刷新成功后再覆盖缓存。
"""
import json
import os
import queue
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import date

import sys

if getattr(sys, "frozen", False):
    # 打包成 exe 后 __file__ 指向临时解压目录，数据需跟随 exe 本体存放
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
CACHE_FILE = os.path.join(DATA_DIR, "cache.json")

UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0 Safari/537.36"),
}

# kind -> (接口地址, 格式)；"qbitai"/"sinatech" 为 AI 频道的隐藏喂养源，不单独显示
SOURCES = {
    "brief":    ("https://60s.viki.moe/v2/60s", "json"),
    "baidu":    ("https://top.baidu.com/api/board?platform=wise&tab=realtime", "json"),
    "tencent":  ("https://r.inews.qq.com/gw/event/hot_ranking_list?page_size=30", "json"),
    "bili":     ("https://s.search.bilibili.com/main/hotword", "json"),
    "zhihu":    ("https://api.zhihu.com/topstory/hot-list?limit=30", "json"),
    "qbitai":   ("https://www.qbitai.com/feed", "xml"),   # 量子位：纯 AI 资讯 RSS
    "sinatech": ("https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516&k=&num=40&page=1", "json"),  # 新浪科技滚动
}

# kind -> 频道显示名称（界面频道顺序即此字典顺序；"ai" 为聚合频道，无独立接口）
CHANNELS = {
    "brief":   "今日简报",
    "ai":      "AI 热点",
    "baidu":   "百度热搜",
    "tencent": "腾讯新闻",
    "bili":    "B站热搜",
    "zhihu":   "知乎热榜",
}

# ---------- AI 话题聚合 ----------
AI_KEYWORDS_CN = [
    "人工智能", "大模型", "大语言模型", "智能体", "具身智能", "生成式",
    "文心", "通义", "豆包", "元宝", "千问", "星火", "混元", "智谱",
    "月之暗面", "商汤", "旷视", "寒武纪", "海光", "昇腾", "地平线",
    "人形机器人", "机器人", "自动驾驶", "智能驾驶", "无人车", "算力",
    "机器学习", "深度学习", "神经网络", "提示词", "智能眼镜", "芯片",
    "半导体", "显卡", "数据中心", "智算", "数字人", "虚拟人",
    "脑机接口", "讯飞", "百川", "天工", "盘古", "悟道", "多模态",
    "优必选", "宇树", "英伟达",
]
AI_KEYWORDS_EN = [
    "ai", "aigc", "gpt", "chatgpt", "openai", "deepseek", "gemini", "claude",
    "anthropic", "copilot", "kimi", "llm", "xai", "grok", "sora", "midjourney",
    "nvidia", "llama", "stable diffusion", "gpu", "agent", "qwen", "glm",
    "robotaxi", "aipc",
]
# 英文关键词按单词边界匹配，避免 "said/wait/air" 这类误命中
_AI_EN_RE = re.compile(
    r"(?<![a-z])(?:" + "|".join(re.escape(k) for k in AI_KEYWORDS_EN) + r")(?![a-z])",
    re.IGNORECASE)

# 聚合顺序：纯 AI 源在前，综合源按热度质量排后；qbitai 全量收录无需筛选
AI_SOURCE_PRIORITY = ["qbitai", "sinatech", "brief", "baidu", "tencent", "zhihu", "bili"]
AI_FULL_SOURCES = {"qbitai"}
AI_SOURCE_NAMES = {"qbitai": "量子位", "sinatech": "新浪科技", "brief": "简报",
                   "baidu": "百度", "tencent": "腾讯", "zhihu": "知乎", "bili": "B站"}
AI_MAX_ITEMS = 80


def _is_ai_topic(title):
    if any(k in title for k in AI_KEYWORDS_CN):
        return True
    return bool(_AI_EN_RE.search(title.lower()))


def build_ai_items(cache):
    """从各频道缓存中筛出 AI 相关话题并聚合去重（按标题精确去重）。

    纯 AI 源（量子位）全量收录；其余源按关键词筛选。
    """
    seen = set()
    items = []
    for kind in AI_SOURCE_PRIORITY:
        entry = cache.get(kind) or {}
        for it in entry.get("items") or []:
            title = it.get("title") or ""
            if not title or title in seen:
                continue
            if kind not in AI_FULL_SOURCES and not _is_ai_topic(title):
                continue
            seen.add(title)
            hot = it.get("hot") or ""
            hot = f"{AI_SOURCE_NAMES[kind]} · {hot}" if hot else AI_SOURCE_NAMES[kind]
            items.append({"rank": len(items) + 1, "title": title,
                          "hot": hot, "url": it.get("url") or ""})
            if len(items) >= AI_MAX_ITEMS:
                return items
    return items


def _http_get(url):
    """GET 原始文本，失败抛出异常由上层降级处理。"""
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=12) as resp:
        return resp.read().decode("utf-8", "replace")


def _fmt_hot(value):
    """热度数值转短格式：953003 -> '95.3万'。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return str(value) if value else ""
    if n <= 0:
        return ""
    if n >= 100000000:
        return f"{n / 100000000:.1f}亿"
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    return str(n)


# ---------- 各数据源解析器：返回 (items, meta) ----------
# 统一条目结构: {"rank": 序号, "title": 标题, "hot": 热度显示文本, "url": 原文链接}

def parse_brief(data):
    d = data.get("data") or {}
    # 简报条目没有原文链接，点击时按标题跳转百度搜索
    items = [{"rank": i + 1, "title": t, "hot": "",
              "url": "https://www.baidu.com/s?wd=" + urllib.parse.quote(t)}
             for i, t in enumerate(d.get("news") or [])]
    meta = {
        "date": d.get("date", ""),
        "tip": d.get("tip", ""),
        "day_of_week": d.get("day_of_week", ""),
        "lunar_date": d.get("lunar_date", ""),
        "link": d.get("link", ""),
    }
    return items, meta


def parse_baidu(data):
    items = []
    for card in data.get("data", {}).get("cards") or []:
        for group in card.get("content") or []:
            for it in group.get("content") or []:
                word = it.get("word")
                if not word:
                    continue
                items.append({
                    "rank": len(items) + 1,
                    "title": word,
                    "hot": "置顶" if it.get("isTop") else "",
                    "url": it.get("url") or "",
                })
    return items, {}


def parse_tencent(data):
    items = []
    for block in data.get("idlist") or []:
        for n in block.get("newslist") or []:
            # 跳过顶部横幅（"腾讯新闻用户最关注的热点…"）
            if n.get("articletype") == 560 or str(n.get("id", "")).startswith("TIP"):
                continue
            hot_event = n.get("hotEvent") or {}
            title = hot_event.get("title") or n.get("title") or ""
            if not title:
                continue
            items.append({
                "rank": len(items) + 1,
                "title": title,
                "hot": _fmt_hot(hot_event.get("hotScore")),
                "url": n.get("url") or "",
            })
    return items, {}


def parse_bili(data):
    items = []
    for it in data.get("list") or []:
        name = it.get("show_name") or it.get("keyword")
        if not name:
            continue
        items.append({
            "rank": len(items) + 1,
            "title": name,
            "hot": _fmt_hot(it.get("heat_score")),
            "url": "https://search.bilibili.com/all?keyword=" + urllib.parse.quote(name),
        })
    return items, {}


def parse_zhihu(data):
    items = []
    for it in data.get("data") or []:
        target = it.get("target") or {}
        title = target.get("title")
        if not title:
            continue
        qid = target.get("id")
        if target.get("type") == "question" and qid:
            url = f"https://www.zhihu.com/question/{qid}"
        else:
            url = target.get("url") or ""
        items.append({
            "rank": len(items) + 1,
            "title": title,
            "hot": it.get("detail_text") or "",
            "url": url,
        })
    return items, {}


def parse_qbitai(raw_xml):
    """量子位 RSS：纯 AI 资讯，全量收录。"""
    import xml.etree.ElementTree as ET
    items = []
    try:
        root = ET.fromstring(raw_xml)
        for node in root.iter("item"):
            title = (node.findtext("title") or "").strip()
            link = (node.findtext("link") or "").strip()
            if not title:
                continue
            items.append({"rank": len(items) + 1, "title": title,
                          "hot": "", "url": link})
    except ET.ParseError:
        pass
    return items, {}


def parse_sinatech(data):
    """新浪科技滚动新闻：全量收录，由 AI 聚合层按关键词筛选。"""
    res = data.get("result") or {}
    items = []
    for x in res.get("data") or []:
        title = x.get("title")
        if not title:
            continue
        items.append({"rank": len(items) + 1, "title": title,
                      "hot": "", "url": x.get("url") or ""})
    return items, {}


PARSERS = {
    "brief": parse_brief,
    "baidu": parse_baidu,
    "tencent": parse_tencent,
    "bili": parse_bili,
    "zhihu": parse_zhihu,
    "qbitai": parse_qbitai,
    "sinatech": parse_sinatech,
}


class NewsService:
    """新闻数据服务：后台拉取 -> 解析 -> 写缓存，通过队列向 UI 报告结果。"""

    STALE_HOT_MINUTES = 120  # 热搜缓存超过 2 小时视为过期

    def __init__(self):
        self.events = queue.Queue()  # 元素: (kind, ok, error_message)
        self._lock = threading.Lock()
        self.cache = self._load_cache()
        # 启动时先用现有缓存重建 AI 频道，保证打开即显示
        if "ai" not in self.cache:
            items = build_ai_items(self.cache)
            if items:
                self.cache["ai"] = {
                    "items": items, "meta": {},
                    "fetched_at": time.time(), "date": str(date.today()),
                }
                self._save_cache()

    # ---------- 本地缓存 ----------
    def _load_cache(self):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_cache(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.cache, f, ensure_ascii=False)
        os.replace(tmp, CACHE_FILE)

    def cached(self, kind):
        """该分类的缓存数据；无缓存返回 None。"""
        entry = self.cache.get(kind)
        if not entry:
            return None
        age_min = (time.time() - entry.get("fetched_at", 0)) / 60
        if kind == "brief":
            stale = entry.get("date") != str(date.today())
        else:
            stale = age_min > self.STALE_HOT_MINUTES
        return {
            "items": entry.get("items", []),
            "meta": entry.get("meta", {}),
            "fetched_at": entry.get("fetched_at", 0),
            "stale": stale,
        }

    # ---------- 后台拉取 ----------
    def refresh_all(self):
        threading.Thread(target=self._fetch_all, daemon=True).start()

    def _fetch_all(self):
        for kind in SOURCES:
            self._fetch_one(kind)
            time.sleep(0.4)  # 控制请求节奏，对接口友好
        self._rebuild_ai()   # 各频道拉取完成后，重建 AI 聚合频道

    def _rebuild_ai(self):
        items = build_ai_items(self.cache)
        if items:
            with self._lock:
                self.cache["ai"] = {
                    "items": items,
                    "meta": {},
                    "fetched_at": time.time(),
                    "date": str(date.today()),
                }
                self._save_cache()
        self.events.put(("ai", True, ""))

    def _fetch_one(self, kind):
        url, fmt = SOURCES[kind]
        try:
            raw = _http_get(url)
            data = json.loads(raw) if fmt == "json" else raw
            items, meta = PARSERS[kind](data)
            if not items:
                raise ValueError("返回内容为空")
            entry = {
                "items": items,
                "meta": meta,
                "fetched_at": time.time(),
                "date": str(date.today()),
            }
            with self._lock:
                self.cache[kind] = entry
                self._save_cache()
            self.events.put((kind, True, ""))
        except Exception as exc:
            # 网络/解析/接口异常：保留旧缓存，只报告失败
            self.events.put((kind, False, str(exc)))

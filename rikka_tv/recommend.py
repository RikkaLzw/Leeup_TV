from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .douban import DoubanClient
from .db import get_recommend_cache, save_recommend_cache
from .maccms import MacCMSClient


_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_LOCK = threading.Lock()
_DOUBAN_MOVIE_CATEGORIES = ["喜剧", "爱情", "动作", "科幻", "动画", "悬疑", "犯罪", "惊悚", "冒险", "音乐", "历史", "奇幻", "恐怖", "战争", "传记", "歌舞", "武侠", "情色", "灾难", "西部", "纪录片", "短片"]
_DOUBAN_TV_CATEGORIES = ["喜剧", "爱情", "悬疑", "动画", "武侠", "古装", "家庭", "犯罪", "科幻", "恐怖", "历史", "战争", "动作", "冒险", "传记", "剧情", "奇幻", "惊悚", "灾难", "歌舞", "音乐"]
_DOUBAN_ANIME_CATEGORIES = ["动画", "喜剧", "爱情", "动作", "冒险", "科幻", "奇幻", "悬疑", "家庭", "音乐"]
_DOUBAN_VARIETY_CATEGORIES = ["真人秀", "脱口秀", "音乐", "歌舞"]
_DOUBAN_CATEGORY_MAP = {
    "电影": set(_DOUBAN_MOVIE_CATEGORIES),
    "剧集": set(_DOUBAN_TV_CATEGORIES),
    "动漫": set(_DOUBAN_ANIME_CATEGORIES),
    "综艺": set(_DOUBAN_VARIETY_CATEGORIES),
}
_DOUBAN_MOVIE_REGIONS = ["华语", "欧美", "韩国", "日本", "中国大陆", "美国", "中国香港", "中国台湾", "英国", "法国", "德国", "意大利", "西班牙", "印度", "泰国", "俄罗斯", "加拿大", "澳大利亚", "爱尔兰", "瑞典", "巴西", "丹麦"]
_DOUBAN_TV_REGIONS = ["华语", "欧美", "国外", "韩国", "日本", "中国大陆", "中国香港", "美国", "英国", "泰国", "中国台湾", "意大利", "法国", "德国", "西班牙", "俄罗斯", "瑞典", "巴西", "丹麦", "印度", "加拿大", "爱尔兰", "澳大利亚"]


def get_recommend_sections(
    config: dict[str, Any],
    force_refresh: bool = False,
    level1: str | None = None,
    expanded: bool = False,
    keys: set[str] | None = None,
    include_filter_sections: bool = False,
) -> list[dict[str, Any]]:
    section_configs = _recommend_section_configs(config)
    selected_keys = None if keys is None else {str(key) for key in keys}
    active_configs: list[dict[str, Any]] = []
    metadata_only_keys: set[str] = set()
    for section in section_configs:
        if section.get("disabled") or (level1 and section.get("level1") != level1):
            continue
        key = str(section.get("key") or section.get("title", ""))
        selected = selected_keys is None or key in selected_keys
        metadata_only = include_filter_sections and not selected and _is_filter_metadata_section(section)
        if not selected and not metadata_only:
            continue
        active_configs.append(section)
        if metadata_only:
            metadata_only_keys.add(key)
    if not active_configs:
        return []
    section_workers = min(int(config.get("recommendations", {}).get("section_workers") or 3), len(active_configs))
    client = DoubanClient(config)
    sections_by_key: dict[str, dict[str, Any]] = {}
    for section_config in active_configs:
        key = str(section_config.get("key") or section_config.get("title", ""))
        sections_by_key[key] = _section_payload(section_config, [], force_filter_only=key in metadata_only_keys)

    fetch_configs = [
        section_config
        for section_config in active_configs
        if section_config.get("prefetch", True) is not False
        and not section_config.get("filter_only")
        and str(section_config.get("key") or section_config.get("title", "")) not in metadata_only_keys
    ]
    if not fetch_configs:
        return [sections_by_key[str(section.get("key") or section.get("title", ""))] for section in active_configs]

    section_workers = min(section_workers, len(fetch_configs))
    with ThreadPoolExecutor(max_workers=section_workers) as pool:
        futures = {
            pool.submit(get_recommend_items, client, config, _section_fetch_config(section_config, expanded), force_refresh): section_config
            for section_config in fetch_configs
        }
        for future in as_completed(futures):
            section_config = futures[future]
            try:
                items = future.result()
            except Exception:
                items = []
            key = str(section_config.get("key") or section_config.get("title", ""))
            sections_by_key[key] = _section_payload(section_config, items)

    return [sections_by_key[str(section.get("key") or section.get("title", ""))] for section in active_configs]


def _section_payload(
    section_config: dict[str, Any],
    items: list[dict[str, Any]],
    force_filter_only: bool = False,
) -> dict[str, Any]:
    key = str(section_config.get("key") or section_config.get("title", ""))
    return {
        "key": key,
        "title": section_config.get("title") or "推荐",
        "level1": section_config.get("level1") or "全部",
        "level2": section_config.get("level2") or section_config.get("title") or "全部",
        "region": section_config.get("region") or "",
        "filter_group": _section_filter_group(section_config),
        "filter_label": _section_filter_label(section_config),
        "filter_only": bool(force_filter_only or section_config.get("filter_only")),
        "source": "douban",
        "items": items,
    }


def _section_fetch_config(section_config: dict[str, Any], expanded: bool) -> dict[str, Any]:
    if not expanded:
        return section_config
    key = str(section_config.get("key") or section_config.get("title") or "")
    limit = int(section_config.get("page_limit") or section_config.get("limit") or 60)
    return {**section_config, "key": f"{key}_page", "limit": limit}


def get_recommend_items(
    client: DoubanClient,
    config: dict[str, Any],
    section_config: dict[str, Any],
    force_refresh: bool = False,
) -> list[dict[str, Any]]:
    cache_seconds = int(config.get("recommendations", {}).get("cache_seconds") or 1800)
    key = _cache_key(section_config)
    now = time.time()
    if not force_refresh:
        with _LOCK:
            cached = _CACHE.get(key)
            if cached and cached[0] > now:
                return cached[1]
        cached_items = get_recommend_cache(key, cache_seconds)
        if cached_items is not None:
            with _LOCK:
                _CACHE[key] = (now + cache_seconds, cached_items)
            return cached_items

    items = _fill_missing_posters(_fetch_section_items(client, section_config), config)
    save_recommend_cache(key, items)
    with _LOCK:
        _CACHE[key] = (now + cache_seconds, items)
    return items


def get_recommend_config(config: dict[str, Any], key: str) -> dict[str, Any] | None:
    sections = _recommend_section_configs(config)
    for section in sections:
        if section.get("key") == key:
            return section
    return None


def get_recommend_config_by_level(config: dict[str, Any], level1: str, level2: str) -> dict[str, Any] | None:
    sections = _recommend_section_configs(config)
    for section in sections:
        if section.get("disabled"):
            continue
        if section.get("level1") == level1 and section.get("level2") == level2:
            return section
    return None


def _fetch_section_items(client: DoubanClient, section_config: dict[str, Any]) -> list[dict[str, Any]]:
    section_config = _effective_douban_filter_config(section_config)
    method = section_config.get("method") or "recent_hot"
    kind = section_config.get("kind") or "movie"
    limit = int(section_config.get("limit") or 12)
    start = int(section_config.get("start") or 0)

    if method == "recommend":
        return client.recommend(
            kind=kind,
            limit=limit,
            start=start,
            category=_none_all(section_config.get("category")),
            format_=_none_all(section_config.get("format")),
            region=_none_all(section_config.get("region")),
            year=_none_all(section_config.get("year")),
            platform=_none_all(section_config.get("platform")),
            sort=_none_all(section_config.get("sort")),
            label=_none_all(section_config.get("label")),
        )[:limit]

    if method == "tag":
        return client.list_by_tag(
            kind=kind,
            tag=section_config.get("tag") or section_config.get("category") or "热门",
            limit=limit,
            start=start,
        )[:limit]

    return client.recent_hot(
        kind=kind,
        category=section_config.get("category") or "热门",
        item_type=section_config.get("type") or "全部",
        limit=limit,
        start=start,
    )[:limit]


def _effective_douban_filter_config(section_config: dict[str, Any]) -> dict[str, Any]:
    if section_config.get("method") != "recommend":
        return section_config
    level1 = str(section_config.get("level1") or "")
    kind = str(section_config.get("kind") or "")
    if kind != "tv":
        return section_config

    result = dict(section_config)
    if level1 == "剧集":
        result.setdefault("format", "电视剧")
    elif level1 == "综艺":
        result.setdefault("format", "综艺")
    elif level1 == "动漫":
        category = str(result.get("category") or "")
        if category and category != "动画" and not result.get("label"):
            result["label"] = category
        result["category"] = "动画"
    return result


def _fill_missing_posters(items: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    missing = [item for item in items if not item.get("poster")]
    if not missing:
        return items

    client = MacCMSClient(config)
    max_workers = min(4, len(missing))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_find_source_poster, client, item): item for item in missing}
        for future in as_completed(futures):
            item = futures[future]
            try:
                poster = future.result()
            except Exception:
                poster = ""
            if poster:
                item["poster"] = poster
                item["poster_source"] = "video_source"
    return items


def _prefer_canonical_douban_posters(items: list[dict[str, Any]], client: DoubanClient) -> list[dict[str, Any]]:
    douban_items = [item for item in items if item.get("provider") == "douban" and item.get("title")]
    if not douban_items:
        return items

    max_workers = min(4, len(douban_items))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(client.find_poster, str(item.get("title") or ""), str(item.get("year") or "")): item
            for item in douban_items
        }
        for future in as_completed(futures):
            item = futures[future]
            try:
                poster = future.result()
            except Exception:
                poster = {}
            if poster.get("poster"):
                item["poster"] = poster["poster"]
                item["raw_poster"] = poster.get("raw_poster", "")
                item["poster_source"] = "douban"
                if poster.get("douban_id"):
                    item["id"] = poster["douban_id"]
    return items


def _find_source_poster(client: MacCMSClient, item: dict[str, Any]) -> str:
    title = item.get("title") or ""
    if not title:
        return ""
    payload = client.search_all(title, max_page=1)
    normalized = _normalize_title(title)
    for result in payload.get("results", []):
        if result.get("poster") and _normalize_title(result.get("title", "")) == normalized:
            return result["poster"]
    for result in payload.get("results", []):
        if result.get("poster"):
            return result["poster"]
    return ""


def _normalize_title(value: str) -> str:
    import re

    return re.sub(r"[\s\-_·:：,，.。!！?？()\[\]（）【】]+", "", str(value).lower())


def _cache_key(section_config: dict[str, Any]) -> str:
    section_config = _effective_douban_filter_config(section_config)
    parts = [
        section_config.get("key"),
        section_config.get("method"),
        section_config.get("kind"),
        section_config.get("category"),
        section_config.get("type"),
        section_config.get("tag"),
        section_config.get("format"),
        section_config.get("region"),
        section_config.get("year"),
        section_config.get("platform"),
        section_config.get("label"),
        section_config.get("sort"),
        section_config.get("limit"),
        section_config.get("start"),
    ]
    return "|".join(str(part or "") for part in parts)


def _none_all(value: Any) -> str:
    value = str(value or "")
    return "" if value == "all" else value


def _recommend_section_configs(config: dict[str, Any]) -> list[dict[str, Any]]:
    configured = config.get("recommendations", {}).get("sections") or []
    sections = configured or _default_sections()
    return _with_generated_filter_sections(sections)


def _with_generated_filter_sections(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    existing_keys = {str(section.get("key") or "") for section in sections}
    existing_pairs = {
        (str(section.get("level1") or ""), str(section.get("level2") or ""))
        for section in sections
    }
    existing_filter_keys = {
        (
            str(section.get("level1") or ""),
            _section_filter_group(section),
            _section_filter_label(section),
        )
        for section in sections
    }
    result = list(sections)
    for section in _generated_filter_sections():
        key = str(section.get("key") or "")
        pair = (str(section.get("level1") or ""), str(section.get("level2") or ""))
        filter_key = (
            str(section.get("level1") or ""),
            _section_filter_group(section),
            _section_filter_label(section),
        )
        if key in existing_keys or pair in existing_pairs or filter_key in existing_filter_keys:
            continue
        result.append(section)
        existing_keys.add(key)
        existing_pairs.add(pair)
        existing_filter_keys.add(filter_key)
    return result


def _is_filter_metadata_section(section_config: dict[str, Any]) -> bool:
    return section_config.get("filter_only") or _section_filter_group(section_config) in {"category", "region"}


def _section_filter_group(section_config: dict[str, Any]) -> str:
    if section_config.get("filter_group"):
        return str(section_config["filter_group"])
    if section_config.get("region"):
        return "region"
    category = str(section_config.get("category") or "")
    level1 = str(section_config.get("level1") or "")
    if category and category in _DOUBAN_CATEGORY_MAP.get(level1, set()) and _section_filter_label(section_config) == category:
        return "category"
    return "featured"


def _section_filter_label(section_config: dict[str, Any]) -> str:
    if section_config.get("filter_label"):
        return str(section_config["filter_label"])
    if section_config.get("region"):
        return str(section_config["region"])
    label = str(section_config.get("level2") or section_config.get("title") or "")
    level1 = str(section_config.get("level1") or "")
    suffixes = [level1]
    if level1 == "电影":
        suffixes.append("电影")
    elif level1 == "剧集":
        suffixes.extend(["剧集", "剧"])
    for suffix in suffixes:
        if suffix and label.endswith(suffix) and len(label) > len(suffix):
            label = label[: -len(suffix)]
            break
    return label or str(section_config.get("level2") or section_config.get("title") or "")


def _default_sections() -> list[dict[str, Any]]:
    return [
        _section("hot_movie", "热门电影", "电影", "热门电影", method="recent_hot", kind="movie", category="热门", type="全部"),
        _section("new_movie", "最新电影", "电影", "最新电影", method="tag", kind="movie", tag="最新"),
        _section("top_movie", "高分电影", "电影", "高分电影", method="recommend", kind="movie", sort="S"),
        _section("science_movie", "科幻电影", "电影", "科幻电影", method="recommend", kind="movie", category="科幻", sort="U"),
        _section("action_movie", "动作电影", "电影", "动作电影", method="recommend", kind="movie", category="动作", sort="U"),
        _section("comedy_movie", "喜剧电影", "电影", "喜剧电影", method="recommend", kind="movie", category="喜剧", sort="U"),
        _section("romance_movie", "爱情电影", "电影", "爱情电影", method="recommend", kind="movie", category="爱情", sort="U"),
        _section("suspense_movie", "悬疑电影", "电影", "悬疑电影", method="recommend", kind="movie", category="悬疑", sort="U"),
        _section("horror_movie", "恐怖电影", "电影", "恐怖电影", method="recommend", kind="movie", category="恐怖", sort="U"),
        _section("chinese_movie", "华语电影", "电影", "华语电影", method="recommend", kind="movie", region="华语", sort="U"),
        _section("western_movie", "欧美电影", "电影", "欧美电影", method="recommend", kind="movie", region="欧美", sort="U"),
        _section("japanese_movie", "日本电影", "电影", "日本电影", method="recommend", kind="movie", region="日本", sort="U"),
        _section("korean_movie", "韩国电影", "电影", "韩国电影", method="recommend", kind="movie", region="韩国", sort="U"),
        _section("hot_tv", "热门剧集", "剧集", "热门剧集", method="recent_hot", kind="tv", category="tv", type="tv"),
        _section("domestic_tv", "国产剧", "剧集", "国产剧", method="recommend", kind="tv", region="中国大陆", sort="U"),
        _section("western_tv", "欧美剧", "剧集", "欧美剧", method="recommend", kind="tv", region="欧美", sort="U"),
        _section("korean_tv", "韩剧", "剧集", "韩剧", method="recommend", kind="tv", region="韩国", sort="U"),
        _section("japanese_tv", "日剧", "剧集", "日剧", method="recommend", kind="tv", region="日本", sort="U"),
        _section("suspense_tv", "悬疑剧", "剧集", "悬疑剧", method="recommend", kind="tv", category="悬疑", sort="U"),
        _section("romance_tv", "爱情剧", "剧集", "爱情剧", method="recommend", kind="tv", category="爱情", sort="U"),
        _section("costume_tv", "古装剧", "剧集", "古装剧", method="recommend", kind="tv", category="古装", sort="U"),
        _section("crime_tv", "犯罪剧", "剧集", "犯罪剧", method="recommend", kind="tv", category="犯罪", sort="U"),
        _section("anime", "动漫新番", "动漫", "动漫新番", method="recommend", kind="tv", category="动画", sort="U"),
        _section("anime_movie", "动画电影", "动漫", "动画电影", method="recommend", kind="movie", category="动画", sort="U"),
        _section("variety", "综艺娱乐", "综艺", "综艺娱乐", method="recent_hot", kind="tv", category="show", type="show"),
    ]


def _generated_filter_sections() -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    specs = {
        "电影": {
            "kind": "movie",
            "categories": _DOUBAN_MOVIE_CATEGORIES,
            "regions": _DOUBAN_MOVIE_REGIONS,
        },
        "剧集": {
            "kind": "tv",
            "categories": _DOUBAN_TV_CATEGORIES,
            "regions": _DOUBAN_TV_REGIONS,
        },
        "动漫": {
            "kind": "tv",
            "categories": _DOUBAN_ANIME_CATEGORIES,
            "regions": _DOUBAN_TV_REGIONS,
        },
        "综艺": {
            "kind": "tv",
            "categories": _DOUBAN_VARIETY_CATEGORIES,
            "regions": _DOUBAN_TV_REGIONS,
        },
    }
    for level1, spec in specs.items():
        kind = str(spec["kind"])
        for category in spec["categories"]:
            level2 = f"{category}{level1}" if level1 == "电影" else f"{category}剧" if level1 == "剧集" else category
            sections.append(_section(
                f"generated_{level1}_{category}",
                level2,
                level1,
                level2,
                method="recommend",
                kind=kind,
                category=category,
                sort="U",
                filter_group="category",
                filter_label=category,
                filter_only=True,
                prefetch=False,
            ))
        for region in spec["regions"]:
            level2 = f"{region}{level1}"
            sections.append(_section(
                f"generated_{level1}_{region}",
                level2,
                level1,
                level2,
                method="recommend",
                kind=kind,
                region=region,
                sort="U",
                filter_group="region",
                filter_label=region,
                filter_only=True,
                prefetch=False,
            ))
    return sections


def _section(
    key: str,
    title: str,
    level1: str,
    level2: str,
    **kwargs: Any,
) -> dict[str, Any]:
    return {
        "key": key,
        "title": title,
        "level1": level1,
        "level2": level2,
        "limit": 12,
        "page_limit": 60,
        **kwargs,
    }

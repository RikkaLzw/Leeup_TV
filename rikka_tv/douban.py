from __future__ import annotations

import json
import re
import threading
import time
from typing import Any
from urllib.parse import quote, urlencode

import requests


DOUBAN_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://movie.douban.com/",
    "Accept": "application/json, text/plain, */*",
}
_POSTER_CACHE: dict[str, tuple[float, dict[str, str]]] = {}
_POSTER_LOCK = threading.Lock()


class DoubanClient:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        douban_config = config.get("douban", {})
        self.base = douban_config.get("base_url") or "https://m.douban.com"
        self.timeout = float(douban_config.get("timeout_seconds") or 10)

    def recent_hot(
        self,
        kind: str,
        category: str,
        item_type: str,
        limit: int = 12,
        start: int = 0,
    ) -> list[dict[str, Any]]:
        params = {
            "start": start,
            "limit": limit,
            "category": category,
            "type": item_type,
        }
        target = f"{self.base.rstrip('/')}/rexxar/api/v2/subject/recent_hot/{kind}?{urlencode(params)}"
        data = self._get_json(target)
        return [self._map_category_item(item, kind) for item in data.get("items", [])]

    def recommend(
        self,
        kind: str,
        limit: int = 12,
        start: int = 0,
        category: str = "",
        format_: str = "",
        region: str = "",
        year: str = "",
        platform: str = "",
        sort: str = "",
        label: str = "",
    ) -> list[dict[str, Any]]:
        selected_categories: dict[str, str] = {}
        if category:
            selected_categories["类型"] = category
        if format_:
            selected_categories["形式"] = format_
        if region:
            selected_categories["地区"] = region
        tags = [tag for tag in [format_, category, label, region, year, platform] if tag]
        params = {
            "refresh": "0",
            "start": start,
            "count": limit,
            "selected_categories": json.dumps(selected_categories, ensure_ascii=False),
            "uncollect": "false",
            "score_range": "0,10",
            "tags": ",".join(tags),
        }
        if sort:
            params["sort"] = sort
        target = f"{self.base.rstrip('/')}/rexxar/api/v2/{kind}/recommend?{urlencode(params)}"
        data = self._get_json(target)
        return [
            self._map_recommend_item(item, kind)
            for item in data.get("items", [])
            if item.get("type") in ("movie", "tv")
        ]

    def list_by_tag(self, kind: str, tag: str, limit: int = 12, start: int = 0) -> list[dict[str, Any]]:
        params = {
            "type": kind,
            "tag": tag,
            "sort": "recommend",
            "page_limit": limit,
            "page_start": start,
        }
        target = f"https://movie.douban.com/j/search_subjects?{urlencode(params)}"
        data = self._get_json(target)
        return [self._map_list_item(item, kind) for item in data.get("subjects", [])]

    def find_poster(self, title: str, year: str = "") -> dict[str, str]:
        title = (title or "").strip()
        if not title:
            return {}
        cache_key = f"{_normalize_title(title)}|{year or ''}"
        now = time.time()
        with _POSTER_LOCK:
            cached = _POSTER_CACHE.get(cache_key)
            if cached and cached[0] > now:
                return dict(cached[1])
        result = self._find_poster_from_rexxar_search(title, year)
        if not result:
            try:
                target = f"https://movie.douban.com/j/subject_suggest?q={quote(title)}"
                items = self._get_json(target)
            except Exception:
                items = []
            if not isinstance(items, list):
                items = []
            result = _pick_suggest_poster(items, title, year)
        with _POSTER_LOCK:
            _POSTER_CACHE[cache_key] = (now + 6 * 60 * 60, dict(result))
        return result

    def _find_poster_from_rexxar_search(self, title: str, year: str = "") -> dict[str, str]:
        try:
            target = f"{self.base.rstrip('/')}/rexxar/api/v2/search/subjects?{urlencode({'q': title, 'type': 'movie'})}"
            data = self._get_json(target)
        except Exception:
            return {}
        items = ((data.get("subjects") or {}).get("items") or []) if isinstance(data, dict) else []
        return _pick_rexxar_poster(items, title, year)

    def _get_json(self, url: str) -> dict[str, Any]:
        response = requests.get(url, headers=DOUBAN_HEADERS, timeout=(5, self.timeout))
        response.raise_for_status()
        return response.json()

    def _map_category_item(self, item: dict[str, Any], kind: str) -> dict[str, Any]:
        subtitle = item.get("card_subtitle") or ""
        poster = item.get("pic", {}).get("normal") or item.get("pic", {}).get("large") or ""
        return {
            "provider": "douban",
            "id": str(item.get("id") or ""),
            "title": item.get("title") or "",
            "poster": _proxy_poster(poster),
            "raw_poster": poster,
            "rate": _rating(item.get("rating", {}).get("value")),
            "year": _extract_year(subtitle),
            "kind": kind,
            "subtitle": subtitle,
        }

    def _map_recommend_item(self, item: dict[str, Any], kind: str) -> dict[str, Any]:
        poster = item.get("pic", {}).get("normal") or item.get("pic", {}).get("large") or ""
        return {
            "provider": "douban",
            "id": str(item.get("id") or ""),
            "title": item.get("title") or "",
            "poster": _proxy_poster(poster),
            "raw_poster": poster,
            "rate": _rating(item.get("rating", {}).get("value")),
            "year": str(item.get("year") or ""),
            "kind": item.get("type") or kind,
            "subtitle": str(item.get("year") or ""),
        }

    def _map_list_item(self, item: dict[str, Any], kind: str) -> dict[str, Any]:
        subtitle = item.get("card_subtitle") or ""
        poster = item.get("cover") or ""
        return {
            "provider": "douban",
            "id": str(item.get("id") or ""),
            "title": item.get("title") or "",
            "poster": _proxy_poster(poster),
            "raw_poster": poster,
            "rate": str(item.get("rate") or ""),
            "year": _extract_year(subtitle),
            "kind": kind,
            "subtitle": subtitle,
        }


def _rating(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return str(value)


def _extract_year(value: str) -> str:
    match = re.search(r"(19|20)\d{2}", value or "")
    return match.group(0) if match else ""


def _pick_suggest_poster(items: list[dict[str, Any]], title: str, year: str = "") -> dict[str, str]:
    normalized = _normalize_title(title)
    if not normalized:
        return {}
    candidates = []
    for item in items:
        item_title = _normalize_title(str(item.get("title") or ""))
        if not item_title:
            continue
        distance = 0 if item_title == normalized else 1 if normalized in item_title or item_title in normalized else 2
        year_penalty = 0 if not year or not item.get("year") or str(item.get("year")) == str(year) else 1
        poster = str(item.get("img") or "")
        if poster:
            candidates.append((distance, year_penalty, item, poster))
    if not candidates:
        return {}
    _distance, _year_penalty, item, poster = sorted(candidates, key=lambda value: (value[0], value[1]))[0]
    return {
        "poster": _proxy_poster(poster),
        "raw_poster": poster,
        "douban_id": str(item.get("id") or ""),
        "douban_title": str(item.get("title") or ""),
        "douban_year": str(item.get("year") or ""),
    }


def _pick_rexxar_poster(items: list[dict[str, Any]], title: str, year: str = "") -> dict[str, str]:
    normalized = _normalize_title(title)
    if not normalized:
        return {}
    candidates = []
    for item in items:
        target = item.get("target") or {}
        if item.get("layout") != "subject" or item.get("target_type") not in ("movie", "tv"):
            continue
        item_title = _normalize_title(str(target.get("title") or ""))
        if not item_title:
            continue
        distance = 0 if item_title == normalized else 1 if normalized in item_title or item_title in normalized else 2
        year_penalty = 0 if not year or not target.get("year") or str(target.get("year")) == str(year) else 1
        poster = str(target.get("cover_url") or "")
        if poster:
            candidates.append((distance, year_penalty, target, poster))
    if not candidates:
        return {}
    _distance, _year_penalty, target, poster = sorted(candidates, key=lambda value: (value[0], value[1]))[0]
    return {
        "poster": _proxy_poster(poster),
        "raw_poster": poster,
        "douban_id": str(target.get("id") or ""),
        "douban_title": str(target.get("title") or ""),
        "douban_year": str(target.get("year") or ""),
    }


def _normalize_title(value: str) -> str:
    return re.sub(r"[\s\-_·:：,，.。!！?？()\[\]（）【】]+", "", str(value).lower())


def _proxy_poster(url: str) -> str:
    if not url:
        return ""
    if "doubanio.com" not in url:
        return url
    return f"/image/douban?url={quote(url, safe='')}"

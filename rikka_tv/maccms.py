from __future__ import annotations

import html
import logging
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

import requests

from .db import get_detail_cache, get_search_cache, save_detail_cache, save_search_cache


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json,application/xml,text/xml,text/plain,*/*",
}
M3U8_RE = re.compile(r"https?://[^\"'\s]+?\.m3u8(?:\?[^\"'\s]*)?", re.I)
LOGGER = logging.getLogger(__name__)


class MacCMSClient:
    def __init__(self, config: dict[str, Any]):
        self.config = config

    def available_sources(self) -> list[dict[str, Any]]:
        sources = []
        seen_apis: set[str] = set()
        for key, site in (self.config.get("api_site") or {}).items():
            if site.get("disabled"):
                continue
            if not site.get("api"):
                continue
            canonical_api = _canonical_api_url(str(site.get("api") or ""))
            if canonical_api in seen_apis:
                continue
            seen_apis.add(canonical_api)
            sources.append({"key": key, **site})
        return sources

    def get_source(self, key: str) -> dict[str, Any] | None:
        for source in self.available_sources():
            if source["key"] == key:
                return source
        return None

    def search_all(
        self,
        query: str,
        selected_sources: list[str] | None = None,
        max_page: int | None = None,
    ) -> dict[str, Any]:
        sources = self.available_sources()
        if selected_sources:
            selected = set(selected_sources)
            sources = [source for source in sources if source["key"] in selected]
        if not sources:
            return {"results": [], "failed": []}
        search_max_page = int(max_page or self.config.get("search_max_page") or 1)
        cache_key = self._search_cache_key(query, sources, search_max_page)
        cached = self._get_search_cache(cache_key)
        if cached:
            return cached

        results: list[dict[str, Any]] = []
        failed: list[dict[str, str]] = []
        with ThreadPoolExecutor(max_workers=min(8, len(sources))) as pool:
            futures = {pool.submit(self.search_source, source, query, search_max_page): source for source in sources}
            for future in as_completed(futures):
                source = futures[future]
                try:
                    results.extend(future.result())
                except Exception as exc:
                    LOGGER.warning(
                        "MacCMS source search failed: source=%s name=%s query=%r error=%s",
                        source["key"],
                        source.get("name", source["key"]),
                        query,
                        exc,
                    )
                    failed.append({"key": source["key"], "name": source.get("name", source["key"]), "error": str(exc)})

        results.sort(key=lambda item: (_title_distance(query, item.get("title", "")), item.get("source_name", "")))
        payload = {"results": results, "failed": failed}
        self._save_search_cache(cache_key, payload)
        return payload

    def search_source(self, source: dict[str, Any], query: str, max_page_override: int | None = None) -> list[dict[str, Any]]:
        max_page = int(max_page_override or self.config.get("search_max_page") or 1)
        results = []
        for page in range(1, max_page + 1):
            url = self._search_url(source["api"], query, page)
            data = self._get_payload(url)
            items = data.get("list") or []
            if not isinstance(items, list):
                break
            results.extend(self._map_item(item, source) for item in items)
            page_count = _safe_int(data.get("pagecount"), 1)
            if page >= page_count:
                break
        return results

    def get_detail(self, source_key: str, video_id: str) -> dict[str, Any]:
        source = self.get_source(source_key)
        if not source:
            raise ValueError("无效的视频源")
        cached = self._get_detail_cache(source_key, str(video_id))
        if cached:
            return cached
        url = f"{source['api']}?ac=videolist&ids={quote(str(video_id))}"
        data = self._get_payload(url)
        items = data.get("list") or []
        if not items:
            if source.get("detail"):
                detail = self._get_html_detail(source, video_id)
                self._save_detail_cache(source_key, str(video_id), detail)
                return detail
            raise ValueError("详情为空")
        detail = self._map_item(items[0], source)
        self._save_detail_cache(source_key, str(video_id), detail)
        return detail

    def find_play_candidates(
        self,
        title: str,
        current_source: str | None,
        current_id: str | None,
        episode_index: int = 0,
        full: bool = False,
        selected_sources: list[str] | None = None,
        expected_year: str = "",
        expected_kind: str = "",
    ) -> list[dict[str, Any]]:
        prefer_config = self.config.get("speed_test") or {}
        if full:
            search_max_page = _config_int(prefer_config, "manual_search_max_page", self.config.get("search_max_page") or 3)
            shortlist_limit = 0
            fallback_limit = 0
        else:
            search_max_page = _config_int(prefer_config, "search_max_page", 1)
            shortlist_limit = max(_config_int(prefer_config, "shortlist_limit", 12), 1)
            fallback_limit = max(_config_int(prefer_config, "fallback_limit", 8), 1)
        candidates: dict[str, dict[str, Any]] = {}
        target_year = str(expected_year or "").strip()
        target_kind = str(expected_kind or "").strip().lower()
        if current_source and current_id:
            try:
                current = self.get_detail(current_source, current_id)
                if not title or _candidate_matches_target(current, title, target_year, target_kind, episode_index):
                    candidates[f"{current['source']}+{current['id']}"] = current
                title = title or current["title"]
                target_year = target_year or str(current.get("year") or "").strip()
                target_kind = target_kind or _infer_item_kind(current)
            except Exception:
                pass

        if title:
            payload = self._search_all_title_variants(title, selected_sources=selected_sources, max_page=search_max_page)
            normalized_query = _normalize_title(title)
            shortlist = []
            for item in payload["results"]:
                if _is_excluded_result(item):
                    continue
                normalized_title = _normalize_title(item.get("title", ""))
                if target_year:
                    if _candidate_matches_target(item, title, target_year, target_kind, episode_index):
                        shortlist.append(item)
                elif normalized_query == normalized_title or normalized_query in normalized_title or normalized_title in normalized_query:
                    shortlist.append(item)
            shortlist.sort(
                key=lambda item: _candidate_target_score(item, target_year, target_kind, episode_index),
                reverse=True,
            )
            if shortlist_limit:
                shortlist = shortlist[:shortlist_limit]
            if not shortlist:
                shortlist = payload["results"] if full else payload["results"][:fallback_limit]

            with ThreadPoolExecutor(max_workers=min(8, max(1, len(shortlist)))) as pool:
                futures = []
                for item in shortlist:
                    key = f"{item['source']}+{item['id']}"
                    if _is_excluded_result(item):
                        continue
                    if not _candidate_matches_target(item, title, target_year, target_kind, episode_index):
                        continue
                    if key in candidates and candidates[key].get("episodes"):
                        continue
                    if target_year and not _candidate_year_value(item):
                        futures.append(pool.submit(self.get_detail, item["source"], item["id"]))
                    elif item.get("episodes") and len(item["episodes"]) > episode_index:
                        candidates[key] = item
                    else:
                        futures.append(pool.submit(self.get_detail, item["source"], item["id"]))
                for future in as_completed(futures):
                    try:
                        detail = future.result()
                        if not _candidate_matches_target(detail, title, target_year, target_kind, episode_index):
                            continue
                        candidates[f"{detail['source']}+{detail['id']}"] = detail
                    except Exception:
                        continue

        deduped = _dedupe_play_candidates(
            candidates.values(),
            title,
            current_source,
            current_id,
            episode_index,
            target_year,
            target_kind,
        )
        return [
            item for item in deduped
            if _is_current_candidate(item, current_source, current_id)
            or _candidate_matches_target(item, title, target_year, target_kind, episode_index)
        ]

    def _search_url(self, api: str, query: str, page: int) -> str:
        encoded = quote(query)
        if page <= 1:
            return f"{api}?ac=videolist&wd={encoded}"
        return f"{api}?ac=videolist&wd={encoded}&pg={page}"

    def _search_all_title_variants(
        self,
        query: str,
        selected_sources: list[str] | None = None,
        max_page: int | None = None,
    ) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        failed: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        seen_failed: set[str] = set()
        for variant in _title_query_variants(query):
            payload = self.search_all(variant, selected_sources=selected_sources, max_page=max_page)
            for item in payload.get("results") or []:
                key = (str(item.get("source") or ""), str(item.get("id") or ""))
                if key in seen:
                    continue
                seen.add(key)
                results.append(item)
            for item in payload.get("failed") or []:
                key = str(item.get("key") or item.get("name") or "")
                if key in seen_failed:
                    continue
                seen_failed.add(key)
                failed.append(item)
        return {"results": results, "failed": failed}

    def _get_payload(self, url: str) -> dict[str, Any]:
        response = requests.get(url, headers=HEADERS, timeout=(5, 12))
        response.raise_for_status()
        try:
            data = response.json()
            if isinstance(data, dict):
                return data
            raise ValueError("JSON payload is not an object")
        except ValueError as json_exc:
            stripped = (response.text or "").lstrip("\ufeff\r\n\t ")
            if not stripped.startswith("<"):
                snippet = re.sub(r"\s+", " ", stripped[:120]).strip()
                raise ValueError(f"采集接口返回非 JSON/XML 内容: {snippet}") from json_exc
            return _parse_maccms_xml(response.content or stripped.encode("utf-8", errors="ignore"))

    def _search_cache_key(self, query: str, sources: list[dict[str, Any]], max_page: int) -> str:
        source_keys = ",".join(sorted(str(source.get("key") or "") for source in sources))
        raw_query = str(query or "").strip().lower()
        return f"v2|{raw_query}|{_normalize_title(query)}|{max_page}|{source_keys}"

    def _get_search_cache(self, cache_key: str) -> dict[str, Any] | None:
        try:
            ttl = int(self.config.get("search_cache_seconds") or 0)
            cached = get_search_cache(cache_key, ttl)
        except Exception:
            return None
        if not cached:
            return None
        results = cached.get("results")
        failed = cached.get("failed")
        if not isinstance(results, list) or not isinstance(failed, list):
            return None
        return cached

    def _save_search_cache(self, cache_key: str, payload: dict[str, Any]) -> None:
        try:
            if int(self.config.get("search_cache_seconds") or 0) > 0:
                save_search_cache(cache_key, payload)
        except Exception:
            return

    def _get_detail_cache(self, source_key: str, video_id: str) -> dict[str, Any] | None:
        try:
            ttl = int(self.config.get("detail_cache_seconds") or self.config.get("cache_time") or 0)
            return get_detail_cache(source_key, video_id, ttl)
        except Exception:
            return None

    def _save_detail_cache(self, source_key: str, video_id: str, payload: dict[str, Any]) -> None:
        try:
            if int(self.config.get("detail_cache_seconds") or self.config.get("cache_time") or 0) > 0:
                save_detail_cache(source_key, video_id, payload)
        except Exception:
            return

    def _map_item(self, item: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
        episodes, titles = parse_episodes(item.get("vod_play_url") or "", item.get("vod_content") or "")
        return {
            "id": str(item.get("vod_id") or ""),
            "title": _clean_title(str(item.get("vod_name") or "")),
            "poster": item.get("vod_pic") or "",
            "episodes": episodes,
            "episodes_titles": titles,
            "source": source["key"],
            "source_name": source.get("name") or source["key"],
            "class": item.get("vod_class") or "",
            "year": _extract_year(str(item.get("vod_year") or "")),
            "desc": clean_html(item.get("vod_content") or ""),
            "type_name": item.get("type_name") or "",
            "douban_id": item.get("vod_douban_id") or "",
        }

    def _get_html_detail(self, source: dict[str, Any], video_id: str) -> dict[str, Any]:
        url = f"{source['detail'].rstrip('/')}/index.php/vod/detail/id/{video_id}.html"
        response = requests.get(url, headers=HEADERS, timeout=(5, 12))
        response.raise_for_status()
        body = response.text
        episodes = list(dict.fromkeys(match.group(0) for match in M3U8_RE.finditer(body)))
        title_match = re.search(r"<h1[^>]*>(.*?)</h1>", body, re.I | re.S)
        poster_match = re.search(r"https?://[^\"'\s]+?\.(?:jpg|jpeg|png|webp)", body, re.I)
        return {
            "id": str(video_id),
            "title": clean_html(title_match.group(1) if title_match else ""),
            "poster": poster_match.group(0) if poster_match else "",
            "episodes": episodes,
            "episodes_titles": [str(i + 1) for i in range(len(episodes))],
            "source": source["key"],
            "source_name": source.get("name") or source["key"],
            "class": "",
            "year": _extract_year(body),
            "desc": "",
            "type_name": "",
            "douban_id": "",
        }


def _parse_maccms_xml(payload: bytes | str) -> dict[str, Any]:
    root = ET.fromstring(payload)
    list_node = root.find(".//list")
    videos = root.findall(".//video")
    if list_node is None:
        list_node = root
    return {
        "code": 1,
        "msg": "数据列表",
        "page": _safe_int(list_node.get("page") if list_node is not None else None, 1),
        "pagecount": _safe_int(list_node.get("pagecount") if list_node is not None else None, 1),
        "limit": _safe_int(list_node.get("pagesize") if list_node is not None else None, len(videos) or 20),
        "total": _safe_int(list_node.get("recordcount") if list_node is not None else None, len(videos)),
        "list": [_xml_video_to_item(video) for video in videos],
    }


def _xml_video_to_item(video: ET.Element) -> dict[str, Any]:
    type_name = _xml_child_text(video, "type", "type_name")
    desc = _xml_child_text(video, "des", "content", "desc")
    return {
        "vod_id": _xml_child_text(video, "id", "vod_id"),
        "vod_name": _xml_child_text(video, "name", "vod_name"),
        "vod_pic": _xml_child_text(video, "pic", "vod_pic"),
        "vod_class": type_name,
        "vod_year": _xml_child_text(video, "year", "vod_year"),
        "vod_content": desc,
        "type_name": type_name,
        "vod_douban_id": _xml_child_text(video, "douban_id", "vod_douban_id"),
        "vod_play_url": _xml_play_url(video),
    }


def _xml_play_url(video: ET.Element) -> str:
    playlists = []
    for node in video.findall(".//dd"):
        value = _xml_node_text(node)
        if value:
            playlists.append(value)
    return "$$$".join(playlists)


def _xml_child_text(node: ET.Element, *names: str) -> str:
    for name in names:
        child = node.find(name)
        if child is not None:
            value = _xml_node_text(child)
            if value:
                return value
    return ""


def _xml_node_text(node: ET.Element) -> str:
    return html.unescape("".join(node.itertext())).strip()


def parse_episodes(vod_play_url: str, fallback_content: str = "") -> tuple[list[str], list[str]]:
    best: tuple[int, list[str], list[str]] = (0, [], [])
    for playlist in (vod_play_url or "").split("$$$"):
        episodes = []
        titles = []
        for entry in playlist.split("#"):
            entry = entry.strip()
            if not entry:
                continue
            if "$" in entry:
                title, url = entry.split("$", 1)
            else:
                title, url = str(len(titles) + 1), entry
            url = url.strip()
            if _is_media_url(url):
                episodes.append(url)
                titles.append(title.strip() or str(len(titles) + 1))
        score = _playlist_score(episodes)
        if score > best[0] or (score == best[0] and len(episodes) > len(best[1])):
            best = (score, episodes, titles)

    best_episodes = best[1]
    best_titles = best[2]
    if not best_episodes and fallback_content:
        best_episodes = list(dict.fromkeys(match.group(0) for match in M3U8_RE.finditer(fallback_content)))
        best_titles = [str(i + 1) for i in range(len(best_episodes))]
    return best_episodes, best_titles


def _dedupe_play_candidates(
    candidates: Any,
    title: str,
    current_source: str | None,
    current_id: str | None,
    episode_index: int,
    expected_year: str = "",
    expected_kind: str = "",
) -> list[dict[str, Any]]:
    best_by_url: dict[str, tuple[tuple[int, int, int, int, int, int, int, int], int, dict[str, Any]]] = {}
    for index, item in enumerate(candidates):
        episodes = item.get("episodes") or []
        if not episodes:
            continue
        selected_episode = min(max(int(episode_index or 0), 0), len(episodes) - 1)
        selected_url = str(episodes[selected_episode] or "").strip()
        if not selected_url:
            continue
        canonical_url = _canonical_media_url(selected_url)
        score = _candidate_dedupe_score(
            item,
            title,
            current_source,
            current_id,
            episode_index,
            selected_url,
            expected_year,
            expected_kind,
        )
        existing = best_by_url.get(canonical_url)
        if not existing or score > existing[0]:
            best_by_url[canonical_url] = (score, index, item)

    best_by_source: dict[str, tuple[tuple[int, int, int, int, int, int, int, int], int, dict[str, Any]]] = {}
    for score, index, item in best_by_url.values():
        source = str(item.get("source") or "")
        existing = best_by_source.get(source)
        if not existing or score > existing[0]:
            best_by_source[source] = (score, index, item)

    return [item for _score, _index, item in sorted(best_by_source.values(), key=lambda entry: entry[1])]


def _candidate_dedupe_score(
    item: dict[str, Any],
    title: str,
    current_source: str | None,
    current_id: str | None,
    episode_index: int,
    selected_url: str,
    expected_year: str = "",
    expected_kind: str = "",
) -> tuple[int, int, int, int, int, int, int, int]:
    normalized_query = _normalize_title(title)
    normalized_title = _normalize_title(item.get("title") or "")
    is_current = str(item.get("source") or "") == str(current_source or "") and str(item.get("id") or "") == str(current_id or "")
    is_exact_title = bool(normalized_query and normalized_query == normalized_title)
    episodes = item.get("episodes") or []
    has_requested_episode = len(episodes) > max(int(episode_index or 0), 0)
    target_score = _candidate_target_score(item, expected_year, expected_kind, episode_index)
    return (
        int(is_current),
        int(is_exact_title),
        target_score,
        int(str(expected_year or "") and str(item.get("year") or "") == str(expected_year or "")),
        int(expected_kind and _infer_item_kind(item) == expected_kind),
        max(0, 2 - _title_distance(title, item.get("title") or "")),
        int(has_requested_episode),
        _media_url_score(selected_url),
    )


def _candidate_target_score(
    item: dict[str, Any],
    expected_year: str = "",
    expected_kind: str = "",
    episode_index: int = 0,
) -> int:
    score = 0
    expected_year = str(expected_year or "").strip()
    expected_kind = str(expected_kind or "").strip().lower()
    item_year = str(item.get("year") or "").strip()
    item_kind = _infer_item_kind(item)
    episodes = item.get("episodes") or []
    if expected_year:
        if item_year == expected_year:
            score += 80
        elif item_year:
            score -= 60
    if expected_kind:
        if item_kind == expected_kind:
            score += 60
        elif item_kind:
            score -= 80
    if expected_kind == "movie":
        if len(episodes) <= 1:
            score += 18
        elif len(episodes) >= 10:
            score -= 40
    elif expected_kind == "tv":
        if len(episodes) > 1:
            score += 14
        elif len(episodes) == 1:
            score -= 8
    if len(episodes) > max(int(episode_index or 0), 0):
        score += 8
    return score


def _candidate_is_compatible(
    item: dict[str, Any],
    expected_year: str = "",
    expected_kind: str = "",
    episode_index: int = 0,
) -> bool:
    expected_kind = str(expected_kind or "").strip().lower()
    if not expected_kind:
        return True
    episodes = item.get("episodes") or []
    item_kind = _infer_item_kind(item)
    if expected_kind == "movie":
        if item_kind == "tv":
            return False
        if len(episodes) >= 10:
            return False
        return len(episodes) > max(int(episode_index or 0), 0)
    if expected_kind == "tv":
        if item_kind == "movie":
            return False
        return len(episodes) > max(int(episode_index or 0), 0)
    return True


def _candidate_matches_target(
    item: dict[str, Any],
    title: str,
    expected_year: str = "",
    expected_kind: str = "",
    episode_index: int = 0,
) -> bool:
    if not _title_matches(title, item.get("title", ""), strict=bool(expected_year)):
        return False
    if not _candidate_year_matches(item, expected_year):
        return False
    return _candidate_is_compatible(item, expected_year, expected_kind, episode_index)


def _candidate_year_matches(item: dict[str, Any], expected_year: str = "") -> bool:
    expected_year = str(expected_year or "").strip()
    if not expected_year:
        return True
    item_year = _candidate_year_value(item)
    if not item_year:
        return True
    return item_year == expected_year


def _candidate_year_value(item: dict[str, Any]) -> str:
    return str(item.get("year") or "").strip() or _extract_year(str(item.get("title") or ""))


def _is_current_candidate(item: dict[str, Any], current_source: str | None, current_id: str | None) -> bool:
    return str(item.get("source") or "") == str(current_source or "") and str(item.get("id") or "") == str(current_id or "")


def _infer_item_kind(item: dict[str, Any]) -> str:
    if _is_movie_type(item):
        return "movie"
    if _is_tv_type(item):
        return "tv"
    episodes = item.get("episodes") or []
    if len(episodes) > 3:
        return "tv"
    if len(episodes) == 1:
        return "movie"
    return ""


def _is_movie_type(item: dict[str, Any]) -> bool:
    value = str(item.get("type_name") or "")
    return any(word in value for word in ("电影", "片", "纪录", "记录"))


def _is_tv_type(item: dict[str, Any]) -> bool:
    value = str(item.get("type_name") or "")
    if _is_movie_type(item):
        return False
    return any(word in value for word in ("剧", "连续", "综艺", "动漫", "番"))


def _canonical_media_url(url: str) -> str:
    parsed = urlparse((url or "").strip())
    return parsed._replace(scheme=parsed.scheme.lower(), netloc=parsed.netloc.lower(), fragment="").geturl()


def _is_media_url(url: str) -> bool:
    value = (url or "").strip().lower()
    if not value.startswith(("http://", "https://")):
        return False
    return bool(re.search(r"\.(m3u8|mp4|m4v|mov|webm)(?:[?#].*)?$", value))


def _media_url_score(url: str) -> int:
    value = (url or "").lower()
    if ".m3u8" in value:
        return 3
    if re.search(r"\.(mp4|m4v|mov|webm)(?:[?#].*)?$", value):
        return 2
    return 1


def _playlist_score(episodes: list[str]) -> int:
    if any(".m3u8" in url.lower() for url in episodes):
        return 3
    if episodes:
        return 2
    return 0


def clean_html(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_year(value: str) -> str:
    match = re.search(r"(19|20)\d{2}", value or "")
    return match.group(0) if match else ""


def _clean_title(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def _normalize_title(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[\s\-_·:：,，.。!！?？()\[\]（）【】]+", "", value)
    return value


def _title_query_variants(value: str) -> list[str]:
    original = str(value or "").strip()
    compact = re.sub(r"\s+", "", original)
    spaced_season = _space_title_season(compact)
    return list(dict.fromkeys(item for item in (original, compact, spaced_season) if item))


def _space_title_season(value: str) -> str:
    match = re.match(r"^(.+?)(第[0-9一二三四五六七八九十百千万零〇两]+季)$", value or "")
    if not match:
        return ""
    return f"{match.group(1)} {match.group(2)}"


def _canonical_api_url(api: str) -> str:
    value = api.strip()
    parsed = urlparse(value)
    query = parse_qs(parsed.query)
    if query.get("url"):
        value = unquote(query["url"][0]).strip()
        parsed = urlparse(value)
    return parsed._replace(scheme=parsed.scheme.lower(), netloc=parsed.netloc.lower(), query="").geturl().rstrip("/")


def _config_int(config: dict[str, Any], key: str, default: int) -> int:
    try:
        return int(config.get(key) or default)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _title_distance(query: str, title: str) -> int:
    q = _normalize_title(query)
    t = _normalize_title(title)
    if q == t:
        return 0
    if q in t or t in q:
        return 1
    return 2


def _title_matches(query: str, title: str, strict: bool = False) -> bool:
    q = _normalize_title(query)
    t = _normalize_title(title)
    if not q or not t:
        return False
    if strict:
        return _canonical_title_value(q) == _canonical_title_value(t)
    if q == t:
        return True
    if q in t and _has_excluded_suffix(t.removeprefix(q)):
        return False
    if t in q and _has_excluded_suffix(q.removeprefix(t)):
        return False
    return q == t or q in t or t in q


def _canonical_title_value(value: str) -> str:
    normalized = re.sub(r"(19|20)\d{2}$", "", _normalize_title(value))
    suffixes = (
        "粤语版",
        "国语版",
        "普通话版",
        "粤语",
        "国语",
        "普通话",
        "高清版",
        "高清",
        "全集",
        "hd",
    )
    changed = True
    while changed:
        changed = False
        for suffix in suffixes:
            if normalized.endswith(suffix) and len(normalized) > len(suffix):
                normalized = normalized[: -len(suffix)]
                changed = True
                break
    return normalized


def _has_excluded_suffix(value: str) -> bool:
    if not value:
        return False
    excluded = ("电影解说", "解说", "预告", "花絮", "片花", "彩蛋", "幕后", "资讯")
    return any(word in value for word in excluded)


def _is_excluded_result(item: dict[str, Any]) -> bool:
    value = _normalize_title(f"{item.get('title') or ''} {item.get('type_name') or ''} {item.get('class') or ''}")
    excluded = ("电影解说", "解说", "预告", "预告片", "花絮", "片花", "彩蛋", "幕后", "资讯")
    return any(word in value for word in excluded)

from __future__ import annotations

import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

import requests


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json,text/plain,*/*",
}
M3U8_RE = re.compile(r"https?://[^\"'\s]+?\.m3u8(?:\?[^\"'\s]*)?", re.I)


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

        results: list[dict[str, Any]] = []
        failed: list[dict[str, str]] = []
        with ThreadPoolExecutor(max_workers=min(8, len(sources))) as pool:
            futures = {pool.submit(self.search_source, source, query, max_page): source for source in sources}
            for future in as_completed(futures):
                source = futures[future]
                try:
                    results.extend(future.result())
                except Exception as exc:
                    failed.append({"key": source["key"], "name": source.get("name", source["key"]), "error": str(exc)})

        results.sort(key=lambda item: (_title_distance(query, item.get("title", "")), item.get("source_name", "")))
        return {"results": results, "failed": failed}

    def search_source(self, source: dict[str, Any], query: str, max_page_override: int | None = None) -> list[dict[str, Any]]:
        max_page = int(max_page_override or self.config.get("search_max_page") or 1)
        results = []
        for page in range(1, max_page + 1):
            url = self._search_url(source["api"], query, page)
            data = self._get_json(url)
            items = data.get("list") or []
            if not isinstance(items, list):
                break
            results.extend(self._map_item(item, source) for item in items)
            page_count = int(data.get("pagecount") or 1)
            if page >= page_count:
                break
        return results

    def get_detail(self, source_key: str, video_id: str) -> dict[str, Any]:
        source = self.get_source(source_key)
        if not source:
            raise ValueError("无效的视频源")
        url = f"{source['api']}?ac=videolist&ids={quote(str(video_id))}"
        data = self._get_json(url)
        items = data.get("list") or []
        if not items:
            if source.get("detail"):
                return self._get_html_detail(source, video_id)
            raise ValueError("详情为空")
        return self._map_item(items[0], source)

    def find_play_candidates(
        self,
        title: str,
        current_source: str | None,
        current_id: str | None,
        episode_index: int = 0,
        full: bool = False,
        selected_sources: list[str] | None = None,
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
        if current_source and current_id:
            try:
                current = self.get_detail(current_source, current_id)
                if not title or _title_matches(title, current.get("title", "")):
                    candidates[f"{current['source']}+{current['id']}"] = current
                title = title or current["title"]
            except Exception:
                pass

        if title:
            payload = self.search_all(title, selected_sources=selected_sources, max_page=search_max_page)
            normalized_query = _normalize_title(title)
            shortlist = []
            for item in payload["results"]:
                if _is_excluded_result(item):
                    continue
                normalized_title = _normalize_title(item.get("title", ""))
                if normalized_query == normalized_title or normalized_query in normalized_title or normalized_title in normalized_query:
                    shortlist.append(item)
                if shortlist_limit and len(shortlist) >= shortlist_limit:
                    break
            if not shortlist:
                shortlist = payload["results"] if full else payload["results"][:fallback_limit]

            with ThreadPoolExecutor(max_workers=min(8, max(1, len(shortlist)))) as pool:
                futures = []
                for item in shortlist:
                    key = f"{item['source']}+{item['id']}"
                    if _is_excluded_result(item):
                        continue
                    if not _title_matches(title, item.get("title", "")):
                        continue
                    if key in candidates and candidates[key].get("episodes"):
                        continue
                    if item.get("episodes") and len(item["episodes"]) > episode_index:
                        candidates[key] = item
                    else:
                        futures.append(pool.submit(self.get_detail, item["source"], item["id"]))
                for future in as_completed(futures):
                    try:
                        detail = future.result()
                        candidates[f"{detail['source']}+{detail['id']}"] = detail
                    except Exception:
                        continue

        return _dedupe_play_candidates(candidates.values(), title, current_source, current_id, episode_index)

    def _search_url(self, api: str, query: str, page: int) -> str:
        encoded = quote(query)
        if page <= 1:
            return f"{api}?ac=videolist&wd={encoded}"
        return f"{api}?ac=videolist&wd={encoded}&pg={page}"

    def _get_json(self, url: str) -> dict[str, Any]:
        response = requests.get(url, headers=HEADERS, timeout=(5, 12))
        response.raise_for_status()
        return response.json()

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


def parse_episodes(vod_play_url: str, fallback_content: str = "") -> tuple[list[str], list[str]]:
    best: tuple[int, list[str], list[str]] = (0, [], [])
    for playlist in (vod_play_url or "").split("$$$"):
        episodes = []
        titles = []
        for entry in playlist.split("#"):
            if "$" not in entry:
                continue
            title, url = entry.split("$", 1)
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
) -> list[dict[str, Any]]:
    best_by_url: dict[str, tuple[tuple[int, int, int, int, int, int], int, dict[str, Any]]] = {}
    for index, item in enumerate(candidates):
        episodes = item.get("episodes") or []
        if not episodes:
            continue
        selected_episode = min(max(int(episode_index or 0), 0), len(episodes) - 1)
        selected_url = str(episodes[selected_episode] or "").strip()
        if not selected_url:
            continue
        canonical_url = _canonical_media_url(selected_url)
        score = _candidate_dedupe_score(item, title, current_source, current_id, episode_index, selected_url)
        existing = best_by_url.get(canonical_url)
        if not existing or score > existing[0]:
            best_by_url[canonical_url] = (score, index, item)

    best_by_source: dict[str, tuple[tuple[int, int, int, int, int, int], int, dict[str, Any]]] = {}
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
) -> tuple[int, int, int, int, int, int]:
    normalized_query = _normalize_title(title)
    normalized_title = _normalize_title(item.get("title") or "")
    is_current = str(item.get("source") or "") == str(current_source or "") and str(item.get("id") or "") == str(current_id or "")
    is_exact_title = bool(normalized_query and normalized_query == normalized_title)
    episodes = item.get("episodes") or []
    has_requested_episode = len(episodes) > max(int(episode_index or 0), 0)
    return (
        int(is_current),
        int(is_exact_title),
        max(0, 2 - _title_distance(title, item.get("title") or "")),
        int(has_requested_episode),
        _media_url_score(selected_url),
        min(len(episodes), 999),
    )


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


def _title_distance(query: str, title: str) -> int:
    q = _normalize_title(query)
    t = _normalize_title(title)
    if q == t:
        return 0
    if q in t or t in q:
        return 1
    return 2


def _title_matches(query: str, title: str) -> bool:
    q = _normalize_title(query)
    t = _normalize_title(title)
    if not q or not t:
        return False
    if q == t:
        return True
    if q in t and _has_excluded_suffix(t.removeprefix(q)):
        return False
    if t in q and _has_excluded_suffix(q.removeprefix(t)):
        return False
    return q == t or q in t or t in q


def _has_excluded_suffix(value: str) -> bool:
    if not value:
        return False
    excluded = ("电影解说", "解说", "预告", "花絮", "片花", "彩蛋", "幕后", "资讯")
    return any(word in value for word in excluded)


def _is_excluded_result(item: dict[str, Any]) -> bool:
    value = _normalize_title(f"{item.get('title') or ''} {item.get('type_name') or ''} {item.get('class') or ''}")
    excluded = ("电影解说", "解说", "预告", "预告片", "花絮", "片花", "彩蛋", "幕后", "资讯")
    return any(word in value for word in excluded)

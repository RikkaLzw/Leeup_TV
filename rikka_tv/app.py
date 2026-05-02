from __future__ import annotations

import os
from typing import Any
from urllib.parse import unquote
from urllib.parse import urlparse
from urllib.parse import urlunparse

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from .config import ROOT_DIR, load_config
from .db import (
    get_source_metrics,
    get_source_metrics_map,
    init_db,
    save_source_test_metrics,
)
from .douban import DoubanClient
from .maccms import MacCMSClient
from .recommend import get_recommend_config, get_recommend_config_by_level, get_recommend_items, get_recommend_sections
from .speedtest import prefer_best_source


templates = Jinja2Templates(directory=str(ROOT_DIR / "templates"))


class PreferPayload(BaseModel):
    title: str = ""
    source: str = ""
    id: str = ""
    episode: int = 0


class SpeedResultPayload(BaseModel):
    candidates: list[dict[str, Any]] = []


class RecommendPagePayload(BaseModel):
    level1: str = ""
    level2: str = ""
    page: int = 1
    page_size: int = 12


def create_app() -> FastAPI:
    config = load_config()
    init_db(config)

    app = FastAPI(title=config.get("site", {}).get("name", "LeeupTV"))
    app.add_middleware(
        SessionMiddleware,
        secret_key=os.environ.get("RIKKA_SECRET_KEY", "rikka-tv-dev-secret"),
        same_site="lax",
    )
    app.mount("/static", StaticFiles(directory=str(ROOT_DIR / "static")), name="static")

    @app.get("/", response_class=HTMLResponse, name="index")
    async def index(request: Request):
        cfg = load_config()
        records: list[dict[str, Any]] = []
        history: list[str] = []
        recommend_sections = get_recommend_sections(cfg)
        return _template(
            request,
            "index.html",
            records=records[:8],
            history=history,
            recommend_sections=recommend_sections,
            poster_map=_build_poster_map(recommend_sections, records),
            configured_sources=MacCMSClient(cfg).available_sources(),
            show_mobile_nav=True,
        )

    @app.get("/browse/{level1}", response_class=HTMLResponse, name="browse")
    async def browse(request: Request, level1: str):
        cfg = load_config()
        allowed_levels = {item["level1"] for item in _mobile_nav_items() if item.get("level1")}
        if level1 not in allowed_levels:
            raise HTTPException(status_code=404)
        sections = get_recommend_sections(cfg, level1=level1, expanded=True)
        return _template(
            request,
            "browse.html",
            browse={"level1": level1, "sections": sections},
            poster_map=_build_poster_map(sections, []),
            show_mobile_nav=True,
            active_mobile_level=level1,
        )

    @app.get("/search", response_class=HTMLResponse, name="search")
    async def search(request: Request, q: str = "", source: list[str] | None = None):
        q = q.strip()
        selected_sources = source or []
        results: list[dict[str, Any]] = []
        failed: list[dict[str, str]] = []
        if q:
            cfg = load_config()
            payload = MacCMSClient(cfg).search_all(q, selected_sources=selected_sources or None)
            results = payload["results"]
            prefer_douban_posters(results, cfg)
            failed = payload["failed"]
        return _template(
            request,
            "search.html",
            q=q,
            results=results,
            failed=failed,
            selected_sources=selected_sources,
            show_mobile_nav=True,
            active_mobile_level="search",
        )

    @app.get("/resolve", response_class=HTMLResponse, name="resolve")
    async def resolve(request: Request, title: str, year: str = "", douban_id: str = "", kind: str = ""):
        title = title.strip()
        if not title:
            return RedirectResponse(str(request.url_for("index")), status_code=303)
        cfg = load_config()
        payload = MacCMSClient(cfg).search_all(title)
        results = payload["results"]
        prefer_douban_posters(results, cfg)
        exact = _pick_best_resolved_result(title, year, results)
        if exact:
            return RedirectResponse(
                str(request.url_for("detail", source=exact["source"], video_id=exact["id"])),
                status_code=303,
            )
        return _template(
            request,
            "search.html",
            q=title,
            results=results,
            failed=payload["failed"],
            selected_sources=[],
            resolved_from={"title": title, "year": year, "douban_id": douban_id, "kind": kind},
        )

    @app.get("/category/{category_key}", response_class=HTMLResponse, name="category")
    async def category(request: Request, category_key: str, refresh: str = "0"):
        cfg = load_config()
        section_config = get_recommend_config(cfg, category_key)
        if not section_config:
            raise HTTPException(status_code=404)
        items = get_recommend_items(
            DoubanClient(cfg),
            cfg,
            {**section_config, "limit": int(section_config.get("page_limit") or 36), "key": f"{section_config.get('key')}_page"},
            force_refresh=refresh == "1",
        )
        return _template(
            request,
            "category.html",
            category={
                "key": section_config.get("key"),
                "title": section_config.get("title"),
                "query": (section_config.get("queries") or [section_config.get("query") or ""])[0],
                "items": items,
            },
        )

    @app.get("/detail/{source}/{video_id}", response_class=HTMLResponse, name="detail")
    async def detail(request: Request, source: str, video_id: str):
        try:
            cfg = load_config()
            detail_data = prefer_douban_poster(MacCMSClient(cfg).get_detail(source, video_id), cfg)
        except Exception as exc:
            _flash(request, f"获取详情失败：{exc}", "error")
            return RedirectResponse(str(request.url_for("index")), status_code=303)
        return _template(request, "detail.html", item=detail_data)

    @app.get("/play/{source}/{video_id}", response_class=HTMLResponse, name="play")
    async def play(request: Request, source: str, video_id: str, episode: int = 0, prefer: str = "1"):
        cfg = load_config()
        video_client = MacCMSClient(cfg)
        try:
            detail_data = video_client.get_detail(source, video_id)
            prefer_douban_poster(detail_data, cfg)
        except Exception as exc:
            _flash(request, f"获取播放信息失败：{exc}", "error")
            return RedirectResponse(str(request.url_for("index")), status_code=303)

        episode = max(int(episode or 0), 0)
        if detail_data.get("episodes"):
            episode = min(episode, len(detail_data["episodes"]) - 1)
        prefer_cfg = cfg.get("speed_test", {})
        prefer_enabled = bool(
            prefer_cfg.get("enabled", True)
            and prefer_cfg.get("prefer_by_default", True)
            and prefer != "0"
        )
        recommended_detail = _pick_recommended_detail(video_client, cfg, detail_data, source, video_id, episode) if prefer_enabled else detail_data
        prefer_douban_poster(recommended_detail, cfg)
        source_metrics = get_source_metrics_map([source, recommended_detail.get("source")]) if prefer_enabled else {}
        player_cfg = cfg.get("player") or {}
        return _template(
            request,
            "play.html",
            item=recommended_detail,
            original_item=detail_data,
            episode=episode,
            record=None,
            prefer_enabled=prefer_enabled,
            source_metrics=source_metrics,
            speed_test_concurrency=int(prefer_cfg.get("browser_concurrency") or 4),
            browser_speed_cap_kbps=int(prefer_cfg.get("browser_speed_cap_kbps") or 12288),
            player_options={
                "skipIntroSeconds": int(player_cfg.get("skip_intro_seconds") or 0),
                "skipOutroSeconds": int(player_cfg.get("skip_outro_seconds") or 0),
            },
        )

    @app.get("/api/search", name="api_search")
    async def api_search(q: str = ""):
        q = q.strip()
        if not q:
            return {"results": [], "failed": []}
        cfg = load_config()
        payload = MacCMSClient(cfg).search_all(q)
        prefer_douban_posters(payload["results"], cfg)
        return payload

    @app.get("/api/recommendations", name="api_recommendations")
    async def api_recommendations(refresh: str = "0"):
        return {"sections": get_recommend_sections(load_config(), force_refresh=refresh == "1")}

    @app.post("/api/recommend-page", name="api_recommend_page")
    async def api_recommend_page(payload: RecommendPagePayload):
        cfg = load_config()
        page_size = min(max(int(payload.page_size or 12), 1), 36)
        page = max(int(payload.page or 1), 1)
        section_config = get_recommend_config_by_level(cfg, payload.level1.strip(), payload.level2.strip())
        if not section_config:
            return JSONResponse({"error": "category not found"}, status_code=404)
        fetch_config = {
            **section_config,
            "key": f"{section_config.get('key')}_page_{page_size}_{page}",
            "limit": page_size,
            "start": (page - 1) * page_size,
        }
        items = get_recommend_items(DoubanClient(cfg), cfg, fetch_config)
        page_items = items[:page_size]
        has_more = False
        if page_items:
            next_probe_config = {
                **section_config,
                "key": f"{section_config.get('key')}_probe_{page_size}_{page + 1}",
                "limit": page_size,
                "start": page * page_size,
            }
            has_more = bool(get_recommend_items(DoubanClient(cfg), cfg, next_probe_config))
        return {
            "ok": True,
            "items": [_recommend_api_item(item) for item in page_items],
            "has_more": has_more,
            "page": page,
            "page_size": page_size,
        }

    @app.get("/api/source-metrics", name="api_source_metrics")
    async def api_source_metrics(limit: int = 50):
        return {"sources": get_source_metrics(limit)}

    @app.get("/api/detail", name="api_detail")
    async def api_detail(source: str, id: str):
        try:
            cfg = load_config()
            return prefer_douban_poster(MacCMSClient(cfg).get_detail(source, id), cfg)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    @app.get("/image/douban", name="douban_image")
    async def douban_image(url: str):
        image_url = unquote(url)
        if not image_url.startswith(("https://", "http://")) or "doubanio.com" not in image_url:
            raise HTTPException(status_code=400)
        candidates = _douban_image_candidates(image_url)
        try:
            upstream = _fetch_first_image(candidates)
        except Exception:
            raise HTTPException(status_code=404)
        return Response(
            upstream.content,
            media_type=upstream.headers.get("Content-Type", "image/jpeg"),
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.post("/api/prefer", name="api_prefer")
    async def api_prefer(payload: PreferPayload):
        title = payload.title.strip()
        source = payload.source.strip()
        video_id = str(payload.id).strip()
        if not title and not (source and video_id):
            return JSONResponse({"error": "missing title or current video"}, status_code=400)
        cfg = load_config()
        video_client = MacCMSClient(cfg)
        candidates = video_client.find_play_candidates(title, source, video_id, payload.episode, full=True)
        prefer_douban_posters(candidates, cfg)
        return {"best": None, "candidates": _prepare_browser_test_candidates(candidates, payload.episode)}

    @app.post("/api/source-metrics", name="api_source_metrics_post")
    async def api_source_metrics_post(payload: SpeedResultPayload):
        candidates = payload.candidates or []
        save_source_test_metrics(candidates)
        result = prefer_best_source(candidates, 0, load_config().get("speed_test", {}), full=True, measured=True)
        return {"ok": True, "best": result.get("best"), "candidates": result.get("candidates", [])}

    return app


def _template(request: Request, name: str, **context: Any) -> HTMLResponse:
    cfg = load_config()
    client = MacCMSClient(cfg)
    base = {
        "request": request,
        "site": cfg.get("site", {}),
        "current_user": None,
        "messages": _pop_flashes(request),
        "sources": client.available_sources(),
        "speed_test_enabled": cfg.get("speed_test", {}).get("enabled", True),
        "is_home": request.url.path == "/",
        "asset_version": _asset_version(),
        "mobile_nav_items": _mobile_nav_items(),
        "show_mobile_nav": False,
        "active_mobile_level": "",
    }
    base.update(context)
    return templates.TemplateResponse(request, name, base)


def _asset_version() -> str:
    static_dir = ROOT_DIR / "static"
    paths = list((static_dir / "css").glob("*.css"))
    paths.extend((static_dir / "js").glob("*.js"))
    if (static_dir / "vendor").exists():
        paths.extend((static_dir / "vendor").rglob("*.js"))
    mtimes = [path.stat().st_mtime for path in paths if path.exists()]
    return str(int(max(mtimes, default=0)))


def _mobile_nav_items() -> list[dict[str, str]]:
    return [
        {"level1": "电影", "label": "电影", "icon": "movie", "kind": "browse"},
        {"level1": "剧集", "label": "剧集", "icon": "series", "kind": "browse"},
        {"level1": "动漫", "label": "动漫", "icon": "anime", "kind": "browse"},
        {"level1": "综艺", "label": "综艺", "icon": "variety", "kind": "browse"},
        {"level1": "", "label": "搜索", "icon": "search", "kind": "search"},
    ]


def _recommend_api_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": item.get("provider") or "",
        "id": str(item.get("id") or ""),
        "title": str(item.get("title") or ""),
        "poster": str(item.get("poster") or ""),
        "raw_poster": str(item.get("raw_poster") or ""),
        "rate": str(item.get("rate") or ""),
        "year": str(item.get("year") or ""),
        "kind": str(item.get("kind") or ""),
        "subtitle": str(item.get("subtitle") or ""),
    }


def _fetch_first_image(urls: list[str]) -> requests.Response:
    last_error: Exception | None = None
    for url in urls:
        try:
            upstream = requests.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Referer": "https://movie.douban.com/",
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                },
                timeout=(5, 15),
            )
            upstream.raise_for_status()
            return upstream
        except Exception as exc:
            last_error = exc
    raise last_error or RuntimeError("image fetch failed")


def _douban_image_candidates(url: str) -> list[str]:
    parsed = urlparse(url)
    hosts = []
    if parsed.netloc:
        hosts.append(parsed.netloc)
    hosts.extend([f"img{index}.doubanio.com" for index in range(1, 4)])
    seen: set[str] = set()
    candidates: list[str] = []
    for host in hosts:
        if "doubanio.com" not in host:
            continue
        candidate = urlunparse(parsed._replace(netloc=host))
        if candidate in seen:
            continue
        seen.add(candidate)
        candidates.append(candidate)
    return candidates


def prefer_douban_posters(items: list[dict[str, Any]], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    client = DoubanClient(cfg)
    for item in items:
        prefer_douban_poster(item, cfg, client)
    return items


def prefer_douban_poster(
    item: dict[str, Any],
    cfg: dict[str, Any],
    client: DoubanClient | None = None,
) -> dict[str, Any]:
    if item.get("provider") == "douban":
        return item
    title = str(item.get("search_title") or item.get("title") or "").strip()
    if not title:
        return item
    client = client or DoubanClient(cfg)
    poster = client.find_poster(title, str(item.get("year") or ""))
    if not poster.get("poster"):
        item.setdefault("poster_source", "video_source" if item.get("poster") else "")
        return item
    fallback = item.get("poster") or item.get("cover") or ""
    item["source_poster"] = fallback
    item["poster"] = poster["poster"]
    item["raw_poster"] = poster.get("raw_poster", "")
    item["poster_source"] = "douban"
    if item.get("cover") is not None:
        item["cover"] = poster["poster"]
    if poster.get("douban_id") and not item.get("douban_id"):
        item["douban_id"] = poster["douban_id"]
    return item


def _build_poster_map(
    recommend_sections: list[dict[str, Any]],
    records: list[dict[str, Any]],
) -> dict[str, dict[str, str]]:
    poster_map: dict[str, dict[str, str]] = {}
    for section in recommend_sections or []:
        for item in section.get("items") or []:
            _remember_poster(poster_map, item)
    for item in records or []:
        _remember_poster(poster_map, item)
    return poster_map


def _remember_poster(poster_map: dict[str, dict[str, str]], item: dict[str, Any]) -> None:
    key = _normalize_title(str(item.get("search_title") or item.get("title") or ""))
    poster = str(item.get("poster") or item.get("cover") or "")
    if not key or not poster:
        return
    existing = poster_map.get(key)
    if existing and existing.get("poster_source") == "douban":
        return
    poster_source = str(item.get("poster_source") or "")
    if item.get("provider") == "douban":
        poster_source = "douban"
    poster_map[key] = {
        "poster": poster,
        "raw_poster": str(item.get("raw_poster") or ""),
        "source_poster": str(item.get("source_poster") or ""),
        "poster_source": poster_source,
    }


def _pick_recommended_detail(
    video_client: MacCMSClient,
    cfg: dict[str, Any],
    detail_data: dict[str, Any],
    source: str,
    video_id: str,
    episode: int,
) -> dict[str, Any]:
    metrics = get_source_metrics_map()
    ranked_sources = [
        key for key, metric in sorted(metrics.items(), key=lambda item: item[1].get("source_score", 0), reverse=True)
        if metric.get("tests_total", 0) >= int((cfg.get("speed_test") or {}).get("recommend_min_tests") or 1)
        and metric.get("last_ok")
    ]
    if not ranked_sources:
        return detail_data

    current_score = (metrics.get(source) or {}).get("source_score", 0)
    for candidate_source in ranked_sources[: int((cfg.get("speed_test") or {}).get("recommend_source_probe_limit") or 5)]:
        if candidate_source == source:
            break
        candidate_score = (metrics.get(candidate_source) or {}).get("source_score", 0)
        if candidate_score <= current_score:
            break
        candidates = video_client.find_play_candidates(
            detail_data.get("title") or "",
            source,
            video_id,
            episode,
            full=False,
            selected_sources=[candidate_source],
        )
        best = _pick_matching_candidate(
            detail_data.get("title") or "",
            [candidate for candidate in candidates if candidate.get("source") == candidate_source],
        )
        if best:
            best["recommended_by_metrics"] = True
            best["original_source"] = source
            best["original_id"] = video_id
            best["source_metric"] = metrics.get(candidate_source)
            return best
    return detail_data


def merge_continue_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    ordered_records = sorted(records or [], key=lambda item: int(item.get("save_time") or 0), reverse=True)
    for record in ordered_records:
        key = _record_identity(record)
        if not key:
            key = f"{record.get('source') or ''}+{record.get('id') or ''}"
        current = grouped.get(key)
        if not current:
            grouped[key] = dict(record)
            continue
        current_cover = current.get("cover") or current.get("poster") or ""
        record_cover = record.get("cover") or record.get("poster") or ""
        if not current_cover and record_cover:
            current["cover"] = record_cover
            current["poster"] = record_cover
        if not current.get("year") and record.get("year"):
            current["year"] = record.get("year")
        if not current.get("total_episodes") and record.get("total_episodes"):
            current["total_episodes"] = record.get("total_episodes")
    return sorted(grouped.values(), key=lambda item: int(item.get("save_time") or 0), reverse=True)


def fill_continue_record_covers(records: list[dict[str, Any]], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    if not records:
        return records
    douban_client = DoubanClient(cfg)
    source_client = MacCMSClient(cfg)
    poster_cache: dict[str, str] = {}
    for record in records[:8]:
        title = str(record.get("search_title") or record.get("title") or "").strip()
        key = _normalize_title(title)
        if not key:
            continue
        if key not in poster_cache:
            poster_cache[key] = _find_record_cover(douban_client, source_client, title, str(record.get("year") or ""))
        if poster_cache[key]:
            record["cover"] = poster_cache[key]
            record["poster"] = poster_cache[key]
    return records


def _find_record_cover(douban_client: DoubanClient, source_client: MacCMSClient, title: str, year: str = "") -> str:
    douban_poster = douban_client.find_poster(title, year).get("poster", "")
    if douban_poster:
        return douban_poster
    try:
        payload = source_client.search_all(title, max_page=1)
    except Exception:
        return ""
    normalized = _normalize_title(title)
    results = payload.get("results") or []
    for result in results:
        if result.get("poster") and _normalize_title(result.get("title") or "") == normalized:
            return str(result["poster"])
    for result in results:
        if result.get("poster"):
            return str(result["poster"])
    return ""


def _prepare_browser_test_candidates(candidates: list[dict[str, Any]], episode: int) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    seen_urls: set[str] = set()
    seen_sources: set[str] = set()
    for candidate in candidates:
        episodes = candidate.get("episodes") or []
        if not episodes:
            continue
        source = str(candidate.get("source") or "")
        if source in seen_sources:
            continue
        selected_episode = min(max(int(episode or 0), 0), len(episodes) - 1)
        selected_url = str(episodes[selected_episode] or "").strip()
        if not selected_url:
            continue
        if selected_url in seen_urls:
            continue
        key = (str(candidate.get("source") or ""), str(candidate.get("id") or ""), selected_url)
        if key in seen:
            continue
        seen.add(key)
        seen_urls.add(selected_url)
        seen_sources.add(source)
        item = dict(candidate)
        item["selected_episode"] = selected_episode
        item["selected_url"] = selected_url
        item["test"] = {
            "ok": False,
            "error": "",
            "error_label": "等待测速",
            "quality": "未知",
            "latency_ms": 0,
            "speed_kbps": 0,
            "speed_label": "等待测速",
            "score": 0,
        }
        prepared.append(item)
    return prepared


def _pick_matching_candidate(title: str, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    normalized_title = _normalize_title(title)
    if not normalized_title:
        return candidates[0] if candidates else None
    for candidate in candidates:
        candidate_title = _normalize_title(candidate.get("title") or "")
        if candidate_title == normalized_title:
            return candidate
    for candidate in candidates:
        candidate_title = _normalize_title(candidate.get("title") or "")
        if normalized_title in candidate_title or candidate_title in normalized_title:
            return candidate
    return candidates[0] if candidates else None


def _pick_best_resolved_result(title: str, year: str, results: list[dict[str, Any]]) -> dict[str, Any] | None:
    normalized_title = _normalize_title(title)
    if not normalized_title:
        return None
    for item in results:
        item_title = _normalize_title(item.get("title", ""))
        if item_title == normalized_title and (not year or not item.get("year") or item.get("year") == year):
            return item
    for item in results:
        item_title = _normalize_title(item.get("title", ""))
        if normalized_title in item_title or item_title in normalized_title:
            return item
    return None


def _normalize_title(value: str) -> str:
    import re

    value = value.lower()
    return re.sub(r"[\s\-_·:：,，.。!！?？()\[\]（）【】]+", "", value)


def _record_identity(record: dict[str, Any]) -> str:
    return _normalize_title(str(record.get("search_title") or record.get("title") or ""))


def _flash(request: Request, message: str, category: str = "info") -> None:
    flashes = request.session.setdefault("_flashes", [])
    flashes.append({"message": message, "category": category})
    request.session["_flashes"] = flashes


def _pop_flashes(request: Request) -> list[dict[str, str]]:
    flashes = request.session.pop("_flashes", [])
    return list(flashes)

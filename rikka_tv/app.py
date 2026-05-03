from __future__ import annotations

import logging
import os
import uuid
from datetime import date
from os import PathLike
from typing import Any
from urllib.parse import parse_qs
from urllib.parse import quote
from urllib.parse import unquote
from urllib.parse import urlparse
from urllib.parse import urlencode
from urllib.parse import urlunparse

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from .config import ROOT_DIR, load_config
from .db import (
    delete_source_resolution_cache,
    get_play_resolution_cache,
    get_source_resolution_cache,
    get_source_metrics,
    get_source_metrics_map,
    init_db,
    record_visitor,
    save_play_resolution_cache,
    save_source_resolution_cache,
    save_source_test_metrics,
)
from .douban import DoubanClient
from .hls_proxy import HlsProxyForbidden
from .hls_proxy import HlsProxyUpstreamError
from .hls_proxy import decode_playlist
from .hls_proxy import fetch_hls_playlist
from .hls_proxy import filter_m3u8_playlist
from .hls_proxy import looks_like_m3u8
from .maccms import MacCMSClient
from .recommend import get_recommend_config, get_recommend_config_by_level, get_recommend_items, get_recommend_sections
from .speedtest import prefer_best_source


@pass_context
def _relative_url_for(context: dict[str, Any], name: str, **path_params: Any) -> str:
    request = context.get("request")
    if not isinstance(request, Request):
        return ""
    return request.url_for(name, **path_params).path


templates = Jinja2Templates(directory=str(ROOT_DIR / "templates"))
templates.env.globals["url_for"] = _relative_url_for
templates.env.globals["static_url"] = lambda path: _static_url(path)
templates.env.filters["direct_poster"] = lambda value: _direct_poster_url(value)
templates.env.filters["poster_src"] = lambda value: _poster_display_url(value)
LOGGER = logging.getLogger(__name__)
HOME_RECOMMEND_KEYS = {"new_movie", "top_movie", "hot_tv", "anime", "variety"}


def _direct_poster_url(value: Any) -> str:
    url = str(value or "").strip()
    if not url:
        return ""
    parsed = urlparse(url)
    path = parsed.path or url
    if path == "/image/douban":
        target = parse_qs(parsed.query).get("url", [""])[0]
        return unquote(target) if target else ""
    return url


def _poster_display_url(value: Any, cfg: dict[str, Any] | None = None) -> str:
    url = _direct_poster_url(value)
    if not url or "doubanio.com" not in url:
        return url
    douban_cfg = (cfg or load_config()).get("douban") or {}
    proxy_type = str(douban_cfg.get("image_proxy_type") or "cmliussss-cdn-ali")
    if proxy_type == "server" or proxy_type == "custom" or "cdn" in proxy_type:
        return f"/image/douban?url={quote(url, safe='')}"
    if proxy_type == "img3":
        return _replace_douban_image_host(url, "img3.doubanio.com")
    return url


def _replace_douban_image_host(url: str, host: str) -> str:
    parsed = urlparse(url)
    if not parsed.netloc or not parsed.netloc.endswith("doubanio.com"):
        return url
    return urlunparse(parsed._replace(netloc=host))


def _static_url(path: str) -> str:
    value = str(path or "").strip()
    if not value.startswith("/"):
        value = f"/{value}"
    return f"/static{value}"


BROWSE_DEFAULT_RECOMMEND_KEYS = {
    "电影": {"new_movie", "top_movie"},
    "剧集": {"hot_tv"},
    "动漫": {"anime"},
    "综艺": {"variety"},
}


class PreferPayload(BaseModel):
    title: str = ""
    source: str = ""
    id: str = ""
    episode: int = 0
    year: str = ""
    kind: str = ""


class SpeedResultPayload(BaseModel):
    candidates: list[dict[str, Any]] = []


class RecommendPagePayload(BaseModel):
    level1: str = ""
    level2: str = ""
    region: str = ""
    page: int = 1
    page_size: int = 12


class CacheControlStaticFiles(StaticFiles):
    def file_response(self, full_path: PathLike, stat_result: os.stat_result, scope: dict[str, Any], status_code: int = 200) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
        return response


def create_app() -> FastAPI:
    config = load_config()
    init_db(config)

    app = FastAPI(title=config.get("site", {}).get("name", "MewkoTV"))
    app.add_middleware(
        ProxyHeadersMiddleware,
        trusted_hosts=config.get("proxy", {}).get("trusted_hosts", "*"),
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=os.environ.get("RIKKA_SECRET_KEY", "rikka-tv-dev-secret"),
        same_site="lax",
    )
    app.mount("/static", CacheControlStaticFiles(directory=str(ROOT_DIR / "static")), name="static")

    @app.get("/", response_class=HTMLResponse, name="index")
    async def index(request: Request):
        cfg = load_config()
        records: list[dict[str, Any]] = []
        history: list[str] = []
        recommend_sections = get_recommend_sections(cfg, keys=HOME_RECOMMEND_KEYS, include_filter_sections=True)
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
        sections = get_recommend_sections(
            cfg,
            level1=level1,
            expanded=True,
            keys=BROWSE_DEFAULT_RECOMMEND_KEYS.get(level1),
            include_filter_sections=True,
        )
        return _template(
            request,
            "browse.html",
            browse={"level1": level1, "sections": sections},
            poster_map=_build_poster_map(sections, []),
            show_mobile_nav=True,
            active_mobile_level=level1,
        )

    @app.get("/search", response_class=HTMLResponse, name="search")
    async def search(request: Request, q: str = "", source: list[str] | None = None, scope: str = "fast"):
        q = q.strip()
        selected_sources = source or []
        search_scope = "custom" if selected_sources else "all" if scope == "all" else "fast"
        results: list[dict[str, Any]] = []
        failed: list[dict[str, str]] = []
        search_meta = _search_scope_meta(load_config(), search_scope, selected_sources)
        if q:
            cfg = load_config()
            client = MacCMSClient(cfg)
            selected_for_search = selected_sources or _preferred_search_source_keys(client, cfg, search_scope)
            search_meta = _search_scope_meta(cfg, search_scope, selected_for_search)
            payload = client.search_all(q, selected_sources=selected_for_search or None)
            results = merge_search_results(payload["results"])
            prefer_douban_posters(results, cfg)
            failed = payload["failed"]
        return _template(
            request,
            "search.html",
            q=q,
            results=results,
            failed=[],
            selected_sources=selected_sources,
            search_scope=search_scope,
            search_meta=search_meta,
            show_mobile_nav=True,
            active_mobile_level="search",
        )

    @app.get("/resolve", response_class=HTMLResponse, name="resolve")
    async def resolve(
        request: Request,
        title: str,
        year: str = "",
        douban_id: str = "",
        kind: str = "",
        poster: str = "",
        raw_poster: str = "",
        source_poster: str = "",
        rate: str = "",
        subtitle: str = "",
    ):
        title = title.strip()
        if not title:
            return RedirectResponse(request.url_for("index").path, status_code=303)
        
        intro = ""
        if douban_id:
            try:
                intro = DoubanClient(load_config()).get_subject(douban_id, kind or "movie").get("intro", "")
            except Exception:
                pass

        detail_data = _douban_detail_item(
            title=title,
            year=year,
            douban_id=douban_id,
            kind=kind,
            poster=poster,
            raw_poster=raw_poster,
            source_poster=source_poster,
            rate=rate,
            subtitle=subtitle,
            intro=intro,
        )
        return _template(request, "detail.html", item=detail_data, show_mobile_nav=True)

    @app.get("/resolve-play", response_class=HTMLResponse, name="resolve_play")
    async def resolve_play(
        request: Request,
        title: str,
        year: str = "",
        douban_id: str = "",
        kind: str = "",
        poster: str = "",
        raw_poster: str = "",
        source_poster: str = "",
        episode: int = 0,
    ):
        title = title.strip()
        if not title:
            return RedirectResponse(request.url_for("index").path, status_code=303)
        cfg = load_config()
        video_client = MacCMSClient(cfg)
        cache_key = _resolve_play_cache_key(title, year, kind, episode)
        cached = get_play_resolution_cache(cache_key, _resolve_play_cache_seconds(cfg))
        cached_detail = _cached_resolved_play_candidate(video_client, cached, title, year, kind, episode) if cached else None
        if cached_detail:
            return RedirectResponse(
                _play_url(
                    request,
                    str(cached["source"]),
                    str(cached["video_id"]),
                    episode=episode,
                    prefer="resolved",
                    poster=poster or str(cached.get("poster") or ""),
                    raw_poster=raw_poster or str(cached.get("raw_poster") or ""),
                    source_poster=source_poster or str(cached.get("source_poster") or ""),
                    resolve_title=title,
                    resolve_year=year,
                    resolve_kind=kind,
                ),
                status_code=303,
            )
        exact, results = _resolve_play_candidate(video_client, cfg, title, year, kind, episode, cache_key)
        if exact:
            save_play_resolution_cache(cache_key, exact)
            return RedirectResponse(
                _play_url(
                    request,
                    exact["source"],
                    exact["id"],
                    episode=episode,
                    prefer="resolved",
                    poster=poster or str(exact.get("poster") or ""),
                    raw_poster=raw_poster or str(exact.get("raw_poster") or ""),
                    source_poster=source_poster or str(exact.get("source_poster") or ""),
                    resolve_title=title,
                    resolve_year=year,
                    resolve_kind=kind,
                ),
                status_code=303,
            )
        results = merge_search_results(_resolve_fallback_results(title, year, results, kind))
        prefer_douban_posters(results, cfg)
        return _template(
            request,
            "search.html",
            q=title,
            results=results,
            failed=[],
            selected_sources=[],
            resolved_from={"title": title, "year": year, "douban_id": douban_id, "kind": kind},
            show_mobile_nav=True,
            active_mobile_level="search",
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
    async def detail(
        request: Request,
        source: str,
        video_id: str,
        poster: str = "",
        raw_poster: str = "",
        source_poster: str = "",
    ):
        try:
            cfg = load_config()
            detail_data = MacCMSClient(cfg).get_detail(source, video_id)
            _apply_poster_hint(detail_data, poster=poster, raw_poster=raw_poster, source_poster=source_poster)
            detail_data["resolve_kind"] = _infer_resolve_kind(detail_data)
        except Exception as exc:
            _flash(request, f"获取详情失败：{exc}", "error")
            return RedirectResponse(request.url_for("index").path, status_code=303)
        return _template(request, "detail.html", item=detail_data, show_mobile_nav=True)

    @app.get("/play/{source}/{video_id}", response_class=HTMLResponse, name="play")
    async def play(
        request: Request,
        source: str,
        video_id: str,
        episode: int = 0,
        prefer: str = "1",
        poster: str = "",
        raw_poster: str = "",
        source_poster: str = "",
        resolve_title: str = "",
        resolve_year: str = "",
        resolve_kind: str = "",
    ):
        cfg = load_config()
        video_client = MacCMSClient(cfg)
        try:
            detail_data = video_client.get_detail(source, video_id)
            _apply_poster_hint(detail_data, poster=poster, raw_poster=raw_poster, source_poster=source_poster)
            prefer_douban_poster(detail_data, cfg)
        except Exception as exc:
            _flash(request, f"获取播放信息失败：{exc}", "error")
            return RedirectResponse(request.url_for("index").path, status_code=303)

        original_item = _resolved_original_item(detail_data, resolve_title, resolve_year, resolve_kind)
        episode = max(int(episode or 0), 0)
        if detail_data.get("episodes"):
            episode = min(episode, len(detail_data["episodes"]) - 1)
        prefer_cfg = cfg.get("speed_test", {})
        prefer_enabled = bool(
            prefer_cfg.get("enabled", True)
            and prefer_cfg.get("prefer_by_default", True)
            and prefer != "0"
        )
        auto_prefer_enabled = prefer_enabled and prefer != "resolved"
        recommended_detail = _pick_recommended_detail(video_client, cfg, detail_data, source, video_id, episode) if auto_prefer_enabled else detail_data
        prefer_douban_poster(recommended_detail, cfg)
        source_metrics = get_source_metrics_map([source, recommended_detail.get("source")]) if prefer_enabled else {}
        player_cfg = cfg.get("player") or {}
        return _template(
            request,
            "play.html",
            item=recommended_detail,
            original_item=original_item,
            episode=episode,
            record=None,
            prefer_enabled=prefer_enabled,
            source_metrics=source_metrics,
            speed_test_concurrency=int(prefer_cfg.get("browser_concurrency") or 4),
            browser_speed_cap_kbps=int(prefer_cfg.get("browser_speed_cap_kbps") or 12288),
            player_options={
                "skipIntroEnabled": bool(player_cfg.get("skip_intro_enabled")),
                "skipIntroSeconds": int(player_cfg.get("skip_intro_seconds") or 0),
                "skipOutroEnabled": bool(player_cfg.get("skip_outro_enabled")),
                "skipOutroSeconds": int(player_cfg.get("skip_outro_seconds") or 0),
                "hlsProxyEnabled": bool(player_cfg.get("hls_proxy_enabled", True)),
                "hlsProxyBypassHosts": _string_list(player_cfg.get("hls_proxy_bypass_hosts")),
                "hlsAdFilterEnabled": bool(player_cfg.get("hls_ad_filter_enabled", True)),
            },
            show_mobile_nav=True,
        )

    @app.get("/api/search", name="api_search")
    async def api_search(q: str = "", scope: str = "fast"):
        q = q.strip()
        if not q:
            return {"results": [], "failed": []}
        cfg = load_config()
        client = MacCMSClient(cfg)
        search_scope = "all" if scope == "all" else "fast"
        selected_sources = _preferred_search_source_keys(client, cfg, search_scope)
        payload = client.search_all(q, selected_sources=selected_sources or None)
        prefer_douban_posters(payload["results"], cfg)
        return {**payload, "scope": search_scope, "search_meta": _search_scope_meta(cfg, search_scope, selected_sources)}

    @app.get("/api/recommendations", name="api_recommendations")
    async def api_recommendations(refresh: str = "0"):
        return {"sections": get_recommend_sections(load_config(), force_refresh=refresh == "1")}

    @app.post("/api/recommend-page", name="api_recommend_page")
    async def api_recommend_page(payload: RecommendPagePayload):
        cfg = load_config()
        page_size = min(max(int(payload.page_size or 12), 1), 36)
        page = max(int(payload.page or 1), 1)
        level1 = payload.level1.strip()
        level2 = payload.level2.strip()
        region = payload.region.strip()
        section_config = _recommend_page_config(cfg, level1, level2, region)
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
        has_more = len(page_items) >= page_size
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
        candidates = _douban_image_candidates(image_url, load_config())
        try:
            upstream = _fetch_first_image(candidates)
        except Exception:
            raise HTTPException(status_code=404)
        return Response(
            upstream.content,
            media_type=upstream.headers.get("Content-Type", "image/jpeg"),
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/hls-proxy", name="hls_proxy")
    async def hls_proxy(url: str):
        player_cfg = (load_config().get("player") or {})
        if not bool(player_cfg.get("hls_proxy_enabled", True)):
            raise HTTPException(status_code=404)
        try:
            upstream = fetch_hls_playlist(unquote(url), player_cfg)
            playlist_text = decode_playlist(upstream.content)
            if not looks_like_m3u8(playlist_text, upstream.content_type):
                raise HlsProxyUpstreamError("上游返回的不是 m3u8 播放列表")
            filtered = filter_m3u8_playlist(playlist_text, upstream.final_url, player_cfg)
        except HlsProxyForbidden as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except HlsProxyUpstreamError as exc:
            LOGGER.warning("HLS proxy failed: %s", exc)
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return Response(
            filtered.text,
            media_type="application/vnd.apple.mpegurl",
            headers={
                "Cache-Control": "no-store",
                "X-HLS-Filtered-Segments": str(filtered.removed_segments),
            },
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
        try:
            candidates = video_client.find_play_candidates(
                title,
                source,
                video_id,
                payload.episode,
                full=True,
                expected_year=payload.year,
                expected_kind=payload.kind,
            )
            prefer_douban_posters(candidates, cfg)
            prepared = _prepare_browser_test_candidates(candidates, payload.episode)
            return {
                "best": None,
                "candidates": prepared,
                "meta": {
                    "configured_sources": len(video_client.available_sources()),
                    "matched_count": len(candidates),
                    "prepared_count": len(prepared),
                },
            }
        except Exception as exc:
            LOGGER.exception(
                "Prefer candidates failed: title=%r source=%s id=%s episode=%s",
                title,
                source,
                video_id,
                payload.episode,
            )
            return JSONResponse({"error": f"测速候选源获取失败：{exc}"}, status_code=500)

    @app.post("/api/source-metrics", name="api_source_metrics_post")
    async def api_source_metrics_post(payload: SpeedResultPayload):
        try:
            candidates = payload.candidates or []
            save_source_test_metrics(candidates)
            result = prefer_best_source(candidates, 0, load_config().get("speed_test", {}), full=True, measured=True)
            return {"ok": True, "best": result.get("best"), "candidates": result.get("candidates", [])}
        except Exception as exc:
            LOGGER.exception("Saving source metrics failed")
            return JSONResponse({"error": f"保存测速结果失败：{exc}"}, status_code=500)

    return app


def _recommend_page_config(config: dict[str, Any], level1: str, level2: str, region: str) -> dict[str, Any] | None:
    if region and level2:
        base = get_recommend_config_by_level(config, level1, level2)
        if not base:
            return None
        return {
            **base,
            "key": f"{base.get('key')}_{region}",
            "title": f"{region}{base.get('filter_label') or base.get('title') or ''}",
            "method": "recommend",
            "region": region,
        }
    if region:
        return _region_recommend_config(config, level1, region)
    return get_recommend_config_by_level(config, level1, level2)


def _region_recommend_config(config: dict[str, Any], level1: str, region: str) -> dict[str, Any] | None:
    sections = config.get("recommendations", {}).get("sections") or []
    for section in sections:
        if section.get("disabled"):
            continue
        if section.get("level1") == level1 and section.get("region") == region:
            return section
    kind = "movie" if level1 == "电影" else "tv"
    return {
        "key": f"{level1}_{region}",
        "title": f"{region}{level1}",
        "level1": level1,
        "level2": f"{region}{level1}",
        "method": "recommend",
        "kind": kind,
        "region": region,
        "sort": "U",
    }


def _preferred_search_source_keys(client: MacCMSClient, cfg: dict[str, Any], scope: str = "fast") -> list[str]:
    if scope == "all":
        return []
    limit = _preferred_search_source_limit(cfg)
    if limit <= 0:
        return []
    return [
        str(source.get("key") or "")
        for source in _ranked_resolve_sources(client, cfg)[:limit]
        if source.get("key")
    ]


def _preferred_search_source_limit(cfg: dict[str, Any]) -> int:
    speed_cfg = cfg.get("speed_test") or {}
    try:
        return max(int(speed_cfg.get("search_preferred_source_limit") or 8), 1)
    except (TypeError, ValueError):
        return 8


def _search_scope_meta(cfg: dict[str, Any], scope: str, selected_sources: list[str] | None = None) -> dict[str, Any]:
    total_sources = len(MacCMSClient(cfg).available_sources())
    selected_count = len([source for source in (selected_sources or []) if source])
    if scope == "all" or not selected_count:
        searched_count = total_sources
        has_more = False
    else:
        searched_count = min(selected_count, total_sources)
        has_more = total_sources > searched_count
    return {
        "scope": scope,
        "searched_count": searched_count,
        "total_count": total_sources,
        "has_more": has_more,
    }


def _template(request: Request, name: str, **context: Any) -> HTMLResponse:
    cfg = load_config()
    client = MacCMSClient(cfg)
    visitor_stats = _record_request_visitor(request)
    base = {
        "request": request,
        "url_for": lambda name, **path_params: request.url_for(name, **path_params).path,
        "site": cfg.get("site", {}),
        "current_user": None,
        "messages": _pop_flashes(request),
        "sources": client.available_sources(),
        "speed_test_enabled": cfg.get("speed_test", {}).get("enabled", True),
        "is_home": request.url.path == "/",
        "asset_version": _asset_version(),
        "image_config": _image_runtime_config(cfg),
        "app_status": _app_status_summary(cfg, client, visitor_stats),
        "mobile_nav_items": _mobile_nav_items(),
        "show_mobile_nav": False,
        "active_mobile_level": "",
    }
    base.update(context)
    return templates.TemplateResponse(request, name, base)


def _record_request_visitor(request: Request) -> dict[str, int]:
    visitor_id = str(request.session.get("visitor_id") or "").strip()
    if not visitor_id:
        visitor_id = uuid.uuid4().hex
        request.session["visitor_id"] = visitor_id
    return record_visitor(visitor_id, date.today().isoformat())


def _asset_version() -> str:
    static_dir = ROOT_DIR / "static"
    paths = list((static_dir / "css").glob("*.css"))
    paths.extend((static_dir / "js").glob("*.js"))
    paths.extend((static_dir / "icons").glob("*"))
    paths.append(static_dir / "site.webmanifest")
    if (static_dir / "vendor").exists():
        paths.extend((static_dir / "vendor").rglob("*.js"))
    mtimes = [path.stat().st_mtime for path in paths if path.exists()]
    return str(int(max(mtimes, default=0)))


def _image_runtime_config(cfg: dict[str, Any]) -> dict[str, str]:
    douban_cfg = cfg.get("douban") or {}
    return {
        "doubanImageProxyMode": _image_proxy_label(douban_cfg),
    }


def _app_status_summary(
    cfg: dict[str, Any],
    client: MacCMSClient,
    visitor_stats: dict[str, int],
) -> dict[str, str | int | bool]:
    speed_cfg = cfg.get("speed_test") or {}
    player_cfg = cfg.get("player") or {}
    recommend_cfg = cfg.get("recommendations") or {}
    douban_cfg = cfg.get("douban") or {}
    source_count = len(client.available_sources())
    prefer_enabled = bool(speed_cfg.get("enabled", True) and speed_cfg.get("prefer_by_default", True))
    speed_enabled = bool(speed_cfg.get("enabled", True))
    skip_intro_enabled = bool(player_cfg.get("skip_intro_enabled"))
    skip_outro_enabled = bool(player_cfg.get("skip_outro_enabled"))
    preferred_limit = _preferred_search_source_limit(cfg)
    return {
        "source_count": source_count,
        "today_users": int(visitor_stats.get("today_users") or 0),
        "total_users": int(visitor_stats.get("total_users") or 0),
        "image_proxy_label": _image_proxy_label(douban_cfg),
        "image_proxy_detail": _image_proxy_detail(douban_cfg),
        "cdn_redacted": _image_proxy_uses_hidden_cdn(douban_cfg),
        "speed_test_label": "已启用" if speed_enabled else "未启用",
        "prefer_label": "默认优选" if prefer_enabled else "手动选择",
        "browser_concurrency": int(speed_cfg.get("browser_concurrency") or 4),
        "search_label": f"优先 {preferred_limit} 个快速源" if speed_enabled else "全源搜索",
        "recommend_cache_label": _duration_label(int(recommend_cfg.get("cache_seconds") or 1800)),
        "skip_label": _skip_label(player_cfg, skip_intro_enabled, skip_outro_enabled),
    }


def _image_proxy_label(douban_cfg: dict[str, Any]) -> str:
    proxy_type = str(douban_cfg.get("image_proxy_type") or "").strip().lower()
    if not proxy_type:
        return "直连图片"
    if proxy_type == "server":
        return "站内代理"
    if proxy_type == "img3":
        return "备用域名"
    if proxy_type == "custom" or "cdn" in proxy_type:
        return "CDN 加速"
    return "图片加速"


def _image_proxy_detail(douban_cfg: dict[str, Any]) -> str:
    proxy_type = str(douban_cfg.get("image_proxy_type") or "").strip().lower()
    if proxy_type == "server":
        return "经本站转发，隐藏上游地址"
    if proxy_type == "custom" or "cdn" in proxy_type:
        return "具体服务商与线路已隐藏"
    if proxy_type == "img3":
        return "使用公共备用图片域名"
    return "未启用外部加速"


def _image_proxy_uses_hidden_cdn(douban_cfg: dict[str, Any]) -> bool:
    proxy_type = str(douban_cfg.get("image_proxy_type") or "").strip().lower()
    return proxy_type == "custom" or "cdn" in proxy_type


def _duration_label(seconds: int) -> str:
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds // 3600} 小时"
    if seconds >= 60:
        return f"{seconds // 60} 分钟"
    return f"{seconds} 秒"


def _skip_label(player_cfg: dict[str, Any], intro_enabled: bool, outro_enabled: bool) -> str:
    parts: list[str] = []
    if intro_enabled:
        parts.append(f"片头 {int(player_cfg.get('skip_intro_seconds') or 0)} 秒")
    if outro_enabled:
        parts.append(f"片尾 {int(player_cfg.get('skip_outro_seconds') or 0)} 秒")
    return " / ".join(parts) if parts else "未启用"


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
        "poster": _direct_poster_url(item.get("poster") or ""),
        "raw_poster": _direct_poster_url(item.get("raw_poster") or ""),
        "display_poster": _poster_display_url(item.get("raw_poster") or item.get("poster") or ""),
        "rate": str(item.get("rate") or ""),
        "year": str(item.get("year") or ""),
        "kind": str(item.get("kind") or ""),
        "subtitle": str(item.get("subtitle") or ""),
    }


def _detail_url(
    request: Request,
    source: str,
    video_id: str,
    poster: str = "",
    raw_poster: str = "",
    source_poster: str = "",
) -> str:
    url = request.url_for("detail", source=source, video_id=video_id).path
    params = {
        key: value
        for key, value in {
            "poster": poster,
            "raw_poster": raw_poster,
            "source_poster": source_poster,
        }.items()
        if value
    }
    if not params:
        return url
    return f"{url}?{urlencode(params)}"


def _play_url(
    request: Request,
    source: str,
    video_id: str,
    episode: int = 0,
    prefer: str = "1",
    poster: str = "",
    raw_poster: str = "",
    source_poster: str = "",
    resolve_title: str = "",
    resolve_year: str = "",
    resolve_kind: str = "",
) -> str:
    url = request.url_for("play", source=source, video_id=video_id).path
    params = {
        key: value
        for key, value in {
            "episode": str(max(int(episode or 0), 0)),
            "prefer": prefer,
            "poster": poster,
            "raw_poster": raw_poster,
            "source_poster": source_poster,
            "resolve_title": resolve_title,
            "resolve_year": resolve_year,
            "resolve_kind": resolve_kind,
        }.items()
        if value
    }
    return f"{url}?{urlencode(params)}" if params else url


def _resolved_original_item(
    detail_data: dict[str, Any],
    resolve_title: str = "",
    resolve_year: str = "",
    resolve_kind: str = "",
) -> dict[str, Any]:
    original = dict(detail_data)
    title = str(resolve_title or "").strip()
    year = str(resolve_year or "").strip()
    kind = str(resolve_kind or "").strip().lower()
    if title:
        original["title"] = title
        original["search_title"] = title
    if year:
        original["year"] = year
    if kind:
        original["kind"] = kind
        original["resolve_kind"] = kind
    return original


def _douban_detail_item(
    title: str,
    year: str = "",
    douban_id: str = "",
    kind: str = "",
    poster: str = "",
    raw_poster: str = "",
    source_poster: str = "",
    rate: str = "",
    subtitle: str = "",
    intro: str = "",
) -> dict[str, Any]:
    kind = str(kind or "").strip()
    type_label = "电影" if kind == "movie" else "剧集" if kind in {"tv", "show"} else "豆瓣推荐"
    desc_parts = []
    if subtitle:
        desc_parts.append(subtitle)
    if rate:
        desc_parts.append(f"豆瓣评分 {rate}")
    
    final_desc = intro if intro else " · ".join(part for part in desc_parts if part)
    
    return {
        "provider": "douban",
        "id": str(douban_id or ""),
        "douban_id": str(douban_id or ""),
        "title": title,
        "poster": _direct_poster_url(poster or raw_poster or source_poster),
        "raw_poster": _direct_poster_url(raw_poster),
        "source_poster": _direct_poster_url(source_poster),
        "poster_source": "douban" if (poster or raw_poster) else "",
        "source": "",
        "source_name": "豆瓣",
        "episodes": [],
        "episodes_titles": [],
        "year": str(year or "").strip(),
        "class": type_label,
        "type_name": type_label,
        "kind": kind,
        "resolve_kind": kind,
        "rate": str(rate or ""),
        "subtitle": str(subtitle or ""),
        "desc": final_desc,
    }


def _resolve_play_candidate(
    video_client: MacCMSClient,
    cfg: dict[str, Any],
    title: str,
    year: str = "",
    kind: str = "",
    episode: int = 0,
    cache_key: str = "",
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    results: list[dict[str, Any]] = []
    max_page = _resolve_play_search_max_page(cfg)
    negative_sources = get_source_resolution_cache(cache_key, _resolve_negative_cache_seconds(cfg))
    probe_limit = _resolve_source_probe_limit(cfg)
    tried_sources = 0
    for source in _ranked_resolve_sources(video_client, cfg):
        source_key = str(source.get("key") or "")
        if source_key in negative_sources:
            continue
        if probe_limit and tried_sources >= probe_limit:
            break
        tried_sources += 1
        try:
            source_results = video_client.search_source(source, title, max_page)
        except Exception as exc:
            LOGGER.warning(
                "MacCMS resolve-play source search failed: source=%s name=%s query=%r error=%s",
                source["key"],
                source.get("name", source["key"]),
                title,
                exc,
            )
            _save_resolution_status(cache_key, source_key, "error")
            continue

        if source_results:
            results.extend(source_results)
        ranked_results = _rank_resolved_results(title, year, source_results, kind)
        if not ranked_results:
            _save_resolution_status(cache_key, source_key, "no_match")
            continue
        source_had_candidate = False
        for candidate in ranked_results:
            source_had_candidate = True
            playable = _playable_resolved_candidate(video_client, candidate, title, year, kind, episode)
            if playable:
                delete_source_resolution_cache(cache_key, source_key)
                return playable, results
        if source_had_candidate:
            _save_resolution_status(cache_key, source_key, "no_playable")
    return None, results


def _ranked_resolve_sources(video_client: MacCMSClient, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    sources = video_client.available_sources()
    metrics = get_source_metrics_map()

    def sort_key(entry: tuple[int, dict[str, Any]]) -> tuple[int, float, float, int]:
        index, source = entry
        metric = metrics.get(source["key"]) or {}
        tests_total = int(metric.get("tests_total") or 0)
        last_ok = bool(metric.get("last_ok"))
        if tests_total and last_ok:
            bucket = 0
        elif tests_total:
            bucket = 2
        else:
            bucket = 1
        return (
            bucket,
            -_resolve_source_rank_score(metric, cfg),
            -float(metric.get("avg_speed_kbps") or 0),
            index,
        )

    return [source for _index, source in sorted(enumerate(sources), key=sort_key)]


def _resolve_source_rank_score(metric: dict[str, Any], cfg: dict[str, Any]) -> float:
    if not metric:
        return 0
    speed_cfg = cfg.get("speed_test") or {}
    speed_weight = _config_float(speed_cfg, "resolve_rank_speed_weight", 0.65)
    success_weight = _config_float(speed_cfg, "resolve_rank_success_weight", 0.2)
    quality_weight = _config_float(speed_cfg, "resolve_rank_quality_weight", 0.1)
    latency_weight = _config_float(speed_cfg, "resolve_rank_latency_weight", 0.05)
    total = max(speed_weight + success_weight + quality_weight + latency_weight, 0.001)
    speed_weight /= total
    success_weight /= total
    quality_weight /= total
    latency_weight /= total

    cap = max(_config_float(speed_cfg, "browser_speed_cap_kbps", 12288), 1024)
    speed_score = min(float(metric.get("avg_speed_kbps") or 0) / cap * 100, 100)
    success_score = min(max(float(metric.get("success_rate") or 0), 0), 1) * 100
    quality_score = min(max(float(metric.get("avg_score") or 0), 0), 100)
    latency_ms = float(metric.get("avg_latency_ms") or 0)
    latency_score = 50 if latency_ms <= 0 else max(0, 100 - min(latency_ms, 5000) / 5000 * 100)
    return round(
        speed_score * speed_weight
        + success_score * success_weight
        + quality_score * quality_weight
        + latency_score * latency_weight,
        3,
    )


def _resolve_play_search_max_page(cfg: dict[str, Any]) -> int:
    speed_cfg = cfg.get("speed_test") or {}
    value = speed_cfg.get("resolve_search_max_page", speed_cfg.get("search_max_page", 1))
    try:
        return max(int(value or 1), 1)
    except (TypeError, ValueError):
        return 1


def _resolve_play_cache_seconds(cfg: dict[str, Any]) -> int:
    speed_cfg = cfg.get("speed_test") or {}
    try:
        return max(int(speed_cfg.get("resolve_cache_seconds") or 0), 0)
    except (TypeError, ValueError):
        return 0


def _resolve_negative_cache_seconds(cfg: dict[str, Any]) -> int:
    speed_cfg = cfg.get("speed_test") or {}
    try:
        return max(int(speed_cfg.get("resolve_negative_cache_seconds") or 0), 0)
    except (TypeError, ValueError):
        return 0


def _resolve_source_probe_limit(cfg: dict[str, Any]) -> int:
    speed_cfg = cfg.get("speed_test") or {}
    try:
        return max(int(speed_cfg.get("resolve_source_probe_limit") or 0), 0)
    except (TypeError, ValueError):
        return 0


def _config_float(config: dict[str, Any], key: str, default: float) -> float:
    try:
        return float(config.get(key) if config.get(key) is not None else default)
    except (TypeError, ValueError):
        return default


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item or "").strip() for item in value if str(item or "").strip()]


def _save_resolution_status(cache_key: str, source: str, status: str) -> None:
    if not cache_key or not source:
        return
    try:
        save_source_resolution_cache(cache_key, source, status)
    except Exception:
        return


def _resolve_play_cache_key(title: str, year: str = "", kind: str = "", episode: int = 0) -> str:
    normalized = _normalize_title(title)
    if not normalized:
        return ""
    selected_episode = max(int(episode or 0), 0)
    return "|".join([
        "v3",
        normalized,
        str(year or "").strip(),
        str(kind or "").strip().lower(),
        str(selected_episode),
    ])


def _playable_resolved_candidate(
    video_client: MacCMSClient,
    candidate: dict[str, Any],
    title: str,
    year: str = "",
    kind: str = "",
    episode: int = 0,
) -> dict[str, Any] | None:
    if _has_playable_episode(candidate, episode) and not _candidate_needs_detail_validation(candidate, year):
        return candidate
    try:
        detail = video_client.get_detail(str(candidate.get("source") or ""), str(candidate.get("id") or ""))
    except Exception as exc:
        LOGGER.warning(
            "MacCMS resolve-play detail failed: source=%s id=%s title=%r error=%s",
            candidate.get("source"),
            candidate.get("id"),
            title,
            exc,
        )
        return None
    if not _pick_best_resolved_result(title, year, [detail], kind):
        return None
    return detail if _has_playable_episode(detail, episode) else None


def _candidate_needs_detail_validation(candidate: dict[str, Any], year: str = "") -> bool:
    return bool(str(year or "").strip() and not _resolve_item_year(candidate))


def _cached_resolved_play_candidate(
    video_client: MacCMSClient,
    cached: dict[str, Any] | None,
    title: str,
    year: str = "",
    kind: str = "",
    episode: int = 0,
) -> dict[str, Any] | None:
    if not cached:
        return None
    source = str(cached.get("source") or "")
    video_id = str(cached.get("video_id") or "")
    if not source or not video_id or not video_client.get_source(source):
        return None
    try:
        detail = video_client.get_detail(source, video_id)
    except Exception as exc:
        LOGGER.warning(
            "MacCMS cached resolve-play detail failed: source=%s id=%s title=%r error=%s",
            source,
            video_id,
            title,
            exc,
        )
        return None
    if not _pick_best_resolved_result(title, year, [detail], kind):
        return None
    return detail if _has_playable_episode(detail, episode) else None


def _has_playable_episode(item: dict[str, Any], episode: int = 0) -> bool:
    episodes = item.get("episodes") or []
    if not isinstance(episodes, list):
        return False
    selected = max(int(episode or 0), 0)
    return len(episodes) > selected and bool(str(episodes[selected] or "").strip())


def _apply_poster_hint(
    item: dict[str, Any],
    poster: str = "",
    raw_poster: str = "",
    source_poster: str = "",
) -> dict[str, Any]:
    poster = poster.strip()
    raw_poster = raw_poster.strip()
    source_poster = source_poster.strip()
    poster = _direct_poster_url(poster)
    raw_poster = _direct_poster_url(raw_poster)
    source_poster = _direct_poster_url(source_poster)
    if not poster and not raw_poster and not source_poster:
        return item
    fallback = item.get("poster") or item.get("cover") or ""
    hint = poster or source_poster or raw_poster
    if hint and (not item.get("poster") or raw_poster):
        item["poster"] = hint
        item["poster_source"] = "douban" if (raw_poster or "doubanio.com" in hint) else "video_source"
        if item.get("cover") is not None:
            item["cover"] = hint
    if raw_poster:
        item["raw_poster"] = raw_poster
    if source_poster or fallback:
        item["source_poster"] = source_poster or fallback
    return item


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


def _douban_image_candidates(url: str, cfg: dict[str, Any] | None = None) -> list[str]:
    parsed = urlparse(url)
    hosts = []
    if parsed.netloc:
        hosts.append(parsed.netloc)
    hosts.extend([f"img{index}.doubanio.com" for index in range(1, 10)])
    seen: set[str] = set()
    candidates: list[str] = []
    configured = _configured_douban_image_candidate(url, cfg or load_config())
    if configured:
        seen.add(configured)
        candidates.append(configured)
    for host in hosts:
        if "doubanio.com" not in host:
            continue
        candidate = urlunparse(parsed._replace(netloc=host))
        if candidate in seen:
            continue
        seen.add(candidate)
        candidates.append(candidate)
    return candidates


def _configured_douban_image_candidate(url: str, cfg: dict[str, Any]) -> str:
    douban_cfg = cfg.get("douban") or {}
    proxy_type = str(douban_cfg.get("image_proxy_type") or "cmliussss-cdn-ali").strip().lower()
    proxy_url = str(douban_cfg.get("image_proxy_url") or "").strip()
    if proxy_type == "cmliussss-cdn-tencent":
        return _replace_douban_image_host(url, "img.doubanio.cmliussss.net")
    if proxy_type == "cmliussss-cdn-ali":
        return _replace_douban_image_host(url, "img.doubanio.cmliussss.com")
    if proxy_type == "img3":
        return _replace_douban_image_host(url, "img3.doubanio.com")
    if proxy_type == "custom" and proxy_url:
        return f"{proxy_url}{quote(url, safe='')}"
    return ""


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


def merge_search_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for item in results or []:
        key = _search_result_identity(item)
        if not key:
            key = f"{item.get('source') or ''}+{item.get('id') or ''}"
        current = grouped.get(key)
        if not current:
            merged = dict(item)
            display_title = _display_search_title(item)
            if display_title:
                merged["title"] = display_title
            merged["_sources"] = [_source_summary(item)]
            merged["source_count"] = 1
            grouped[key] = merged
            continue
        current["_sources"].append(_source_summary(item))
        current["source_count"] = len(current["_sources"])
        if _search_result_score(item) > _search_result_score(current):
            _copy_primary_search_fields(current, item)
        _merge_search_poster(current, item)
        _merge_search_meta(current, item)
    return sorted(grouped.values(), key=_merged_search_sort_key)


def _search_result_identity(item: dict[str, Any]) -> str:
    title = _canonical_search_title(item)
    if not title:
        return ""
    year = str(item.get("year") or "") or _year_from_search_title(item) or _extract_year_from_text(item.get("desc") or "")
    kind = "excluded" if _looks_like_excluded_resolve(item) else _infer_resolve_kind(item)
    episodes = len(item.get("episodes") or [])
    fingerprint = _search_desc_fingerprint(item.get("desc") or "")
    if fingerprint:
        return f"{title}|{kind or 'unknown'}|fp:{fingerprint}"
    if kind == "movie":
        return f"{title}|movie|{year or 'unknown'}"
    if kind == "tv":
        return f"{title}|tv|{year or 'unknown'}|{episodes or 'unknown'}"
    return f"{title}|unknown|{year or episodes or 'unknown'}"


def _source_summary(item: dict[str, Any]) -> dict[str, str]:
    return {
        "source": str(item.get("source") or ""),
        "source_name": str(item.get("source_name") or item.get("source") or ""),
        "id": str(item.get("id") or ""),
    }


def _search_result_score(item: dict[str, Any]) -> int:
    score = 0
    if item.get("poster"):
        score += 20
    if item.get("poster_source") == "douban" or item.get("raw_poster"):
        score += 16
    if item.get("year"):
        score += 10
    if item.get("desc"):
        score += 8
    if _infer_resolve_kind(item) == "movie" and len(item.get("episodes") or []) <= 1:
        score += 6
    return score


def _copy_primary_search_fields(target: dict[str, Any], source: dict[str, Any]) -> None:
    keep = {"_sources", "source_count", "poster", "raw_poster", "source_poster", "poster_source"}
    for key, value in source.items():
        if key in keep:
            continue
        target[key] = value


def _merge_search_poster(target: dict[str, Any], source: dict[str, Any]) -> None:
    current_poster = str(target.get("poster") or "")
    source_poster = str(source.get("poster") or "")
    if not current_poster and source_poster:
        target["poster"] = source_poster
        target["raw_poster"] = source.get("raw_poster") or target.get("raw_poster") or ""
        target["source_poster"] = source.get("source_poster") or target.get("source_poster") or ""
        target["poster_source"] = source.get("poster_source") or target.get("poster_source") or ""
    if source.get("poster_source") == "douban" and target.get("poster_source") != "douban":
        target["source_poster"] = current_poster or target.get("source_poster") or ""
        target["poster"] = source_poster
        target["raw_poster"] = source.get("raw_poster") or ""
        target["poster_source"] = "douban"


def _merge_search_meta(target: dict[str, Any], source: dict[str, Any]) -> None:
    if not target.get("year") and source.get("year"):
        target["year"] = source["year"]
    if not target.get("desc") and source.get("desc"):
        target["desc"] = source["desc"]
    if not target.get("class") and source.get("class"):
        target["class"] = source["class"]
    if not target.get("type_name") and source.get("type_name"):
        target["type_name"] = source["type_name"]


def _merged_search_sort_key(item: dict[str, Any]) -> tuple[int, int, str]:
    exactness = 0 if _normalize_title(item.get("title") or "") else 1
    return (exactness, -int(item.get("source_count") or 1), str(item.get("title") or ""))


def _canonical_search_title(item: dict[str, Any]) -> str:
    import re

    value = _normalize_title(str(item.get("title") or ""))
    value = re.sub(r"(19|20)\d{2}$", "", value)
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
            if value.endswith(suffix) and len(value) > len(suffix):
                value = value[: -len(suffix)]
                changed = True
                break
    return value


def _display_search_title(item: dict[str, Any]) -> str:
    import re

    value = str(item.get("title") or "").strip()
    cleaned = re.sub(r"\s*(19|20)\d{2}$", "", value).strip()
    return cleaned or value


def _year_from_search_title(item: dict[str, Any]) -> str:
    import re

    match = re.search(r"(19|20)\d{2}", str(item.get("title") or ""))
    return match.group(0) if match else ""


def _search_desc_fingerprint(value: str) -> str:
    import re

    text = re.sub(r"<[^>]+>", " ", value or "")
    text = _normalize_title(text)
    if not text or text in {"暂无简介", "暂无", "无"} or len(text) < 20:
        return ""
    return text[:80]


def _extract_year_from_text(value: str) -> str:
    import re

    match = re.search(r"(19|20)\d{2}", value or "")
    return match.group(0) if match else ""


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
            expected_year=str(detail_data.get("year") or ""),
            expected_kind=_infer_resolve_kind(detail_data),
        )
        best = _pick_matching_candidate(
            detail_data.get("title") or "",
            [candidate for candidate in candidates if candidate.get("source") == candidate_source],
            str(detail_data.get("year") or ""),
            _infer_resolve_kind(detail_data),
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


def _pick_matching_candidate(
    title: str,
    candidates: list[dict[str, Any]],
    year: str = "",
    kind: str = "",
) -> dict[str, Any] | None:
    return _pick_best_resolved_result(title, year, candidates, kind)


def _rank_resolved_results(
    title: str,
    year: str,
    results: list[dict[str, Any]],
    kind: str = "",
) -> list[dict[str, Any]]:
    canonical_title = _canonical_title_value(title)
    if not canonical_title:
        return []
    dominant_year = _dominant_resolve_year(canonical_title, results, kind) if not year else ""
    ranked: list[tuple[int, int, dict[str, Any]]] = []
    for index, item in enumerate(results):
        item_title = _canonical_title_value(str(item.get("title") or ""))
        if not item_title:
            continue
        if item_title != canonical_title:
            continue
        item_year = _resolve_item_year(item)
        if year and item_year and item_year != str(year).strip():
            continue
        ranked.append((
            _resolve_candidate_score(item, canonical_title, year, kind, dominant_year),
            -index,
            item,
        ))
    if not ranked:
        return []
    return [
        item
        for _score, _index, item in sorted(ranked, key=lambda entry: (entry[0], entry[1]), reverse=True)
    ]


def _pick_best_resolved_result(
    title: str,
    year: str,
    results: list[dict[str, Any]],
    kind: str = "",
) -> dict[str, Any] | None:
    ranked = _rank_resolved_results(title, year, results, kind)
    return ranked[0] if ranked else None


def _resolve_fallback_results(
    title: str,
    year: str,
    results: list[dict[str, Any]],
    kind: str = "",
) -> list[dict[str, Any]]:
    ranked = _rank_resolved_results(title, year, results, kind)
    if ranked:
        return ranked
    if year:
        return []
    return [
        item for item in results
        if _canonical_title_value(str(item.get("title") or "")) == _canonical_title_value(title)
    ]


def _resolve_candidate_score(
    item: dict[str, Any],
    canonical_title: str,
    year: str = "",
    kind: str = "",
    dominant_year: str = "",
) -> int:
    item_title = _canonical_title_value(str(item.get("title") or ""))
    score = 100 if item_title == canonical_title else 0
    item_year = _resolve_item_year(item)
    if year:
        if item_year == year:
            score += 70
        elif not item_year:
            score += 4
        else:
            score -= 70
    elif dominant_year:
        score += 28 if item_year == dominant_year else -10 if item_year else 0
    score += _resolve_kind_score(item, kind)
    if _looks_like_excluded_resolve(item, canonical_title):
        score -= 120
    return score


def _dominant_resolve_year(canonical_title: str, results: list[dict[str, Any]], kind: str = "") -> str:
    from collections import Counter

    years = []
    for item in results:
        if _canonical_title_value(str(item.get("title") or "")) != canonical_title:
            continue
        item_year = _resolve_item_year(item)
        if not item_year:
            continue
        if kind and _resolve_kind_score(item, kind) < 0:
            continue
        years.append(item_year)
    if not years:
        return ""
    counts = Counter(years).most_common(2)
    if counts[0][1] < 2:
        return ""
    if len(counts) > 1 and counts[0][1] == counts[1][1]:
        return ""
    return counts[0][0]


def _resolve_kind_score(item: dict[str, Any], kind: str = "") -> int:
    kind = (kind or "").lower()
    episodes = len(item.get("episodes") or [])
    movie_type = _is_source_movie_type(item)
    tv_type = _is_source_tv_type(item)
    if kind == "movie":
        score = 0
        if movie_type:
            score += 25
        if tv_type:
            score -= 42
        if episodes <= 1:
            score += 14
        elif episodes <= 3:
            score += 5
        elif episodes >= 10:
            score -= 28
        return score
    if kind == "tv":
        score = 0
        if tv_type:
            score += 25
        if movie_type:
            score -= 22
        if episodes > 1:
            score += 12
        elif episodes == 1:
            score -= 6
        return score
    return 0


def _is_source_movie_type(item: dict[str, Any]) -> bool:
    type_name = str(item.get("type_name") or "")
    return any(word in type_name for word in ("电影", "片", "纪录", "记录"))


def _is_source_tv_type(item: dict[str, Any]) -> bool:
    type_name = str(item.get("type_name") or "")
    if _is_source_movie_type(item):
        return False
    return any(word in type_name for word in ("剧", "连续", "综艺", "动漫", "番"))


def _infer_resolve_kind(item: dict[str, Any]) -> str:
    if _is_source_movie_type(item):
        return "movie"
    if _is_source_tv_type(item):
        return "tv"
    episodes = len(item.get("episodes") or [])
    if episodes > 3:
        return "tv"
    if episodes == 1:
        return "movie"
    return ""


def _looks_like_excluded_resolve(item: dict[str, Any], expected_title: str = "") -> bool:
    value = _normalize_title(f"{item.get('title') or ''} {item.get('type_name') or ''} {item.get('class') or ''}")
    excluded = [
        "电影解说",
        "解说",
        "预告",
        "预告片",
        "花絮",
        "片花",
        "彩蛋",
        "幕后",
        "资讯",
    ]
    if expected_title and _canonical_title_value(str(item.get("title") or "")) != expected_title:
        return True
    return any(word in value for word in excluded)


def _canonical_title_value(value: str) -> str:
    import re

    normalized = _normalize_title(value)
    normalized = re.sub(r"(19|20)\d{2}$", "", normalized)
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


def _resolve_item_year(item: dict[str, Any]) -> str:
    return str(item.get("year") or "").strip() or _year_from_search_title(item) or _extract_year_from_text(item.get("desc") or "")


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

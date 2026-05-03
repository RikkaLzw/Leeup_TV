from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT_DIR / "config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "site": {
        "name": "MewkoTV",
        "announcement": "本站仅接入你有权使用的视频源，不存储任何视频文件，只提供搜索、记录和播放入口。",
        "disclaimer": "所有内容来自已配置的第三方接口，本站不上传、不制作、不保存视频；请遵守当地法律法规，仅用于学习交流。",
        "qq_group_url": "https://qm.qq.com/q/LFmzAHiqYQ",
    },
    "database": {"path": "data/rikka_tv.sqlite3"},
    "proxy": {"trusted_hosts": "127.0.0.1"},
    "cache_time": 7200,
    "detail_cache_seconds": 7200,
    "search_cache_seconds": 900,
    "search_max_page": 3,
    "speed_test": {
        "enabled": True,
        "prefer_by_default": True,
        "timeout_seconds": 6,
        "sample_bytes": 512 * 1024,
        "max_workers": 6,
        "manual_max_workers": 6,
        "browser_concurrency": 4,
        "browser_speed_cap_kbps": 12288,
        "score_speed_weight": 0.85,
        "score_quality_weight": 0.1,
        "score_latency_weight": 0.05,
        "max_candidates": 8,
        "cache_seconds": 21600,
        "failure_cache_seconds": 1800,
        "search_max_page": 1,
        "manual_search_max_page": 3,
        "search_preferred_source_limit": 8,
        "shortlist_limit": 12,
        "fallback_limit": 8,
        "recommend_min_tests": 1,
        "recommend_source_probe_limit": 5,
        "resolve_cache_seconds": 86400,
        "resolve_negative_cache_seconds": 21600,
        "resolve_source_probe_limit": 12,
        "resolve_rank_speed_weight": 0.65,
        "resolve_rank_success_weight": 0.2,
        "resolve_rank_quality_weight": 0.1,
        "resolve_rank_latency_weight": 0.05,
    },
    "player": {
        "skip_intro_enabled": False,
        "skip_intro_seconds": 0,
        "skip_outro_enabled": False,
        "skip_outro_seconds": 0,
        "hls_proxy_enabled": True,
        "hls_ad_filter_enabled": True,
        "hls_proxy_timeout_seconds": 12,
        "hls_proxy_max_playlist_bytes": 2097152,
        "hls_ad_filter_max_cue_segments": 24,
        "hls_ad_filter_keywords": [
            "/ad/",
            "/ads/",
            "/adv/",
            "/gg/",
            "-ad-",
            "_ad_",
            "_ad.",
            "adinsert",
            "adserver",
            "adsegment",
            "advert",
            "advertise",
            "commercial",
            "freevod-ad",
            "mid_ad",
            "post_ad",
            "pre_ad",
            "sponsor",
            "video_ads",
            "vodads",
        ],
    },
    "douban": {
        "base_url": "https://m.douban.com",
        "timeout_seconds": 10,
        "image_proxy_type": "cmliussss-cdn-ali",
        "image_proxy_url": "",
    },
    "api_site": {},
}


def load_config() -> dict[str, Any]:
    config = deepcopy(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
        _deep_update(config, raw)
    return config


def database_path(config: dict[str, Any]) -> Path:
    raw = config.get("database", {}).get("path", "data/rikka_tv.sqlite3")
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT_DIR / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _deep_update(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = value

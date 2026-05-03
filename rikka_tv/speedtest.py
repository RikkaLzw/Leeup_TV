from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from threading import Lock
from typing import Any
from urllib.parse import urljoin

import requests
from requests import exceptions as request_exceptions


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "*/*",
}
_STREAM_TEST_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_STREAM_TEST_CACHE_LOCK = Lock()


def prefer_best_source(
    candidates: list[dict[str, Any]],
    episode_index: int,
    config: dict[str, Any],
    full: bool = False,
    measured: bool = False,
) -> dict[str, Any]:
    if measured:
        ranked = _rank(_select_measured_candidates(candidates), config)
        best = next((item for item in ranked if item["test"].get("ok")), ranked[0] if ranked else None)
        return {"best": best, "candidates": ranked}

    max_candidates = 0 if full else max(_config_int(config, "max_candidates", 8), 1)
    candidates = _select_candidates(candidates, episode_index, max_candidates)
    if not candidates:
        return {"best": None, "candidates": []}

    timeout = float(config.get("timeout_seconds") or 6)
    sample_bytes = int(config.get("sample_bytes") or 512 * 1024)
    cache_seconds = max(_config_int(config, "cache_seconds", 6 * 60 * 60), 0)
    failure_cache_seconds = max(_config_int(config, "failure_cache_seconds", 30 * 60), 0)
    worker_key = "manual_max_workers" if full else "max_workers"
    max_workers = min(int(config.get(worker_key) or config.get("max_workers") or 6), len(candidates))

    measured: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(
                _measure_candidate,
                candidate,
                episode_index,
                timeout,
                sample_bytes,
                cache_seconds,
                failure_cache_seconds,
                not full,
            ): candidate
            for candidate in candidates
        }
        for future in as_completed(futures):
            measured.append(future.result())

    ranked = _rank(measured, config)
    best = next((item for item in ranked if item["test"].get("ok")), ranked[0] if ranked else None)
    return {"best": best, "candidates": ranked}


def _measure_candidate(
    candidate: dict[str, Any],
    episode_index: int,
    timeout: float,
    sample_bytes: int,
    cache_seconds: int,
    failure_cache_seconds: int,
    use_cache: bool,
) -> dict[str, Any]:
    episodes = candidate.get("episodes") or []
    selected_index = min(max(episode_index, 0), len(episodes) - 1) if episodes else 0
    url = episodes[selected_index] if episodes else ""
    result = dict(candidate)
    result["selected_episode"] = selected_index
    result["selected_url"] = url
    if use_cache:
        result["test"] = _measure_stream_cached(url, timeout, sample_bytes, cache_seconds, failure_cache_seconds)
    else:
        result["test"] = measure_stream(url, timeout, sample_bytes)
        result["test"]["cached"] = False
    return result


def _measure_stream_cached(
    url: str,
    timeout: float,
    sample_bytes: int,
    cache_seconds: int,
    failure_cache_seconds: int,
) -> dict[str, Any]:
    if not url:
        return measure_stream(url, timeout, sample_bytes)

    now = time.time()
    if cache_seconds or failure_cache_seconds:
        with _STREAM_TEST_CACHE_LOCK:
            cached = _STREAM_TEST_CACHE.get(url)
            if cached:
                expires_at, value = cached
                if expires_at > now:
                    result = deepcopy(value)
                    result["cached"] = True
                    return result
                _STREAM_TEST_CACHE.pop(url, None)

    result = measure_stream(url, timeout, sample_bytes)
    ttl = cache_seconds if result.get("ok") else failure_cache_seconds
    if ttl > 0:
        stored = deepcopy(result)
        stored["cached"] = False
        with _STREAM_TEST_CACHE_LOCK:
            if len(_STREAM_TEST_CACHE) > 512:
                _prune_stream_cache_locked(now)
            _STREAM_TEST_CACHE[url] = (now + ttl, stored)
    result["cached"] = False
    return result


def measure_stream(url: str, timeout: float, sample_bytes: int) -> dict[str, Any]:
    if not url:
        return _fail("没有可测速的播放地址")
    try:
        target_url = url
        height = 0
        bandwidth = 0
        if ".m3u8" in url.lower():
            target_urls, height, bandwidth = _resolve_m3u8_media_samples(url, timeout, sample_bytes)
            return _download_probe_samples(target_urls, timeout, sample_bytes, height, bandwidth)
        return _download_probe(target_url, timeout, sample_bytes, height, bandwidth)
    except Exception as exc:
        return _fail(_friendly_error(exc))


def _resolve_m3u8_media(url: str, timeout: float, depth: int = 0) -> tuple[str, int, int]:
    if depth > 2:
        raise ValueError("m3u8 嵌套过深")
    response = requests.get(url, headers=HEADERS, timeout=(timeout, timeout))
    response.raise_for_status()
    text = response.text
    base = url.rsplit("/", 1)[0] + "/"
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    variants = []
    for index, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF") and index + 1 < len(lines):
            next_line = lines[index + 1]
            if next_line.startswith("#"):
                continue
            height = _parse_height(line)
            bandwidth = _parse_int_attr(line, "BANDWIDTH")
            variants.append((height, bandwidth, urljoin(base, next_line)))
    if variants:
        height, bandwidth, variant_url = sorted(variants, key=lambda item: (item[0], item[1]), reverse=True)[0]
        media_url, media_height, media_bandwidth = _resolve_m3u8_media(variant_url, timeout, depth + 1)
        return media_url, media_height or height, media_bandwidth or bandwidth

    for line in lines:
        if line.startswith("#"):
            continue
        absolute = urljoin(base, line)
        if ".m3u8" in absolute.lower():
            return _resolve_m3u8_media(absolute, timeout, depth + 1)
        return absolute, 0, 0
    raise ValueError("没有找到可测速的媒体分片")


def _resolve_m3u8_media_samples(url: str, timeout: float, sample_bytes: int, depth: int = 0) -> tuple[list[str], int, int]:
    if depth > 2:
        raise ValueError("m3u8 嵌套过深")
    response = requests.get(url, headers=HEADERS, timeout=(timeout, timeout))
    response.raise_for_status()
    text = response.text
    base = url.rsplit("/", 1)[0] + "/"
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    variants = []
    for index, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF") and index + 1 < len(lines):
            next_line = lines[index + 1]
            if next_line.startswith("#"):
                continue
            height = _parse_height(line)
            bandwidth = _parse_int_attr(line, "BANDWIDTH")
            variants.append((height, bandwidth, urljoin(base, next_line)))
    if variants:
        height, bandwidth, variant_url = sorted(variants, key=lambda item: (item[0], item[1]), reverse=True)[0]
        media_urls, media_height, media_bandwidth = _resolve_m3u8_media_samples(variant_url, timeout, sample_bytes, depth + 1)
        return media_urls, media_height or height, media_bandwidth or bandwidth

    urls = []
    for line in lines:
        if line.startswith("#"):
            continue
        absolute = urljoin(base, line)
        if ".m3u8" in absolute.lower():
            return _resolve_m3u8_media_samples(absolute, timeout, sample_bytes, depth + 1)
        urls.append(absolute)
        if len(urls) >= 4:
            break
    if urls:
        return urls, 0, 0
    raise ValueError("没有找到可测速的媒体分片")


def _download_probe(url: str, timeout: float, sample_bytes: int, height: int, bandwidth: int) -> dict[str, Any]:
    headers = {**HEADERS, "Range": f"bytes=0-{max(sample_bytes - 1, 0)}"}
    started = time.perf_counter()
    total = 0
    with requests.get(url, headers=headers, stream=True, timeout=(timeout, timeout)) as response:
        response.raise_for_status()
        latency_ms = int((time.perf_counter() - started) * 1000)
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total >= sample_bytes:
                break
    elapsed = max(time.perf_counter() - started, 0.001)
    speed_kbps = (total / 1024) / elapsed
    return {
        "ok": True,
        "error": "",
        "tested_url": url,
        "height": height,
        "bandwidth": bandwidth,
        "quality": _quality_label(height),
        "latency_ms": latency_ms,
        "speed_kbps": round(speed_kbps, 1),
        "speed_label": _speed_label(speed_kbps),
        "score": 0,
    }


def _download_probe_samples(urls: list[str], timeout: float, sample_bytes: int, height: int, bandwidth: int) -> dict[str, Any]:
    headers = {**HEADERS, "Range": f"bytes=0-{max(sample_bytes - 1, 0)}"}
    started = time.perf_counter()
    latency_ms = 0
    total = 0
    tested_urls = []
    for url in urls:
        tested_urls.append(url)
        with requests.get(url, headers=headers, stream=True, timeout=(timeout, timeout)) as response:
            response.raise_for_status()
            if not latency_ms:
                latency_ms = int((time.perf_counter() - started) * 1000)
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total >= sample_bytes:
                    break
        if total >= sample_bytes:
            break
    elapsed = max(time.perf_counter() - started, 0.001)
    speed_kbps = (total / 1024) / elapsed
    return {
        "ok": True,
        "error": "",
        "tested_url": tested_urls[0] if len(tested_urls) == 1 else ",".join(tested_urls),
        "height": height,
        "bandwidth": bandwidth,
        "quality": _quality_label(height),
        "latency_ms": latency_ms,
        "speed_kbps": round(speed_kbps, 1),
        "speed_label": _speed_label(speed_kbps),
        "score": 0,
    }


def _rank(items: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    ok_items = [item for item in items if item.get("test", {}).get("ok")]
    max_speed = max([item["test"]["speed_kbps"] for item in ok_items] or [1])
    latencies = [item["test"]["latency_ms"] for item in ok_items]
    min_latency = min(latencies or [0])
    max_latency = max(latencies or [1])
    speed_weight, quality_weight, latency_weight = _score_weights(config)

    for item in items:
        test = item.get("test") or {}
        if not test.get("ok"):
            test["score"] = 0
            continue
        quality_score = _quality_score(test.get("height") or 0)
        speed_score = min(100, (float(test.get("speed_kbps") or 0) / max_speed) * 100)
        if max_latency == min_latency:
            latency_score = 100
        else:
            latency_score = ((max_latency - float(test.get("latency_ms") or max_latency)) / (max_latency - min_latency)) * 100
        test["score"] = round(
            speed_score * speed_weight
            + quality_score * quality_weight
            + latency_score * latency_weight,
            2,
        )

    return sorted(items, key=lambda item: item.get("test", {}).get("score", 0), reverse=True)


def _select_candidates(candidates: list[dict[str, Any]], episode_index: int, limit: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    seen_urls: set[str] = set()
    for candidate in candidates:
        episodes = candidate.get("episodes") or []
        if not episodes:
            continue
        selected_index = min(max(episode_index, 0), len(episodes) - 1)
        url = str(episodes[selected_index] or "").strip()
        if not url:
            continue
        key = (str(candidate.get("source") or ""), str(candidate.get("id") or ""))
        if key in seen_keys or url in seen_urls:
            continue
        seen_keys.add(key)
        seen_urls.add(url)
        selected.append(candidate)
        if limit > 0 and len(selected) >= limit:
            break
    return selected


def _select_measured_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str, str]] = set()
    for candidate in candidates:
        selected_url = str(candidate.get("selected_url") or "").strip()
        episodes = candidate.get("episodes") or []
        if not selected_url and episodes:
            selected_episode = int(candidate.get("selected_episode") or 0)
            selected_episode = min(max(selected_episode, 0), len(episodes) - 1)
            selected_url = str(episodes[selected_episode] or "").strip()
            candidate = {**candidate, "selected_episode": selected_episode, "selected_url": selected_url}
        key = (str(candidate.get("source") or ""), str(candidate.get("id") or ""), selected_url)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        selected.append(candidate)
    return selected


def _config_int(config: dict[str, Any], key: str, default: int) -> int:
    try:
        return int(config.get(key) or default)
    except (TypeError, ValueError):
        return default


def _config_float(config: dict[str, Any], key: str, default: float) -> float:
    try:
        return float(config.get(key) if config.get(key) is not None else default)
    except (TypeError, ValueError):
        return default


def _score_weights(config: dict[str, Any]) -> tuple[float, float, float]:
    speed = max(_config_float(config, "score_speed_weight", 0.85), 0)
    quality = max(_config_float(config, "score_quality_weight", 0.1), 0)
    latency = max(_config_float(config, "score_latency_weight", 0.05), 0)
    total = speed + quality + latency
    if total <= 0:
        return 0.85, 0.1, 0.05
    return speed / total, quality / total, latency / total


def _prune_stream_cache_locked(now: float) -> None:
    for key, (expires_at, _) in list(_STREAM_TEST_CACHE.items()):
        if expires_at <= now:
            _STREAM_TEST_CACHE.pop(key, None)


def _parse_height(line: str) -> int:
    match = re.search(r"RESOLUTION=\d+x(\d+)", line)
    return int(match.group(1)) if match else 0


def _parse_int_attr(line: str, name: str) -> int:
    match = re.search(rf"{name}=(\d+)", line)
    return int(match.group(1)) if match else 0


def _quality_label(height: int) -> str:
    if height >= 2160:
        return "4K"
    if height >= 1440:
        return "2K"
    if height >= 1080:
        return "1080p"
    if height >= 720:
        return "720p"
    if height >= 480:
        return "480p"
    return "未知"


def _quality_score(height: int) -> int:
    if height >= 2160:
        return 100
    if height >= 1440:
        return 85
    if height >= 1080:
        return 75
    if height >= 720:
        return 60
    if height >= 480:
        return 40
    return 25


def _speed_label(speed_kbps: float) -> str:
    if speed_kbps >= 1024:
        return f"{speed_kbps / 1024:.2f} MB/s"
    return f"{speed_kbps:.0f} KB/s"


def _friendly_error(exc: Exception) -> str:
    if isinstance(exc, request_exceptions.SSLError):
        return "ssl_error"
    if isinstance(exc, request_exceptions.ConnectTimeout | request_exceptions.ReadTimeout | TimeoutError):
        return "timeout"
    if isinstance(exc, request_exceptions.ConnectionError):
        return "connection_error"
    if isinstance(exc, request_exceptions.HTTPError):
        return "http_error"
    return "probe_failed"


def _fail(message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": message,
        "error_label": "测速失败",
        "tested_url": "",
        "height": 0,
        "bandwidth": 0,
        "quality": "未知",
        "latency_ms": 0,
        "speed_kbps": 0,
        "speed_label": "失败",
        "score": 0,
    }

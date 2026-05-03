from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote
from urllib.parse import unquote
from urllib.parse import urljoin
from urllib.parse import urlparse

import requests


DEFAULT_AD_FILTER_KEYWORDS = [
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
]

_BLOCKED_SCHEMES = {"data", "skd", "urn"}
_URI_ATTR_PREFIX = 'URI="'


class HlsProxyError(Exception):
    """Base class for HLS proxy failures."""


class HlsProxyForbidden(HlsProxyError):
    """Raised when a target URL is not safe to fetch."""


class HlsProxyUpstreamError(HlsProxyError):
    """Raised when the upstream playlist cannot be fetched."""


@dataclass(frozen=True)
class HlsPlaylistResponse:
    content: bytes
    content_type: str
    final_url: str


@dataclass(frozen=True)
class FilteredPlaylist:
    text: str
    removed_segments: int


def proxy_playlist_url(url: str) -> str:
    return f"/hls-proxy?url={quote(url, safe='')}"


def fetch_hls_playlist(url: str, player_cfg: dict[str, Any]) -> HlsPlaylistResponse:
    target = validate_public_url(str(url or "").strip())
    timeout_seconds = _positive_number(player_cfg.get("hls_proxy_timeout_seconds"), 12.0)
    max_bytes = int(_positive_number(player_cfg.get("hls_proxy_max_playlist_bytes"), 2 * 1024 * 1024))
    headers = _hls_request_headers(target)

    for _ in range(5):
        try:
            with requests.get(
                target,
                headers=headers,
                stream=True,
                timeout=(5, timeout_seconds),
                allow_redirects=False,
            ) as upstream:
                if 300 <= upstream.status_code < 400:
                    location = upstream.headers.get("Location")
                    if not location:
                        raise HlsProxyUpstreamError("上游播放列表重定向缺少 Location")
                    target = validate_public_url(urljoin(target, location))
                    headers = _hls_request_headers(target)
                    continue
                if upstream.status_code >= 400:
                    raise HlsProxyUpstreamError(f"上游播放列表返回 {upstream.status_code}")
                content = _read_limited(upstream, max_bytes)
                return HlsPlaylistResponse(
                    content=content,
                    content_type=upstream.headers.get("Content-Type", "application/vnd.apple.mpegurl"),
                    final_url=target,
                )
        except requests.RequestException as exc:
            raise HlsProxyUpstreamError("上游播放列表请求失败") from exc

    raise HlsProxyUpstreamError("上游播放列表重定向次数过多")


def filter_m3u8_playlist(text: str, base_url: str, player_cfg: dict[str, Any]) -> FilteredPlaylist:
    lines = text.splitlines()
    if not lines:
        return FilteredPlaylist(text=text, removed_segments=0)

    ad_filter_enabled = bool(player_cfg.get("hls_ad_filter_enabled", True))
    keywords = _normalise_keywords(player_cfg.get("hls_ad_filter_keywords") or DEFAULT_AD_FILTER_KEYWORDS)
    max_cue_segments = int(_positive_number(player_cfg.get("hls_ad_filter_max_cue_segments"), 24))
    output: list[str] = []
    pending: list[str] = []
    removed_segments = 0
    ad_break_active = False
    pending_ad_marker = False
    cue_removed_segments = 0

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if pending:
                pending.append(line)
            else:
                output.append(line)
            continue

        if stripped.startswith("#"):
            upper = stripped.upper()
            rewritten = rewrite_tag_uris(line, base_url)
            if ad_filter_enabled and _is_cue_in(upper):
                ad_break_active = False
                pending_ad_marker = False
                cue_removed_segments = 0
                continue
            if ad_filter_enabled and _is_ad_marker(stripped, keywords):
                pending_ad_marker = True
                if _starts_ad_break(upper):
                    ad_break_active = True
                    cue_removed_segments = 0
                continue
            if _expects_next_uri(upper) or _is_segment_context_tag(upper):
                pending.append(rewritten)
            else:
                output.append(rewritten)
            continue

        absolute_url = urljoin(base_url, stripped)
        should_proxy = _pending_expects_playlist(pending) or _looks_like_playlist_url(absolute_url)
        rewritten_uri = proxy_playlist_url(absolute_url) if should_proxy else absolute_url
        drop_segment = False
        if ad_filter_enabled:
            drop_segment = (
                ad_break_active
                or pending_ad_marker
                or _has_ad_keyword(absolute_url, keywords)
                or _pending_has_ad_keyword(pending, keywords)
            )
        if drop_segment:
            pending.clear()
            pending_ad_marker = False
            removed_segments += 1
            if ad_break_active:
                cue_removed_segments += 1
                if cue_removed_segments >= max_cue_segments:
                    ad_break_active = False
                    cue_removed_segments = 0
            continue

        output.extend(pending)
        pending.clear()
        pending_ad_marker = False
        output.append(_rewrite_uri_line(line, rewritten_uri))

    output.extend(pending)
    suffix = "\n" if text.endswith(("\n", "\r")) else ""
    return FilteredPlaylist(text="\n".join(output) + suffix, removed_segments=removed_segments)


def rewrite_tag_uris(line: str, base_url: str) -> str:
    rewritten = line
    cursor = 0
    while True:
        start = rewritten.find(_URI_ATTR_PREFIX, cursor)
        if start < 0:
            return rewritten
        value_start = start + len(_URI_ATTR_PREFIX)
        value_end = rewritten.find('"', value_start)
        if value_end < 0:
            return rewritten
        uri = rewritten[value_start:value_end]
        replacement = rewrite_playlist_uri(uri, base_url)
        rewritten = f"{rewritten[:value_start]}{replacement}{rewritten[value_end:]}"
        cursor = value_start + len(replacement) + 1


def rewrite_playlist_uri(uri: str, base_url: str) -> str:
    parsed = urlparse(uri)
    if parsed.scheme.lower() in _BLOCKED_SCHEMES:
        return uri
    absolute_url = urljoin(base_url, uri)
    return proxy_playlist_url(absolute_url) if _looks_like_playlist_url(absolute_url) else absolute_url


def decode_playlist(content: bytes) -> str:
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return content.decode("utf-8", errors="replace")


def looks_like_m3u8(text: str, content_type: str = "") -> bool:
    stripped = text.lstrip("\ufeff\r\n\t ")
    if stripped.startswith("#EXTM3U"):
        return True
    lower_type = str(content_type or "").lower()
    return "mpegurl" in lower_type or "application/vnd.apple.mpegurl" in lower_type


def validate_public_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HlsProxyForbidden("播放列表地址必须是 http/https")
    _validate_public_host(parsed.hostname, parsed.port)
    return url


def _read_limited(response: requests.Response, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=8192):
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise HlsProxyUpstreamError("上游播放列表过大")
        chunks.append(chunk)
    return b"".join(chunks)


def _hls_request_headers(url: str) -> dict[str, str]:
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return {
        "Accept": "application/vnd.apple.mpegurl, application/x-mpegURL, text/plain, */*",
        "Referer": f"{origin}/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
    }


def _validate_public_host(host: str, port: int | None) -> None:
    try:
        addresses = socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise HlsProxyForbidden("播放列表域名无法解析") from exc
    if not addresses:
        raise HlsProxyForbidden("播放列表域名无法解析")
    for _family, _socktype, _proto, _canonname, sockaddr in addresses:
        address = sockaddr[0]
        if not _is_public_ip(address):
            raise HlsProxyForbidden("播放列表地址不允许指向内网")


def _is_public_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _positive_number(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _normalise_keywords(value: Any) -> list[str]:
    if not isinstance(value, list):
        return list(DEFAULT_AD_FILTER_KEYWORDS)
    keywords = [str(item or "").strip().lower() for item in value]
    return [item for item in keywords if item]


def _expects_next_uri(upper_line: str) -> bool:
    return upper_line.startswith("#EXT-X-STREAM-INF") or upper_line.startswith("#EXTINF")


def _pending_expects_playlist(pending: list[str]) -> bool:
    return any(line.strip().upper().startswith("#EXT-X-STREAM-INF") for line in pending)


def _is_segment_context_tag(upper_line: str) -> bool:
    return upper_line.startswith(
        (
            "#EXT-X-BYTERANGE",
            "#EXT-X-DISCONTINUITY",
            "#EXT-X-PROGRAM-DATE-TIME",
            "#EXT-X-DATERANGE",
            "#EXT-X-CUE-OUT",
            "#EXT-X-CUE-OUT-CONT",
            "#EXT-X-CUE-IN",
            "#EXT-OATCLS-SCTE35",
            "#EXT-X-ASSET",
            "#EXT-X-SCTE35",
        )
    )


def _looks_like_playlist_url(url: str) -> bool:
    parsed = urlparse(url)
    path = unquote(parsed.path or "").lower()
    return path.endswith(".m3u8") or ".m3u8/" in path


def _rewrite_uri_line(line: str, uri: str) -> str:
    leading = line[: len(line) - len(line.lstrip())]
    return f"{leading}{uri}"


def _has_ad_keyword(value: str, keywords: list[str]) -> bool:
    lower = unquote(str(value or "")).lower()
    return any(keyword in lower for keyword in keywords)


def _pending_has_ad_keyword(pending: list[str], keywords: list[str]) -> bool:
    return any(_has_ad_keyword(line, keywords) for line in pending)


def _is_ad_marker(line: str, keywords: list[str]) -> bool:
    lower = line.lower()
    if lower.startswith(("#ext-x-cue-out", "#ext-oatcls-scte35", "#ext-x-scte35")):
        return True
    if lower.startswith("#ext-x-daterange") and any(token in lower for token in ("scte", "ad", "advert", "commercial")):
        return True
    return lower.startswith(("#ext-x-asset", "#ext-x-cue")) and _has_ad_keyword(lower, keywords)


def _starts_ad_break(upper_line: str) -> bool:
    return upper_line.startswith(("#EXT-X-CUE-OUT", "#EXT-OATCLS-SCTE35", "#EXT-X-SCTE35"))


def _is_cue_in(upper_line: str) -> bool:
    return upper_line.startswith("#EXT-X-CUE-IN")

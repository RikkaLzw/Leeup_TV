from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import shutil
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse, urlunparse

import requests


ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = ROOT_DIR / "接口源.txt"
DEFAULT_CONFIG = ROOT_DIR / "config.json"
DEFAULT_OUTPUT = ROOT_DIR / "data" / "tvbox_source_probe_report.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json,text/json,text/plain,text/xml,application/xml,*/*",
}

URL_RE = re.compile(r"https?:/{1,3}[^\s#\"'<>]+", re.I)
MACCMS_HINT_RE = re.compile(
    r"https?://[^\s\"'<>\\]+?(?:"
    r"api\.php/provide/vod|"
    r"api\.php/provide|"
    r"provide/vod|"
    r"inc/api[_-]?(?:mac|mac10|vod)?\.php|"
    r"api[_-]?mac(?:10)?\.php|"
    r"xgapp\.php/v1|"
    r"api\.php/app"
    r")[^\s\"'<>\\]*",
    re.I,
)
CONFIG_LIKE_EXTENSIONS = (".json", ".txt", ".png", ".js")
DEFAULT_KEYWORDS = ("庆余年", "三体", "哪吒")


@dataclass
class SourceRef:
    url: str
    name: str = ""
    section: str = ""
    parent: str = ""
    depth: int = 0


@dataclass
class FetchResult:
    ref: SourceRef
    ok: bool
    status_code: int = 0
    final_url: str = ""
    content_type: str = ""
    text: str = ""
    error: str = ""


@dataclass
class Candidate:
    api: str
    name: str = ""
    detail: str = ""
    type_value: Any = ""
    origin_url: str = ""
    origin_name: str = ""
    origins: list[str] = field(default_factory=list)


def main() -> int:
    args = parse_args()
    input_path = Path(args.input)
    config_path = Path(args.config)
    output_path = Path(args.output)

    config = load_json_file(config_path)
    existing_api_site = config.get("api_site") or {}
    existing_keys = set(existing_api_site)
    existing_apis = {
        canonical_api_url(str(site.get("api") or ""))
        for site in existing_api_site.values()
        if isinstance(site, dict) and site.get("api")
    }

    seeds = parse_source_file(input_path)
    fetch_results, candidates = discover_candidates(seeds, args)
    unique_candidates = list(candidates.values())

    skipped_existing = []
    candidates_to_probe = []
    for candidate in unique_candidates:
        canonical = canonical_api_url(candidate.api)
        if canonical in existing_apis:
            skipped_existing.append(candidate)
        else:
            candidates_to_probe.append(candidate)

    probe_results = probe_candidates(candidates_to_probe, args)
    usable_results = [item for item in probe_results if item["status"] == "usable"]
    maybe_results = [item for item in probe_results if item["status"] == "maybe"]
    failed_results = [item for item in probe_results if item["status"] == "failed"]

    applyable_results = usable_results + (maybe_results if args.include_maybe else [])
    snippet = build_config_snippet(applyable_results, existing_keys)

    report = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "input": str(input_path),
        "config": str(config_path),
        "summary": {
            "seed_count": len(seeds),
            "config_fetch_count": len(fetch_results),
            "config_fetch_ok": sum(1 for item in fetch_results if item.ok),
            "candidate_count": len(unique_candidates),
            "skipped_existing": len(skipped_existing),
            "tested": len(probe_results),
            "usable": len(usable_results),
            "maybe": len(maybe_results),
            "failed": len(failed_results),
            "config_entries_ready": len(snippet["api_site"]),
        },
        "usable": usable_results,
        "maybe": maybe_results,
        "skipped_existing": [candidate_to_report(item, reason="already_configured") for item in skipped_existing],
        "failed_candidates": failed_results,
        "failed_configs": [fetch_to_report(item) for item in fetch_results if not item.ok],
        "config_snippet": snippet,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.apply:
        added = apply_to_config(config_path, config, snippet)
    else:
        added = 0

    print_summary(report, output_path, added, args.apply)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe TVBox warehouses and extract MacCMS APIs for MewkoTV.")
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="Path to 接口源.txt")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Path to config.json")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Where to write the JSON report")
    parser.add_argument("--apply", action="store_true", help="Merge usable entries into config.json")
    parser.add_argument(
        "--include-maybe",
        action="store_true",
        help="With --apply, also add APIs that list content but did not hit the test keywords",
    )
    parser.add_argument("--max-depth", type=int, default=1, help="How deep to follow multi-warehouse URLs")
    parser.add_argument("--max-configs", type=int, default=80, help="Maximum TVBox configs to fetch")
    parser.add_argument("--max-workers", type=int, default=12, help="Concurrent network workers")
    parser.add_argument("--timeout", type=float, default=10.0, help="Read timeout per request")
    parser.add_argument("--keyword", action="append", dest="keywords", help="Probe keyword; can be repeated")
    return parser.parse_args()


def load_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def parse_source_file(path: Path) -> list[SourceRef]:
    if not path.exists():
        raise SystemExit(f"Input file not found: {path}")

    refs: list[SourceRef] = []
    section = ""
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "单仓" in line and not URL_RE.search(line):
            section = "single"
            continue
        if "多仓" in line and not URL_RE.search(line):
            section = "multi"
            continue
        body, _, comment = line.partition("#")
        match = URL_RE.search(body)
        if not match:
            continue
        url = normalize_url(match.group(0))
        if not url:
            continue
        refs.append(SourceRef(url=url, name=clean_name(comment), section=section))
    return refs


def discover_candidates(seeds: list[SourceRef], args: argparse.Namespace) -> tuple[list[FetchResult], dict[str, Candidate]]:
    fetched: set[str] = set()
    pending = list(seeds)
    fetch_results: list[FetchResult] = []
    candidates: dict[str, Candidate] = {}

    for depth in range(max(args.max_depth, 0) + 1):
        level_refs: list[SourceRef] = []
        while pending and len(fetched) + len(level_refs) < args.max_configs:
            ref = pending.pop(0)
            normalized = canonical_config_url(ref.url)
            if normalized in fetched:
                continue
            ref.depth = depth
            fetched.add(normalized)
            level_refs.append(ref)
        if not level_refs:
            break

        with ThreadPoolExecutor(max_workers=max(1, min(args.max_workers, len(level_refs)))) as pool:
            futures = {pool.submit(fetch_url, ref, args.timeout): ref for ref in level_refs}
            for future in as_completed(futures):
                result = future.result()
                fetch_results.append(result)
                if not result.ok:
                    continue
                data, _parse_error = parse_loose_json(result.text)
                extract_candidates(data, result, candidates)
                if depth < args.max_depth:
                    nested = extract_nested_refs(data, result)
                    for nested_ref in nested:
                        if canonical_config_url(nested_ref.url) not in fetched:
                            pending.append(nested_ref)

        if len(fetched) >= args.max_configs:
            break

    return fetch_results, candidates


def fetch_url(ref: SourceRef, timeout: float) -> FetchResult:
    try:
        response = requests.get(
            ref.url,
            headers=HEADERS,
            timeout=(5, timeout),
            allow_redirects=True,
        )
        text = response_text(response)
        if response.status_code >= 400:
            return FetchResult(
                ref=ref,
                ok=False,
                status_code=response.status_code,
                final_url=response.url,
                content_type=response.headers.get("Content-Type", ""),
                text=text[:500],
                error=f"HTTP {response.status_code}",
            )
        return FetchResult(
            ref=ref,
            ok=True,
            status_code=response.status_code,
            final_url=response.url,
            content_type=response.headers.get("Content-Type", ""),
            text=text,
        )
    except requests.RequestException as exc:
        return FetchResult(ref=ref, ok=False, error=str(exc))


def response_text(response: requests.Response) -> str:
    if not response.encoding or response.encoding.lower() == "iso-8859-1":
        response.encoding = response.apparent_encoding or "utf-8"
    return response.text


def parse_loose_json(text: str) -> tuple[Any | None, str]:
    raw = (text or "").lstrip("\ufeff\r\n\t ")
    if not raw:
        return None, "empty response"

    blobs = [raw]
    extracted = extract_json_blob(raw)
    if extracted and extracted != raw:
        blobs.append(extracted)

    errors: list[str] = []
    for blob in blobs:
        variants = [
            blob,
            strip_json_comments(blob),
            strip_trailing_commas(strip_json_comments(blob)),
        ]
        for variant in dict.fromkeys(variants):
            try:
                return json.loads(variant), ""
            except json.JSONDecodeError as exc:
                errors.append(str(exc))
    return None, errors[-1] if errors else "not json"


def extract_json_blob(text: str) -> str:
    starts = [index for index in (text.find("{"), text.find("[")) if index >= 0]
    if not starts:
        return ""
    start = min(starts)
    end = max(text.rfind("}"), text.rfind("]"))
    if end <= start:
        return ""
    return text[start : end + 1]


def strip_json_comments(text: str) -> str:
    output: list[str] = []
    in_string = False
    quote_char = ""
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote_char:
                in_string = False
            index += 1
            continue

        if char in {"\"", "'"}:
            in_string = True
            quote_char = char
            output.append(char)
            index += 1
            continue
        if char == "/" and next_char == "/":
            index += 2
            while index < len(text) and text[index] not in "\r\n":
                index += 1
            continue
        if char == "/" and next_char == "*":
            index += 2
            while index + 1 < len(text) and not (text[index] == "*" and text[index + 1] == "/"):
                index += 1
            index += 2
            continue
        output.append(char)
        index += 1
    return "".join(output)


def strip_trailing_commas(text: str) -> str:
    previous = ""
    current = text
    while previous != current:
        previous = current
        current = re.sub(r",(\s*[}\]])", r"\1", current)
    return current


def extract_nested_refs(data: Any, result: FetchResult) -> list[SourceRef]:
    refs: list[SourceRef] = []
    if data is None:
        for url in URL_RE.findall(result.text or ""):
            add_nested_ref(refs, url, "", result)
        return refs

    if isinstance(data, dict):
        for key in ("urls", "storeHouse", "storehouse", "dcs", "list"):
            value = data.get(key)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, str):
                        add_nested_ref(refs, item, "", result)
                    elif isinstance(item, dict):
                        url = first_value(item, "url", "sourceUrl", "sourceURL", "api", "地址")
                        name = first_value(item, "name", "sourceName", "sourceName", "title", "名称")
                        add_nested_ref(refs, str(url or ""), clean_name(str(name or "")), result)
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, str):
                add_nested_ref(refs, item, "", result)
            elif isinstance(item, dict):
                url = first_value(item, "url", "sourceUrl", "sourceURL", "api", "地址")
                name = first_value(item, "name", "sourceName", "title", "名称")
                add_nested_ref(refs, str(url or ""), clean_name(str(name or "")), result)
    return refs


def add_nested_ref(refs: list[SourceRef], raw_url: str, name: str, result: FetchResult) -> None:
    url = normalize_url(urljoin(result.final_url or result.ref.url, html.unescape(str(raw_url or ""))))
    if not url or looks_like_maccms_api(url) or not looks_like_config_url(url):
        return
    refs.append(
        SourceRef(
            url=url,
            name=name or result.ref.name,
            section="nested",
            parent=result.ref.url,
            depth=result.ref.depth + 1,
        )
    )


def extract_candidates(data: Any, result: FetchResult, candidates: dict[str, Candidate]) -> None:
    origin_label = result.ref.name or host_label(result.final_url or result.ref.url)

    if isinstance(data, dict) and isinstance(data.get("sites"), list):
        for site in data["sites"]:
            if not isinstance(site, dict):
                continue
            api = normalize_url(str(site.get("api") or ""))
            name = clean_name(str(site.get("name") or origin_label))
            type_value = site.get("type", "")
            if api and api.startswith(("http://", "https://")) and (is_type_one(type_value) or looks_like_maccms_api(api)):
                add_candidate(candidates, api, name, site.get("ext") or "", type_value, result)
            for value in iter_strings(site.get("ext")):
                for api_url in MACCMS_HINT_RE.findall(value):
                    add_candidate(candidates, api_url, name, "", type_value, result)

    for value in iter_strings(data):
        for api_url in MACCMS_HINT_RE.findall(value):
            add_candidate(candidates, api_url, origin_label, "", "", result)


def add_candidate(
    candidates: dict[str, Candidate],
    raw_api: str,
    name: str,
    detail_hint: Any,
    type_value: Any,
    result: FetchResult,
) -> None:
    api = normalize_api_url(raw_api)
    if not api or not api.startswith(("http://", "https://")):
        return
    if not looks_like_maccms_api(api) and not is_type_one(type_value):
        return
    canonical = canonical_api_url(api)
    origin = result.final_url or result.ref.url
    if canonical in candidates:
        current = candidates[canonical]
        if name and not current.name:
            current.name = name
        if origin not in current.origins:
            current.origins.append(origin)
        return

    candidates[canonical] = Candidate(
        api=api,
        name=name or host_label(api),
        detail=detail_url(api, detail_hint),
        type_value=type_value,
        origin_url=origin,
        origin_name=result.ref.name,
        origins=[origin],
    )


def probe_candidates(candidates: list[Candidate], args: argparse.Namespace) -> list[dict[str, Any]]:
    if not candidates:
        return []
    keywords = tuple(args.keywords or DEFAULT_KEYWORDS)
    with ThreadPoolExecutor(max_workers=max(1, min(args.max_workers, len(candidates)))) as pool:
        futures = {pool.submit(probe_candidate, candidate, keywords, args.timeout): candidate for candidate in candidates}
        return [future.result() for future in as_completed(futures)]


def probe_candidate(candidate: Candidate, keywords: tuple[str, ...], timeout: float) -> dict[str, Any]:
    errors: list[str] = []
    empty_search_ok = False
    for keyword in keywords:
        url = app_style_api_url(candidate.api, {"ac": "videolist", "wd": keyword})
        ok, payload, error = fetch_maccms_payload(url, timeout)
        if not ok:
            errors.append(f"{keyword}: {error}")
            continue
        valid, count, sample = validate_maccms_payload(payload)
        if valid and count > 0:
            report = candidate_to_report(candidate)
            report.update(
                {
                    "status": "usable",
                    "matched_by": "search",
                    "keyword": keyword,
                    "items": count,
                    "sample_title": sample,
                    "probe_url": url,
                }
            )
            return report
        if valid:
            empty_search_ok = True

    list_url = app_style_api_url(candidate.api, {"ac": "videolist", "pg": "1"})
    ok, payload, error = fetch_maccms_payload(list_url, timeout)
    if ok:
        valid, count, sample = validate_maccms_payload(payload)
        if valid and count > 0:
            report = candidate_to_report(candidate)
            report.update(
                {
                    "status": "maybe",
                    "matched_by": "list",
                    "keyword": "",
                    "items": count,
                    "sample_title": sample,
                    "probe_url": list_url,
                    "note": "接口能列出内容，但测试关键词没有命中；默认不自动合并。",
                }
            )
            return report
        if valid:
            empty_search_ok = True
    else:
        errors.append(f"list: {error}")

    report = candidate_to_report(candidate)
    report.update(
        {
            "status": "failed",
            "matched_by": "",
            "keyword": "",
            "items": 0,
            "sample_title": "",
            "probe_url": list_url,
            "error": "; ".join(errors[-3:]) or ("valid payload but empty list" if empty_search_ok else "unknown error"),
        }
    )
    return report


def fetch_maccms_payload(url: str, timeout: float) -> tuple[bool, Any, str]:
    try:
        response = requests.get(url, headers=HEADERS, timeout=(5, timeout), allow_redirects=True)
        text = response_text(response)
        if response.status_code >= 400:
            return False, None, f"HTTP {response.status_code}"
    except requests.RequestException as exc:
        return False, None, str(exc)

    data, json_error = parse_loose_json(text)
    if data is not None:
        return True, data, ""

    stripped = text.lstrip("\ufeff\r\n\t ")
    if stripped.startswith("<"):
        try:
            return True, parse_maccms_xml(stripped), ""
        except ET.ParseError as exc:
            return False, None, f"XML parse failed: {exc}"

    snippet = re.sub(r"\s+", " ", stripped[:160]).strip()
    return False, None, f"not JSON/XML: {snippet or json_error}"


def parse_maccms_xml(text: str) -> dict[str, Any]:
    root = ET.fromstring(text)
    videos = root.findall(".//video")
    list_node = root.find(".//list") or root
    return {
        "page": safe_int(list_node.get("page") if list_node is not None else None, 1),
        "pagecount": safe_int(list_node.get("pagecount") if list_node is not None else None, 1),
        "list": [
            {
                "vod_id": child_text(video, "id", "vod_id"),
                "vod_name": child_text(video, "name", "vod_name"),
                "type_name": child_text(video, "type", "type_name"),
            }
            for video in videos
        ],
    }


def validate_maccms_payload(payload: Any) -> tuple[bool, int, str]:
    if not isinstance(payload, dict):
        return False, 0, ""
    items = payload.get("list")
    if not isinstance(items, list):
        return False, 0, ""
    sample = ""
    for item in items:
        if not isinstance(item, dict):
            continue
        sample = str(item.get("vod_name") or item.get("name") or item.get("title") or "").strip()
        if sample:
            break
    return True, len(items), sample


def build_config_snippet(results: list[dict[str, Any]], existing_keys: set[str]) -> dict[str, Any]:
    api_site: dict[str, dict[str, Any]] = {}
    used = set(existing_keys)
    for result in sorted(results, key=lambda item: str(item.get("name") or item.get("api") or "")):
        key = make_config_key(str(result.get("api") or ""), used)
        used.add(key)
        entry = {
            "name": str(result.get("name") or key),
            "api": str(result.get("api") or ""),
            "disabled": False,
        }
        detail = str(result.get("detail") or "").strip()
        if detail:
            entry["detail"] = detail
        api_site[key] = entry
    return {"api_site": api_site}


def apply_to_config(config_path: Path, config: dict[str, Any], snippet: dict[str, Any]) -> int:
    entries = snippet.get("api_site") or {}
    if not entries:
        return 0
    backup = config_path.with_name(f"{config_path.name}.bak.{dt.datetime.now().strftime('%Y%m%d%H%M%S')}")
    shutil.copy2(config_path, backup)
    config.setdefault("api_site", {})
    added = 0
    for key, value in entries.items():
        if key in config["api_site"]:
            continue
        config["api_site"][key] = value
        added += 1
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Backup written: {backup}")
    return added


def print_summary(report: dict[str, Any], output_path: Path, added: int, applied: bool) -> None:
    summary = report["summary"]
    print("TVBox source probe finished.")
    print(
        "Seeds: {seed_count}, configs fetched: {config_fetch_ok}/{config_fetch_count}, "
        "candidates: {candidate_count}, existing duplicates: {skipped_existing}".format(**summary)
    )
    print(
        "Usable: {usable}, maybe: {maybe}, failed: {failed}, entries ready: {config_entries_ready}".format(
            **summary
        )
    )
    for item in report["usable"][:20]:
        print(f"  + {item['name']} -> {item['api']} ({item.get('sample_title') or 'ok'})")
    if applied:
        print(f"Added to config.json: {added}")
    elif summary["config_entries_ready"]:
        print("Nothing was changed. Re-run with --apply to merge usable entries.")
    print(f"Report: {output_path}")


def normalize_url(raw: str) -> str:
    value = html.unescape(str(raw or "")).strip().strip("\"'`,;；，")
    if not value:
        return ""
    value = value.replace("\\/", "/")
    value = re.sub(r"^(https?):/([^/])", r"\1://\2", value, flags=re.I)
    value = re.sub(r"^(https?):/{3,}", r"\1://", value, flags=re.I)
    return value.rstrip(".,;；，)]}")


def normalize_api_url(raw: str) -> str:
    value = normalize_url(raw)
    if not value:
        return ""
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return value.rstrip("/")


def canonical_config_url(url: str) -> str:
    parsed = urlparse(normalize_url(url))
    return urlunparse(parsed._replace(scheme=parsed.scheme.lower(), netloc=parsed.netloc.lower(), fragment=""))


def canonical_api_url(api: str) -> str:
    value = normalize_api_url(api)
    parsed = urlparse(value)
    query = parse_qs(parsed.query)
    if query.get("url"):
        embedded = normalize_api_url(unquote(query["url"][0]))
        if embedded:
            parsed = urlparse(embedded)
    return urlunparse(parsed._replace(scheme=parsed.scheme.lower(), netloc=parsed.netloc.lower(), query="", fragment="")).rstrip("/")


def app_style_api_url(api: str, params: dict[str, str]) -> str:
    query = "&".join(f"{key}={quote(str(value))}" for key, value in params.items())
    return f"{api}?{query}"


def looks_like_maccms_api(url: str) -> bool:
    value = normalize_url(url).lower()
    return bool(MACCMS_HINT_RE.search(value))


def looks_like_config_url(url: str) -> bool:
    parsed = urlparse(normalize_url(url))
    path = parsed.path.lower()
    if path.endswith(CONFIG_LIKE_EXTENSIONS):
        return True
    return any(token in path for token in ("/tv", "/dc", "/ok", "/xhz", "/m/", "/json/", "/raw/"))


def is_type_one(value: Any) -> bool:
    try:
        return int(str(value).strip()) == 1
    except (TypeError, ValueError):
        return False


def detail_url(api: str, detail_hint: Any = "") -> str:
    hint = normalize_url(str(detail_hint or ""))
    if hint.startswith(("http://", "https://")) and not looks_like_maccms_api(hint):
        parsed_hint = urlparse(hint)
        return urlunparse(parsed_hint._replace(path="", params="", query="", fragment="")).rstrip("/")
    parsed = urlparse(canonical_api_url(api))
    if parsed.scheme and parsed.netloc:
        return urlunparse(parsed._replace(path="", params="", query="", fragment="")).rstrip("/")
    return ""


def clean_name(value: str) -> str:
    value = html.unescape(str(value or "")).strip()
    value = re.sub(r"\s+", " ", value)
    return value.strip(" -:\t")


def host_label(url: str) -> str:
    hostname = urlparse(normalize_url(url)).hostname or "source"
    return hostname


def make_config_key(api: str, used: set[str]) -> str:
    hostname = urlparse(canonical_api_url(api)).hostname or "tvbox_source"
    try:
        hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    base = re.sub(r"[^a-zA-Z0-9_]+", "_", hostname.lower()).strip("_") or "tvbox_source"
    if base[0].isdigit():
        base = f"tvbox_{base}"
    base = base[:48].strip("_") or "tvbox_source"
    key = base
    index = 2
    while key in used:
        suffix = f"_{index}"
        key = f"{base[: 64 - len(suffix)]}{suffix}"
        index += 1
    return key


def first_value(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if mapping.get(key):
            return mapping[key]
    return None


def iter_strings(value: Any) -> list[str]:
    strings: list[str] = []
    if isinstance(value, str):
        strings.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            strings.extend(iter_strings(item))
    elif isinstance(value, list):
        for item in value:
            strings.extend(iter_strings(item))
    return strings


def candidate_to_report(candidate: Candidate, reason: str = "") -> dict[str, Any]:
    report = {
        "name": candidate.name or host_label(candidate.api),
        "api": candidate.api,
        "detail": candidate.detail,
        "type": candidate.type_value,
        "origin_name": candidate.origin_name,
        "origin_url": candidate.origin_url,
        "origins": candidate.origins[:5],
    }
    if reason:
        report["reason"] = reason
    return report


def fetch_to_report(result: FetchResult) -> dict[str, Any]:
    return {
        "name": result.ref.name,
        "url": result.ref.url,
        "final_url": result.final_url,
        "status_code": result.status_code,
        "content_type": result.content_type,
        "error": result.error,
    }


def child_text(node: ET.Element, *names: str) -> str:
    for name in names:
        child = node.find(name)
        if child is not None:
            value = "".join(child.itertext()).strip()
            if value:
                return value
    return ""


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


if __name__ == "__main__":
    raise SystemExit(main())

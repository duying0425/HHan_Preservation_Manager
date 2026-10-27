#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HHanClub preservation-zone automation manager.

V6 data engine (backward-compatible ledger records):
- user_preservation_cache.json is the only persistent ledger.
- Every run reconciles the ledger against current qBittorrent HHan tasks.
- Remote HHan action=7 history is refreshed automatically when stale.
- If the cache file is missing, it is rebuilt automatically from action=7 + qB.
- Policy is provided by hhan_pzone_manager.py; downloading tasks have a separate space reservation.
"""

import argparse
import hashlib
import html as html_module
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import urlparse, parse_qs

import requests
import urllib3
from bs4 import BeautifulSoup


if getattr(sys.stdout, "encoding", "") and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(BASE_DIR, "user_preservation_cache.json")
CACHE_VERSION = 6
CACHE_READ_ONLY = False
LAST_PORTFOLIO_CONTEXT = {}
LAST_STRATEGY_CONTEXT = {}

CONFIG = {
    "hhan_base_url": "https://hhanclub.net",
    "hhan_cookie": "",
    "hhan_passkey": "",
    "hhan_ua": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "user_id": "",
    "qb_base_url": "http://127.0.0.1:8080",
    "qb_cookie": "",
    "category_keep": "保种",
    "category_del": "待删除",
    "max_preservation_space_gb": 4096.0,
    "max_candidate_seeders": 3,
    "min_candidate_seeders": 1,
    "max_single_download_gb": 80.0,
    "max_batch_download_gb": 200.0,
    "max_batch_download_count": 10,
    "portfolio_unit_gb": 0.1,
    "protect_init_one": True,
    "protect_unknown_records": True,
    "cache_remote_refresh_hours": 24.0,
    "max_downloading_space_gb": 250.0,
    "space_reward_loss_ratio": 0.10,
    "space_reward_reference_value": 0.0,
    "space_min_saving_gb": 5.0,
    "space_optimization_start_ratio": 0.95,
    "add_retry_seconds": 600.0,
}


def load_config():
    path = os.path.join(BASE_DIR, "config.json")
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            CONFIG.update(json.load(f))
    except Exception as exc:
        raise RuntimeError(f"加载 config.json 失败: {exc}") from exc


def parse_size_to_gb(text):
    m = re.search(r"([\d.]+)\s*([KMGTP]?B)", str(text), re.I)
    if not m:
        return 0.0
    value = float(m.group(1))
    unit = m.group(2).upper()
    factors = {
        "B": 1.0 / (1024.0 ** 3),
        "KB": 1.0 / (1024.0 * 1024.0),
        "MB": 1.0 / 1024.0,
        "GB": 1.0,
        "TB": 1024.0,
        "PB": 1024.0 * 1024.0,
    }
    return value * factors.get(unit, 1.0)


def normalize_name(value):
    return re.sub(r"[.\-_\s\[\]()]+", " ", str(value).lower()).strip()


def safe_int(value, default=0):
    try:
        return int(float(value))
    except Exception:
        return default


def safe_float(value, default=0.0):
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (ValueError, TypeError, OverflowError):
        return default


def preservation_multiplier(init_seeders):
    init_seeders = safe_int(init_seeders)
    if init_seeders == 1:
        return 2.00
    if 2 <= init_seeders <= 3:
        return 1.75
    if 4 <= init_seeders <= 5:
        return 1.50
    return 1.00


def calc_preservation_metrics(size_gb, age_weeks, curr_seeders, init_seeders):
    """Comparison proxy: current swarm factor and locked initial multiplier."""
    size_gb = max(0.0, safe_float(size_gb))
    t0 = max(0.1, safe_float(CONFIG.get("score_age_t0_weeks"), 8.0))
    n0 = max(1.01, safe_float(CONFIG.get("score_seeder_n0"), 10.0))
    time_factor = (1.0 if age_weeks is None else
                   1.0 - math.pow(10.0, -max(0.0, safe_float(age_weeks)) / t0))
    seeder_factor = 1.0 + math.sqrt(2.0) * math.pow(
        10.0, -max(0.0, safe_int(curr_seeders, 1) - 1.0) / (n0 - 1.0))
    multiplier = preservation_multiplier(init_seeders)
    value_per_gb = time_factor * seeder_factor * multiplier
    return {"time_factor": time_factor, "seeder_factor": seeder_factor,
            "multiplier": multiplier, "value_per_gb": value_per_gb,
            "portfolio_value": size_gb * value_per_gb,
            "dpi": 1.0 / max(1e-12, value_per_gb)}


def age_weeks_from_time(value, default=None):
    if not value:
        return default
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(str(value).strip(), fmt)
            return max(0.0, (datetime.now() - dt).total_seconds() / 604800.0)
        except ValueError:
            pass
    return default


def iso_now():
    return datetime.now().isoformat(timespec="seconds")


def parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def curl_get_hhan(path, max_retries=3):
    curl_bin = shutil.which("curl") or "curl"
    url = f"{CONFIG['hhan_base_url'].rstrip('/')}/{path.lstrip('/')}"
    cmd = [
        curl_bin,
        "--noproxy", "*",
        "-s", "-k", "--compressed", "--connect-timeout", "10", "--max-time", "45",
        "-H", f"User-Agent: {CONFIG['hhan_ua']}",
        "-H", f"Cookie: {CONFIG['hhan_cookie']}",
        url,
    ]
    last = ""
    for _ in range(max_retries):
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore"
        )
        last = result.stdout or ""
        if len(last) > 500:
            return last
        time.sleep(1.0)
    return last


def curl_download_torrent(torrent_id, max_retries=3):
    curl_bin = shutil.which("curl") or "curl"
    base = CONFIG["hhan_base_url"].rstrip("/")
    url = f"{base}/download.php?id={torrent_id}&passkey={CONFIG['hhan_passkey']}"
    cmd = [
        curl_bin,
        "--noproxy", "*",
        "-s", "-k", "--connect-timeout", "10", "--max-time", "45",
        "-H", f"User-Agent: {CONFIG['hhan_ua']}",
        "-H", f"Cookie: {CONFIG['hhan_cookie']}",
        url,
    ]
    last = b""
    for _ in range(max_retries):
        result = subprocess.run(cmd, capture_output=True)
        last = result.stdout or b""
        if len(last) > 100:
            return last
        time.sleep(1.0)
    return last


def get_qb_session():
    session = requests.Session()
    session.verify = False
    raw_cookie = str(CONFIG.get("qb_cookie", "") or "").strip()
    if raw_cookie:
        session.headers.update({"Cookie": raw_cookie})
    return session


def qb_all_torrents(session):
    url = f"{CONFIG['qb_base_url'].rstrip('/')}/api/v2/torrents/info"
    response = session.get(url, timeout=20)
    if response.status_code != 200:
        raise RuntimeError(f"qBittorrent 连接失败，HTTP {response.status_code}")
    return response.json()


def is_hhan_torrent(torrent):
    tracker = str(torrent.get("tracker", "") or "").lower()
    return "hhan" in tracker


# -----------------------------------------------------------------------------
# Cache / ledger
# -----------------------------------------------------------------------------

def empty_cache():
    return {
        "version": CACHE_VERSION,
        "updated_at": None,
        "remote_updated_at": None,
        "records": [],
    }


def load_cache():
    if not os.path.exists(CACHE_PATH):
        return empty_cache()
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            data = {"records": data}
        payload = empty_cache()
        payload.update(data if isinstance(data, dict) else {})
        payload["version"] = CACHE_VERSION
        payload.setdefault("records", [])
        # V3 legacy-migration metadata is obsolete in V4.
        payload.pop("legacy_migrated_at", None)
        payload.pop("legacy_source_updated_at", None)
        return payload
    except Exception as exc:
        raise RuntimeError(f"读取 user_preservation_cache.json 失败: {exc}") from exc


def save_cache(payload):
    if CACHE_READ_ONLY:
        return
    payload["version"] = CACHE_VERSION
    payload["updated_at"] = iso_now()
    payload.pop("legacy_migrated_at", None)
    payload.pop("legacy_source_updated_at", None)
    tmp_path = CACHE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp_path, CACHE_PATH)


def record_match_score(record, *, torrent_id=None, torrent_hash=None, title=None, size_gb=None):
    if torrent_hash and str(record.get("hash", "")).lower() == str(torrent_hash).lower():
        return (0, 0.0)
    if torrent_id and str(record.get("torrent_id", "")) == str(torrent_id):
        return (1, 0.0)
    # Known, contradictory IDs/hashes must never be joined by a similar name.
    if torrent_id and record.get("torrent_id"):
        return None
    if torrent_hash and record.get("hash"):
        return None
    if not title:
        return None
    a = normalize_name(record.get("title") or record.get("name") or "")
    b = normalize_name(title)
    if not a or not b or not (a == b or (min(len(a), len(b)) > 12 and (a in b or b in a))):
        return None
    rec_size, req_size = safe_float(record.get("size_gb")), safe_float(size_gb)
    delta = abs(rec_size - req_size) / max(rec_size, req_size) if rec_size > 0 and req_size > 0 else 1.0
    if rec_size > 0 and req_size > 0 and delta >= 0.05:
        return None
    return (2 if a == b else 3, delta)


def find_cache_record(records, **kwargs):
    best = None
    best_score = None
    for record in records:
        score = record_match_score(record, **kwargs)
        if score is not None and (best_score is None or score < best_score):
            best = record
            best_score = score
    return best


def normalize_record_metrics(record):
    # Legacy age_weeks/completed_time describe the local download, not publication.
    age = age_weeks_from_time(record.get("published_time"))
    record["age_weeks"] = round(age, 6) if age is not None else None
    metrics = calc_preservation_metrics(record.get("size_gb"), age,
                                        record.get("curr_seeders", 1), record.get("init_seeders"))
    record["priority_score"] = record["dpi"] = round(metrics["dpi"], 6)
    record["value_per_gb"] = round(metrics["value_per_gb"], 6)
    record["portfolio_value"] = round(metrics["portfolio_value"], 6)


def merge_record(records, source_record, source_name):
    record = find_cache_record(records, torrent_id=source_record.get("torrent_id") or None,
        torrent_hash=source_record.get("hash") or None,
        title=source_record.get("title") or source_record.get("name"),
        size_gb=source_record.get("size_gb"))
    if record is None:
        record = {}
        records.append(record)
    fields = ("torrent_id", "hash", "hash_v1", "hash_v2", "title", "name", "size_gb",
              "completed_time", "published_time", "added_on", "last_settlement", "last_settlement_time",
              "last_settlement_points", "today_seeding", "today_qualified", "remote_observed_at")
    for field in fields:
        if field in source_record and source_record[field] not in (None, ""):
            record[field] = source_record[field]
    init_n = safe_int(source_record.get("init_seeders"))
    if init_n > 0 and (source_name == "remote_action7" or not (record.get("init_seeders_source") == "remote_action7" or record.get("source") == "remote_action7")):
        record["init_seeders"] = init_n
        record["init_seeders_source"] = source_name
    if "curr_seeders" in source_record:
        record["curr_seeders"] = max(0, safe_int(source_record["curr_seeders"]))
    record["source"] = source_name
    normalize_record_metrics(record)
    return record


def deduplicate_records(records):
    """Join transitive ID/hash duplicates; official init wins, qB state is retained."""
    groups = []
    for item in records:
        matches = [g for g in groups if
                   (item.get("hash") and item.get("hash") in g["hashes"]) or
                   (item.get("torrent_id") and str(item["torrent_id"]) in g["ids"])]
        members = [item]
        for group in matches:
            members.extend(group["members"])
            groups.remove(group)
        groups.append({"members": members,
                       "hashes": {x["hash"] for x in members if x.get("hash")},
                       "ids": {str(x["torrent_id"]) for x in members if x.get("torrent_id")}})
    result = []
    for group in groups:
        members = sorted(group["members"], key=lambda x: (
            x.get("init_seeders_source") == "remote_action7" or x.get("source") == "remote_action7",
            str(x.get("remote_observed_at") or x.get("last_seen") or "")))
        merged = {}
        for item in members:
            for key, value in item.items():
                if value is not None and value != "":
                    if key == "init_seeders" and safe_int(value) <= 0:
                        continue
                    merged[key] = value
        # Live qB identity/status takes precedence over stale remote records.
        for item in sorted(members, key=lambda x: str(x.get("last_seen") or "")):
            if item.get("present_in_qb"):
                for key in ("hash", "title", "name", "category", "state", "progress", "active",
                            "present_in_qb", "last_seen", "identity_lookup_checked", "add_status"):
                    if key in item:
                        merged[key] = item[key]
        if any(safe_int(x.get("init_seeders")) > 0 and (x.get("source") == "remote_action7" or x.get("init_seeders_source") == "remote_action7") for x in members):
            merged["init_seeders_source"] = "remote_action7"
        normalize_record_metrics(merged)
        result.append(merged)
    records[:] = result
    return records


def remote_refresh_due(payload, force=False):
    if force:
        return True
    hours = safe_float(CONFIG.get("cache_remote_refresh_hours"), 24.0)
    if hours <= 0:
        return False
    last = parse_iso(payload.get("remote_updated_at"))
    if last is None:
        return True
    return (datetime.now() - last).total_seconds() >= hours * 3600.0


def fetch_remote_preservation_records():
    uid = str(CONFIG.get("user_id", "") or "").strip()
    if not uid:
        print("[缓存] user_id 未配置，跳过远端刷新。")
        return []
    print("[缓存] 正在完整读取 HHan action=7 档案...")
    started, observed = datetime.now(), iso_now()

    def read_page(page):
        html = curl_get_hhan(f"userdetails.php?action=7&id={uid}&page={page}")
        if not html:
            raise RuntimeError(f"action=7 第 {page} 页读取失败，保留原账本")
        soup = BeautifulSoup(html, "html.parser")
        if soup.select_one('input[name="password"], #challenge-running, #cf-challenge-running'):
            raise RuntimeError("action=7 需要重新登录或完成验证，保留原账本")
        rows, valid_table = [], False
        for table in soup.find_all("table"):
            table_rows = table.find_all("tr", recursive=False)
            if not table_rows:
                body = table.find("tbody", recursive=False)
                table_rows = body.find_all("tr", recursive=False) if body else []
            headers = []
            for row in table_rows:
                cells = row.find_all(["td", "th"], recursive=False)
                labels = [c.get_text(" ", strip=True) for c in cells]
                if len(cells) >= 6 and not any(c.find("a", href=re.compile(r"details\.php")) for c in cells):
                    if any("初始" in label or "今日达标" in label or "今日達標" in label for label in labels):
                        headers = labels
                        valid_table = True
                if len(cells) < 6:
                    continue
                link = cells[1].find("a", href=re.compile(r"details\.php"))
                if not link:
                    continue
                match = re.search(r"(?:[?&])id=(\d+)", html_module.unescape(link.get("href", "")))
                if not match:
                    continue
                valid_table = True
                item = {"torrent_id": match.group(1), "title": link.get_text(strip=True),
                        "name": link.get_text(strip=True), "size_gb": round(parse_size_to_gb(labels[2]), 6),
                        "init_seeders": safe_int(labels[3]), "curr_seeders": safe_int(labels[4]),
                        "completed_time": labels[5], "remote_observed_at": observed}
                aliases = {"last_settlement": ("上次结算", "上次結算"),
                           "today_seeding": ("今日做种", "今日做種"),
                           "today_qualified": ("今日是否达标", "今日是否達標", "今日达标", "今日達標")}
                for fallback, (field, names) in enumerate(aliases.items(), 6):
                    index = next((i for i, label in enumerate(headers) if any(n in label for n in names)), fallback)
                    if index < len(cells):
                        item[field] = labels[index]
                raw_points = item.get("last_settlement", "")
                if re.match(r"\d{4}-\d{2}-\d{2}", raw_points):
                    item["last_settlement_time"] = raw_points
                if re.fullmatch(r"[\d,.]+", raw_points):
                    item["last_settlement_points"] = safe_float(raw_points.replace(",", ""))
                rows.append(item)
        if not valid_table:
            raise RuntimeError(f"action=7 第 {page} 页未识别到档案表，保留原账本")
        return rows, html

    first, html = read_page(0)
    matches = re.findall(r"\bmaxpage\s*[:=]\s*['\"]?(\d+)", html, re.I)
    soup = BeautifulSoup(html, "html.parser")
    for link in soup.find_all("a", href=True):
        if link.get("rel") == ["last"] or link.get_text(strip=True) in ("末页", "末頁", "Last", ">>"):
            match = re.search(r"[?&]page=(\d+)", html_module.unescape(link["href"]))
            if match:
                matches.append(match.group(1))
    for page_input in soup.select('input[type="number"][max]'):
        count = safe_int(page_input.get("max"))
        if count > 0:
            matches.append(str(count-1))
    last_page = max(map(int, matches)) if matches else None
    records = {r["torrent_id"]: r for r in first}
    page = 1
    while last_page is None or page <= last_page:
        rows, _ = read_page(page)
        if not rows:
            if last_page is not None and page < last_page:
                raise RuntimeError(f"action=7 第 {page} 页意外为空，取消本次刷新")
            break
        fresh = {r["torrent_id"]: r for r in rows if r["torrent_id"] not in records}
        if not fresh:
            if last_page is not None and page < last_page:
                raise RuntimeError("action=7 提前重复页面，取消本次刷新")
            break
        records.update(fresh)
        page += 1
    print(f"      完整读取 {len(records)} 条，耗时 {(datetime.now()-started).total_seconds():.1f}s")
    return list(records.values())


def merge_remote_into_cache(payload, remote_records):
    records = payload.setdefault("records", [])
    for item in remote_records:
        merge_record(records, item, "remote_action7")
    if remote_records:
        payload["remote_updated_at"] = iso_now()


def reconcile_cache_with_qb(payload, qb_torrents, session=None):
    records = deduplicate_records(payload.setdefault("records", []))
    now = iso_now()
    for record in records:
        record["present_in_qb"] = False
        record["active"] = False
    stats = {"hhan_in_qb": 0, "active_keep": 0, "restored": 0, "unknown": 0}
    base = CONFIG["qb_base_url"].rstrip("/")
    for torrent in qb_torrents:
        if not is_hhan_torrent(torrent):
            continue
        stats["hhan_in_qb"] += 1
        title, torrent_hash = torrent.get("name", ""), str(torrent.get("hash", "")).lower()
        size_gb = torrent_size_gb(torrent)
        record = find_cache_record(records, torrent_hash=torrent_hash, title=title, size_gb=size_gb)
        if record is None:
            record = {"hash": torrent_hash, "title": title, "init_seeders": 0, "source": "qb_discovered"}
            records.append(record)
        if (not record.get("torrent_id") or safe_int(record.get("init_seeders")) <= 0) and not record.get("identity_lookup_checked") and session is not None:
            try:
                response = session.get(f"{base}/api/v2/torrents/properties", params={"hash": torrent_hash}, timeout=20)
                if response.status_code == 200:
                    properties = response.json()
                    if isinstance(properties, dict):
                        torrent_id = torrent_id_from_comment(properties.get("comment", ""))
                        record["identity_lookup_checked"] = True
                        if torrent_id:
                            record["torrent_id"] = torrent_id
                            known = find_cache_record(records, torrent_id=torrent_id)
                            if known is not None and known is not record:
                                known["hash"] = torrent_hash
                                # Force ID/hash union before applying fresh qB state.
                                deduplicate_records(records)
                                record = find_cache_record(records, torrent_hash=torrent_hash, torrent_id=torrent_id)
            except (requests.RequestException, ValueError, TypeError):
                pass  # Transient failures do not consume the one successful lookup.
        previous_category = record.get("last_category", record.get("category"))
        record.update({"hash": torrent_hash, "title": title, "name": title,
                       "size_gb": round(size_gb, 6), "category": torrent.get("category", "") or "",
                       "state": torrent.get("state", ""),
                       "progress": round(safe_float(torrent.get("progress")) * 100.0, 6),
                       "present_in_qb": True, "last_seen": now, "add_status": "confirmed"})
        qb_seeders = safe_int(torrent.get("num_complete"), -1)
        if qb_seeders >= 0:
            record["curr_seeders"] = qb_seeders
        record["active"] = record["category"] == CONFIG["category_keep"]
        if record["active"]:
            stats["active_keep"] += 1
            record["last_active"] = now
            stats["restored"] += previous_category == CONFIG["category_del"]
            stats["unknown"] += safe_int(record.get("init_seeders")) <= 0
        record["last_category"] = record["category"]
        normalize_record_metrics(record)
    for record in records:
        if not record.get("present_in_qb") and record.get("add_status") in ("submitted", "uncertain"):
            attempted = parse_iso(record.get("add_attempted_at"))
            if attempted and (datetime.now()-attempted).total_seconds() >= safe_float(CONFIG.get("add_retry_seconds"), 600):
                record["add_status"] = "failed"
                record["add_attempted_at"] = now
    deduplicate_records(records)
    payload["qb_reconciled_at"] = now
    stats["cache_records"] = len(records)
    return stats


def torrent_size_gb(torrent):
    return max(safe_float(torrent.get("total_size")), safe_float(torrent.get("size"))) / 1024.0 ** 3


def torrent_is_complete(torrent):
    state = str(torrent.get("state", "")).lower()
    return (safe_float(torrent.get("progress")) >= 1.0 - 1e-9
            and safe_int(torrent.get("amount_left"), 0) == 0
            and state not in {"metadl", "downloading", "forceddl", "stalleddl", "queueddl", "pauseddl", "stoppeddl", "checkingdl", "checkingup", "checkingresumedata", "missingfiles", "error", "unknown"})


def torrent_id_from_comment(comment):
    allowed_host = urlparse(CONFIG["hhan_base_url"]).hostname
    text = html_module.unescape(str(comment))
    for url in re.findall(r"https?://[^\s<>\"']+", text):
        parsed = urlparse(url.rstrip(").,;"))
        if parsed.hostname == allowed_host and parsed.path.endswith("/details.php"):
            values = parse_qs(parsed.query).get("id", [])
            if values and values[0].isdigit():
                return values[0]
    return None


def refresh_cache(session, force_remote=False, quiet=False):
    cache_missing = not os.path.exists(CACHE_PATH)
    payload = load_cache()

    if cache_missing and not quiet:
        print("[缓存] 未检测到 user_preservation_cache.json，将从 action=7 + 当前 qB 自动重建。")

    # Missing cache always forces a fresh action=7 fetch; no legacy file is read.
    should_refresh_remote = remote_refresh_due(payload, force=(force_remote or cache_missing))
    remote_refreshed = False
    if should_refresh_remote:
        try:
            remote_records = fetch_remote_preservation_records()
        except RuntimeError as exc:
            print(f"[缓存] {exc}")
            remote_records = []
        if remote_records:
            merge_remote_into_cache(payload, remote_records)
            remote_refreshed = True
        elif cache_missing:
            print("[缓存] ⚠️ action=7 未取得记录，将仅根据当前 qB 建账；未知任务会按保护策略处理。")
        elif force_remote:
            print("[缓存] 强制远端刷新未取得记录，保留现有账本。")

    qb_torrents = qb_all_torrents(session)
    stats = reconcile_cache_with_qb(payload, qb_torrents, session=session)
    save_cache(payload)
    stats["cache_rebuilt"] = cache_missing
    stats["remote_refreshed"] = remote_refreshed
    stats["remote_updated_at"] = payload.get("remote_updated_at")

    if not quiet:
        action = "重建完成" if cache_missing else "对账完成"
        print(
            f"[缓存] {action}：账本 {stats['cache_records']} 条，"
            f"qB 中 HHan {stats['hhan_in_qb']} 个，当前【{CONFIG['category_keep']}】 {stats['active_keep']} 个"
        )
        if stats["restored"]:
            print(f"      检测到 {stats['restored']} 个任务重新回到【{CONFIG['category_keep']}】，已恢复为 active。")
        if stats["unknown"]:
            print(f"      新发现 {stats['unknown']} 个无 action=7 元数据任务，暂按未知记录保护。")
    return payload, qb_torrents, stats


# -----------------------------------------------------------------------------
# Current qB portfolio / rescue candidates
# -----------------------------------------------------------------------------

def check_qb_status(qb_torrents, cache_payload):
    global LAST_PORTFOLIO_CONTEXT
    print("[1/4] 正在读取 qBittorrent 已完成保种任务...")
    existing_names = {normalize_name(t.get("name", "")) for t in qb_torrents}
    records = cache_payload.get("records", [])
    keep = [t for t in qb_torrents if is_hhan_torrent(t) and t.get("category") == CONFIG["category_keep"]]
    downloading = [t for t in keep if not torrent_is_complete(t)]
    completed = [t for t in keep if torrent_is_complete(t)]
    items, unmatched = [], 0
    for torrent in completed:
        size_gb = torrent_size_gb(torrent)
        record = find_cache_record(records, torrent_hash=torrent.get("hash"), title=torrent.get("name"), size_gb=size_gb)
        init_n = safe_int(record.get("init_seeders")) if record else 0
        curr_n = safe_int(record.get("curr_seeders"), 1) if record else safe_int(torrent.get("num_complete"), 1)
        age = age_weeks_from_time(record.get("published_time")) if record else None
        unmatched += init_n <= 0
        reason = ""
        if CONFIG.get("protect_init_one", True) and init_n == 1:
            reason = "初始1人硬保护"
        elif CONFIG.get("protect_unknown_records", True) and init_n <= 0:
            reason = "action=7元数据未知"
        metrics = calc_preservation_metrics(size_gb, age, curr_n, init_n)
        items.append({"key": f"existing:{torrent['hash']}", "source": "existing",
                      "hash": torrent["hash"], "title": torrent.get("name", ""), "name": torrent.get("name", ""),
                      "size_gb": size_gb, "init_seeders": init_n, "curr_seeders": curr_n,
                      "age_weeks": age, "protected": bool(reason), "protect_reason": reason, **metrics})
    pending = [r for r in records if not r.get("present_in_qb") and r.get("add_status") in ("submitted", "uncertain")]
    downloading_gb = sum(torrent_size_gb(t) for t in downloading) + sum(safe_float(r.get("size_gb")) for r in pending)
    current_gb = sum(torrent_size_gb(t) for t in completed)
    LAST_PORTFOLIO_CONTEXT = {"downloading_count": len(downloading)+len(pending),
                              "downloading_gb": downloading_gb, "records": records,
                              "strategy_state": cache_payload.setdefault("strategy_state", {})}
    print(f"      已完成 {len(completed)} 个 / {current_gb:.2f} GB；下载/待确认 {len(downloading)+len(pending)} 个 / {downloading_gb:.2f} GB（收益评估排除）")
    return existing_names, current_gb, items, unmatched


def fetch_rescue_candidates(existing_names, existing_ids=None):
    existing_ids = set(map(str, existing_ids or []))
    for record in LAST_PORTFOLIO_CONTEXT.get("records", []):
        if record.get("present_in_qb") or record.get("add_status") in ("submitted", "uncertain"):
            if record.get("torrent_id"):
                existing_ids.add(str(record["torrent_id"]))
        elif record.get("add_status") == "failed":
            last = parse_iso(record.get("add_attempted_at"))
            if last and (datetime.now()-last).total_seconds() < safe_float(CONFIG.get("add_retry_seconds"), 600):
                existing_ids.add(str(record.get("torrent_id", "")))
    print("[2/4] 正在扫描 HHanClub 保种区候选...")
    cards = []
    seen_ids = set()
    pages = 0
    for page in range(20):
        html = curl_get_hhan(f"rescue.php?page={page}")
        if not html:
            break
        soup = BeautifulSoup(html, "html.parser")
        page_cards = soup.find_all("div", class_=re.compile(r"torrent-table-sub-info"))
        if not page_cards:
            break
        page_ids = []
        for card in page_cards:
            link = card.find("a", class_=re.compile(r"torrent-info-text-name"))
            if link:
                m = re.search(r"id=(\d+)", link.get("href", ""))
                if m:
                    page_ids.append(m.group(1))
        new_ids = set(page_ids) - seen_ids
        if page > 0 and not new_ids:
            break
        seen_ids.update(new_ids)
        cards.extend(page_cards)
        pages += 1
        if len(page_cards) < 30:
            break

    stats = {
        "total_in_zone": len(cards),
        "dead_count": 0,
        "crowded_count": 0,
        "huge_count": 0,
        "already_have": 0,
        "eligible_count": 0,
        "pages": pages,
    }
    candidates = []
    now = datetime.now()

    processed_ids = set()
    for card in cards:
        link = card.find("a", class_=re.compile(r"torrent-info-text-name"))
        if not link:
            continue
        title = link.get_text(strip=True)
        m = re.search(r"id=(\d+)", link.get("href", ""))
        if not m:
            continue
        torrent_id = m.group(1)
        if torrent_id in processed_ids:
            continue
        processed_ids.add(torrent_id)
        if torrent_id in existing_ids:
            stats["already_have"] += 1
            continue
        size_div = card.find("div", class_=re.compile(r"torrent-info-text-size"))
        size_gb = parse_size_to_gb(size_div.get_text(strip=True) if size_div else "")
        seed_div = card.find("div", class_=re.compile(r"torrent-info-text-seeders"))
        seeders = safe_int(seed_div.get_text(strip=True) if seed_div else 0)

        added_div = card.find("div", class_=re.compile(r"torrent-info-text-added"))
        span = added_div.find("span") if added_div else None
        added_text = span.get("title", "") if span else ""
        age_weeks = age_weeks_from_time(added_text)

        normalized = normalize_name(title)
        if normalized in existing_names or any(
            normalized in name or name in normalized
            for name in existing_names
            if len(name) > 12
        ):
            stats["already_have"] += 1
            continue
        if seeders < safe_int(CONFIG["min_candidate_seeders"]):
            stats["dead_count"] += 1
            continue
        if seeders > safe_int(CONFIG["max_candidate_seeders"]):
            stats["crowded_count"] += 1
            continue
        if size_gb <= 0 or size_gb > safe_float(CONFIG["max_single_download_gb"]):
            stats["huge_count"] += 1
            continue

        metrics = calc_preservation_metrics(size_gb, age_weeks, seeders, seeders)
        candidates.append({
            "id": torrent_id,
            "title": title,
            "size_gb": round(size_gb, 3),
            "seeders": seeders,
            "age_weeks": round(age_weeks, 6) if age_weeks is not None else None,
            "published_time": added_text or None,
            "value_per_gb": round(metrics["value_per_gb"], 6),
            "portfolio_value": round(metrics["portfolio_value"], 6),
        })

    candidates.sort(
        key=lambda x: (x["value_per_gb"], x["portfolio_value"]), reverse=True
    )
    stats["eligible_count"] = len(candidates)
    print(f"      扫描 {pages} 页，保种区 {len(cards)} 个；合格候选 {len(candidates)} 个")
    return candidates, stats


# -----------------------------------------------------------------------------
# Portfolio optimizer
# -----------------------------------------------------------------------------


def knapsack_select(items, capacity_gb, unit_gb, target_value=None):
    if not items or capacity_gb <= 0:
        return []
    unit_gb = max(0.05, safe_float(unit_gb, 0.1))
    capacity = int(math.floor(safe_float(capacity_gb) / unit_gb + 1e-12))
    if capacity <= 0:
        return []

    filtered, weights, values = [], [], []
    for item in items:
        size_gb = max(0.0, safe_float(item.get("size_gb")))
        value = max(0.0, safe_float(item.get("portfolio_value")))
        if size_gb <= 0 or value <= 0:
            continue
        weight = max(1, int(math.ceil(size_gb / unit_gb - 1e-12)))
        if weight > capacity:
            continue
        filtered.append(item)
        weights.append(weight)
        values.append(value)

    if not filtered:
        return []

    neg_inf = float("-inf")
    dp = [neg_inf] * (capacity + 1)
    dp[0] = 0.0
    decisions = []
    for weight, value in zip(weights, values):
        row = bytearray(capacity + 1)
        for cap in range(capacity, weight - 1, -1):
            prev = dp[cap - weight]
            if prev == neg_inf:
                continue
            candidate = prev + value
            if candidate > dp[cap] + 1e-12:
                dp[cap] = candidate
                row[cap] = 1
        decisions.append(row)

    if target_value is not None:
        cap = next((c for c, value in enumerate(dp) if value + 1e-9 >= target_value), None)
        if cap is None:
            return []
    else:
        cap = max(range(capacity + 1), key=lambda c: dp[c])
    selected = []
    for idx in range(len(filtered) - 1, -1, -1):
        if decisions[idx][cap]:
            selected.append(filtered[idx])
            cap -= weights[idx]
    selected.reverse()
    return selected


def generate_strategy(candidates, current_keep_gb, current_items):
    print("[3/4] 正在执行 4TB 保种专项配额全局组合优化...")
    max_space = safe_float(CONFIG["max_preservation_space_gb"])
    unit_gb = safe_float(CONFIG.get("portfolio_unit_gb"), 0.1)

    protected = [item for item in current_items if item.get("protected")]
    optional_existing = [item for item in current_items if not item.get("protected")]
    protected_gb = sum(safe_float(item["size_gb"]) for item in protected)

    new_items = []
    candidate_lookup = {}
    for candidate in candidates:
        key = f"new:{candidate['id']}"
        item = {
            "key": key,
            "source": "new",
            "id": candidate["id"],
            "title": candidate["title"],
            "name": candidate["title"],
            "size_gb": safe_float(candidate["size_gb"]),
            "init_seeders": safe_int(candidate["seeders"]),
            "curr_seeders": safe_int(candidate["seeders"]),
            "age_weeks": safe_float(candidate.get("age_weeks"), 16.0),
            "value_per_gb": safe_float(candidate["value_per_gb"]),
            "portfolio_value": safe_float(candidate["portfolio_value"]),
            "protected": False,
        }
        new_items.append(item)
        candidate_lookup[key] = candidate

    remaining = max(0.0, max_space - protected_gb)
    selected_optional = knapsack_select(optional_existing + new_items, remaining, unit_gb)
    target = protected + selected_optional
    target_keys = {item["key"] for item in target}
    target_new = [item for item in selected_optional if item["source"] == "new"]
    target_existing = [item for item in target if item["source"] == "existing"]
    excluded_existing = [
        item for item in optional_existing if item["key"] not in target_keys
    ]

    target_new.sort(
        key=lambda x: (x["value_per_gb"], x["portfolio_value"]), reverse=True
    )
    batch_gb = safe_float(CONFIG.get("max_batch_download_gb"), 200.0)
    batch_count = safe_int(CONFIG.get("max_batch_download_count"), 10)
    selected_batch, download_gb = [], 0.0
    for item in target_new:
        if len(selected_batch) >= batch_count:
            break
        if download_gb + item["size_gb"] <= batch_gb + 1e-9:
            selected_batch.append(item)
            download_gb += item["size_gb"]

    to_download = [candidate_lookup[item["key"]] for item in selected_batch]
    projected = current_keep_gb + download_gb
    overflow = max(0.0, projected - max_space)
    excluded_existing.sort(key=lambda x: (x["value_per_gb"], -x["size_gb"]))

    to_mark_delete, marked_gb = [], 0.0
    for item in excluded_existing:
        if marked_gb + 1e-9 >= overflow:
            break
        to_mark_delete.append({
            "hash": item["hash"],
            "name": item["name"],
            "size_gb": round(item["size_gb"], 3),
            "init_seeders": item["init_seeders"],
            "curr_seeders": item["curr_seeders"],
            "value_per_gb": round(item["value_per_gb"], 6),
            "portfolio_value": round(item["portfolio_value"], 6),
            "reason": "不在4TB目标组合",
        })
        marked_gb += item["size_gb"]

    current_value = sum(safe_float(x.get("portfolio_value")) for x in current_items)
    target_value = sum(safe_float(x.get("portfolio_value")) for x in target)
    stats = {
        "protected_count": len(protected),
        "protected_gb": protected_gb,
        "target_count": len(target),
        "target_existing_count": len(target_existing),
        "target_new_count": len(target_new),
        "target_space_gb": sum(safe_float(x["size_gb"]) for x in target),
        "current_value": current_value,
        "target_value": target_value,
        "value_gain": target_value - current_value,
        "unit_gb": unit_gb,
        "unresolved_overflow_gb": max(0.0, overflow - marked_gb),
    }
    return to_download, download_gb, to_mark_delete, marked_gb, projected, stats


def print_strategy_report(
    to_download,
    download_gb,
    to_mark_delete,
    marked_gb,
    current_keep_gb,
    zone_stats,
    portfolio_stats,
    unmatched_count,
    cache_stats,
    dry_run,
):
    max_space = safe_float(CONFIG["max_preservation_space_gb"])
    mode = "DRY-RUN" if dry_run else "EXECUTE"
    print("\n" + "=" * 96)
    print(f"HHanClub 保种区策略报告 [{mode}]")
    print("=" * 96)
    print(
        f"缓存: {cache_stats['cache_records']} 条 | 当前保种 {cache_stats['active_keep']} | "
        f"{'本轮自动重建 | ' if cache_stats.get('cache_rebuilt') else ''}"
        f"远端刷新 {'是' if cache_stats['remote_refreshed'] else '否'} | "
        f"remote_updated_at={cache_stats.get('remote_updated_at') or '-'}"
    )
    print(
        f"保种区: {zone_stats['total_in_zone']} 个 | 已有 {zone_stats['already_have']} | "
        f"0人 {zone_stats['dead_count']} | 超人数阈值 {zone_stats['crowded_count']} | "
        f"超体积阈值 {zone_stats['huge_count']} | 候选 {zone_stats['eligible_count']}"
    )
    print(
        f"当前【{CONFIG['category_keep']}】专项配额: {current_keep_gb:.2f} / "
        f"{max_space:.2f} GB ({current_keep_gb / 1024.0:.2f} TB)"
    )
    print(
        f"全局目标组合: {portfolio_stats['target_count']} 个 / {portfolio_stats['target_space_gb']:.2f} GB | "
        f"预测价值 {portfolio_stats['current_value']:.2f} -> {portfolio_stats['target_value']:.2f} "
        f"(Δ {portfolio_stats['value_gain']:+.2f})"
    )
    print(
        f"硬保护: {portfolio_stats['protected_count']} 个 / {portfolio_stats['protected_gb']:.2f} GB | "
        f"action=7元数据未知: {unmatched_count} 个"
    )

    print(f"\n[+] 本轮新增: {len(to_download)} 个 / {download_gb:.2f} GB")
    for idx, item in enumerate(to_download, 1):
        print(
            f"  {idx:>2}. {item['size_gb']:>7.2f} GB | init={item['seeders']} curr={item['seeders']} | "
            f"价值/GB {item['value_per_gb']:.3f} | {item['title']}"
        )

    print(
        f"\n[-] 本轮退出【{CONFIG['category_keep']}】: "
        f"{len(to_mark_delete)} 个 / {marked_gb:.2f} GB"
    )
    for idx, item in enumerate(to_mark_delete, 1):
        print(
            f"  {idx:>2}. {item['size_gb']:>7.2f} GB | init={item['init_seeders']} curr={item['curr_seeders']} | "
            f"价值/GB {item['value_per_gb']:.3f} | {item['name']}"
        )

    final_estimate = current_keep_gb + download_gb - marked_gb
    print(f"\n本轮执行后预计专项配额: {final_estimate:.2f} GB / {max_space:.2f} GB")
    if portfolio_stats["unresolved_overflow_gb"] > 0.01:
        print(
            f"⚠️ 仍有 {portfolio_stats['unresolved_overflow_gb']:.2f} GB "
            "无法释放，请检查硬保护项和未知元数据。"
        )
    print(f"背包离散粒度: {portfolio_stats['unit_gb']:.2f} GB")
    print("=" * 96 + "\n")


# -----------------------------------------------------------------------------
# Execute
# -----------------------------------------------------------------------------

def upsert_candidate_cache(candidate):
    payload = load_cache()
    record = merge_record(payload.setdefault("records", []), {
        "torrent_id": str(candidate["id"]), "hash": candidate.get("hash"),
        "hash_v1": candidate.get("hash_v1"), "hash_v2": candidate.get("hash_v2"),
        "title": candidate["title"], "name": candidate["title"], "size_gb": candidate["size_gb"],
        "init_seeders": candidate["seeders"], "curr_seeders": candidate["seeders"],
        "published_time": candidate.get("published_time")}, "download_candidate")
    record.update({"add_status": candidate.get("add_status", "submitted"),
                   "add_attempted_at": iso_now(), "category": CONFIG["category_keep"],
                   "active": False, "identity_lookup_checked": True})
    save_cache(payload)


def torrent_metadata(data):
    """Hash the original bencoded info slice; support v1, v2 and hybrid torrents."""
    if not isinstance(data, bytes):
        raise ValueError("torrent is not bytes")
    position, info_range = 0, None
    def decode(depth=0):
        nonlocal position, info_range
        if depth > 100 or position >= len(data):
            raise ValueError("invalid bencode")
        token = data[position:position+1]
        if token == b"i":
            end = data.find(b"e", position)
            raw = data[position+1:end]
            if end < 0 or not re.fullmatch(rb"-?(0|[1-9]\d*)", raw) or raw == b"-0":
                raise ValueError("invalid integer")
            position = end+1
            return int(raw)
        if token in (b"d", b"l"):
            position += 1
            result = {} if token == b"d" else []
            while position < len(data) and data[position:position+1] != b"e":
                if token == b"d":
                    key = decode(depth+1)
                    if not isinstance(key, bytes) or key in result:
                        raise ValueError("invalid dictionary")
                    start = position
                    result[key] = decode(depth+1)
                    if depth == 0 and key == b"info":
                        info_range = (start, position)
                else:
                    result.append(decode(depth+1))
            if position >= len(data):
                raise ValueError("unterminated bencode")
            position += 1
            return result
        end = data.find(b":", position)
        raw_length = data[position:end]
        if end < 0 or not re.fullmatch(rb"0|[1-9]\d*", raw_length):
            raise ValueError("invalid string")
        length = int(raw_length)
        position = end+1
        if position+length > len(data):
            raise ValueError("truncated string")
        result = data[position:position+length]
        position += length
        return result
    meta = decode()
    if position != len(data) or not isinstance(meta, dict) or info_range is None:
        raise ValueError("not a torrent")
    info = meta.get(b"info")
    if not isinstance(info, dict):
        raise ValueError("missing info dictionary")
    raw_info = data[slice(*info_range)]
    v2 = info.get(b"meta version") == 2
    v1 = not v2 or b"pieces" in info
    hash_v1 = hashlib.sha1(raw_info).hexdigest() if v1 else ""
    hash_v2 = hashlib.sha256(raw_info).hexdigest() if v2 else ""
    def tree_size(node):
        if not isinstance(node, dict):
            raise ValueError("invalid file tree")
        total = 0
        for key, value in node.items():
            if key == b"":
                if not isinstance(value, dict) or not isinstance(value.get(b"length"), int):
                    raise ValueError("invalid file length")
                total += value[b"length"]
            else:
                total += tree_size(value)
        return total
    if b"files" in info:
        lengths = [f.get(b"length") for f in info[b"files"]]
        if not lengths or any(not isinstance(n, int) or n < 0 for n in lengths):
            raise ValueError("invalid file lengths")
        size = sum(lengths)
    elif isinstance(info.get(b"length"), int):
        size = info[b"length"]
    elif v2:
        size = tree_size(info.get(b"file tree"))
    else:
        raise ValueError("missing size")
    if size <= 0:
        raise ValueError("empty torrent")
    return {"hash": hash_v1 or hash_v2[:40], "hash_v1": hash_v1, "hash_v2": hash_v2,
            "size_gb": size / 1024.0 ** 3}


def qb_find_hash(session, metadata):
    base = CONFIG["qb_base_url"].rstrip("/")
    response = session.get(f"{base}/api/v2/torrents/info", params={"hashes": metadata["hash"]}, timeout=20)
    response.raise_for_status()
    values = response.json()
    if not isinstance(values, list):
        raise ValueError("invalid torrent list")
    hashes = {metadata.get("hash"), metadata.get("hash_v1"), metadata.get("hash_v2"), str(metadata.get("hash_v2", ""))[:40]} - {"", None}
    return next((item for item in values if str(item.get("hash", "")).lower() in hashes), None)


def current_keep_gb(session):
    torrents = qb_all_torrents(session)
    total = sum(
        safe_int(t.get("size")) for t in torrents
        if is_hhan_torrent(t) and t.get("category") == CONFIG["category_keep"]
    )
    return total / (1024.0 ** 3)


def execute_actions(session, to_download, to_mark_delete):
    print("[4/4] 执行 V6 操作（下载完成前不以新种收益清退老种）...")
    base = CONFIG["qb_base_url"].rstrip("/")
    # One fresh PRE-add snapshot; no dependence on immediate post-add visibility.
    before = qb_all_torrents(session)
    keep = [t for t in before if is_hhan_torrent(t) and t.get("category") == CONFIG["category_keep"]]
    completed = {str(t["hash"]).lower(): t for t in keep if torrent_is_complete(t)}
    downloading_gb = sum(torrent_size_gb(t) for t in keep if not torrent_is_complete(t))
    pending = [r for r in load_cache().get("records", []) if not r.get("present_in_qb") and r.get("add_status") in ("submitted", "uncertain")]
    visible = {str(t.get("hash", "")).lower() for t in before}
    downloading_gb += sum(safe_float(r.get("size_gb")) for r in pending if str(r.get("hash", "")).lower() not in visible)
    buffer_limit = max(0.0, safe_float(CONFIG.get("max_downloading_space_gb"), 250))
    quota = safe_float(CONFIG["max_preservation_space_gb"])
    payload = load_cache()
    payload["strategy_state"] = dict(LAST_STRATEGY_CONTEXT.get("strategy_state", payload.get("strategy_state", {})))
    save_cache(payload)
    added, known_added_gb = 0, 0.0
    # Do not execute a stale deletion plan after a concurrent manual/category change.
    expected = set(LAST_STRATEGY_CONTEXT.get("completed_hashes", completed))
    if set(completed) != expected:
        print("      当前已完成组合已变化，本轮取消操作；下一轮重新计算。")
        return
    for candidate in to_download:
        try:
            torrent_bytes = curl_download_torrent(candidate["id"])
            metadata = torrent_metadata(torrent_bytes)
        except (ValueError, TypeError, KeyError):
            print(f"      #{candidate['id']} 种子数据无效，未提交")
            continue
        item = dict(candidate, **metadata)
        if metadata["hash"] in visible:
            item["add_status"] = "existing"
            upsert_candidate_cache(item)
            print(f"      #{candidate['id']} hash 已存在，已合并身份")
            continue
        if metadata["size_gb"] > safe_float(CONFIG["max_single_download_gb"]) + 1e-9:
            print(f"      #{candidate['id']} 实际大小超过单种上限，跳过")
            continue
        if downloading_gb + known_added_gb + metadata["size_gb"] > buffer_limit + 1e-9:
            print(f"      #{candidate['id']} 实际大小超过剩余下载缓冲，跳过")
            continue
        # Persist the identity before submitting, so interrupted/ambiguous requests cannot churn.
        item["add_status"] = "uncertain"
        upsert_candidate_cache(item)
        try:
            response = session.post(f"{base}/api/v2/torrents/add",
                files={"torrents": (f"{candidate['id']}.torrent", torrent_bytes, "application/x-bittorrent")},
                data={"category": CONFIG["category_keep"], "autoTMM": "true", "paused": "false"}, timeout=30)
            success = response.status_code == 200 and response.text.strip().lower() == "ok."
            if not success:
                try:
                    result = response.json()
                    success = response.status_code == 200 and isinstance(result, dict) and safe_int(result.get("success_count")) + safe_int(result.get("pending_count")) > 0
                except ValueError:
                    pass
            if success:
                item["add_status"] = "submitted"
                added += 1
                known_added_gb += metadata["size_gb"]
                visible.add(metadata["hash"])
            else:
                existing = qb_find_hash(session, metadata)
                item["add_status"] = "existing" if existing else "failed"
                print(f"      #{candidate['id']} {'已存在并合并' if existing else '未确认加入，进入重试冷却'}（HTTP {response.status_code}）")
        except (requests.RequestException, ValueError):
            item["add_status"] = "uncertain"
            known_added_gb += metadata["size_gb"]
            print(f"      #{candidate['id']} 提交结果待确认，预留空间并暂停重复提交")
        upsert_candidate_cache(item)
    selected = []
    floor = safe_float(LAST_STRATEGY_CONTEXT.get("value_floor"))
    actual_values = {}
    for h, torrent in completed.items():
        record = find_cache_record(payload.get("records", []), torrent_hash=h) or {}
        curr = safe_int(torrent.get("num_complete"), -1)
        if curr < 0:
            curr = safe_int(record.get("curr_seeders"), 1)
        actual_values[h] = calc_preservation_metrics(torrent_size_gb(torrent),
            age_weeks_from_time(record.get("published_time")), curr, record.get("init_seeders"))["portfolio_value"]
    remaining_value = sum(actual_values.values())
    for item in to_mark_delete:
        h = str(item["hash"]).lower()
        if h not in completed:
            continue
        if item.get("reason") != "OVERQUOTA" and remaining_value - actual_values[h] < floor - 1e-6:
            print("      实时收益约束不满足，停止空间清退")
            break
        selected.append(item)
        remaining_value -= actual_values[h]
    marked = 0
    if selected:
        response = session.post(f"{base}/api/v2/torrents/setCategory",
            data={"hashes": "|".join(x["hash"] for x in selected), "category": CONFIG["category_del"]}, timeout=20)
        if response.status_code == 200:
            marked = len(selected)
            updated = load_cache()
            for selected_item in selected:
                record = find_cache_record(updated.get("records", []), torrent_hash=selected_item["hash"])
                if record is not None:
                    record.update({"active": False, "category": CONFIG["category_del"], "last_category": CONFIG["category_del"]})
            save_cache(updated)
            print(f"      {marked} 个已完成任务转为【{CONFIG['category_del']}】，分类配额释放 {sum(safe_float(x['size_gb']) for x in selected):.2f} GB；文件清理由用户处理")
    print(f"执行完成：新增 {added} 个 / 已知预留 {known_added_gb:.2f} GB，转分类 {marked} 个。")


def apply_config_overrides(overrides, parser):
    for override in overrides:
        if "=" not in override:
            parser.error("--set 必须为 KEY=VALUE")
        key, raw = override.split("=", 1)
        if key not in CONFIG:
            parser.error(f"未知配置项: {key}")
        old = CONFIG[key]
        try:
            if isinstance(old, bool):
                if raw.lower() not in ("true", "false"):
                    raise ValueError()
                value = raw.lower() == "true"
            elif isinstance(old, int):
                value = int(raw)
            elif isinstance(old, float):
                value = float(raw)
                if not math.isfinite(value):
                    raise ValueError()
            else:
                value = raw
        except ValueError:
            parser.error(f"配置项 {key} 的类型不正确")
        CONFIG[key] = value
    for key in ("max_preservation_space_gb", "max_downloading_space_gb", "max_batch_download_gb", "max_batch_download_count", "space_reward_reference_value", "space_min_saving_gb"):
        if safe_float(CONFIG.get(key)) < 0:
            parser.error(f"配置项 {key} 不能为负数")
    if not 0 <= safe_float(CONFIG.get("space_reward_loss_ratio")) < 1:
        parser.error("space_reward_loss_ratio 必须在 [0, 1) 内")
    if not 0 < safe_float(CONFIG.get("space_optimization_start_ratio")) <= 1:
        parser.error("space_optimization_start_ratio 必须在 (0, 1] 内")


def main():
    global CACHE_READ_ONLY, LAST_STRATEGY_CONTEXT
    parser = argparse.ArgumentParser(description="HHanClub 保种区自动化综合管理")
    parser.add_argument(
        "--execute", action="store_true", help="实际执行下载和分类变更；默认仅 dry-run"
    )
    parser.add_argument(
        "--refresh-cache",
        action="store_true",
        help="强制刷新 HHan action=7 并与 qB 对账，只更新缓存后退出",
    )
    parser.add_argument(
        "--sync", action="store_true", help="兼容旧参数：等同 --refresh-cache"
    )
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="dry-run 临时配置覆盖，可重复；不修改配置或账本")
    args = parser.parse_args()
    if args.set and (args.execute or args.refresh_cache or args.sync):
        parser.error("--set 仅用于 dry-run，不能与 --execute/--refresh-cache/--sync 组合")

    load_config()
    apply_config_overrides(args.set, parser)
    CACHE_READ_ONLY = not (args.execute or args.refresh_cache or args.sync)
    LAST_STRATEGY_CONTEXT = {}
    session = get_qb_session()

    cache_only = bool(args.refresh_cache or args.sync)
    cache_payload, qb_torrents, cache_stats = refresh_cache(
        session, force_remote=cache_only
    )
    if cache_only:
        print(
            f"缓存刷新完成：{cache_stats['cache_records']} 条；"
            f"当前【{CONFIG['category_keep']}】 {cache_stats['active_keep']} 个；"
            f"远端更新时间 {cache_stats.get('remote_updated_at') or '-'}"
        )
        return

    existing_names, keep_gb, current_items, unmatched = check_qb_status(
        qb_torrents, cache_payload
    )
    candidates, zone_stats = fetch_rescue_candidates(existing_names)
    (
        to_download,
        download_gb,
        to_mark_delete,
        marked_gb,
        _projected,
        portfolio_stats,
    ) = generate_strategy(candidates, keep_gb, current_items)

    dry_run = not args.execute
    print_strategy_report(
        to_download,
        download_gb,
        to_mark_delete,
        marked_gb,
        keep_gb,
        zone_stats,
        portfolio_stats,
        unmatched,
        cache_stats,
        dry_run,
    )

    if args.execute:
        execute_actions(session, to_download, to_mark_delete)
    else:
        print("DRY-RUN：确认策略后运行 python hhan_pzone_manager.py --execute")


if __name__ == "__main__":
    from hhan_pzone_manager import core as entry_core
    entry_core.main()

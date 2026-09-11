#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HHanClub preservation-zone automation manager.

V4 cache model:
- user_preservation_cache.json is the only persistent ledger.
- Every run reconciles the ledger against current qBittorrent HHan tasks.
- Remote HHan action=7 history is refreshed automatically when stale.
- If the cache file is missing, it is rebuilt automatically from action=7 + qB.
- Current preservation torrents and new candidates use the same value model and a 0/1 knapsack.
"""

import argparse
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
CACHE_VERSION = 4

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
        return float(value)
    except Exception:
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
    size_gb = max(0.0, safe_float(size_gb))
    age_weeks = max(0.1, safe_float(age_weeks, 0.1))
    curr_seeders = max(1, safe_int(curr_seeders, 1))
    init_seeders = safe_int(init_seeders)

    time_factor = 1.0 - math.pow(10.0, -age_weeks / 8.0)
    seeder_factor = 1.0 + math.sqrt(2.0) * math.pow(
        10.0, -max(0.0, curr_seeders - 1.0) / 9.0
    )
    multiplier = preservation_multiplier(init_seeders)
    value_per_gb = time_factor * seeder_factor * multiplier
    portfolio_value = size_gb * value_per_gb
    dpi = 1.0 / max(1e-12, value_per_gb)
    return {
        "time_factor": time_factor,
        "seeder_factor": seeder_factor,
        "multiplier": multiplier,
        "value_per_gb": value_per_gb,
        "portfolio_value": portfolio_value,
        "dpi": dpi,
    }


def age_weeks_from_time(value, default=16.0):
    if not value:
        return default
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(text, fmt)
            return max(0.1, (datetime.now() - dt).total_seconds() / 604800.0)
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
        "-s", "-k", "--compressed",
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
        "-s", "-k",
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

    if not title:
        return None
    a = normalize_name(record.get("title") or record.get("name") or "")
    b = normalize_name(title)
    if not a or not b or not (a == b or a in b or b in a):
        return None

    rec_size = safe_float(record.get("size_gb"))
    req_size = safe_float(size_gb)
    if rec_size > 0 and req_size > 0:
        delta = abs(rec_size - req_size) / max(rec_size, req_size)
        if delta >= 0.05:
            return None
    else:
        delta = 1.0
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
    size_gb = safe_float(record.get("size_gb"))
    init_n = safe_int(record.get("init_seeders"))
    curr_n = safe_int(record.get("curr_seeders"), 1)
    age_weeks = safe_float(record.get("age_weeks"))
    if age_weeks <= 0:
        age_weeks = age_weeks_from_time(record.get("completed_time"), 16.0)
    record["age_weeks"] = round(max(0.1, age_weeks), 3)
    metrics = calc_preservation_metrics(size_gb, age_weeks, curr_n, init_n)
    record["priority_score"] = round(metrics["dpi"], 6)
    record["dpi"] = round(metrics["dpi"], 6)
    record["value_per_gb"] = round(metrics["value_per_gb"], 6)
    record["portfolio_value"] = round(metrics["portfolio_value"], 6)


def merge_record(records, source_record, source_name):
    title = source_record.get("title") or source_record.get("name") or ""
    torrent_id = source_record.get("torrent_id") or ""
    torrent_hash = source_record.get("hash") or ""
    size_gb = safe_float(source_record.get("size_gb"))
    record = find_cache_record(
        records,
        torrent_id=torrent_id or None,
        torrent_hash=torrent_hash or None,
        title=title,
        size_gb=size_gb,
    )
    if record is None:
        record = {}
        records.append(record)

    for field in ("torrent_id", "hash", "title", "name", "size_gb", "completed_time", "added_on"):
        value = source_record.get(field)
        if value not in (None, "", 0, 0.0):
            record[field] = value

    init_n = safe_int(source_record.get("init_seeders"))
    if init_n > 0:
        record["init_seeders"] = init_n

    curr_n = safe_int(source_record.get("curr_seeders"))
    if curr_n > 0:
        record["curr_seeders"] = curr_n

    age_weeks = safe_float(source_record.get("age_weeks"))
    if age_weeks > 0:
        record["age_weeks"] = age_weeks
    elif source_record.get("completed_time") and safe_float(record.get("age_weeks")) <= 0:
        record["age_weeks"] = age_weeks_from_time(source_record.get("completed_time"))

    record["source"] = source_name
    normalize_record_metrics(record)
    return record


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
        print("[缓存] user_id 未配置，跳过 HHan 远端档案刷新。")
        return []

    print("[缓存] 正在从 HHanClub action=7 刷新保种档案...")
    started = datetime.now()

    def parse_row(tds):
        torrent_id = tds[0].get_text(strip=True)
        title_a = tds[1].find("a")
        title = title_a.get_text(strip=True) if title_a else tds[1].get_text(strip=True)
        size_gb = parse_size_to_gb(tds[2].get_text(strip=True))
        init_n = safe_int(tds[3].get_text(strip=True))
        curr_n = safe_int(tds[4].get_text(strip=True))
        completed_time = tds[5].get_text(strip=True)
        age_weeks = age_weeks_from_time(completed_time)
        return {
            "torrent_id": torrent_id,
            "title": title,
            "name": title,
            "size_gb": round(size_gb, 3),
            "init_seeders": init_n,
            "curr_seeders": curr_n,
            "completed_time": completed_time,
            "age_weeks": round(age_weeks, 3),
        }

    def fetch_page(page):
        html = curl_get_hhan(f"userdetails.php?action=7&id={uid}&page={page}")
        if not html:
            return []
        soup = BeautifulSoup(html, "html.parser")
        tables = soup.find_all("table")
        if not tables:
            return []
        rows = []
        for row in tables[0].find_all("tr")[1:]:
            tds = row.find_all("td")
            if len(tds) >= 6:
                try:
                    rows.append(parse_row(tds))
                except Exception:
                    pass
        return rows

    records = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        for page_records in executor.map(fetch_page, range(25)):
            records.extend(page_records)

    elapsed = (datetime.now() - started).total_seconds()
    print(f"      远端档案: {len(records)} 条，耗时 {elapsed:.1f}s")
    return records


def merge_remote_into_cache(payload, remote_records):
    records = payload.setdefault("records", [])
    for item in remote_records:
        merge_record(records, item, "remote_action7")
    if remote_records:
        payload["remote_updated_at"] = iso_now()


def reconcile_cache_with_qb(payload, qb_torrents):
    """Reconcile ledger with qB on every run.

    A task manually moved from 待删除 back to 保种 becomes active on the next run
    and therefore re-enters the global portfolio. Tasks that exist in qB but do
    not have action=7 metadata remain unknown and are protected when
    protect_unknown_records=true.
    """
    records = payload.setdefault("records", [])
    now = iso_now()

    for record in records:
        record["present_in_qb"] = False
        record["active"] = False

    hhan_count = 0
    active_count = 0
    restored_count = 0
    unknown_count = 0

    for torrent in qb_torrents:
        if not is_hhan_torrent(torrent):
            continue
        hhan_count += 1
        title = torrent.get("name", "")
        torrent_hash = torrent.get("hash", "")
        size_gb = safe_float(torrent.get("size")) / (1024.0 ** 3)
        category = torrent.get("category", "") or ""
        record = find_cache_record(
            records,
            torrent_hash=torrent_hash,
            title=title,
            size_gb=size_gb,
        )
        if record is None:
            record = {
                "hash": torrent_hash,
                "title": title,
                "name": title,
                "size_gb": round(size_gb, 3),
                "init_seeders": 0,
                "curr_seeders": max(1, safe_int(torrent.get("num_complete"), 1)),
                "age_weeks": 16.0,
                "source": "qb_discovered",
            }
            records.append(record)
            unknown_count += 1

        record["hash"] = torrent_hash
        record["title"] = title
        record["name"] = title
        record["size_gb"] = round(size_gb, 3)
        record["category"] = category
        record["state"] = torrent.get("state", "")
        record["progress"] = round(safe_float(torrent.get("progress")) * 100.0, 3)
        record["present_in_qb"] = True
        record["last_seen"] = now

        qb_seeders = safe_int(torrent.get("num_complete"))
        if qb_seeders > 0:
            record["curr_seeders"] = qb_seeders

        is_active = category == CONFIG["category_keep"]
        record["active"] = is_active
        if is_active:
            active_count += 1
            record["last_active"] = now
            if record.get("last_category") == CONFIG["category_del"]:
                restored_count += 1
        record["last_category"] = category
        normalize_record_metrics(record)

    payload["qb_reconciled_at"] = now
    return {
        "hhan_in_qb": hhan_count,
        "active_keep": active_count,
        "restored": restored_count,
        "unknown": unknown_count,
        "cache_records": len(records),
    }


def refresh_cache(session, force_remote=False, quiet=False):
    cache_missing = not os.path.exists(CACHE_PATH)
    payload = load_cache()

    if cache_missing and not quiet:
        print("[缓存] 未检测到 user_preservation_cache.json，将从 action=7 + 当前 qB 自动重建。")

    # Missing cache always forces a fresh action=7 fetch; no legacy file is read.
    should_refresh_remote = remote_refresh_due(payload, force=(force_remote or cache_missing))
    remote_refreshed = False
    if should_refresh_remote:
        remote_records = fetch_remote_preservation_records()
        if remote_records:
            merge_remote_into_cache(payload, remote_records)
            remote_refreshed = True
        elif cache_missing:
            print("[缓存] ⚠️ action=7 未取得记录，将仅根据当前 qB 建账；未知任务会按保护策略处理。")
        elif force_remote:
            print("[缓存] 强制远端刷新未取得记录，保留现有账本。")

    qb_torrents = qb_all_torrents(session)
    stats = reconcile_cache_with_qb(payload, qb_torrents)
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
    print("[1/4] 正在读取 qBittorrent 当前保种任务...")
    existing_names = {normalize_name(t.get("name", "")) for t in qb_torrents}
    keep = [
        t for t in qb_torrents
        if is_hhan_torrent(t) and t.get("category") == CONFIG["category_keep"]
    ]
    current_keep_gb = sum(safe_int(t.get("size")) for t in keep) / (1024.0 ** 3)
    records = cache_payload.get("records", [])

    items = []
    unmatched = 0
    for torrent in keep:
        size_gb = safe_float(torrent.get("size")) / (1024.0 ** 3)
        record = find_cache_record(
            records,
            torrent_hash=torrent.get("hash"),
            title=torrent.get("name", ""),
            size_gb=size_gb,
        )
        init_n = safe_int(record.get("init_seeders")) if record else 0
        curr_n = (
            safe_int(record.get("curr_seeders"), 1)
            if record else max(1, safe_int(torrent.get("num_complete"), 1))
        )
        age_weeks = safe_float(record.get("age_weeks"), 16.0) if record else 16.0

        if init_n <= 0:
            unmatched += 1
        protected = False
        protect_reason = ""
        if CONFIG.get("protect_init_one", True) and init_n == 1:
            protected = True
            protect_reason = "初始1人硬保护"
        elif CONFIG.get("protect_unknown_records", True) and init_n <= 0:
            protected = True
            protect_reason = "action=7元数据未知"

        metrics = calc_preservation_metrics(size_gb, age_weeks, curr_n, init_n)
        items.append({
            "key": f"existing:{torrent['hash']}",
            "source": "existing",
            "hash": torrent["hash"],
            "title": torrent.get("name", ""),
            "name": torrent.get("name", ""),
            "size_gb": size_gb,
            "init_seeders": init_n,
            "curr_seeders": curr_n,
            "age_weeks": age_weeks,
            "value_per_gb": metrics["value_per_gb"],
            "portfolio_value": metrics["portfolio_value"],
            "dpi": metrics["dpi"],
            "protected": protected,
            "protect_reason": protect_reason,
        })

    print(
        f"      当前【{CONFIG['category_keep']}】专项配额占用: "
        f"{current_keep_gb:.2f} GB / {safe_float(CONFIG['max_preservation_space_gb']):.2f} GB"
    )
    return existing_names, current_keep_gb, items, unmatched


def fetch_rescue_candidates(existing_names):
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

    for card in cards:
        link = card.find("a", class_=re.compile(r"torrent-info-text-name"))
        if not link:
            continue
        title = link.get_text(strip=True)
        m = re.search(r"id=(\d+)", link.get("href", ""))
        if not m:
            continue
        torrent_id = m.group(1)
        size_div = card.find("div", class_=re.compile(r"torrent-info-text-size"))
        size_gb = parse_size_to_gb(size_div.get_text(strip=True) if size_div else "")
        seed_div = card.find("div", class_=re.compile(r"torrent-info-text-seeders"))
        seeders = safe_int(seed_div.get_text(strip=True) if seed_div else 0)

        added_div = card.find("div", class_=re.compile(r"torrent-info-text-added"))
        span = added_div.find("span") if added_div else None
        added_text = span.get("title", "") if span else ""
        age_weeks = 16.0
        if added_text:
            try:
                dt = datetime.strptime(added_text.strip(), "%Y-%m-%d %H:%M:%S")
                age_weeks = max(0.1, (now - dt).total_seconds() / 604800.0)
            except ValueError:
                pass

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
            "age_weeks": round(age_weeks, 3),
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

def knapsack_select(items, capacity_gb, unit_gb):
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
    records = payload.setdefault("records", [])
    record = find_cache_record(
        records,
        torrent_id=candidate["id"],
        title=candidate["title"],
        size_gb=candidate["size_gb"],
    )
    if record is None:
        record = {}
        records.append(record)
    record.update({
        "torrent_id": str(candidate["id"]),
        "title": candidate["title"],
        "name": candidate["title"],
        "size_gb": candidate["size_gb"],
        "init_seeders": candidate["seeders"],
        "curr_seeders": candidate["seeders"],
        "age_weeks": candidate.get("age_weeks", 0.1),
        "completed_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "source": "download_candidate",
        "active": True,
        "category": CONFIG["category_keep"],
        "last_active": iso_now(),
        "last_seen": iso_now(),
    })
    normalize_record_metrics(record)
    save_cache(payload)


def current_keep_gb(session):
    torrents = qb_all_torrents(session)
    total = sum(
        safe_int(t.get("size")) for t in torrents
        if is_hhan_torrent(t) and t.get("category") == CONFIG["category_keep"]
    )
    return total / (1024.0 ** 3)


def execute_actions(session, to_download, to_mark_delete):
    print("[4/4] 正在执行本轮操作...")
    base = CONFIG["qb_base_url"].rstrip("/")
    added = 0

    for candidate in to_download:
        torrent_bytes = curl_download_torrent(candidate["id"])
        if not torrent_bytes or len(torrent_bytes) < 100:
            print(f"      ❌ #{candidate['id']} 种子文件下载失败")
            continue
        files = {
            "torrents": (
                f"{candidate['id']}.torrent",
                torrent_bytes,
                "application/x-bittorrent",
            )
        }
        data = {
            "category": CONFIG["category_keep"],
            "autoTMM": "true",
            "paused": "false",
        }
        response = session.post(
            f"{base}/api/v2/torrents/add", files=files, data=data, timeout=30
        )
        if response.status_code == 200 and "fails" not in response.text.lower():
            added += 1
            upsert_candidate_cache(candidate)
            print(f"      ✅ 已加入 #{candidate['id']} {candidate['title'][:55]}")
        else:
            print(
                f"      ❌ #{candidate['id']} 加入 qB 失败: "
                f"HTTP {response.status_code} {response.text.strip()}"
            )
        time.sleep(0.5)

    marked = 0
    if to_mark_delete:
        try:
            required_free = max(
                0.0,
                current_keep_gb(session) - safe_float(CONFIG["max_preservation_space_gb"]),
            )
        except Exception as exc:
            print(f"      ⚠️ 无法复核 qB 配额，跳过退出旧任务: {exc}")
            required_free = 0.0

        selected, selected_gb = [], 0.0
        for item in to_mark_delete:
            if selected_gb + 1e-9 >= required_free:
                break
            selected.append(item)
            selected_gb += safe_float(item["size_gb"])

        if selected:
            hashes = "|".join(item["hash"] for item in selected)
            response = session.post(
                f"{base}/api/v2/torrents/setCategory",
                data={"hashes": hashes, "category": CONFIG["category_del"]},
                timeout=20,
            )
            if response.status_code == 200:
                marked = len(selected)
                print(
                    f"      ✅ {marked} 个旧任务转为【{CONFIG['category_del']}】，"
                    f"退出专项配额约 {selected_gb:.2f} GB"
                )
            else:
                print(f"      ❌ 分类变更失败: HTTP {response.status_code}")

    try:
        refresh_cache(session, force_remote=False, quiet=True)
    except Exception as exc:
        print(f"      ⚠️ 执行后缓存对账失败: {exc}")
    print(f"执行完成：新增 {added} 个，转为【{CONFIG['category_del']}】 {marked} 个。")


def main():
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
    args = parser.parse_args()

    load_config()
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
    main()

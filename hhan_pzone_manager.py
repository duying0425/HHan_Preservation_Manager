#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HHanClub preservation-zone automation manager.

Design goals:
- Treat max_preservation_space_gb as a dedicated qBittorrent category quota, not total disk usage.
- Reuse existing preservation history JSON/cache data.
- Evaluate current preservation torrents and new candidates with one value model.
- Use a 0/1 knapsack to select the highest-value portfolio under the quota.
- Converge toward that target gradually using per-run download limits.
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
}


def load_config():
    path = os.path.join(BASE_DIR, "config.json")
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        CONFIG.update(data)
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


def preservation_multiplier(init_seeders):
    init_seeders = int(init_seeders or 0)
    if init_seeders == 1:
        return 2.00
    if 2 <= init_seeders <= 3:
        return 1.75
    if 4 <= init_seeders <= 5:
        return 1.50
    return 1.00


def calc_preservation_metrics(size_gb, age_weeks, curr_seeders, init_seeders):
    """Return the unified value metrics used by both existing and new torrents."""
    size_gb = max(0.0, float(size_gb or 0.0))
    age_weeks = max(0.1, float(age_weeks or 0.1))
    curr_seeders = max(1, int(float(curr_seeders or 1)))
    init_seeders = int(float(init_seeders or 0))

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


def sync_user_preservation_records(force_sync=False, max_age_hours=24.0):
    active_path = os.path.join(BASE_DIR, "hhan_active_preservation.json")
    cache_path = os.path.join(BASE_DIR, "user_preservation_cache.json")
    csv_path = os.path.join(BASE_DIR, "preservation_torrents_ranked.csv")

    if not force_sync and os.path.exists(active_path):
        try:
            with open(active_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("records"):
                return data["records"]
        except Exception:
            pass

    if not force_sync and os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            updated_at = datetime.fromisoformat(data.get("updated_at", "2000-01-01"))
            age_hours = (datetime.now() - updated_at).total_seconds() / 3600.0
            if age_hours < max_age_hours and data.get("records"):
                return data["records"]
        except Exception:
            pass

    if not force_sync and os.path.exists(csv_path):
        import csv
        try:
            with open(csv_path, "r", encoding="utf-8-sig") as f:
                return list(csv.DictReader(f))
        except Exception:
            pass

    uid = str(CONFIG.get("user_id", "") or "").strip()
    if not uid:
        print("[同步] user_id 未配置，无法在线同步历史档案；本轮按空历史处理。")
        return []

    print("[同步] 正在从 HHanClub 在线同步保种档案...")
    now = datetime.now()

    def parse_row(tds):
        torrent_id = tds[0].get_text(strip=True)
        title_a = tds[1].find("a")
        title = title_a.get_text(strip=True) if title_a else tds[1].get_text(strip=True)
        size_gb = parse_size_to_gb(tds[2].get_text(strip=True))
        try:
            init_n = int(tds[3].get_text(strip=True))
        except Exception:
            init_n = 0
        try:
            curr_n = int(tds[4].get_text(strip=True))
        except Exception:
            curr_n = 0
        completed_time = tds[5].get_text(strip=True)
        age_weeks = age_weeks_from_time(completed_time)
        metrics = calc_preservation_metrics(size_gb, age_weeks, curr_n, init_n)
        return {
            "torrent_id": torrent_id,
            "title": title,
            "size_gb": round(size_gb, 3),
            "init_seeders": init_n,
            "curr_seeders": curr_n,
            "completed_time": completed_time,
            "age_weeks": round(age_weeks, 3),
            "priority_score": round(metrics["dpi"], 6),
            "dpi": round(metrics["dpi"], 6),
            "value_per_gb": round(metrics["value_per_gb"], 6),
            "portfolio_value": round(metrics["portfolio_value"], 6),
        }

    def fetch_page(page):
        html = curl_get_hhan(f"userdetails.php?action=7&id={uid}&page={page}")
        if not html:
            return []
        soup = BeautifulSoup(html, "html.parser")
        tables = soup.find_all("table")
        if not tables:
            return []
        records = []
        for row in tables[0].find_all("tr")[1:]:
            tds = row.find_all("td")
            if len(tds) >= 6:
                try:
                    records.append(parse_row(tds))
                except Exception:
                    pass
        return records

    records = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        for page_records in executor.map(fetch_page, range(25)):
            records.extend(page_records)

    if records:
        payload = {"updated_at": now.isoformat(), "records": records}
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"      [同步完成] {len(records)} 条历史档案")
    else:
        print("      [同步提示] 未读取到历史档案")
    return records


def match_history_record(qb_torrent, history_records):
    q_name = normalize_name(qb_torrent.get("name", ""))
    q_size_gb = float(qb_torrent.get("size", 0) or 0) / (1024.0 ** 3)
    best = None
    best_score = None
    for record in history_records or []:
        r_name = normalize_name(record.get("title", ""))
        try:
            r_size_gb = float(record.get("size_gb", 0) or 0)
        except Exception:
            continue
        if r_size_gb <= 0:
            continue
        size_delta = abs(q_size_gb - r_size_gb) / max(r_size_gb, 1e-9)
        if size_delta >= 0.05:
            continue
        if not (q_name == r_name or q_name in r_name or r_name in q_name):
            continue
        score = (0 if q_name == r_name else 1, size_delta)
        if best_score is None or score < best_score:
            best = record
            best_score = score
    return best


def check_qb_status(session, history_records):
    print("[1/4] 正在读取 qBittorrent 当前保种任务...")
    torrents = qb_all_torrents(session)
    existing_names = {normalize_name(t.get("name", "")) for t in torrents}
    keep = [
        t for t in torrents
        if is_hhan_torrent(t) and t.get("category") == CONFIG["category_keep"]
    ]
    current_keep_gb = sum(int(t.get("size", 0) or 0) for t in keep) / (1024.0 ** 3)

    items = []
    unmatched = 0
    for torrent in keep:
        size_gb = float(torrent.get("size", 0) or 0) / (1024.0 ** 3)
        record = match_history_record(torrent, history_records)
        if record:
            init_n = int(float(record.get("init_seeders", 0) or 0))
            curr_n = int(float(record.get("curr_seeders", 0) or 0))
            try:
                age_weeks = float(record.get("age_weeks", 0) or 0)
            except Exception:
                age_weeks = 0.0
            if age_weeks <= 0:
                age_weeks = age_weeks_from_time(record.get("completed_time"))
            protected = bool(CONFIG.get("protect_init_one", True) and init_n == 1)
            protect_reason = "初始1人硬保护" if protected else ""
        else:
            unmatched += 1
            init_n = 0
            curr_n = max(1, int(torrent.get("num_complete", 1) or 1))
            age_weeks = 16.0
            protected = bool(CONFIG.get("protect_unknown_records", True))
            protect_reason = "历史档案未匹配" if protected else ""

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
        f"{current_keep_gb:.2f} GB / {float(CONFIG['max_preservation_space_gb']):.2f} GB"
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
        try:
            seeders = int(seed_div.get_text(strip=True)) if seed_div else 0
        except Exception:
            seeders = 0

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
        if seeders < int(CONFIG["min_candidate_seeders"]):
            stats["dead_count"] += 1
            continue
        if seeders > int(CONFIG["max_candidate_seeders"]):
            stats["crowded_count"] += 1
            continue
        if size_gb <= 0 or size_gb > float(CONFIG["max_single_download_gb"]):
            stats["huge_count"] += 1
            continue

        metrics = calc_preservation_metrics(size_gb, age_weeks, seeders, seeders)
        if seeders == 1:
            tier = "Tier 0 (1人独种)"
        elif seeders == 2:
            tier = "Tier 1 (2人良种)"
        else:
            tier = f"Tier {seeders - 1} ({seeders}人)"

        candidates.append({
            "id": torrent_id,
            "title": title,
            "size_gb": round(size_gb, 3),
            "seeders": seeders,
            "age_weeks": round(age_weeks, 3),
            "tier": tier,
            "score_pts": round(metrics["value_per_gb"], 6),
            "value_per_gb": round(metrics["value_per_gb"], 6),
            "portfolio_value": round(metrics["portfolio_value"], 6),
        })

    candidates.sort(
        key=lambda x: (x["value_per_gb"], x["portfolio_value"]), reverse=True
    )
    stats["eligible_count"] = len(candidates)
    print(
        f"      扫描 {pages} 页，保种区 {len(cards)} 个；"
        f"合格候选 {len(candidates)} 个"
    )
    return candidates, stats


def knapsack_select(items, capacity_gb, unit_gb):
    """0/1 knapsack maximizing portfolio_value under the dedicated space quota."""
    if not items or capacity_gb <= 0:
        return []
    unit_gb = max(0.05, float(unit_gb or 0.1))
    capacity = int(math.floor(float(capacity_gb) / unit_gb + 1e-12))
    if capacity <= 0:
        return []

    filtered = []
    weights = []
    values = []
    for item in items:
        size_gb = max(0.0, float(item.get("size_gb", 0) or 0))
        value = max(0.0, float(item.get("portfolio_value", 0) or 0))
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
    max_space = float(CONFIG["max_preservation_space_gb"])
    unit_gb = float(CONFIG.get("portfolio_unit_gb", 0.1))

    protected = [item for item in current_items if item.get("protected")]
    optional_existing = [item for item in current_items if not item.get("protected")]
    protected_gb = sum(float(item["size_gb"]) for item in protected)

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
            "size_gb": float(candidate["size_gb"]),
            "init_seeders": int(candidate["seeders"]),
            "curr_seeders": int(candidate["seeders"]),
            "age_weeks": float(candidate.get("age_weeks", 16.0)),
            "value_per_gb": float(candidate["value_per_gb"]),
            "portfolio_value": float(candidate["portfolio_value"]),
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
    batch_gb = float(CONFIG.get("max_batch_download_gb", 200.0))
    batch_count = int(CONFIG.get("max_batch_download_count", 10))
    selected_batch = []
    download_gb = 0.0
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
    to_mark_delete = []
    marked_gb = 0.0
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

    current_value = sum(float(x.get("portfolio_value", 0)) for x in current_items)
    target_value = sum(float(x.get("portfolio_value", 0)) for x in target)
    stats = {
        "protected_count": len(protected),
        "protected_gb": protected_gb,
        "target_count": len(target),
        "target_existing_count": len(target_existing),
        "target_new_count": len(target_new),
        "target_space_gb": sum(float(x["size_gb"]) for x in target),
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
    dry_run,
):
    max_space = float(CONFIG["max_preservation_space_gb"])
    mode = "DRY-RUN" if dry_run else "EXECUTE"
    print("\n" + "=" * 88)
    print(f"HHanClub 保种区策略报告 [{mode}]")
    print("=" * 88)
    print(
        f"保种区: {zone_stats['total_in_zone']} 个 | "
        f"已有 {zone_stats['already_have']} | "
        f"0人 {zone_stats['dead_count']} | "
        f"超人数阈值 {zone_stats['crowded_count']} | "
        f"超体积阈值 {zone_stats['huge_count']} | "
        f"候选 {zone_stats['eligible_count']}"
    )
    print(
        f"当前【{CONFIG['category_keep']}】专项配额: {current_keep_gb:.2f} / "
        f"{max_space:.2f} GB ({current_keep_gb / 1024.0:.2f} TB)"
    )
    print(
        f"全局目标组合: {portfolio_stats['target_count']} 个 / "
        f"{portfolio_stats['target_space_gb']:.2f} GB | "
        f"预测价值 {portfolio_stats['current_value']:.2f} -> "
        f"{portfolio_stats['target_value']:.2f} "
        f"(Δ {portfolio_stats['value_gain']:+.2f})"
    )
    print(
        f"硬保护: {portfolio_stats['protected_count']} 个 / "
        f"{portfolio_stats['protected_gb']:.2f} GB | "
        f"历史未匹配: {unmatched_count} 个"
    )

    print(f"\n[+] 本轮新增: {len(to_download)} 个 / {download_gb:.2f} GB")
    for idx, item in enumerate(to_download, 1):
        print(
            f"  {idx:>2}. {item['size_gb']:>7.2f} GB | "
            f"{item['seeders']}人 | 价值/GB {item['value_per_gb']:.3f} | "
            f"{item['title']}"
        )

    print(
        f"\n[-] 本轮退出【{CONFIG['category_keep']}】专项配额: "
        f"{len(to_mark_delete)} 个 / {marked_gb:.2f} GB"
    )
    for idx, item in enumerate(to_mark_delete, 1):
        print(
            f"  {idx:>2}. {item['size_gb']:>7.2f} GB | "
            f"价值/GB {item['value_per_gb']:.3f} | {item['name']}"
        )

    final_estimate = current_keep_gb + download_gb - marked_gb
    print(
        f"\n本轮执行后预计专项配额: {final_estimate:.2f} GB / {max_space:.2f} GB"
    )
    if portfolio_stats["unresolved_overflow_gb"] > 0.01:
        print(
            f"⚠️ 仍有 {portfolio_stats['unresolved_overflow_gb']:.2f} GB "
            "无法释放，请检查硬保护项和历史匹配。"
        )
    print(f"背包离散粒度: {portfolio_stats['unit_gb']:.2f} GB")
    print("=" * 88 + "\n")


def append_candidate_to_cache(candidate):
    path = os.path.join(BASE_DIR, "user_preservation_cache.json")
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        else:
            payload = {"updated_at": datetime.now().isoformat(), "records": []}
        records = payload.setdefault("records", [])
        torrent_id = str(candidate["id"])
        records[:] = [r for r in records if str(r.get("torrent_id", "")) != torrent_id]
        metrics = calc_preservation_metrics(
            candidate["size_gb"],
            candidate.get("age_weeks", 0.1),
            candidate["seeders"],
            candidate["seeders"],
        )
        records.append({
            "torrent_id": torrent_id,
            "title": candidate["title"],
            "size_gb": candidate["size_gb"],
            "init_seeders": candidate["seeders"],
            "curr_seeders": candidate["seeders"],
            "age_weeks": candidate.get("age_weeks", 0.1),
            "completed_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "priority_score": round(metrics["dpi"], 6),
            "dpi": round(metrics["dpi"], 6),
            "value_per_gb": round(metrics["value_per_gb"], 6),
            "portfolio_value": round(metrics["portfolio_value"], 6),
        })
        payload["updated_at"] = datetime.now().isoformat()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        print(f"      ⚠️ 更新本地缓存失败: {exc}")


def current_keep_gb(session):
    torrents = qb_all_torrents(session)
    total = sum(
        int(t.get("size", 0) or 0)
        for t in torrents
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
            append_candidate_to_cache(candidate)
            print(f"      ✅ 已加入 #{candidate['id']} {candidate['title'][:40]}")
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
                current_keep_gb(session) - float(CONFIG["max_preservation_space_gb"]),
            )
        except Exception as exc:
            print(f"      ⚠️ 无法复核 qB 配额，跳过退出旧任务: {exc}")
            required_free = 0.0

        selected = []
        selected_gb = 0.0
        for item in to_mark_delete:
            if selected_gb + 1e-9 >= required_free:
                break
            selected.append(item)
            selected_gb += float(item["size_gb"])

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

    print(f"执行完成：新增 {added} 个，转为【{CONFIG['category_del']}】 {marked} 个。")


def main():
    parser = argparse.ArgumentParser(description="HHanClub 保种区自动化综合管理")
    parser.add_argument(
        "--execute", action="store_true", help="实际执行下载和分类变更；默认仅 dry-run"
    )
    parser.add_argument(
        "--sync", action="store_true", help="强制从 action=7 重新同步历史保种档案"
    )
    args = parser.parse_args()

    load_config()
    history = sync_user_preservation_records(force_sync=args.sync) or []
    session = get_qb_session()
    existing_names, keep_gb, current_items, unmatched = check_qb_status(session, history)
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
        dry_run,
    )

    if args.execute:
        execute_actions(session, to_download, to_mark_delete)
    else:
        print("DRY-RUN：确认策略后运行 python hhan_pzone_manager.py --execute")


if __name__ == "__main__":
    main()

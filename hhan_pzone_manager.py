#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=============================================================================
HHanClub 保种区自动化综合管理流水线脚本 (hhan_pzone_manager.py)
=============================================================================
核心策略原则：
 1. 【宁缺毋滥，拒绝平庸】：
    - 绝不下载 0 人死种（无做种源，无法完成下载，7天会被系统惩罚取消名额）；
    - 绝不下载 4~5 人满员种（只有 1.5x 低倍率，且濒临踢出保种区）；
    - 仅在保种区出现 1~2 人极品神种 (Tier 0 / Tier 1，锁定 2.0x/1.75x 积分) 时才建议下载。
 2. 【4TB 容量水位管理】：
    - 设定保种分类总空间预算为 4.0 TB (4,096 GB)；
    - 当前占用 < 4TB 且无极品新种时：静默守护，不下载、不删除；
    - 当前占用 < 4TB 且有极品新种时：直接下载吸纳，无需淘汰老种；
    - 当（当前占用 + 新增下载）超出 4TB 时：按评分/DPI 从最差到最好，精准淘汰最劣质老种标记为“待删除”，将总容量压回 4TB 以内。
=============================================================================
"""

import sys
import os
import re
import json
import math
import time
import argparse
import subprocess
import shutil
from datetime import datetime
from bs4 import BeautifulSoup
import requests
import urllib3

if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==================== 认证与全局配置 ====================
CONFIG = {
    # HHanClub 认证信息 (推荐在同级目录下 config.json 中配置)
    "hhan_base_url": "https://hhanclub.net",
    "hhan_cookie": "",
    "hhan_passkey": "",
    "hhan_ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "user_id": "",
    
    # qBittorrent WebUI 认证信息
    "qb_base_url": "http://127.0.0.1:8080",
    "qb_cookie": "",
    "category_keep": "保种",
    "category_del": "待删除",

    # 【核心策略参数】
    "max_preservation_space_gb": 4096.0, # 保留上限 4.0 TB (超出后按评分删除)
    "max_candidate_seeders": 3,          # 极品/优质选种阈值: 下载 1~3 人种子 (宁缺毋滥，拒绝4~5人)
    "min_candidate_seeders": 1,          # 绝不下载 0 人死种
    "max_single_download_gb": 80.0,      # 单个种子最大体积 (超大包排除)
    "max_batch_download_gb": 200.0,      # 单批次最多下载的总体积
}

# 动态加载本地 config.json（凭据与策略分离保护）
_config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
if os.path.exists(_config_path):
    try:
        with open(_config_path, "r", encoding="utf-8") as _f:
            _loaded = json.load(_f)
            CONFIG.update(_loaded)
    except Exception as _e:
        print(f"⚠️ 加载 {_config_path} 异常: {_e}")

# ==================== 基础辅助工具 ====================
def parse_size_to_gb(size_str: str) -> float:
    m = re.search(r"([\d\.]+)\s*([KMGTP]?B)", size_str, re.I)
    if not m:
        return 0.0
    val, unit = float(m.group(1)), m.group(2).upper()
    if unit == "TB": return val * 1024.0
    if unit == "GB": return val
    if unit == "MB": return val / 1024.0
    if unit == "KB": return val / (1024.0 * 1024.0)
    return val

def normalize_name(s: str) -> str:
    return re.sub(r'[\.\-_\s\[\]\(\)]+', ' ', s.lower()).strip()

def curl_get_hhan(path: str, max_retries: int = 3) -> str:
    url = f"{CONFIG['hhan_base_url']}/{path.lstrip('/')}"
    curl_bin = shutil.which("curl") or "curl"
    cmd = [
        curl_bin, "--noproxy", "*", "-s", "-k", "--compressed",
        "-H", f"User-Agent: {CONFIG['hhan_ua']}",
        "-H", f"Cookie: {CONFIG['hhan_cookie']}",
        url
    ]
    for attempt in range(max_retries):
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore")
        if res.stdout and len(res.stdout) > 1000:
            return res.stdout
        time.sleep(1.0)
    return res.stdout if res.stdout else ""

def curl_download_torrent(t_id: str, max_retries: int = 3) -> bytes:
    url = f"{CONFIG['hhan_base_url']}/download.php?id={t_id}&passkey={CONFIG['hhan_passkey']}"
    curl_bin = shutil.which("curl") or "curl"
    cmd = [
        curl_bin, "--noproxy", "*", "-s", "-k",
        "-H", f"User-Agent: {CONFIG['hhan_ua']}",
        "-H", f"Cookie: {CONFIG['hhan_cookie']}",
        url
    ]
    for attempt in range(max_retries):
        res = subprocess.run(cmd, capture_output=True)
        if res.stdout and len(res.stdout) > 100:
            return res.stdout
        time.sleep(1.0)
    return res.stdout if res.stdout else b""

# ==================== 模块 1: 扫描保种区候选种子 ====================
def fetch_rescue_candidates(existing_qb_names: set) -> tuple:
    print("[1/4] 正在抓取 HHanClub 保种区 (rescue.php) 种子列表 (支持多页扫描)...")
    cards = []
    seen_page_ids = set()
    page = 0
    max_pages = 20  # 安全翻页上限

    while page < max_pages:
        url_path = f"rescue.php?page={page}"
        html = curl_get_hhan(url_path)
        if not html:
            break
        soup = BeautifulSoup(html, "html.parser")
        page_cards = soup.find_all("div", class_=re.compile(r"torrent-table-sub-info"))
        if not page_cards:
            break

        current_ids = []
        for c in page_cards:
            name_a = c.find("a", class_=re.compile(r"torrent-info-text-name"))
            if name_a:
                m = re.search(r"id=(\d+)", name_a.get("href", ""))
                if m:
                    current_ids.append(m.group(1))

        # 防死循环检测：如果整页ID均已被抓取，说明已越界回环，立即终止
        new_ids = set(current_ids) - seen_page_ids
        if not new_ids and page > 0:
            break

        seen_page_ids.update(new_ids)
        cards.extend(page_cards)

        # 若当页卡片数显著少于常规单页容量，说明已至末页
        if len(page_cards) < 30 and page > 0:
            page += 1
            break
        page += 1

    print(f"      [多页检索完成] 共检索 {page} 页，累计获取 {len(cards)} 个保种区种子。")
    total_in_zone = len(cards)
    dead_count = 0
    crowded_count = 0
    huge_count = 0
    already_have = 0
    high_value_candidates = []
    now = datetime.now()

    for c in cards:
        name_a = c.find("a", class_=re.compile(r"torrent-info-text-name"))
        if not name_a:
            continue
        title = name_a.get_text(strip=True)
        m = re.search(r"id=(\d+)", name_a.get("href", ""))
        t_id = m.group(1) if m else ""

        # 体积
        size_div = c.find("div", class_=re.compile(r"torrent-info-text-size"))
        size_str = size_div.get_text(strip=True) if size_div else ""
        size_gb = parse_size_to_gb(size_str)

        # 当前做种人数
        seed_div = c.find("div", class_=re.compile(r"torrent-info-text-seeders"))
        try:
            seeders = int(seed_div.get_text(strip=True)) if seed_div else 0
        except:
            seeders = 0

        # 发布时间
        added_div = c.find("div", class_=re.compile(r"torrent-info-text-added"))
        span_added = added_div.find("span") if added_div else None
        added_str = span_added.get("title", "") if span_added else ""
        age_weeks = 16.0
        if added_str:
            try:
                dt = datetime.strptime(added_str.strip(), "%Y-%m-%d %H:%M:%S")
                age_weeks = max(0.1, (now - dt).total_seconds() / (86400.0 * 7.0))
            except:
                pass

        # 统计过滤原因
        norm_t = normalize_name(title)
        if norm_t in existing_qb_names or any(norm_t in qn or qn in norm_t for qn in existing_qb_names if len(qn) > 12):
            already_have += 1
            continue
        if seeders < CONFIG["min_candidate_seeders"]:
            dead_count += 1
            continue
        if size_gb > CONFIG["max_single_download_gb"] or size_gb <= 0:
            huge_count += 1
            continue
        if seeders > CONFIG["max_candidate_seeders"]:
            crowded_count += 1
            continue

        # 满足极品/优质选种条件 (1~3 人, 体积适中, 有源存活)
        time_factor = 1.0 - math.pow(10.0, -age_weeks / 8.0)
        seeder_factor = 1.0 + math.sqrt(2.0) * math.pow(10.0, -max(0.0, (seeders - 1.0)) / 9.0)
        m_pts = 2.00 if seeders == 1 else (1.75 if seeders <= 3 else 1.50)
        size_bonus = 1.0 + (2.0 / math.sqrt(max(1.0, size_gb)))
        score_pts = time_factor * seeder_factor * m_pts * size_bonus

        if seeders == 1:
            tier = "Tier 0 (1人独种)"
        elif seeders == 2:
            tier = "Tier 1 (2人良种)"
        else:
            tier = "Tier 2 (3人中品)"

        high_value_candidates.append({
            "id": t_id,
            "title": title,
            "size_gb": round(size_gb, 2),
            "seeders": seeders,
            "age_weeks": round(age_weeks, 1),
            "score_pts": round(score_pts, 3),
            "tier": tier
        })

    high_value_candidates.sort(key=lambda x: x["score_pts"], reverse=True)
    summary_dict = {
        "total_in_zone": total_in_zone,
        "dead_count": dead_count,
        "crowded_count": crowded_count,
        "huge_count": huge_count,
        "already_have": already_have,
        "eligible_count": len(high_value_candidates)
    }
    return high_value_candidates, summary_dict

# ==================== 模块 2: 检查 qB 做种状态与劣质种 ====================
def get_qb_session():
    s = requests.Session()
    s.cookies.set("SID", CONFIG["qb_cookie"].split("=")[-1], domain="qb.tmhcorps.cn")
    s.verify = False
    return s

def check_qb_status(qb_session, ranked_records_list: list):
    print("[2/4] 正在连接 qBittorrent 获取做种状态并评估劣质种子...")
    r = qb_session.get(f"{CONFIG['qb_base_url']}/api/v2/torrents/info")
    if r.status_code != 200:
        raise Exception(f"qBittorrent 连接失败，状态码: {r.status_code}")
    
    all_torrents = r.json()
    hhan_torrents = [t for t in all_torrents if "hhan" in t.get("tracker", "").lower()]
    existing_names = set(normalize_name(t["name"]) for t in all_torrents)

    # 仅针对分类为 "保种" 的任务计算当前占用容量并排查劣质种
    keep_category_torrents = [t for t in hhan_torrents if t.get("category") == CONFIG["category_keep"]]
    current_keep_bytes = sum(t["size"] for t in keep_category_torrents)
    current_keep_gb = current_keep_bytes / (1024.0 ** 3)

    prune_candidates = []
    for qt in keep_category_torrents:
        q_norm = normalize_name(qt["name"])
        q_size_gb = qt["size"] / (1024.0 ** 3)

        rec = None
        for r_item in ranked_records_list:
            r_norm = normalize_name(r_item["title"])
            r_size_gb = float(r_item["size_gb"])
            if abs(q_size_gb - r_size_gb) / max(1e-4, r_size_gb) < 0.05:
                if q_norm == r_norm or q_norm in r_norm or r_norm in q_norm:
                    rec = r_item
                    break

        if rec:
            init_n = int(rec.get("init_seeders", 0))
            curr_n = int(rec.get("curr_seeders", 0))
            p_score = float(rec.get("priority_score", 0.0))
            # 严禁删除初始 1 人神种 (Level 5 传家宝)
            if init_n == 1:
                continue
            # 劣质淘汰池：
            # 1. 初始 4~5 人 (享受 1.5x 最低保底倍率)
            # 2. 初始 2~3 人但当前严重拥挤 (>= 20人)
            if init_n in [4, 5]:
                prune_candidates.append({
                    "hash": qt["hash"],
                    "name": qt["name"],
                    "size_gb": round(q_size_gb, 2),
                    "init_seeders": init_n,
                    "curr_seeders": curr_n,
                    "priority_score": p_score,
                    "reason": f"初始{init_n}人(1.5x低倍率) + 当前{curr_n}人" + ("拥挤" if curr_n >= 9 else "")
                })
            elif init_n in [2, 3] and curr_n >= 20:
                prune_candidates.append({
                    "hash": qt["hash"],
                    "name": qt["name"],
                    "size_gb": round(q_size_gb, 2),
                    "init_seeders": init_n,
                    "curr_seeders": curr_n,
                    "priority_score": p_score,
                    "reason": f"初始{init_n}人 + 当前{curr_n}人严重拥挤"
                })

    # 按劣质分值降序排列 (最该删的排在最前面)
    prune_candidates.sort(key=lambda x: x["priority_score"], reverse=True)
    return existing_names, current_keep_gb, prune_candidates, len(keep_category_torrents)

# ==================== 模块 3: 容量水位平衡对策引擎 ====================
def generate_strategy(download_candidates: list, current_keep_gb: float, prune_candidates: list):
    max_space = CONFIG["max_preservation_space_gb"]
    max_space_tb = max_space / 1024.0
    print(f"[3/4] 正在根据 {max_space_tb:.1f}TB 水位规则计算下载与删除对策...")

    # 1. 挑选真正值得下载的极品神种 (Tier 0 / Tier 1, N <= 2)
    to_download = []
    dl_total_gb = 0.0
    for c in download_candidates:
        if dl_total_gb + c["size_gb"] <= CONFIG["max_batch_download_gb"]:
            to_download.append(c)
            dl_total_gb += c["size_gb"]
        if len(to_download) >= 10:
            break

    # 2. 计算预计总空间 (当前保种空间 + 拟下载空间)
    projected_total_gb = current_keep_gb + dl_total_gb

    # 3. 容量水位管理
    to_mark_delete = []
    freed_total_gb = 0.0

    if projected_total_gb > max_space:
        # 超出水位上限！必须按评分淘汰最劣质种子将空间压回上限内
        overflow_gb = projected_total_gb - max_space
        for p in prune_candidates:
            if freed_total_gb < overflow_gb:
                to_mark_delete.append(p)
                freed_total_gb += p["size_gb"]
            else:
                break
    else:
        # 未超出水位上限：完全不需要删除任何种子！
        pass

    return to_download, dl_total_gb, to_mark_delete, freed_total_gb, projected_total_gb

def print_strategy_report(to_download, dl_total_gb, to_mark_delete, freed_total_gb, current_keep_gb, projected_total_gb, zone_stats, dry_run=True):
    mode_str = "[DRY-RUN 试运行模式 - 仅分析对策，不修改 qB / 不下载]" if dry_run else "[EXECUTE 执行模式 - 正在实际执行变更与下载]"
    max_space = CONFIG["max_preservation_space_gb"]
    
    print("\n" + "=" * 85)
    print(f"                 HHanClub 保种区对策分析报告 {mode_str}")
    print("=" * 85)

    # 0. 保种区深度诊断
    print("\n🔍 保种区现场深度诊断 (为什么目前不建议盲目下载？):")
    print(f"   • 保种区当前总计种子数: {zone_stats['total_in_zone']} 个")
    print(f"   • 0人做种的死种/断种: {zone_stats['dead_count']} 个 (⚠️ 严重警告: 无做种源，下载必卡死，7天被系统踢出！)")
    print(f"   • 4~5人满员/濒临踢出种: {zone_stats['crowded_count']} 个 (⚠️ 仅1.5x最低倍率，且濒临5人上限，不值得占用配额)")
    print(f"   • 单包超标大种 (>80GB): {zone_stats['huge_count']} 个 (太臃肿，空间利用率低)")
    print(f"   • 真正值得下入的 1~3人优质种子: {zone_stats['eligible_count']} 个")

    # 1. 待下载对策
    print(f"\n[+] 一、 推荐下载的保种区极品/优质种子 (共 {len(to_download)} 个，总计 {dl_total_gb:.2f} GB):")
    if not to_download:
        print("   ✅ 【当前无需下载任何种子】")
        print("   原因：当前保种区中无 1~3 人存活种子。宁缺毋滥，拒绝下载 0 人死种与 4~5 人平庸种！")
        print("   提示：保种区每天下午 14:00 大更新，建议将脚本设在 14:05 运行，即可抢到热气腾腾的 1~2 人顶格神种！")
    else:
        print(f"   {'序号':<4} {'等级':<16} {'体积':<10} {'做种人数':<8} {'评分':<8} {'种子名称'}")
        print("   " + "-" * 78)
        for idx, c in enumerate(to_download, 1):
            print(f"   {idx:<4} {c['tier']:<16} {c['size_gb']:<8.2f}GB {c['seeders']:<8} {c['score_pts']:<8.2f} {c['title'][:44]}")

    # 2. 待删除对策
    print(f"\n[-] 二、 推荐变更分类为【{CONFIG['category_del']}】的劣质做种任务 (共 {len(to_mark_delete)} 个，预计腾出 {freed_total_gb:.2f} GB):")
    max_space_tb = max_space / 1024.0
    if not to_mark_delete:
        print("   ✅ 【当前无需删除任何种子】")
        print(f"   原因：当前【保种】分类占用 {current_keep_gb:.2f} GB (约 {current_keep_gb/1024.0:.2f} TB)，未超过 {max_space_tb:.1f} TB 预算上限，余量充裕！")
    else:
        print(f"   {'序号':<4} {'体积':<10} {'初始人数':<8} {'当前人数':<8} {'原因说明':<22} {'种子名称'}")
        print("   " + "-" * 78)
        for idx, p in enumerate(to_mark_delete, 1):
            print(f"   {idx:<4} {p['size_gb']:<8.2f}GB {p['init_seeders']:<8} {p['curr_seeders']:<8} {p['reason']:<22} {p['name'][:38]}")

    # 3. 水位与效益评估
    final_space_gb = current_keep_gb + dl_total_gb - freed_total_gb
    print("\n" + "-" * 85)
    print(f"📊 三、 {max_space_tb:.1f}TB 水位与综合效益看板:")
    print(f"   • 当前【保种】占用容量: {current_keep_gb:.2f} GB ({current_keep_gb/1024.0:.2f} TB)")
    print(f"   • 设定最大容量预算: {max_space:.2f} GB ({max_space_tb:.2f} TB)")
    print(f"   • 预算剩余可用空间: {max_space - current_keep_gb:.2f} GB (约 {(max_space - current_keep_gb)/1024.0:.2f} TB)")
    print(f"   • 新增下载占用: +{dl_total_gb:.2f} GB")
    print(f"   • 淘汰劣质释放: -{freed_total_gb:.2f} GB")
    print(f"   • 执行后预计总占用: {final_space_gb:.2f} GB ({final_space_gb/1024.0:.2f} TB / {max_space_tb:.2f} TB，完美受控！)")
    print("=" * 85 + "\n")

# ==================== 模块 4: 执行引擎 ====================
def execute_actions(qb_session, to_download, to_mark_delete):
    print("[4/4] 正在执行对策操作...")

    # 1. 淘汰处理
    if to_mark_delete:
        hashes = "|".join(p["hash"] for p in to_mark_delete)
        res = qb_session.post(
            f"{CONFIG['qb_base_url']}/api/v2/torrents/setCategory",
            data={"hashes": hashes, "category": CONFIG["category_del"]}
        )
        print(f"      [分类变更] 已成功将 {len(to_mark_delete)} 个种子分类更新为【{CONFIG['category_del']}】 (API状态: {res.status_code})")

    # 2. 下载推送
    added_count = 0
    for c in to_download:
        t_id = c["id"]
        t_title = c["title"]
        print(f"      [下载推送] 正在获取种子文件: #{t_id} ({t_title[:30]}...)...", end="")
        torrent_bytes = curl_download_torrent(t_id)
        if not torrent_bytes or len(torrent_bytes) < 100 or not (b"announce" in torrent_bytes[:100]):
            print(" ❌ 下载失败")
            continue
        
        files = {
            "torrents": (f"{t_id}.torrent", torrent_bytes, "application/x-bittorrent")
        }
        data = {
            "category": CONFIG["category_keep"],
            "autoTMM": "true",
            "paused": "false"
        }
        res_add = qb_session.post(f"{CONFIG['qb_base_url']}/api/v2/torrents/add", files=files, data=data)
        if res_add.status_code == 200 and "fails" not in res_add.text.lower():
            print(f" [OK] 已推入 qB 并启用自动管理模式！({res_add.text.strip()})")
            added_count += 1
            # 立即将新下载种子注册入本地缓存，实时锁定初始人数
            try:
                base_dir = os.path.dirname(os.path.abspath(__file__))
                cache_path = os.path.join(base_dir, "user_preservation_cache.json")
                if os.path.exists(cache_path):
                    with open(cache_path, "r", encoding="utf-8") as f:
                        cdata = json.load(f)
                    cdata.setdefault("records", []).append({
                        "torrent_id": t_id,
                        "title": t_title,
                        "size_gb": c["size_gb"],
                        "init_seeders": c["seeders"], # 下载时的做种人数即为锁定的初始人数
                        "curr_seeders": c["seeders"],
                        "priority_score": -500.0,      # 新下种子进入保护状态，不可淘汰
                        "dpi": 0.0
                    })
                    with open(cache_path, "w", encoding="utf-8") as f:
                        json.dump(cdata, f, ensure_ascii=False, indent=2)
            except Exception:
                pass
        else:
            print(f" [FAIL] 推送失败 (状态: {res_add.status_code}, 响应: {res_add.text.strip()})")
        time.sleep(0.5)

    print(f"\n🎉 执行全部完成！共推送新增 {added_count} 个神种，标记 {len(to_mark_delete)} 个待删除任务。")

# ==================== 保种档案在线同步与缓存机制 ====================
def sync_user_preservation_records(base_dir: str, force_sync: bool = False, max_age_hours: float = 24.0) -> list:
    active_path = os.path.join(base_dir, "hhan_active_preservation.json")
    cache_path = os.path.join(base_dir, "user_preservation_cache.json")
    csv_path = os.path.join(base_dir, "preservation_torrents_ranked.csv")

    # 1. 优先读取已核实纯净的当前有效在保数据集 (hhan_active_preservation.json)
    if not force_sync and os.path.exists(active_path):
        try:
            with open(active_path, "r", encoding="utf-8") as f:
                active_data = json.load(f)
            if active_data.get("records"):
                return active_data["records"]
        except Exception:
            pass

    # 2. 检查全量主账本缓存是否有效且未过期
    if not force_sync and os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cache_data = json.load(f)
            updated_dt = datetime.fromisoformat(cache_data.get("updated_at", "2000-01-01"))
            age_h = (datetime.now() - updated_dt).total_seconds() / 3600.0
            if age_h < max_age_hours and cache_data.get("records"):
                return cache_data["records"]
        except Exception:
            pass

    # 若有旧 CSV 且未要求强制联网，作为快速回退
    if not force_sync and os.path.exists(csv_path):
        import csv
        try:
            with open(csv_path, "r", encoding="utf-8-sig") as f:
                return list(csv.DictReader(f))
        except Exception:
            pass

    # 2. 实时联网全量同步 (userdetails.php?action=7)
    print(f"[同步] 正在从 HHanClub 实时在线同步您的全部保种档案 (userdetails.php?action=7)...")
    from concurrent.futures import ThreadPoolExecutor
    now = datetime.now()

    def parse_row(tds):
        t_id = tds[0].get_text(strip=True)
        title_a = tds[1].find("a")
        title = title_a.get_text(strip=True) if title_a else tds[1].get_text(strip=True)
        size_str = tds[2].get_text(strip=True)
        size_gb = parse_size_to_gb(size_str)
        try: init_n = int(tds[3].get_text(strip=True))
        except: init_n = 0
        try: curr_n = int(tds[4].get_text(strip=True))
        except: curr_n = 0
        comp_time = tds[5].get_text(strip=True)
        age_weeks = 16.0
        try:
            dt = datetime.strptime(comp_time, "%Y-%m-%d %H:%M")
            age_weeks = max(0.1, (now - dt).total_seconds() / (86400.0 * 7.0))
        except: pass

        time_factor = 1.0 - math.pow(10.0, -age_weeks / 8.0)
        curr_seeder_factor = 1.0 + math.sqrt(2.0) * math.pow(10.0, -max(0.0, (curr_n - 1.0)) / 9.0)
        e_base = time_factor * curr_seeder_factor

        if init_n == 1: m_pts = 2.00
        elif 2 <= init_n <= 3: m_pts = 1.75
        elif 4 <= init_n <= 5: m_pts = 1.50
        else: m_pts = 1.00

        yield_pts_per_gb = e_base * m_pts
        dpi = 1.0 / max(1e-4, yield_pts_per_gb)

        if init_n == 1: p_score = -1000.0
        elif size_gb <= 0.5: p_score = -500.0
        elif init_n in [4, 5] and curr_n >= 20 and size_gb >= 20.0: p_score = dpi * (size_gb ** 0.5)
        elif init_n in [4, 5] and curr_n >= 10 and size_gb >= 15.0: p_score = dpi * (size_gb ** 0.5)
        elif init_n in [4, 5]: p_score = dpi * 0.8
        elif init_n in [2, 3] and curr_n >= 30 and size_gb >= 40.0: p_score = dpi * 0.7
        elif init_n in [2, 3]: p_score = dpi * 0.1
        else: p_score = dpi * 0.2

        return {
            "torrent_id": t_id,
            "title": title,
            "size_gb": round(size_gb, 3),
            "init_seeders": init_n,
            "curr_seeders": curr_n,
            "completed_time": comp_time,
            "priority_score": round(p_score, 3),
            "dpi": round(dpi, 3)
        }

    uid = CONFIG["user_id"]
    def fetch_page(p):
        html = curl_get_hhan(f"userdetails.php?action=7&id={uid}&page={p}")
        if not html: return []
        soup = BeautifulSoup(html, "html.parser")
        tables = soup.find_all("table")
        if not tables: return []
        rows = tables[0].find_all("tr")
        return [parse_row(r.find_all("td")) for r in rows[1:] if len(r.find_all("td")) >= 6]

    all_records = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        pages_data = list(ex.map(fetch_page, range(25)))
    for pd in pages_data:
        all_records.extend(pd)

    if all_records:
        cache_data = {"updated_at": now.isoformat(), "records": all_records}
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)
        print(f"      [同步完成] 已成功在线同步并持久化缓存 {len(all_records)} 条做种档案！")

    return all_records

# ==================== 主入口 ====================
def main():
    parser = argparse.ArgumentParser(description="HHanClub 保种区自动化综合管理流水线脚本")
    parser.add_argument("--execute", action="store_true", help="实际执行分类变更与下载操作 (默认为 dry-run 试运行)")
    parser.add_argument("--sync", action="store_true", help="强制从站点全量在线重新同步用户的保种档案 (action=7)")
    args = parser.parse_args()

    dry_run = not args.execute

    base_dir = os.path.dirname(os.path.abspath(__file__))
    ranked_records_list = sync_user_preservation_records(base_dir, force_sync=args.sync)

    qb_s = get_qb_session()

    # 1. 检查 qB 状态
    existing_qb_names, current_keep_gb, prune_candidates, keep_count = check_qb_status(qb_s, ranked_records_list)

    # 2. 扫描保种区候选种 (多页自适应)
    download_candidates, zone_stats = fetch_rescue_candidates(existing_qb_names)

    # 3. 5TB 水位平衡对策
    to_download, dl_total_gb, to_mark_delete, freed_total_gb, projected_total_gb = generate_strategy(
        download_candidates, current_keep_gb, prune_candidates
    )

    # 4. 打印报告
    print_strategy_report(
        to_download, dl_total_gb, to_mark_delete, freed_total_gb, current_keep_gb, projected_total_gb, zone_stats, dry_run=dry_run
    )

    # 5. 执行
    if not dry_run:
        execute_actions(qb_s, to_download, to_mark_delete)
    else:
        print("💡 当前为 DRY-RUN 试运行模式。若确认以上对策无误，可运行命令开始实际执行：")
        print("   python hhan_pzone_manager.py --execute\n")

if __name__ == "__main__":
    main()

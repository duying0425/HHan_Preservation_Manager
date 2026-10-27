#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V6: completed-torrent quota, bounded-loss space optimization and download buffer.

The score remains a comparison proxy, not an exact HHan points prediction.
No settlement-day calibration, daily batch limit or midnight scheduler is used.
"""
import re
import hhan_pzone_core as core

V6_DEFAULTS = {
    "base_points_daily_cap": 1200.0,
    "rescue_points_daily_cap": 1800.0,
    "points_cap_guard_ratio": 0.99,
    "freeze_replacements_at_rescue_cap": True,
    "allow_free_space_downloads_at_cap": False,
    "score_age_t0_weeks": 8.0,
    "score_seeder_n0": 10.0,
}
for _key, _value in V6_DEFAULTS.items():
    core.CONFIG.setdefault(_key, _value)
core.CACHE_VERSION = 6
LAST_SITE_POINTS = {}


def calc_preservation_metrics(size_gb, age_weeks, curr_seeders, init_seeders):
    return core.calc_preservation_metrics(size_gb, age_weeks, curr_seeders, init_seeders)


def _parse_number(text):
    if not text:
        return None
    match = re.search(r"[-+]?\d[\d,]*(?:\.\d+)?", str(text))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def _parse_base_points_per_hour(html):
    if not html:
        return None
    soup = core.BeautifulSoup(html, "html.parser")
    markers = (
        "你当前每小时能获取",
        "你當前每小時能獲取",
        "You are currently getting",
    )
    values = []
    for div in soup.find_all("div"):
        text = div.get_text(" ", strip=True)
        for marker in markers:
            pos = text.find(marker)
            if pos < 0:
                continue
            value = _parse_number(text[pos + len(marker):])
            if value is not None:
                values.append(value)
            break
    if values:
        return values[-1]

    text = soup.get_text(" ", strip=True)
    for marker in markers:
        pattern = re.escape(marker) + r"[^0-9]{0,40}([0-9][0-9,]*(?:\.[0-9]+)?)"
        matches = re.findall(pattern, text, flags=re.I)
        if matches:
            try:
                return float(matches[-1].replace(",", ""))
            except ValueError:
                pass
    return None


def _last_rescue_settlement_value(html):
    if not html:
        return None
    soup = core.BeautifulSoup(html, "html.parser")
    for table in reversed(soup.find_all("table")):
        rows = table.find_all("tr")
        for row in reversed(rows):
            tds = row.find_all("td")
            if len(tds) < 6:
                continue
            value = _parse_number(tds[5].get_text(" ", strip=True))
            if value is not None:
                return value
    return None


def _fetch_rescue_settlement_daily():
    uid = str(core.CONFIG.get("user_id", "") or "").strip()
    if not uid:
        return None

    first = core.curl_get_hhan(f"rescuesettleinfo.php?id={uid}&page=0")
    if not first:
        return None

    soup = core.BeautifulSoup(first, "html.parser")
    pages = {0}
    for link in soup.find_all("a", href=True):
        match = re.search(r"(?:[?&]|^)page=(\d+)", link.get("href", ""))
        if match:
            pages.add(core.safe_int(match.group(1)))

    last_page = max(pages) if pages else 0
    html = first
    if last_page > 0:
        candidate = core.curl_get_hhan(
            f"rescuesettleinfo.php?id={uid}&page={last_page}"
        )
        if candidate:
            html = candidate
    return _last_rescue_settlement_value(html)


def fetch_site_points_snapshot():
    base_cap = max(
        0.0, core.safe_float(core.CONFIG.get("base_points_daily_cap"), 1200.0)
    )
    rescue_cap = max(
        0.0, core.safe_float(core.CONFIG.get("rescue_points_daily_cap"), 1800.0)
    )

    base_hour = None
    rescue_day = None
    try:
        base_hour = _parse_base_points_per_hour(core.curl_get_hhan("mybonus.php"))
    except Exception:
        base_hour = None
    try:
        rescue_day = _fetch_rescue_settlement_daily()
    except Exception:
        rescue_day = None

    base_day_raw = base_hour * 24.0 if base_hour is not None else None
    base_day = (
        min(base_day_raw, base_cap)
        if base_day_raw is not None and base_cap > 0
        else base_day_raw
    )
    rescue_day_capped = (
        min(rescue_day, rescue_cap)
        if rescue_day is not None and rescue_cap > 0
        else rescue_day
    )

    total = None
    if base_day is not None or rescue_day_capped is not None:
        total = (base_day or 0.0) + (rescue_day_capped or 0.0)

    guard_ratio = min(
        1.0,
        max(
            0.0,
            core.safe_float(core.CONFIG.get("points_cap_guard_ratio"), 0.99),
        ),
    )
    rescue_near_cap = bool(
        rescue_day is not None
        and rescue_cap > 0
        and rescue_day >= rescue_cap * guard_ratio
    )
    base_near_cap = bool(
        base_day_raw is not None
        and base_cap > 0
        and base_day_raw >= base_cap * guard_ratio
    )

    return {
        "base_hour": base_hour,
        "base_day_raw": base_day_raw,
        "base_day": base_day,
        "base_cap": base_cap,
        "base_near_cap": base_near_cap,
        "rescue_day": rescue_day,
        "rescue_day_capped": rescue_day_capped,
        "rescue_cap": rescue_cap,
        "rescue_near_cap": rescue_near_cap,
        "guard_ratio": guard_ratio,
        "total_day": total,
    }


def tier_label(init_seeders):
    n = core.safe_int(init_seeders)
    return "1人" if n == 1 else "2–3人" if 2 <= n <= 3 else "4–5人" if 4 <= n <= 5 else "未知/其他"


def _candidate_to_item(candidate):
    n = core.safe_int(candidate["seeders"])
    metrics = calc_preservation_metrics(candidate["size_gb"], candidate.get("age_weeks"), n, n)
    return {"key": f"new:{candidate['id']}", "source": "new", "id": candidate["id"],
            "title": candidate["title"], "name": candidate["title"],
            "size_gb": core.safe_float(candidate["size_gb"]), "init_seeders": n,
            "curr_seeders": n, "protected": False, **metrics}


def _delete_payload(item, reason="SPACE"):
    result = {k: item[k] for k in ("hash", "name", "size_gb", "init_seeders", "curr_seeders", "value_per_gb", "portfolio_value")}
    result["reason"] = reason
    return result


def _select_deletions(items, required_gb):
    selected, released = [], 0.0
    for item in sorted(items, key=lambda x: (x["value_per_gb"], -x.get("curr_seeders", 0), -x["size_gb"])):
        if released + 1e-9 >= required_gb:
            break
        selected.append(item)
        released += item["size_gb"]
    return selected, released


def _minimum_portfolio(protected, optional, capacity, floor):
    protected_gb = sum(x["size_gb"] for x in protected)
    protected_value = sum(x["portfolio_value"] for x in protected)
    if protected_gb > capacity + 1e-9:
        return None
    target = max(0.0, floor-protected_value)
    if target <= 1e-9:
        return list(protected)
    # Existing items win exact ties; among them, retain lower current seeder counts.
    ordered = sorted(optional, key=lambda x: (x.get("source") == "new", x.get("curr_seeders", 0)))
    selected = core.knapsack_select(ordered, capacity-protected_gb,
                                   core.CONFIG.get("portfolio_unit_gb", 0.1), target_value=target)
    result = list(protected) + selected
    if sum(x["portfolio_value"] for x in result) < floor - 1e-6:
        return None
    return result


def _batch_candidates(items, capacity):
    capacity = min(max(0.0, capacity), core.safe_float(core.CONFIG["max_batch_download_gb"]))
    count = max(0, core.safe_int(core.CONFIG["max_batch_download_count"]))
    if not count or capacity <= 0:
        return []
    selected = core.knapsack_select(items, capacity, core.CONFIG.get("portfolio_unit_gb", 0.1))
    return sorted(selected, key=lambda x: (x["value_per_gb"], x["portfolio_value"]), reverse=True)[:count]


def generate_strategy(candidates, current_keep_gb, current_items):
    global LAST_SITE_POINTS
    print("[3/4] V6 已完成组合与空间优化...")
    snapshot = fetch_site_points_snapshot()
    LAST_SITE_POINTS = snapshot
    context = core.LAST_PORTFOLIO_CONTEXT
    state = dict(context.get("strategy_state", {}))
    quota = core.safe_float(core.CONFIG["max_preservation_space_gb"])
    downloading_gb = core.safe_float(context.get("downloading_gb"))
    buffer_limit = core.safe_float(core.CONFIG["max_downloading_space_gb"])
    buffer_free = max(0.0, buffer_limit-downloading_gb)
    current_value = sum(x["portfolio_value"] for x in current_items)
    configured_reference = core.safe_float(core.CONFIG.get("space_reward_reference_value"))
    reference = configured_reference or (core.safe_float(state.get("reference_value")) if state.get("space_optimized") else max(current_value, core.safe_float(state.get("reference_value"))))
    if reference <= 0:
        reference = current_value
    floor = reference * (1.0-core.safe_float(core.CONFIG["space_reward_loss_ratio"]))
    state.update({"model_version": 6, "reference_value": reference})
    at_cap = bool(snapshot.get("rescue_near_cap")) and bool(core.CONFIG.get("freeze_replacements_at_rescue_cap", True))
    # Growth ends once a completed portfolio is established, or points reach the cap.
    # The persisted flag prevents space recovered by compression being blindly refilled.
    space_mode = bool(state.get("space_optimized")) or at_cap or (quota > 0 and current_keep_gb >= quota * core.safe_float(core.CONFIG["space_optimization_start_ratio"]))
    protected = [x for x in current_items if x.get("protected")]
    optional = [x for x in current_items if not x.get("protected")]
    new_items = [_candidate_to_item(c) for c in candidates]
    lookup = {str(c["id"]): c for c in candidates}
    deleted, batch, reasons = [], [], []
    mode = "GROW"
    retained = list(current_items)
    min_saving = core.safe_float(core.CONFIG["space_min_saving_gb"])

    if current_keep_gb > quota + 1e-9:
        mode = "OVERQUOTA"
        deleted, _ = _select_deletions(optional, current_keep_gb-quota)
        reasons.append("已完成任务超配额：本轮只释放、不新增；配额恢复优先于收益降幅")
        removed = {x["key"] for x in deleted}
        retained = [x for x in current_items if x["key"] not in removed]
    elif space_mode:
        mode = "SPACE-OPTIMIZE"
        state["space_optimized"] = True
        target = _minimum_portfolio(protected, optional, quota, floor)
        if target is not None and current_keep_gb-sum(x["size_gb"] for x in target) >= min_saving-1e-9:
            keys = {x["key"] for x in target}
            deleted = [x for x in optional if x["key"] not in keys]
            retained = target
        else:
            reasons.append("当前组合没有同时满足收益下限和最小节省空间的清退方案")
        retained_gb = sum(x["size_gb"] for x in retained)
        # Only completed torrents back today's deletion plan. New downloads are a
        # look-ahead opportunity and never contribute to current/planned value.
        if context.get("downloading_count", 0):
            reasons.append("已有下载/待确认任务：等待完成后再规划下一批空间置换")
        elif buffer_free <= 0:
            reasons.append("下载缓冲已满")
        elif new_items:
            future = _minimum_portfolio(protected, optional + new_items,
                                        max(0.0, retained_gb-min_saving), floor)
            if future is not None:
                wanted = [x for x in future if x.get("source") == "new"]
                batch = _batch_candidates(wanted, buffer_free)
                if not batch:
                    reasons.append("候选无法装入本轮下载缓冲或单批上限")
            else:
                reasons.append("候选不能在收益下限内进一步减少空间")
        if at_cap:
            reasons.append("CAP-GUARD：停止扩仓；允许收益约束内的空间压缩")
    else:
        growth_free = max(0.0, quota-current_keep_gb-downloading_gb)
        batch = _batch_candidates(new_items, min(growth_free, buffer_free))
        if not batch:
            reasons.append("没有可装入剩余配额、下载缓冲及单批上限的候选")
    planned_value = sum(x["portfolio_value"] for x in retained)
    marked_gb = sum(x["size_gb"] for x in deleted)
    dl_gb = sum(x["size_gb"] for x in batch)
    core.LAST_STRATEGY_CONTEXT = {
        "strategy_state": state, "value_floor": floor,
        "completed_hashes": [str(x["hash"]).lower() for x in current_items]}
    stats = {"strategy_mode": mode, "suppression_reason": "；".join(reasons),
             "site_points": snapshot, "current_value": current_value, "planned_value": planned_value,
             "planned_value_gain": planned_value-current_value, "reference_value": reference,
             "value_floor": floor, "protected_count": len(protected),
             "protected_gb": sum(x["size_gb"] for x in protected),
             "downloading_gb": downloading_gb, "downloading_count": context.get("downloading_count", 0),
             "unresolved_overflow_gb": max(0.0, current_keep_gb-marked_gb-quota),
             "completed_items": current_items}
    return ([lookup[str(x["id"])] for x in batch], dl_gb,
            [_delete_payload(x, "OVERQUOTA" if mode == "OVERQUOTA" else "SPACE") for x in deleted],
            marked_gb, current_keep_gb+dl_gb, stats)


def print_strategy_report(to_download, download_gb, to_mark_delete, marked_gb,
                          current_keep_gb, zone_stats, portfolio_stats,
                          unmatched_count, cache_stats, dry_run):
    stats = portfolio_stats
    print("\n" + "="*90)
    print(f"HHan V6 [{'DRY-RUN' if dry_run else 'EXECUTE'}] {stats['strategy_mode']}")
    print(f"已完成配额 {current_keep_gb:.2f} -> {current_keep_gb-marked_gb:.2f} / {core.CONFIG['max_preservation_space_gb']:.2f} GB")
    print(f"下载预留 {stats['downloading_gb']:.2f} + {download_gb:.2f} / {core.CONFIG['max_downloading_space_gb']:.2f} GB（完整大小；不计收益）")
    print(f"代理值 {stats['current_value']:.2f} -> {stats['planned_value']:.2f}；固定参考基准 {stats['reference_value']:.2f}；下限 {stats['value_floor']:.2f}")
    print(f"硬保护 {stats['protected_count']} 个 / {stats['protected_gb']:.2f} GB；元数据未知 {unmatched_count} 个")
    print(f"站点最近保种结算：{stats['site_points'].get('rescue_day')}（仅观测及封顶保护；不反推当前组合收益）")
    if stats['suppression_reason']:
        print(f"判断说明：{stats['suppression_reason']}")
    print(f"候选 {zone_stats['eligible_count']} 个；已有/重复过滤 {zone_stats['already_have']} 个")
    deleted_hashes = {x['hash'] for x in to_mark_delete}
    print("\n已完成任务（档位由 init 确定）：")
    for item in stats['completed_items']:
        action = '待清退' if item['hash'] in deleted_hashes else '保留'
        print(f"  {action} | 档位 {tier_label(item['init_seeders'])} | {item['size_gb']:.2f} GB | init={item['init_seeders']} curr={item['curr_seeders']} | 代理 {item['portfolio_value']:.2f} | {item['name']}" + (f" | {item['protect_reason']}" if item.get('protected') else ''))
    print(f"\n新增 {len(to_download)} 个 / {download_gb:.2f} GB：")
    for item in to_download:
        print(f"  #{item['id']} | 档位 {tier_label(item['seeders'])}（下载时估计，远端档案校正） | {item['size_gb']:.2f} GB | {item['title']}")
    print(f"转为【{core.CONFIG['category_del']}】{len(to_mark_delete)} 个 / {marked_gb:.2f} GB；分类变更不会立即释放磁盘空间。")
    if stats['unresolved_overflow_gb'] > 1e-6:
        print(f"仍超配额 {stats['unresolved_overflow_gb']:.2f} GB：受硬保护限制，需人工处理。")
    print("10% 默认降幅约束比较模型；不保证站点实际积分同比例变化。下载中异常任务由用户处理。")
    print("="*90)


core.generate_strategy = generate_strategy
core.print_strategy_report = print_strategy_report

if __name__ == "__main__":
    core.main()

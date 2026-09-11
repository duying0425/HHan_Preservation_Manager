#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HHanClub preservation manager V5.

V5 keeps the V4 cache/qB execution engine in hhan_pzone_core.py and replaces the
portfolio layer with a points-cap-aware policy:

- Keep user_preservation_cache.json reconciliation and manual hard protection.
- Preserve the existing HHan-oriented size/age/seeder proxy for comparing items.
- Observe HHan's base seeding points and rescue-zone settlement points when the
  site pages are available.
- Respect the documented daily caps (base 1200, rescue extra 1800).
- When rescue points are already near the cap, stop replacement churn; only use
  genuinely free quota space (or release quota if already over 4 TB).
- When below the cap, use the current rescue settlement to locally calibrate the
  proxy and stop a batch once the configured cap target is reached.

The generic NexusPHP B(A) parameters are intentionally NOT hard-coded as HHan
parameters because HHan's current public pages do not expose a complete set of
single-torrent formula parameters reliably. This avoids false precision.
"""

import math
import re

import hhan_pzone_core as core


V5_DEFAULTS = {
    "base_points_daily_cap": 1200.0,
    "rescue_points_daily_cap": 1800.0,
    "points_cap_guard_ratio": 0.99,
    "freeze_replacements_at_rescue_cap": True,
    "allow_free_space_downloads_at_cap": True,
    "min_replacement_gain_ratio": 0.02,
    "score_age_t0_weeks": 8.0,
    "score_seeder_n0": 10.0,
}
for _key, _value in V5_DEFAULTS.items():
    core.CONFIG.setdefault(_key, _value)
core.CACHE_VERSION = 5


LAST_SITE_POINTS = {}


def calc_preservation_metrics(size_gb, age_weeks, curr_seeders, init_seeders):
    """Return the comparison proxy used to rank preservation items."""
    size_gb = max(0.0, core.safe_float(size_gb))
    age_weeks = max(0.1, core.safe_float(age_weeks, 0.1))
    curr_seeders = max(1, core.safe_int(curr_seeders, 1))
    init_seeders = core.safe_int(init_seeders)

    t0 = max(0.1, core.safe_float(core.CONFIG.get("score_age_t0_weeks"), 8.0))
    n0 = max(1.01, core.safe_float(core.CONFIG.get("score_seeder_n0"), 10.0))

    time_factor = 1.0 - math.pow(10.0, -age_weeks / t0)
    seeder_factor = 1.0 + math.sqrt(2.0) * math.pow(
        10.0, -max(0.0, curr_seeders - 1.0) / (n0 - 1.0)
    )
    multiplier = core.preservation_multiplier(init_seeders)
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


def _candidate_to_item(candidate):
    key = f"new:{candidate['id']}"
    return {
        "key": key,
        "source": "new",
        "id": candidate["id"],
        "title": candidate["title"],
        "name": candidate["title"],
        "size_gb": core.safe_float(candidate["size_gb"]),
        "init_seeders": core.safe_int(candidate["seeders"]),
        "curr_seeders": core.safe_int(candidate["seeders"]),
        "age_weeks": core.safe_float(candidate.get("age_weeks"), 16.0),
        "value_per_gb": core.safe_float(candidate["value_per_gb"]),
        "portfolio_value": core.safe_float(candidate["portfolio_value"]),
        "protected": False,
    }


def _delete_payload(item):
    return {
        "hash": item["hash"],
        "name": item["name"],
        "size_gb": round(core.safe_float(item["size_gb"]), 3),
        "init_seeders": core.safe_int(item.get("init_seeders")),
        "curr_seeders": core.safe_int(item.get("curr_seeders")),
        "value_per_gb": round(core.safe_float(item.get("value_per_gb")), 6),
        "portfolio_value": round(core.safe_float(item.get("portfolio_value")), 6),
        "reason": "不在4TB目标组合",
    }


def _select_deletions(items, required_gb):
    ordered = sorted(
        items,
        key=lambda x: (
            core.safe_float(x.get("value_per_gb")),
            -core.safe_float(x.get("size_gb")),
        ),
    )
    selected = []
    selected_gb = 0.0
    for item in ordered:
        if selected_gb + 1e-9 >= required_gb:
            break
        selected.append(item)
        selected_gb += core.safe_float(item.get("size_gb"))
    return selected, selected_gb


def _free_space_new_items(new_items, free_gb):
    batch_gb = max(
        0.0, core.safe_float(core.CONFIG.get("max_batch_download_gb"), 200.0)
    )
    batch_count = max(
        0, core.safe_int(core.CONFIG.get("max_batch_download_count"), 10)
    )
    capacity = min(max(0.0, free_gb), batch_gb)
    if capacity <= 0 or batch_count <= 0:
        return []

    chosen = core.knapsack_select(
        new_items,
        capacity,
        core.safe_float(core.CONFIG.get("portfolio_unit_gb"), 0.1),
    )
    chosen.sort(
        key=lambda x: (
            core.safe_float(x.get("value_per_gb")),
            core.safe_float(x.get("portfolio_value")),
        ),
        reverse=True,
    )
    if len(chosen) > batch_count:
        chosen = chosen[:batch_count]

    used = sum(core.safe_float(x.get("size_gb")) for x in chosen)
    chosen_keys = {x["key"] for x in chosen}
    for item in sorted(
        new_items,
        key=lambda x: core.safe_float(x.get("value_per_gb")),
        reverse=True,
    ):
        if len(chosen) >= batch_count:
            break
        if item["key"] in chosen_keys:
            continue
        size = core.safe_float(item.get("size_gb"))
        if used + size <= capacity + 1e-9:
            chosen.append(item)
            chosen_keys.add(item["key"])
            used += size
    return chosen


def _predicted_rescue_points(current_proxy, planned_proxy, snapshot):
    observed = snapshot.get("rescue_day")
    cap = core.safe_float(snapshot.get("rescue_cap"))
    if observed is None or current_proxy <= 0:
        return None
    if snapshot.get("rescue_near_cap"):
        return (
            min(core.safe_float(observed), cap)
            if cap > 0
            else core.safe_float(observed)
        )
    ratio = core.safe_float(observed) / current_proxy
    predicted = max(0.0, planned_proxy * ratio)
    return min(predicted, cap) if cap > 0 else predicted


def _plan_batch(
    batch_items, current_keep_gb, current_proxy, deletable_existing, max_space
):
    download_gb = sum(core.safe_float(x.get("size_gb")) for x in batch_items)
    overflow = max(0.0, current_keep_gb + download_gb - max_space)
    deleted, deleted_gb = _select_deletions(deletable_existing, overflow)
    deleted_value = sum(
        core.safe_float(x.get("portfolio_value")) for x in deleted
    )
    new_value = sum(core.safe_float(x.get("portfolio_value")) for x in batch_items)
    planned_proxy = current_proxy + new_value - deleted_value
    return download_gb, deleted, deleted_gb, planned_proxy, overflow


def generate_strategy(candidates, current_keep_gb, current_items):
    global LAST_SITE_POINTS

    print("[3/4] 正在执行 V5 积分上限感知的 4TB 组合优化...")
    snapshot = fetch_site_points_snapshot()
    LAST_SITE_POINTS = snapshot

    max_space = core.safe_float(core.CONFIG["max_preservation_space_gb"])
    unit_gb = core.safe_float(core.CONFIG.get("portfolio_unit_gb"), 0.1)
    protected = [x for x in current_items if x.get("protected")]
    optional_existing = [x for x in current_items if not x.get("protected")]
    protected_gb = sum(core.safe_float(x.get("size_gb")) for x in protected)
    current_proxy = sum(
        core.safe_float(x.get("portfolio_value")) for x in current_items
    )

    new_items = [_candidate_to_item(c) for c in candidates]
    candidate_lookup = {f"new:{c['id']}": c for c in candidates}

    remaining = max(0.0, max_space - protected_gb)
    selected_optional = core.knapsack_select(
        optional_existing + new_items, remaining, unit_gb
    )
    full_target = protected + selected_optional
    full_target_keys = {x["key"] for x in full_target}
    target_new = [x for x in selected_optional if x["source"] == "new"]
    excluded_existing = [
        x for x in optional_existing if x["key"] not in full_target_keys
    ]
    target_new.sort(
        key=lambda x: (
            core.safe_float(x.get("value_per_gb")),
            core.safe_float(x.get("portfolio_value")),
        ),
        reverse=True,
    )

    strategy_mode = "PROXY-OPTIMIZE"
    suppression_reason = ""
    batch_items = []
    deleted_items = []
    deleted_gb = 0.0
    download_gb = 0.0
    planned_proxy = current_proxy
    overflow = max(0.0, current_keep_gb - max_space)

    freeze_at_cap = bool(
        core.CONFIG.get("freeze_replacements_at_rescue_cap", True)
    )
    at_rescue_cap = bool(snapshot.get("rescue_near_cap"))

    if at_rescue_cap and freeze_at_cap:
        strategy_mode = "CAP-GUARD"
        if current_keep_gb > max_space + 1e-9:
            deleted_items, deleted_gb = _select_deletions(
                optional_existing, current_keep_gb - max_space
            )
            planned_proxy -= sum(
                core.safe_float(x.get("portfolio_value")) for x in deleted_items
            )
            suppression_reason = (
                "保种积分已接近上限，仅释放超额配额，不做收益型换种"
            )
        elif bool(core.CONFIG.get("allow_free_space_downloads_at_cap", True)):
            free_gb = max(0.0, max_space - current_keep_gb)
            batch_items = _free_space_new_items(new_items, free_gb)
            download_gb = sum(
                core.safe_float(x.get("size_gb")) for x in batch_items
            )
            planned_proxy += sum(
                core.safe_float(x.get("portfolio_value")) for x in batch_items
            )
            suppression_reason = (
                "保种积分已接近上限，不淘汰旧种；仅使用真实空闲配额"
            )
        else:
            suppression_reason = "保种积分已接近上限，冻结新增与换种"
    else:
        batch_gb_limit = max(
            0.0,
            core.safe_float(core.CONFIG.get("max_batch_download_gb"), 200.0),
        )
        batch_count_limit = max(
            0, core.safe_int(core.CONFIG.get("max_batch_download_count"), 10)
        )
        used = 0.0
        for item in target_new:
            if len(batch_items) >= batch_count_limit:
                break
            size = core.safe_float(item.get("size_gb"))
            if used + size <= batch_gb_limit + 1e-9:
                batch_items.append(item)
                used += size

        (
            download_gb,
            deleted_items,
            deleted_gb,
            planned_proxy,
            overflow,
        ) = _plan_batch(
            batch_items,
            current_keep_gb,
            current_proxy,
            excluded_existing,
            max_space,
        )

        predicted = _predicted_rescue_points(
            current_proxy, planned_proxy, snapshot
        )
        rescue_cap = core.safe_float(snapshot.get("rescue_cap"))
        target_points = rescue_cap * core.safe_float(
            snapshot.get("guard_ratio"), 0.99
        )

        if (
            predicted is not None
            and rescue_cap > 0
            and predicted >= target_points
        ):
            strategy_mode = "CAP-TARGET"
            while batch_items:
                test_batch = batch_items[:-1]
                (
                    t_download,
                    t_deleted,
                    t_deleted_gb,
                    t_proxy,
                    t_overflow,
                ) = _plan_batch(
                    test_batch,
                    current_keep_gb,
                    current_proxy,
                    excluded_existing,
                    max_space,
                )
                t_predicted = _predicted_rescue_points(
                    current_proxy, t_proxy, snapshot
                )
                if t_predicted is None or t_predicted < target_points:
                    break
                batch_items = test_batch
                download_gb = t_download
                deleted_items = t_deleted
                deleted_gb = t_deleted_gb
                planned_proxy = t_proxy
                overflow = t_overflow

        min_gain = max(
            0.0,
            core.safe_float(core.CONFIG.get("min_replacement_gain_ratio"), 0.02),
        )
        gain_ratio = (planned_proxy - current_proxy) / max(current_proxy, 1e-12)
        if (
            deleted_items
            and gain_ratio < min_gain
            and current_keep_gb <= max_space + 1e-9
        ):
            strategy_mode = "CHURN-GUARD"
            suppression_reason = (
                f"预计代理收益仅提升 {gain_ratio * 100:.2f}% ，"
                f"低于换种阈值 {min_gain * 100:.2f}%"
            )
            free_gb = max(0.0, max_space - current_keep_gb)
            batch_items = _free_space_new_items(new_items, free_gb)
            download_gb = sum(
                core.safe_float(x.get("size_gb")) for x in batch_items
            )
            deleted_items = []
            deleted_gb = 0.0
            overflow = 0.0
            planned_proxy = current_proxy + sum(
                core.safe_float(x.get("portfolio_value")) for x in batch_items
            )

    to_download = [candidate_lookup[x["key"]] for x in batch_items]
    to_mark_delete = [_delete_payload(x) for x in deleted_items]

    predicted_rescue_after = _predicted_rescue_points(
        current_proxy, planned_proxy, snapshot
    )
    predicted_total_after = None
    if snapshot.get("base_day") is not None or predicted_rescue_after is not None:
        predicted_total_after = (snapshot.get("base_day") or 0.0) + (
            predicted_rescue_after or 0.0
        )

    full_target_value = sum(
        core.safe_float(x.get("portfolio_value")) for x in full_target
    )
    stats = {
        "protected_count": len(protected),
        "protected_gb": protected_gb,
        "target_count": len(full_target),
        "target_existing_count": sum(
            1 for x in full_target if x["source"] == "existing"
        ),
        "target_new_count": sum(1 for x in full_target if x["source"] == "new"),
        "target_space_gb": sum(
            core.safe_float(x.get("size_gb")) for x in full_target
        ),
        "current_value": current_proxy,
        "target_value": full_target_value,
        "value_gain": full_target_value - current_proxy,
        "planned_value": planned_proxy,
        "planned_value_gain": planned_proxy - current_proxy,
        "unit_gb": unit_gb,
        "unresolved_overflow_gb": max(0.0, overflow - deleted_gb),
        "strategy_mode": strategy_mode,
        "suppression_reason": suppression_reason,
        "site_points": snapshot,
        "predicted_rescue_after": predicted_rescue_after,
        "predicted_total_after": predicted_total_after,
    }
    return (
        to_download,
        download_gb,
        to_mark_delete,
        deleted_gb,
        current_keep_gb + download_gb,
        stats,
    )


def _fmt_points(value, cap):
    if value is None:
        return "未解析"
    if cap and cap > 0:
        return f"{value:.2f} / {cap:.0f} /天"
    return f"{value:.2f} /天"


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
    max_space = core.safe_float(core.CONFIG["max_preservation_space_gb"])
    mode = "DRY-RUN" if dry_run else "EXECUTE"
    points = portfolio_stats.get("site_points", {})

    print("\n" + "=" * 100)
    print(f"HHanClub 保种区 V5 策略报告 [{mode}]")
    print("=" * 100)
    print(
        f"缓存: {cache_stats['cache_records']} 条 | 当前保种 {cache_stats['active_keep']} | "
        f"{'本轮自动重建 | ' if cache_stats.get('cache_rebuilt') else ''}"
        f"远端刷新 {'是' if cache_stats['remote_refreshed'] else '否'}"
    )
    print(
        f"站点积分观测: 基础 "
        f"{_fmt_points(points.get('base_day_raw'), points.get('base_cap'))} | "
        f"保种区最近结算 "
        f"{_fmt_points(points.get('rescue_day'), points.get('rescue_cap'))}"
    )
    if (
        points.get("base_day_raw") is not None
        and points.get("rescue_day") is not None
    ):
        total_cap = core.safe_float(points.get("base_cap")) + core.safe_float(
            points.get("rescue_cap")
        )
        observed_total = min(
            core.safe_float(points.get("base_day_raw")),
            core.safe_float(points.get("base_cap")),
        ) + min(
            core.safe_float(points.get("rescue_day")),
            core.safe_float(points.get("rescue_cap")),
        )
        print(f"               合计 {observed_total:.2f} / {total_cap:.0f} /天")

    print(
        f"策略模式: {portfolio_stats.get('strategy_mode', '-')} | "
        f"保种代理值 {portfolio_stats['current_value']:.2f} -> "
        f"{portfolio_stats['planned_value']:.2f} "
        f"(Δ {portfolio_stats['planned_value_gain']:+.2f})"
    )
    if portfolio_stats.get("predicted_rescue_after") is not None:
        print(
            f"预计本轮后保种积分: "
            f"{portfolio_stats['predicted_rescue_after']:.2f} / "
            f"{core.safe_float(points.get('rescue_cap')):.0f} /天"
        )
    if portfolio_stats.get("suppression_reason"):
        print(f"换种抑制: {portfolio_stats['suppression_reason']}")

    print(
        f"保种区候选: {zone_stats['total_in_zone']} 个 | "
        f"已有 {zone_stats['already_have']} | 0人 {zone_stats['dead_count']} | "
        f"超人数阈值 {zone_stats['crowded_count']} | "
        f"超体积阈值 {zone_stats['huge_count']} | "
        f"合格 {zone_stats['eligible_count']}"
    )
    print(
        f"当前【{core.CONFIG['category_keep']}】专项配额: "
        f"{current_keep_gb:.2f} / {max_space:.2f} GB "
        f"({current_keep_gb / 1024.0:.2f} TB)"
    )
    print(
        f"硬保护: {portfolio_stats['protected_count']} 个 / "
        f"{portfolio_stats['protected_gb']:.2f} GB | "
        f"action=7元数据未知: {unmatched_count} 个"
    )

    print(f"\n[+] 本轮新增: {len(to_download)} 个 / {download_gb:.2f} GB")
    for idx, item in enumerate(to_download, 1):
        print(
            f"  {idx:>2}. {item['size_gb']:>7.2f} GB | "
            f"init={item['seeders']} curr={item['seeders']} | "
            f"代理/GB {item['value_per_gb']:.3f} | {item['title']}"
        )

    print(
        f"\n[-] 本轮退出【{core.CONFIG['category_keep']}】: "
        f"{len(to_mark_delete)} 个 / {marked_gb:.2f} GB"
    )
    for idx, item in enumerate(to_mark_delete, 1):
        print(
            f"  {idx:>2}. {item['size_gb']:>7.2f} GB | "
            f"init={item['init_seeders']} curr={item['curr_seeders']} | "
            f"代理/GB {item['value_per_gb']:.3f} | {item['name']}"
        )

    final_estimate = current_keep_gb + download_gb - marked_gb
    print(
        f"\n本轮执行后预计专项配额: "
        f"{final_estimate:.2f} GB / {max_space:.2f} GB"
    )
    if portfolio_stats["unresolved_overflow_gb"] > 0.01:
        print(
            f"⚠️ 仍有 {portfolio_stats['unresolved_overflow_gb']:.2f} GB 无法释放，"
            "请检查硬保护项和未知元数据。"
        )
    print(
        "说明: 基础1200/天与保种额外1800/天按站点观测做上限控制；"
        "单种代理仅用于组合比较，不伪装成HHan精确单种积分。"
    )
    print("=" * 100 + "\n")


core.calc_preservation_metrics = calc_preservation_metrics
core.generate_strategy = generate_strategy
core.print_strategy_report = print_strategy_report


if __name__ == "__main__":
    core.main()

"""
revival_engine.py — BMSS Order App
Detects "dead" items and generates revival recommendations.

Definition: In yearly history (2025) but absent from Jan-Apr 2026 monthly report
AND absent from all 4 recent weekly reports.

Also surfaces items "archived from orders" (discarded from the orders pane) —
these were in recent inventory/sales but manually removed by the user.

Exclusions:
  - Beer kegs
  - Placeholder POS items
  - Limited edition / vintage-year items (cognac, whisky, wine with year in name)
    override-able per-item via flags.json
  - Items in discarded_from_revival flag (permanently excluded — never resurface)
  - Items marked obsolete in flags.json

Output: sorted by est_weekly_revenue. Top 20 tagged top_pick=True.
"""

import re
from data_loader import (
    normalise_name, load_inventory, build_name_index,
    is_keg, is_placeholder, load_flags,
)
from sales_engine import build_historical_baseline, load_all_weekly, load_monthly, load_yearly
from order_engine import _best_match


MIN_HISTORICAL_REVENUE = 150.0
MIN_HISTORICAL_TXNS = 8
TOP_N_HIGHLIGHT = 20

LIMITED_EDITION_CATEGORIES = {
    "cognac", "whisky", "whiskey", "scotch", "brandy", "rum",
    "wine red", "wine white", "wine sparkling", "wine rose", "bourbon",
}

LIMITED_KEYWORDS = [
    "limited", "special release", "special edition", "anniversary",
    "cask strength", "single cask", "single barrel", "expression",
    "vintage", "collector", "reserve edition",
]

YEAR_PATTERN = re.compile(r"\b(20[0-9]{2}|199[0-9]|198[0-9])\b")


def is_limited_edition(name: str, category: str) -> bool:
    name_l = name.lower()
    cat_l = category.lower().strip()
    for kw in LIMITED_KEYWORDS:
        if kw in name_l:
            return True
    for cat_key in LIMITED_EDITION_CATEGORIES:
        if cat_key in cat_l:
            if YEAR_PATTERN.search(name_l):
                return True
            break
    return False


def build_revival_list(data_root: str) -> list:
    monthly_rows = load_monthly(data_root)
    yearly_rows  = load_yearly(data_root)
    weekly_rows  = load_all_weekly(data_root)
    inventory    = load_inventory(data_root)

    flags = load_flags(data_root)
    obsolete_names           = {normalise_name(n) for n in flags.get("obsolete_items", [])}
    limited_overrides        = {normalise_name(n) for n in flags.get("limited_edition_overrides", [])}
    # Items permanently discarded from the revival tab — never resurface
    revival_discarded_names  = {normalise_name(n) for n in flags.get("discarded_from_revival", [])}
    # Items archived from orders pane — surface here for consideration
    archived_from_orders     = {normalise_name(n) for n in flags.get("archived_from_orders", [])}

    historical  = build_historical_baseline(monthly_rows, yearly_rows)
    inv_index   = build_name_index(inventory)

    monthly_names = {normalise_name(r["name"]) for r in monthly_rows}
    weekly_names  = {normalise_name(r["name"]) for r in weekly_rows}

    def period_weeks(label: str) -> float:
        l = label.lower()
        if "31.12" in l and "1.1" in l:  return 52.0
        if "31.12" in l and "1.7"  in l: return 26.0
        return 14.0

    yearly_by_name = {}
    for row in yearly_rows:
        key = normalise_name(row["name"])
        pw  = period_weeks(row.get("period", ""))
        if key not in yearly_by_name:
            yearly_by_name[key] = {
                "revenue": 0.0, "txns": 0, "cases": 0.0, "weeks": pw,
                "category": row["category"], "profit_pct": row.get("profit_pct", 0.0),
            }
        yearly_by_name[key]["revenue"]    += row.get("revenue", 0.0)
        yearly_by_name[key]["txns"]       += row.get("txn_count", 0)
        yearly_by_name[key]["cases"]      += row.get("cases_sold", 0.0)
        yearly_by_name[key]["profit_pct"]  = max(
            yearly_by_name[key]["profit_pct"], row.get("profit_pct", 0.0)
        )

    revival_candidates = []
    seen_keys = set()

    # ── Pass 1: standard historical revival (in 2025 but absent from 2026) ──
    for key, data in historical.items():
        if not data["in_yearly"]:
            continue
        if data["in_monthly"] or key in monthly_names:
            continue
        if key in weekly_names:
            continue

        canonical_name = data["canonical_name"]
        category       = data.get("category", "")

        # Permanent exclusions
        if is_placeholder(canonical_name):
            continue
        if key in obsolete_names:
            continue
        if key in revival_discarded_names:
            continue  # permanently removed from revival

        yearly_data    = yearly_by_name.get(key, {})
        yearly_rev     = yearly_data.get("revenue",    data.get("yearly_revenue", 0.0))
        yearly_txns    = yearly_data.get("txns",       data.get("yearly_txns",    0))
        yearly_cases   = yearly_data.get("cases",      data.get("yearly_cases",   0.0))
        weeks          = yearly_data.get("weeks",      52.0)
        category       = yearly_data.get("category",   category)
        profit_pct     = yearly_data.get("profit_pct", 0.0)

        if yearly_rev  < MIN_HISTORICAL_REVENUE: continue
        if yearly_txns < MIN_HISTORICAL_TXNS:    continue
        if is_keg(canonical_name, category):     continue

        auto_limited = is_limited_edition(canonical_name, category)
        if auto_limited and key not in limited_overrides:
            continue

        est_weekly_rev   = yearly_rev   / weeks
        est_weekly_cases = yearly_cases / weeks

        inv_match           = _best_match(key, inv_index)
        in_inventory        = inv_match is not None
        current_stock_cases = 0.0
        case_qty            = 24
        if in_inventory and inv_match:
            inv_item            = inv_index[inv_match]
            case_qty            = max(inv_item.get("case_qty", 24), 1)
            current_stock_cases = inv_item.get("total_units", 0.0) / case_qty

        suggested_order_cases = max(1, round(est_weekly_cases * 2 - current_stock_cases))

        rationale_parts = []
        if est_weekly_rev >= 100:
            rationale_parts.append(f"Strong historical revenue (approx ${est_weekly_rev:.0f}/wk)")
        if profit_pct >= 30:
            rationale_parts.append(f"Good margin ({profit_pct:.1f}%)")
        if not in_inventory:
            rationale_parts.append("Not currently stocked - needs to be reordered from supplier")
        elif current_stock_cases == 0:
            rationale_parts.append("In system but zero stock")
        else:
            rationale_parts.append(f"Currently {current_stock_cases:.1f} cases on hand")

        revival_candidates.append({
            "name":                 canonical_name,
            "category":             category,
            "yearly_revenue":       round(yearly_rev,        2),
            "yearly_txns":          yearly_txns,
            "yearly_cases":         round(yearly_cases,      2),
            "est_weekly_revenue":   round(est_weekly_rev,    2),
            "est_weekly_cases":     round(est_weekly_cases,  3),
            "margin_pct_historical":round(profit_pct,        1),
            "in_inventory":         in_inventory,
            "current_stock_cases":  round(current_stock_cases, 1),
            "case_qty":             case_qty,
            "suggested_order_cases":suggested_order_cases,
            "revival_rationale":    "; ".join(rationale_parts) if rationale_parts else "Historical seller",
            "last_seen":            "2025 yearly report",
            "is_limited_edition":   False,
            "source":               "historical",
        })
        seen_keys.add(key)

    # ── Pass 2: items archived from orders pane ──
    # These are recently-active items the user manually discarded from the orders panel.
    # They may still have recent sales data so we build their profile differently.
    weekly_by_name = {}
    for row in weekly_rows:
        k = normalise_name(row["name"])
        if k not in weekly_by_name:
            weekly_by_name[k] = {"revenue": 0.0, "txns": 0, "cases": 0.0,
                                  "category": row["category"], "profit_pct": row.get("profit_pct", 0.0),
                                  "canonical_name": row["name"]}
        weekly_by_name[k]["revenue"] += row.get("revenue", 0.0)
        weekly_by_name[k]["txns"]    += row.get("txn_count", 0)
        weekly_by_name[k]["cases"]   += row.get("cases_sold", 0.0)
        weekly_by_name[k]["profit_pct"] = max(weekly_by_name[k]["profit_pct"], row.get("profit_pct", 0.0))

    for archived_name in flags.get("archived_from_orders", []):
        key = normalise_name(archived_name)
        if key in seen_keys:
            continue
        if key in revival_discarded_names:
            continue  # user also discarded from revival — never resurface
        if key in obsolete_names:
            continue

        # Look up what we know about this item
        wd = weekly_by_name.get(key, {})
        if not wd:
            # Try fuzzy match in weekly data
            fk = _best_match(key, weekly_by_name)
            if fk:
                wd = weekly_by_name[fk]

        n_weeks = max(len(set(r["week_index"] for r in weekly_rows if r["week_index"] >= 0)), 1)
        weekly_rev = wd.get("revenue", 0.0) / n_weeks if wd else 0.0
        weekly_cases = wd.get("cases", 0.0) / n_weeks if wd else 0.0
        profit_pct = wd.get("profit_pct", 0.0)
        category = wd.get("category", "")
        canonical_name = wd.get("canonical_name", archived_name)

        if is_placeholder(canonical_name) or is_keg(canonical_name, category):
            continue

        inv_match           = _best_match(key, inv_index)
        in_inventory        = inv_match is not None
        current_stock_cases = 0.0
        case_qty            = 24
        if in_inventory and inv_match:
            inv_item            = inv_index[inv_match]
            case_qty            = max(inv_item.get("case_qty", 24), 1)
            current_stock_cases = inv_item.get("total_units", 0.0) / case_qty

        suggested_order_cases = max(1, round(weekly_cases * 2 - current_stock_cases))

        revival_candidates.append({
            "name":                 canonical_name,
            "category":             category,
            "yearly_revenue":       0.0,
            "yearly_txns":          wd.get("txns", 0),
            "yearly_cases":         0.0,
            "est_weekly_revenue":   round(weekly_rev, 2),
            "est_weekly_cases":     round(weekly_cases, 3),
            "margin_pct_historical":round(profit_pct, 1),
            "in_inventory":         in_inventory,
            "current_stock_cases":  round(current_stock_cases, 1),
            "case_qty":             case_qty,
            "suggested_order_cases":suggested_order_cases,
            "revival_rationale":    "Archived from orders — consider whether to stock again",
            "last_seen":            "Recent weekly reports",
            "is_limited_edition":   False,
            "source":               "archived_from_orders",
        })
        seen_keys.add(key)

    revival_candidates.sort(key=lambda x: x["est_weekly_revenue"], reverse=True)

    for i, item in enumerate(revival_candidates):
        item["revival_rank"] = i + 1
        item["top_pick"]     = (i + 1) <= TOP_N_HIGHLIGHT

    return revival_candidates

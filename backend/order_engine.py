"""
order_engine.py — BMSS Order App
Generates the weekly order recommendation list.

Urgency rules:
  CRITICAL = profit-protected item that is out of stock OR < 0.5 wks cover
  HIGH     = out of stock (non-protected) OR profit-protected with < 1 wk cover
  MEDIUM   = <= reorder threshold (default 1.0 wk)
  LOW      = revenue-protected or rising momentum, low but not critical

Exclusions:
  - Beer kegs
  - Placeholder POS items (surcharges, bags, cafe sales, etc.)
  - Items with zero recent sales (unless protected + out of stock)
  - Items with < 1 unit sold in the past 6 months (dead stock — never reorder)
  - Items marked obsolete in flags.json
  - Items marked skipped in flags.json (skip = short-term, restorable)
  - Gift packs / seasonal / yearly-cycle items (auto-detected + flags.json override)

Name matching: volume (ml) and format (bottle vs can) aware — prevents
cross-matching e.g. "Jim Beam White & Cola Bt 330ml" <-> "...Can 375ml"
"""

import math
import re
from typing import Optional
from data_loader import (
    normalise_name, load_inventory, load_protected_items, build_name_index,
    is_keg, is_placeholder, get_weekly_sold_names, load_flags,
    load_current_cart, names_are_compatible, build_six_month_rate,
)
from sales_engine import build_sales_analysis

REORDER_THRESHOLD = 1.0
TARGET_WEEKS_COVER = 2.0
MIN_ORDER_CASES = 1

# Minimum units sold across monthly + weekly combined (~6 months) to be eligible for reorder
# Items below this are truly dead and must not be surfaced in the reorder panel.
SIX_MONTH_MIN_UNITS = 1.0

# ---------------------------------------------------------------------------
# Gift pack / yearly cycle keyword filters
# ---------------------------------------------------------------------------

GIFT_PACK_KEYWORDS = [
    "gift pack", "gift set", "gift box", "gift tin", "gift bag",
    "hamper", "gift tower", "gift wrap",
]

YEARLY_CYCLE_KEYWORDS = [
    "year of the", "chinese new year", "lunar new year",
    "new year dragon", "new year rabbit", "new year tiger",
    "new year horse", "new year ox", "new year rat",
    "christmas pack", "xmas pack", "easter pack",
    "limited edition",
]

def is_gift_or_seasonal(name: str) -> bool:
    n = name.lower()
    for kw in GIFT_PACK_KEYWORDS + YEARLY_CYCLE_KEYWORDS:
        if kw in n:
            return True
    return False


# ---------------------------------------------------------------------------
# Cognac / expensive spirits — order by bottle, not case
# ---------------------------------------------------------------------------

_BOTTLE_ORDER_CATEGORIES = {"cognac", "scotch", "brandy"}

def should_order_by_bottle(category: str) -> bool:
    """Expensive spirits where the unit of ordering is a bottle, not a case."""
    cat = category.lower()
    return any(k in cat for k in _BOTTLE_ORDER_CATEGORIES)


# ---------------------------------------------------------------------------
# Volume / format-aware name matching
# ---------------------------------------------------------------------------

def _best_match(norm_name: str, index: dict, threshold: int = 80) -> Optional[str]:
    """
    Find the best matching key in index for norm_name.

    Critically: uses volume (ml) and format (bottle/can) compatibility to prevent
    cross-matching between e.g. "Jim Beam White & Cola Bt 330ml" and "...Can 375ml".
    """
    # Exact match first
    if norm_name in index:
        return norm_name

    # Prefix check — requires >=30 chars to reduce false positives from shared brand names
    for key in index:
        min_len = min(len(norm_name), len(key))
        if min_len >= 30 and norm_name[:30] == key[:30]:
            if names_are_compatible(norm_name, key):
                return key

    # Word-overlap fallback
    words_a = set(norm_name.split())
    best_key = None
    best_overlap = 0
    for key in index:
        words_b = set(key.split())
        overlap = len(words_a & words_b)
        union = len(words_a | words_b)
        if union > 0 and overlap / union >= 0.6 and overlap > best_overlap:
            if names_are_compatible(norm_name, key):
                best_overlap = overlap
                best_key = key
    return best_key


# ---------------------------------------------------------------------------
# Cart lookup helpers
# ---------------------------------------------------------------------------

def _build_cart_index(cart_items: list) -> dict:
    """Build {norm_name: cart_item} from current cart."""
    return {normalise_name(item["name"]): item for item in cart_items}


def _cart_units_already_ordered(norm_name: str, cart_index: dict) -> float:
    """Return total units already in the cart for this item (0 if not in cart)."""
    match = _best_match(norm_name, cart_index)
    if match and match in cart_index:
        return cart_index[match].get("total_units_ordered", 0.0)
    return 0.0


# ---------------------------------------------------------------------------
# Main order list builder
# ---------------------------------------------------------------------------

def build_order_list(data_root: str,
                     reorder_threshold: float = REORDER_THRESHOLD,
                     target_weeks: float = TARGET_WEEKS_COVER) -> list:
    inventory  = load_inventory(data_root)
    protected  = load_protected_items(data_root)
    analysis   = build_sales_analysis(data_root)
    flags      = load_flags(data_root)
    cart_items = load_current_cart(data_root)
    six_month_rate = build_six_month_rate(data_root)

    obsolete_names     = {normalise_name(n) for n in flags.get("obsolete_items", [])}
    seasonal_overrides = {normalise_name(n) for n in flags.get("seasonal_overrides", [])}
    skipped_names      = {normalise_name(n) for n in flags.get("skipped_items", [])}
    # Items discarded from orders pane — treated as archived for revival, not shown in orders
    archived_names     = {normalise_name(n) for n in flags.get("archived_from_orders", [])}

    weekly_sold  = get_weekly_sold_names(data_root)
    velocity_map = analysis["velocity"]
    momentum_map = analysis["momentum"]
    cart_index   = _build_cart_index(cart_items)

    profit_names  = {normalise_name(p["name"]): p for p in protected["profit"]}
    revenue_names = {normalise_name(p["name"]): p for p in protected["revenue"]}
    inv_index     = build_name_index(inventory)

    order_list = []
    visited = set()

    for item in inventory:
        key = normalise_name(item["name"])
        if key in visited:
            continue
        visited.add(key)

        # Exclusions
        if is_keg(item["name"], item.get("category", "")):
            continue
        if is_placeholder(item["name"]):
            continue
        if key in obsolete_names:
            continue
        if key in archived_names:
            continue
        if key in skipped_names:
            continue
        if is_gift_or_seasonal(item["name"]) and key not in seasonal_overrides:
            continue

        # Skip items with < 1 unit sold in the past 6 months — dead stock, never reorder
        six_month_units = six_month_rate.get(key, 0.0)
        if six_month_units == 0:
            six_match = _best_match(key, six_month_rate)
            if six_match:
                six_month_units = six_month_rate.get(six_match, 0.0)
        if six_month_units < SIX_MONTH_MIN_UNITS:
            # Only include if explicitly profit-protected AND genuinely zero stock
            profit_check = _best_match(key, profit_names)
            if not (profit_check is not None and item.get("total_units", 0) == 0):
                continue

        # Skip items with no recent weekly sales (unless protected + zero stock)
        sold_recently = _best_match(key, {k: True for k in weekly_sold}) is not None
        if not sold_recently:
            profit_check = _best_match(key, profit_names)
            rev_check    = _best_match(key, revenue_names)
            is_protected = (profit_check is not None) or (rev_check is not None)
            if not (is_protected and item.get("total_units", 0) == 0):
                continue

        case_qty      = max(item["case_qty"], 1)
        total_units   = item["total_units"]
        current_cases = total_units / case_qty

        # Velocity
        vel_key = _best_match(key, velocity_map)
        if vel_key and vel_key in velocity_map:
            vel      = velocity_map[vel_key]["avg_cases_per_week"]
            last_vel = velocity_map[vel_key]["last_week_cases"]
        else:
            vel      = 0.0
            last_vel = 0.0

        # Weeks of cover
        if vel > 0:
            woc = current_cases / vel
        elif total_units > 0:
            woc = 999.0
        else:
            woc = 0.0

        # Protection status
        profit_match = _best_match(key, profit_names)
        rev_match    = _best_match(key, revenue_names)
        is_profit_protected  = profit_match is not None
        is_revenue_protected = rev_match is not None
        protection_rank = None
        if is_profit_protected and profit_match:
            protection_rank = profit_names[profit_match].get("rank")
        elif is_revenue_protected and rev_match:
            protection_rank = revenue_names[rev_match].get("rank")

        # Should we order?
        should_order = False
        notes = []

        if woc <= reorder_threshold and vel > 0:
            should_order = True
            if woc == 0:
                notes.append("Out of stock — reorder immediately.")
            else:
                notes.append(f"Only {woc:.1f} weeks of cover remaining.")

        if (is_profit_protected or is_revenue_protected) and current_cases < 2 and vel > 0:
            should_order = True
            notes.append("Protected item with low stock — keep buffer above 2 cases.")

        if (is_profit_protected or is_revenue_protected) and total_units == 0:
            should_order = True
            notes.append("Protected item — currently zero stock. Order to restore shelf presence.")

        if not should_order:
            continue

        # Cognac / expensive spirits: order by bottle
        by_bottle = should_order_by_bottle(item.get("category", ""))

        if by_bottle:
            needed_units    = max(1, math.ceil((target_weeks * vel * case_qty) - total_units))
            recommend_units = needed_units
            recommend_cases = math.ceil(needed_units / case_qty) if case_qty > 1 else needed_units
        else:
            needed_units    = (target_weeks * vel * case_qty) - total_units
            recommend_cases = max(MIN_ORDER_CASES, math.ceil(needed_units / case_qty))
            recommend_units = recommend_cases * case_qty

        # Urgency
        if is_profit_protected and (woc == 0 or woc < 0.5):
            urgency = "CRITICAL"
        elif woc == 0:
            urgency = "HIGH"
        elif is_profit_protected and woc < reorder_threshold:
            urgency = "HIGH"
        elif woc <= reorder_threshold:
            urgency = "MEDIUM"
        else:
            urgency = "LOW"

        # Momentum note
        mom_key = _best_match(key, momentum_map)
        if mom_key and momentum_map[mom_key]["direction"] == "rising":
            notes.append("Sales are rising — consider ordering extra buffer stock.")
            if momentum_map[mom_key]["pct_change"] > 30:
                recommend_cases = math.ceil(recommend_cases * 1.2)
                recommend_units = recommend_cases * case_qty

        # Promo note
        promo_key   = _best_match(key, analysis["promo_margins"])
        promo_savings = 0.0
        if promo_key and promo_key in analysis["promo_margins"]:
            promo_savings = analysis["promo_margins"][promo_key].get("promo_savings", 0.0)
        if promo_savings > 0:
            notes.append(f"Bottlemart covered ${promo_savings:.2f} in promo discounts last week.")

        # Current cart — items already ordered this week
        cart_units = _cart_units_already_ordered(key, cart_index)
        if cart_units > 0:
            cart_cases_ordered = cart_units / case_qty
            still_needed = max(0, recommend_units - cart_units)
            if still_needed <= 0:
                notes.append(f"Already ordered {cart_units:.0f} units ({cart_cases_ordered:.1f} cs) this week — order covered.")
                urgency = "ORDERED"
            else:
                notes.append(f"Partly ordered: {cart_units:.0f} units ({cart_cases_ordered:.1f} cs) in cart. Still need {still_needed:.0f} more units.")

        order_list.append({
            "name":                     item["name"],
            "category":                 item["category"],
            "current_cases":            round(current_cases, 1),
            "current_units":            round(total_units, 0),
            "case_qty":                 case_qty,
            "weekly_velocity_cases":    round(vel, 2),
            "last_week_velocity_cases": round(last_vel, 2),
            "weeks_of_cover":           round(woc, 2) if woc < 999 else None,
            "recommend_cases":          recommend_cases,
            "recommend_units":          recommend_units,
            "order_by_bottle":          by_bottle,
            "urgency":                  urgency,
            "is_profit_protected":      is_profit_protected,
            "is_revenue_protected":     is_revenue_protected,
            "protection_rank":          protection_rank,
            "promo_savings_last_week":  promo_savings,
            "is_obsolete":              False,
            "cart_units_ordered":       cart_units,
            "notes":                    " ".join(notes) if notes else "Reorder to maintain stock levels.",
        })

    urgency_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "ORDERED": 4}
    order_list.sort(key=lambda x: (
        urgency_order.get(x["urgency"], 99),
        0 if x["is_profit_protected"] else 1,
        x["protection_rank"] or 999,
    ))

    return order_list

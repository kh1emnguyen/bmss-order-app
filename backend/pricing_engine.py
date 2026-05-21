"""
pricing_engine.py — BMSS Order App
Identifies items that are candidates for price increases.

Exclusions (updated):
  - Promotional items (chain-funded, not our control)
  - Falling-momentum items
  - Items where the suggested price rounds to the same as current (trivial change < $0.50)
  - Items with < $0.50 meaningful increase after rounding

Risk levels:
  LOW    = spirits, cognac, tequila, wine sparkling, accessories
  MEDIUM = bourbon, RTD, cider, wine red/white
  HIGH   = beer (local/international), structural/chain-priced items
"""

from data_loader import normalise_name, load_inventory, build_name_index
from margin_engine import build_margin_analysis, get_floor, CATEGORY_FLOORS
from sales_engine import build_sales_analysis, promo_adjusted_margin
from order_engine import _best_match

PRICE_SENSITIVITY = {
    "beer local": "high",
    "beer international": "high",
    "beer craft": "medium",
    "beer keg": "low",
    "rtd": "medium",
    "spirits": "low",
    "whisky": "low",
    "bourbon": "medium",
    "gin": "low",
    "vodka": "medium",
    "rum": "medium",
    "tequila": "low",
    "cognac": "low",
    "brandy": "low",
    "scotch": "low",
    "soju": "low",
    "wine red": "medium",
    "wine white": "medium",
    "wine sparkling": "low",
    "cider": "medium",
    "soft drink": "low",
    "accessories": "low",
    "misc": "low",
}

MIN_REVENUE_THRESHOLD = 50.0
MAX_PRICE_INCREASE_PCT = 15.0
MIN_MEANINGFUL_INCREASE = 0.50   # ignore suggestions with < $0.50 real impact


def get_sensitivity(category: str) -> str:
    cat_lower = category.lower().strip()
    for key in PRICE_SENSITIVITY:
        if key in cat_lower:
            return PRICE_SENSITIVITY[key]
    return "medium"


def suggest_price_increase(current_price: float, current_margin_pct: float,
                            target_margin_pct: float) -> dict:
    """
    Return suggested price to achieve target margin.
    Capped at +15%, rounded to nearest $0.50 (standard Aus pricing).
    Returns None if the increase is trivial (< $0.50 after rounding).
    """
    if current_price <= 0:
        return None

    cogs_per_unit = current_price * (1 - current_margin_pct / 100.0)
    if cogs_per_unit <= 0:
        return None

    raw_target = cogs_per_unit / max(1 - target_margin_pct / 100.0, 0.001)
    raw_increase = raw_target - current_price
    increase_pct = (raw_increase / current_price) * 100.0

    # Cap
    if increase_pct > MAX_PRICE_INCREASE_PCT:
        raw_target = current_price * (1 + MAX_PRICE_INCREASE_PCT / 100.0)
        raw_increase = raw_target - current_price
        increase_pct = MAX_PRICE_INCREASE_PCT

    # Round to nearest $0.50
    suggested = round(raw_target * 2) / 2.0

    # Check if rounding wiped out the increase (trivial)
    actual_increase = suggested - current_price
    if actual_increase < MIN_MEANINGFUL_INCREASE:
        return None  # Not worth it

    return {
        "suggested_price": suggested,
        "increase_dollar": round(actual_increase, 2),
        "increase_pct": round((actual_increase / current_price) * 100.0, 1),
    }


def build_pricing_recommendations(data_root: str) -> list[dict]:
    margin_issues = build_margin_analysis(data_root)
    analysis      = build_sales_analysis(data_root)
    momentum_map  = analysis["momentum"]
    inventory     = load_inventory(data_root)
    inv_index     = build_name_index(inventory)

    recommendations = []

    for issue in margin_issues:
        if issue["revenue"] < MIN_REVENUE_THRESHOLD:
            continue
        if issue["cause"] == "promotional":
            continue

        key = normalise_name(issue["name"])

        # Skip falling-momentum items
        mom_key = _best_match(key, momentum_map)
        if mom_key and momentum_map[mom_key]["direction"] == "falling":
            continue

        # Single-unit shelf price from inventory
        inv_match = _best_match(key, inv_index)
        single_unit_price = None
        if inv_match and inv_match in inv_index:
            inv_item = inv_index[inv_match]
            tiers = inv_item.get("price_tiers", [])
            single_tiers = [t for t in tiers if t.get("qty", 0) == 1]
            if single_tiers:
                single_unit_price = single_tiers[0]["price"]
            elif tiers:
                single_unit_price = min(tiers, key=lambda t: t["qty"])["price"]
            else:
                single_unit_price = inv_item.get("price_single")

        revenue    = issue["revenue"]
        txn_count  = issue.get("txn_count", 1) or 1
        avg_price_per_txn = revenue / txn_count
        current_price = single_unit_price if single_unit_price else avg_price_per_txn

        suggestion = suggest_price_increase(
            current_price=current_price,
            current_margin_pct=issue["true_margin_pct"],
            target_margin_pct=issue["floor_pct"],
        )

        # Skip trivial / impossible increases
        if suggestion is None:
            continue

        margin_lift_pts = issue["floor_pct"] - issue["true_margin_pct"]
        weekly_profit_gain = margin_lift_pts / 100.0 * revenue

        cause = issue["cause"]
        sensitivity = get_sensitivity(issue["category"])

        if cause == "structural" or sensitivity == "high":
            risk = "high"
        elif sensitivity == "medium":
            risk = "medium"
        else:
            risk = "low"

        mom_label = "→ Stable"
        if mom_key and mom_key in momentum_map:
            mom_label = momentum_map[mom_key].get("label", "→ Stable")

        recommendations.append({
            "name":                 issue["name"],
            "category":             issue["category"],
            "revenue_per_week":     issue["revenue"],
            "current_margin_pct":   issue["true_margin_pct"],
            "floor_margin_pct":     issue["floor_pct"],
            "margin_gap_pts":       issue["margin_gap_pts"],
            "current_unit_price":   round(current_price, 2),
            "current_avg_price":    round(avg_price_per_txn, 2),
            "price_source":         "inventory" if single_unit_price else "avg_txn",
            "suggested_price":      suggestion["suggested_price"],
            "price_increase_dollar": suggestion["increase_dollar"],
            "price_increase_pct":   suggestion["increase_pct"],
            "weekly_profit_gain":   round(weekly_profit_gain, 2),
            "annual_profit_gain":   round(weekly_profit_gain * 52, 2),
            "cause":                cause,
            "risk_level":           risk,
            "momentum":             mom_label,
            "promo_savings":        issue["promo_savings"],
        })

    risk_order = {"low": 0, "medium": 1, "high": 2}
    recommendations.sort(key=lambda x: (
        risk_order.get(x["risk_level"], 1),
        -x["annual_profit_gain"]
    ))

    return recommendations

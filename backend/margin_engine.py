"""
margin_engine.py — BMSS Order App
Applies exemplar margin floors, calculates dollar drag, separates
promo vs structural causes.

Margin Floors (from sales_analysis_agent_exemplar.md):
  Single (qty 1):  35%
  Pack (qty 2-12): 25%
  Case (qty 13+):   8%
  Beer Kegs:       60%
  Spirits:         15%  (floor; cognac/scotch often compressed)
  Soju/Asian RTD:  45%
  Soft Drinks/Misc: 45%
"""

from data_loader import normalise_name, load_all_weekly, load_protected_items
from sales_engine import build_sales_analysis, promo_adjusted_margin

# ---------------------------------------------------------------------------
# Margin floor configuration
# ---------------------------------------------------------------------------

# Category-level floors (applied when quantity-tier isn't deterministic)
CATEGORY_FLOORS = {
    "beer local": {"single": 35, "pack": 25, "case": 8},
    "beer international": {"single": 35, "pack": 25, "case": 8},
    "beer craft": {"single": 35, "pack": 25, "case": 8},
    "beer keg": {"single": 60, "pack": 60, "case": 60},
    "rtd": {"single": 35, "pack": 25, "case": 8},
    "spirits": {"single": 15, "pack": 15, "case": 15},
    "whisky": {"single": 15, "pack": 15, "case": 15},
    "bourbon": {"single": 20, "pack": 20, "case": 20},
    "gin": {"single": 20, "pack": 20, "case": 20},
    "vodka": {"single": 20, "pack": 20, "case": 20},
    "rum": {"single": 20, "pack": 20, "case": 20},
    "tequila": {"single": 20, "pack": 20, "case": 20},
    "cognac": {"single": 15, "pack": 15, "case": 15},
    "brandy": {"single": 15, "pack": 15, "case": 15},
    "scotch": {"single": 15, "pack": 15, "case": 15},
    "soju": {"single": 45, "pack": 45, "case": 45},
    "wine red": {"single": 30, "pack": 25, "case": 20},
    "wine white": {"single": 30, "pack": 25, "case": 20},
    "wine sparkling": {"single": 30, "pack": 25, "case": 20},
    "wine rose": {"single": 30, "pack": 25, "case": 20},
    "cider": {"single": 35, "pack": 25, "case": 8},
    "soft drink": {"single": 45, "pack": 45, "case": 40},
    "accessories": {"single": 45, "pack": 45, "case": 45},
    "misc": {"single": 45, "pack": 45, "case": 45},
    "default": {"single": 30, "pack": 25, "case": 8},
}

# Quantity thresholds for tier classification
def _qty_tier(items_sold: float, cases_sold: float) -> str:
    """Classify whether this line sold as single, pack, or case."""
    if cases_sold >= 1:
        return "case"
    if items_sold >= 2:
        return "pack"
    return "single"


def get_floor(category: str, items_sold: float, cases_sold: float) -> float:
    """Return the applicable margin floor % for this item."""
    cat_lower = category.lower().strip()

    # Keg check (name-based)
    floors = None
    for cat_key in CATEGORY_FLOORS:
        if cat_key in cat_lower:
            floors = CATEGORY_FLOORS[cat_key]
            break
    if floors is None:
        floors = CATEGORY_FLOORS["default"]

    tier = _qty_tier(items_sold, cases_sold)
    return floors.get(tier, floors["single"])


# ---------------------------------------------------------------------------
# Cause classification
# ---------------------------------------------------------------------------

def classify_cause(row: dict, wow_delta: dict) -> str:
    """
    Classify why margin is below floor:
      'promotional'  — Promotional Savings > 0 or large volume spike
      'structural'   — chain competition (known brands, stable volume)
      'pricing_error'— margin dropped sharply WoW with no promo savings
      'unknown'
    """
    promo = row.get("promo_savings", 0.0)
    revenue = row.get("revenue", 1.0)

    if promo > 0 and (promo / revenue) > 0.02:
        return "promotional"

    delta = wow_delta or {}
    vol_delta = delta.get("volume_delta_cases", 0.0)
    rev_delta = delta.get("revenue_delta", 0.0)
    margin_delta = delta.get("margin_delta_pts", 0.0)

    # Volume spike with margin drop = likely promotional
    if vol_delta > 0.5 and margin_delta < -3:
        return "promotional"

    # Margin dropped sharply, volume flat = pricing error
    if margin_delta < -8 and abs(vol_delta) < 0.3:
        return "pricing_error"

    # Known structurally compressed brands
    name_lower = row.get("name", "").lower()
    structural_brands = [
        "corona", "heineken", "peroni", "carlton", "victoria bitter",
        "vb ", "great northern", "hahn", "pure blonde", "crown lager",
        "grants", "glenlivet", "jameson", "jack daniel", "jim beam",
        "johnnie walker", "chivas", "martell", "hennessy"
    ]
    if any(b in name_lower for b in structural_brands):
        return "structural"

    return "unknown"


# ---------------------------------------------------------------------------
# Main margin analysis
# ---------------------------------------------------------------------------

def build_margin_analysis(data_root: str) -> list[dict]:
    """
    Returns list of items below their floor margin, sorted by dollar drag (highest first).
    Each dict:
    {
      name, category, revenue, reported_margin_pct, true_margin_pct,
      floor_pct, dollar_drag, promo_savings, cause, wow_margin_delta,
      is_price_increase_candidate
    }
    """
    analysis = build_sales_analysis(data_root)
    weekly_matrix = analysis["weekly_matrix"]
    wow_deltas = analysis["wow_deltas"]
    latest_wi = analysis["latest_week_index"]

    results = []

    for key, weeks in weekly_matrix.items():
        if latest_wi not in weeks:
            continue
        row = weeks[latest_wi]
        revenue = row.get("revenue", 0.0)
        if revenue <= 0:
            continue

        promo_data = promo_adjusted_margin(row)
        true_margin = promo_data["true_margin_pct"]
        reported_margin = promo_data["reported_margin_pct"]
        promo_savings = promo_data["promo_savings"]

        floor = get_floor(row["category"], row.get("items_sold", 0), row.get("cases_sold", 0))
        # Use true margin for comparison
        margin_to_compare = true_margin

        if margin_to_compare >= floor:
            continue  # Above floor — skip

        dollar_drag = (floor - margin_to_compare) / 100.0 * revenue

        wow = wow_deltas.get(key, {})
        cause = classify_cause(row, wow)

        # chain_subsidised_loss: item is selling at a loss (true margin < 0)
        # and has Bottlemart promo activity — worth flagging for review.
        chain_subsidised_loss = (true_margin < 0 and promo_savings > 0)

        # Exact calculation breakdown for the frontend "show your working" pane
        reported_profit = promo_data["reported_profit"]
        true_profit     = promo_data["true_profit"]
        cogs_implied    = revenue - reported_profit
        calculation_breakdown = {
            "step1_revenue":         round(revenue, 2),
            "step2_profit":          round(reported_profit, 2),
            "step2_cogs_implied":    round(cogs_implied, 2),
            "step3_margin":          f"{reported_margin:.2f}%  (= ${reported_profit:.2f} profit ÷ ${revenue:.2f} revenue × 100)",
            "step4_promo_savings":   round(promo_savings, 2),
            "step4_note":            "Promo savings is informational — already reflected in POS profit figure above",
            "step5_floor":           f"{floor:.0f}%  (category floor for {row['category']})",
            "step6_gap_pts":         round(floor - true_margin, 2),
            "step7_dollar_drag":     f"${dollar_drag:.2f}/wk  (= {floor - true_margin:.2f} pts ÷ 100 × ${revenue:.2f})",
        }

        results.append({
            "name": row["name"],
            "category": row["category"],
            "revenue": round(revenue, 2),
            "reported_margin_pct": reported_margin,
            "true_margin_pct": round(true_margin, 2),
            "floor_pct": floor,
            "margin_gap_pts": round(floor - margin_to_compare, 2),
            "dollar_drag": round(dollar_drag, 2),
            "promo_savings": promo_savings,
            "promo_contribution_pts": promo_data["promo_contribution_pts"],
            "cause": cause,
            "chain_subsidised_loss": chain_subsidised_loss,
            "wow_margin_delta_pts": wow.get("margin_delta_pts", 0.0),
            "wow_revenue_delta": wow.get("revenue_delta", 0.0),
            "txn_count": row.get("txn_count", 0),
            "calculation_breakdown": calculation_breakdown,
        })

    # Sort by dollar drag (most damaging first)
    results.sort(key=lambda x: x["dollar_drag"], reverse=True)

    # Add dollar drag summary
    total_drag = sum(r["dollar_drag"] for r in results)
    for r in results:
        r["pct_of_total_drag"] = round(r["dollar_drag"] / total_drag * 100, 1) if total_drag else 0.0

    return results


# ---------------------------------------------------------------------------
# Margin summary (store-level)
# ---------------------------------------------------------------------------

def build_margin_summary(data_root: str) -> dict:
    """
    Store-level margin summary for the latest week:
    total revenue, blended margin, margin vs prior week, margin vs monthly baseline.
    """
    analysis = build_sales_analysis(data_root)
    weekly_matrix = analysis["weekly_matrix"]
    latest_wi = analysis["latest_week_index"]
    prior_wi = latest_wi - 1

    def week_totals(wi):
        total_rev = 0.0
        total_profit = 0.0
        total_promo = 0.0
        for weeks in weekly_matrix.values():
            if wi in weeks:
                total_rev += weeks[wi].get("revenue", 0.0)
                total_profit += weeks[wi].get("profit", 0.0)
                total_promo += weeks[wi].get("promo_savings", 0.0)
        margin = (total_profit / total_rev * 100) if total_rev else 0.0
        true_margin = ((total_profit - total_promo) / total_rev * 100) if total_rev else 0.0
        return {"revenue": total_rev, "margin": margin, "true_margin": true_margin, "promo": total_promo}

    curr = week_totals(latest_wi)
    prev = week_totals(prior_wi) if prior_wi >= 0 else None

    return {
        "latest_week_revenue": round(curr["revenue"], 2),
        "latest_week_margin_pct": round(curr["margin"], 2),
        "latest_week_true_margin_pct": round(curr["true_margin"], 2),
        "latest_week_promo_total": round(curr["promo"], 2),
        "wow_margin_delta_pts": round(curr["margin"] - prev["margin"], 2) if prev else None,
        "wow_true_margin_delta_pts": round(curr["true_margin"] - prev["true_margin"], 2) if prev else None,
    }

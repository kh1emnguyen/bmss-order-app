"""
sales_engine.py — BMSS Order App
Computes velocity, momentum, and trend signals from sales data.

Key outputs:
  - weekly_velocity(name) → avg cases sold/week over last 4 weeks
  - momentum_score(name)  → +ve = accelerating, -ve = decelerating
  - promo_adjusted_margin → margin after stripping Bottlemart promo savings
  - wow_delta            → week-on-week revenue/margin change
"""

from collections import defaultdict
from typing import Optional
from data_loader import normalise_name, load_all_weekly, load_monthly, load_yearly


# ---------------------------------------------------------------------------
# Velocity + momentum
# ---------------------------------------------------------------------------

def _units_sold(row: dict) -> float:
    """Convert cases + loose items into a single 'effective cases' figure."""
    # Inventory uses "Case Quantity" but sales CSV just gives cases + items separately.
    # For velocity we track total units as cases + fractional leftover.
    return row.get("cases_sold", 0.0) + row.get("items_sold", 0.0) / max(row.get("case_qty_hint", 24), 1)


def build_weekly_matrix(weekly_rows: list[dict]) -> dict:
    """
    Returns:
    {
      norm_name: {
        week_index: {
          revenue, profit, profit_pct, promo_savings, cases_sold, items_sold, txn_count, category
        }
      }
    }
    """
    matrix = defaultdict(dict)
    for row in weekly_rows:
        key = normalise_name(row["name"])
        wi = row.get("week_index", 0)
        matrix[key][wi] = {
            "name": row["name"],
            "category": row["category"],
            "revenue": row["revenue"],
            "profit": row["profit"],
            "profit_pct": row["profit_pct"],
            "promo_savings": row["promo_savings"],
            "cases_sold": row["cases_sold"],
            "items_sold": row["items_sold"],
            "txn_count": row["txn_count"],
            "week_date": row.get("week_date"),
            "period": row.get("period", ""),
        }
    return dict(matrix)


def compute_velocity(matrix: dict, n_weeks: int = 4) -> dict:
    """
    Returns per-item velocity dict:
    {
      norm_name: {
        avg_cases_per_week: float,
        last_week_cases: float,
        weeks_present: int,
        velocities: [float, ...],  # one per week, oldest first
      }
    }
    """
    velocity = {}
    for key, weeks in matrix.items():
        indices = sorted(weeks.keys())
        recent = indices[-n_weeks:] if len(indices) >= n_weeks else indices
        vels = []
        for wi in range(max(indices) + 1):  # fill gaps with 0
            if wi in weeks:
                vels.append(weeks[wi]["cases_sold"] + weeks[wi]["items_sold"] / 24.0)
            # gap weeks contribute 0 (don't include for avg — handled below)

        # Only use weeks where item actually appeared
        present_vels = [weeks[wi]["cases_sold"] + weeks[wi]["items_sold"] / 24.0
                        for wi in recent if wi in weeks]
        avg = sum(present_vels) / len(present_vels) if present_vels else 0.0
        last_wi = max(indices)
        last = weeks[last_wi]["cases_sold"] + weeks[last_wi]["items_sold"] / 24.0

        velocity[key] = {
            "avg_cases_per_week": round(avg, 3),
            "last_week_cases": round(last, 3),
            "weeks_present": len(recent),
            "velocities": [round(weeks[wi]["cases_sold"] + weeks[wi]["items_sold"] / 24.0, 3)
                           if wi in weeks else 0.0
                           for wi in sorted(matrix[key].keys())],
        }
    return velocity


def compute_momentum(matrix: dict) -> dict:
    """
    Momentum score = slope of linear trend in weekly velocity.
    Positive = accelerating. Negative = decelerating.

    pct_change = compound (CAGR-style) average week-on-week growth rate:
      rate = (v_last / v_first)^(1/(n-1)) - 1  (per week)
    This is the geometric mean of all week-on-week ratios — more meaningful
    than a simple oldest-to-newest comparison (which ignores intermediate weeks).

    Returns { norm_name: { score, direction, pct_change, label, wow_rates } }
    """
    results = {}
    for key, weeks in matrix.items():
        indices = sorted(weeks.keys())
        if len(indices) < 2:
            results[key] = {
                "score": 0.0, "direction": "stable",
                "pct_change": 0.0, "label": "Stable", "wow_rates": [],
            }
            continue

        vels = [weeks[wi]["cases_sold"] + weeks[wi]["items_sold"] / 24.0 for wi in indices]
        n = len(vels)

        # Linear slope (used for direction classification)
        x_mean = (n - 1) / 2
        y_mean = sum(vels) / n
        numerator = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(vels))
        denominator = sum((i - x_mean) ** 2 for i in range(n))
        slope = numerator / denominator if denominator else 0.0

        # Week-on-week individual rates
        wow_rates = []
        for i in range(1, n):
            prev = vels[i - 1]
            curr = vels[i]
            if prev > 0:
                wow_rates.append(round(((curr - prev) / prev) * 100.0, 1))
            else:
                # From zero: treat as +∞ if curr>0, else 0
                wow_rates.append(None)

        # Compound weekly growth rate (CAGR-style, geometric mean of ratios)
        # Find first and last non-zero velocity for the compound calc
        nonzero_vels = [(i, v) for i, v in enumerate(vels) if v > 0]
        pct_change = 0.0
        if len(nonzero_vels) >= 2:
            i0, v0 = nonzero_vels[0]
            i1, v1 = nonzero_vels[-1]
            periods = i1 - i0
            if periods > 0:
                pct_change = round(((v1 / v0) ** (1.0 / periods) - 1) * 100.0, 1)
        elif len(vels) >= 2 and vels[0] == 0 and vels[-1] > 0:
            pct_change = 100.0  # Started from zero

        if slope > 0.05:
            direction = "rising"
            label = "↑ Rising"
        elif slope < -0.05:
            direction = "falling"
            label = "↓ Falling"
        else:
            direction = "stable"
            label = "→ Stable"

        results[key] = {
            "score": round(slope, 4),
            "direction": direction,
            "pct_change": round(pct_change, 1),
            "label": label,
            "wow_rates": wow_rates,
        }
    return results


# ---------------------------------------------------------------------------
# Promo-adjusted margin
# ---------------------------------------------------------------------------

def promo_adjusted_margin(row: dict) -> dict:
    """
    Returns adjusted margin metrics with promo savings stripped out.
    Promotional Savings column = sum of Bottlemart chain discounts applied.
    """
    revenue = row.get("revenue", 0.0)
    cogs = row.get("cogs", 0.0)
    promo = row.get("promo_savings", 0.0)

    # Reported margin includes promo subsidy inflating profit
    reported_profit = row.get("profit", revenue - cogs)
    reported_margin = row.get("profit_pct", 0.0)

    # True margin = (profit - promo savings) / revenue
    # Because promo savings reduces COGS but is funded by the chain, not real store margin
    true_profit = reported_profit - promo
    true_margin = (true_profit / revenue * 100.0) if revenue else 0.0

    promo_contribution = (promo / revenue * 100.0) if revenue else 0.0

    return {
        "reported_margin_pct": round(reported_margin, 2),
        "true_margin_pct": round(true_margin, 2),
        "promo_savings": round(promo, 2),
        "promo_contribution_pts": round(promo_contribution, 2),
        "true_profit": round(true_profit, 2),
        "reported_profit": round(reported_profit, 2),
    }


# ---------------------------------------------------------------------------
# Week-on-week deltas
# ---------------------------------------------------------------------------

def compute_wow_deltas(matrix: dict) -> dict:
    """
    For the latest two available weeks, compute:
    { norm_name: { revenue_delta, profit_delta, margin_delta, volume_delta, direction } }
    """
    deltas = {}
    for key, weeks in matrix.items():
        indices = sorted(weeks.keys())
        if len(indices) < 2:
            continue
        curr = weeks[indices[-1]]
        prev = weeks[indices[-2]]

        rev_d = curr["revenue"] - prev["revenue"]
        prof_d = curr["profit"] - prev["profit"]
        mar_d = curr["profit_pct"] - prev["profit_pct"]
        vol_d = (curr["cases_sold"] - prev["cases_sold"]) + (curr["items_sold"] - prev["items_sold"]) / 24.0

        deltas[key] = {
            "revenue_delta": round(rev_d, 2),
            "profit_delta": round(prof_d, 2),
            "margin_delta_pts": round(mar_d, 2),
            "volume_delta_cases": round(vol_d, 3),
            "direction": "up" if rev_d > 0 else ("down" if rev_d < 0 else "flat"),
        }
    return deltas


# ---------------------------------------------------------------------------
# Historical baseline (monthly / yearly → avg weekly equivalent)
# ---------------------------------------------------------------------------

def build_historical_baseline(monthly_rows: list[dict], yearly_rows: list[dict]) -> dict:
    """
    Returns per-item historical baseline:
    {
      norm_name: {
        monthly_revenue,   # total in monthly period
        yearly_revenue,    # total in yearly period
        monthly_txns,
        yearly_txns,
        monthly_cases,
        yearly_cases,
        in_monthly: bool,
        in_yearly: bool,
      }
    }
    """
    baseline = {}

    def _add(rows, field_prefix, in_flag):
        for row in rows:
            key = normalise_name(row["name"])
            if key not in baseline:
                baseline[key] = {
                    "canonical_name": row["name"],
                    "category": row["category"],
                    "monthly_revenue": 0.0,
                    "monthly_txns": 0,
                    "monthly_cases": 0.0,
                    "yearly_revenue": 0.0,
                    "yearly_txns": 0,
                    "yearly_cases": 0.0,
                    "in_monthly": False,
                    "in_yearly": False,
                }
            baseline[key][f"{field_prefix}_revenue"] += row.get("revenue", 0.0)
            baseline[key][f"{field_prefix}_txns"] += row.get("txn_count", 0)
            baseline[key][f"{field_prefix}_cases"] += row.get("cases_sold", 0.0)
            baseline[key][in_flag] = True

    _add(monthly_rows, "monthly", "in_monthly")
    _add(yearly_rows, "yearly", "in_yearly")

    return baseline


# ---------------------------------------------------------------------------
# Full sales analysis bundle
# ---------------------------------------------------------------------------

def build_sales_analysis(data_root: str) -> dict:
    """
    Master function — loads all sales data and returns a complete analysis bundle.
    """
    weekly_rows = load_all_weekly(data_root)
    monthly_rows = load_monthly(data_root)
    yearly_rows = load_yearly(data_root)

    weekly_matrix = build_weekly_matrix(weekly_rows)
    velocity = compute_velocity(weekly_matrix)
    momentum = compute_momentum(weekly_matrix)
    wow = compute_wow_deltas(weekly_matrix)
    historical = build_historical_baseline(monthly_rows, yearly_rows)

    # Compute promo-adjusted margins for the most recent week
    n_weeks = max((r.get("week_index", 0) for r in weekly_rows), default=0)
    latest_week_rows = [r for r in weekly_rows if r.get("week_index") == n_weeks]
    promo_margins = {
        normalise_name(r["name"]): promo_adjusted_margin(r)
        for r in latest_week_rows
    }

    return {
        "weekly_matrix": weekly_matrix,
        "velocity": velocity,
        "momentum": momentum,
        "wow_deltas": wow,
        "historical_baseline": historical,
        "promo_margins": promo_margins,
        "n_weekly_periods": n_weeks + 1,
        "latest_week_index": n_weeks,
    }

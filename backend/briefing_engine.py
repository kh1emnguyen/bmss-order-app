"""
briefing_engine.py — BMSS Order App
Generates "Max's Briefing" — a ~1000-word plain-English weekly summary
pre-computed from all analysis engines. No AI calls required.

Outputs briefing.json with:
  {
    "generated_at": "ISO datetime",
    "week_ending": "dd Mon YYYY",
    "sections": [
      { "id": "orders",   "heading": "...", "body": "..." },
      { "id": "margins",  "heading": "...", "body": "..." },
      { "id": "pricing",  "heading": "...", "body": "..." },
      { "id": "momentum", "heading": "...", "body": "..." },
      { "id": "revival",  "heading": "...", "body": "..." },
    ],
    "full_text": "..."   # concatenated for easy display
  }
"""

from datetime import datetime
from data_loader import load_protected_items, normalise_name
from order_engine import build_order_list
from margin_engine import build_margin_analysis, build_margin_summary
from pricing_engine import build_pricing_recommendations
from revival_engine import build_revival_list
from sales_engine import build_sales_analysis
from order_engine import _best_match


def _plural(n, singular, plural=None):
    if plural is None:
        plural = singular + "s"
    return f"{n} {singular if n == 1 else plural}"


def _fmt_dollar(v):
    return f"${v:,.0f}" if v >= 1 else f"${v:.2f}"


def build_briefing(data_root: str) -> dict:
    orders   = build_order_list(data_root)
    margins  = build_margin_analysis(data_root)
    mar_sum  = build_margin_summary(data_root)
    pricing  = build_pricing_recommendations(data_root)
    revival  = build_revival_list(data_root)
    analysis = build_sales_analysis(data_root)
    protected = load_protected_items(data_root)

    now = datetime.now()
    sections = []

    # ── 1. Store snapshot ────────────────────────────────────────────────────
    rev = mar_sum.get("latest_week_revenue", 0)
    rep_margin = mar_sum.get("latest_week_margin_pct", 0)
    true_margin = mar_sum.get("latest_week_true_margin_pct", 0)
    promo_total = mar_sum.get("latest_week_promo_total", 0)
    wow_margin  = mar_sum.get("wow_margin_delta_pts")

    wow_str = ""
    if wow_margin is not None:
        direction = "up" if wow_margin > 0 else "down"
        wow_str = f" That's {abs(wow_margin):.1f} percentage point{'s' if abs(wow_margin) != 1 else ''} {direction} versus last week."

    promo_note = ""
    if promo_total > 0:
        promo_note = (
            f" Bottlemart covered {_fmt_dollar(promo_total)} in promotional discounts this week — "
            "that's chain-funded and doesn't come out of your pocket."
        )

    snapshot_body = (
        f"This week BMSS turned over {_fmt_dollar(rev)} in revenue. "
        f"Reported margin sits at {rep_margin:.1f}%, and the true margin — "
        f"after stripping out Bottlemart's promo credits — is {true_margin:.1f}%.{wow_str}{promo_note}"
    )
    sections.append({"id": "snapshot", "heading": "This Week at a Glance", "body": snapshot_body})

    # ── 2. Orders ────────────────────────────────────────────────────────────
    critical = [o for o in orders if o["urgency"] == "CRITICAL"]
    high     = [o for o in orders if o["urgency"] == "HIGH"]
    medium   = [o for o in orders if o["urgency"] == "MEDIUM"]
    total_order = len(orders)

    if not orders:
        order_body = "Stock levels look solid across all categories. No items need ordering this week."
    else:
        parts = []
        parts.append(
            f"You have {_plural(total_order, 'item')} to order this week — "
            f"{len(critical)} critical, {len(high)} high priority, and {len(medium)} medium."
        )
        if critical:
            names = ", ".join(o["name"] for o in critical[:3])
            tail = f" and {len(critical)-3} more" if len(critical) > 3 else ""
            parts.append(
                f"Critical items are your profit-protected sellers that have dropped below half a week of cover: "
                f"{names}{tail}. Get these ordered today."
            )
        if high:
            names = ", ".join(o["name"] for o in high[:3])
            tail = f" and {len(high)-3} more" if len(high) > 3 else ""
            parts.append(
                f"High-priority items include {names}{tail} — these are either out of stock or very close to it."
            )
        if medium:
            parts.append(
                f"The remaining {len(medium)} medium-priority items are within a week of running out. "
                "Order this week to avoid gaps on the shelf."
            )
        order_body = " ".join(parts)
    sections.append({"id": "orders", "heading": "What to Order", "body": order_body})

    # ── 3. Margins ───────────────────────────────────────────────────────────
    total_drag = sum(m["dollar_drag"] for m in margins)
    pricing_errors = [m for m in margins if m["cause"] == "pricing_error"]
    structural = [m for m in margins if m["cause"] == "structural"]
    chain_loss = [m for m in margins if m.get("chain_subsidised_loss", False)]

    if not margins:
        margin_body = "All active items are meeting their category margin floors. Good week for profitability."
    else:
        parts = []
        parts.append(
            f"There are {_plural(len(margins), 'item')} selling below their target margin, "
            f"representing {_fmt_dollar(total_drag)} in weekly profit drag — "
            f"roughly {_fmt_dollar(total_drag * 52)} left on the table per year if nothing changes."
        )
        if pricing_errors:
            pe_names = ", ".join(m["name"] for m in pricing_errors[:2])
            tail = f" and {len(pricing_errors)-2} more" if len(pricing_errors) > 2 else ""
            parts.append(
                f"The most urgent: {pe_names}{tail} appear to have pricing errors — "
                "their shelf price may be set too low in the POS. Check and correct these first."
            )
        if chain_loss:
            cl_names = ", ".join(m["name"] for m in chain_loss[:2])
            parts.append(
                f"Worth noting: {cl_names} and similar items are only profitable because "
                "Bottlemart is subsidising the margin. Without that chain support, "
                "you'd be selling them at a loss. Keep an eye on whether those promos continue."
            )
        if structural and not pricing_errors:
            parts.append(
                f"Most of the drag ({len(structural)} items) comes from structurally chain-priced "
                "brands where raising the price would risk losing the sale. Flag these for "
                "supplier negotiation rather than a shelf-price change."
            )
        margin_body = " ".join(parts)
    sections.append({"id": "margins", "heading": "Margin Health", "body": margin_body})

    # ── 4. Pricing opportunities ─────────────────────────────────────────────
    annual_upside = sum(p["annual_profit_gain"] for p in pricing)
    low_risk = [p for p in pricing if p["risk_level"] == "low"]
    med_risk  = [p for p in pricing if p["risk_level"] == "medium"]

    if not pricing:
        pricing_body = "No clean price increase opportunities this week — either margins are healthy or increases would be too aggressive."
    else:
        parts = []
        parts.append(
            f"There are {_plural(len(pricing), 'item')} where a small price increase "
            f"could add roughly {_fmt_dollar(annual_upside)} to annual profit at current volume."
        )
        if low_risk:
            lr_item = low_risk[0]
            parts.append(
                f"The lowest-risk opportunity is {lr_item['name']} — "
                f"a {_fmt_dollar(lr_item['price_increase_dollar'])} increase to "
                f"${lr_item['suggested_price']:.2f} would add about "
                f"{_fmt_dollar(lr_item['annual_profit_gain'])} per year. "
                "Customers in this category rarely price-shop."
            )
        if med_risk:
            parts.append(
                f"There are {len(med_risk)} medium-risk candidates in categories like bourbon and wine "
                "where you could test a modest increase but watch volume closely."
            )
        pricing_body = " ".join(parts)
    sections.append({"id": "pricing", "heading": "Pricing Opportunities", "body": pricing_body})

    # ── 5. Momentum ──────────────────────────────────────────────────────────
    velocity = analysis["velocity"]
    momentum = analysis["momentum"]

    rising = [(k, v) for k, v in momentum.items() if v["direction"] == "rising"]
    falling = [(k, v) for k, v in momentum.items() if v["direction"] == "falling"]

    rising.sort(key=lambda x: -x[1]["pct_change"])
    falling.sort(key=lambda x: x[1]["pct_change"])

    def _name_for_key(k):
        # Find canonical name from velocity map
        return velocity.get(k, {}).get("canonical_name", k.title())

    if not rising and not falling:
        momentum_body = "Sales velocity is broadly stable week on week. No standout movers this week."
    else:
        parts = []
        if rising[:3]:
            r_names = ", ".join(_name_for_key(k) for k, _ in rising[:3])
            parts.append(
                f"The strongest upward movers this week are {r_names}. "
                "Make sure these are well-stocked to capture demand."
            )
        if falling[:3]:
            f_names = ", ".join(_name_for_key(k) for k, _ in falling[:3])
            parts.append(
                f"Declining velocity on {f_names} — worth checking whether it's seasonal, "
                "a competitor move, or simply less promotional support than usual."
            )
        momentum_body = " ".join(parts)
    sections.append({"id": "momentum", "heading": "Sales Momentum", "body": momentum_body})

    # ── 6. Revival ───────────────────────────────────────────────────────────
    top_revival = revival[:3] if revival else []
    total_rev_opportunity = sum(r["est_weekly_revenue"] for r in revival)

    if not revival:
        revival_body = "All historical sellers from 2025 appear to be active in 2026. No gap to close."
    else:
        parts = []
        parts.append(
            f"There are {_plural(len(revival), 'product')} that sold in 2025 but "
            f"haven't appeared in any 2026 report yet, representing roughly "
            f"{_fmt_dollar(total_rev_opportunity)}/week in potentially recoverable revenue."
        )
        if top_revival:
            top_names = ", ".join(r["name"] for r in top_revival)
            parts.append(
                f"The top picks to chase are {top_names} — "
                "these had the highest weekly revenue in 2025 and are worth contacting your rep about."
            )
        revival_body = " ".join(parts)
    sections.append({"id": "revival", "heading": "What's Gone Missing", "body": revival_body})

    # ── Assemble full text ────────────────────────────────────────────────────
    full_text = "\n\n".join(
        f"## {s['heading']}\n\n{s['body']}" for s in sections
    )

    return {
        "generated_at": now.isoformat(),
        "week_ending": now.strftime("%d %b %Y"),
        "sections": sections,
        "full_text": full_text,
        "stats": {
            "weekly_revenue": round(rev, 2),
            "reported_margin_pct": round(rep_margin, 2),
            "true_margin_pct": round(true_margin, 2),
            "orders_critical": len(critical),
            "orders_high": len(high),
            "orders_total": total_order,
            "margin_drag_weekly": round(total_drag, 2),
            "pricing_annual_upside": round(annual_upside, 2),
            "revival_count": len(revival),
        }
    }

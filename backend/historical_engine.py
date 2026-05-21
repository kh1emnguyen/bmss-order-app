"""
historical_engine.py — BMSS Order App
Year-on-year revenue analysis across three periods:
  - 2026 YTD:  Jan 1 – Apr 12, 2026  (102 days ≈ 3.35 months)
  - 2025 full: Jan 1 – Dec 31, 2025  (365 days = 12 months)
  - H2 2024:   Jul 1 – Dec 31, 2024  (184 days ≈ 6.04 months)

Pro-rata monthly averages allow apples-to-apples YoY comparison.
"""

from data_loader import normalise_name, load_monthly, load_yearly, is_placeholder, is_keg, display_name

# Exact day counts → months (using 30.4375 days/month)
_DAYS_PER_MONTH = 30.4375
PERIOD_MONTHS = {
    "2026_ytd":  102 / _DAYS_PER_MONTH,  # Jan 1 – Apr 12, 2026
    "2025_full": 365 / _DAYS_PER_MONTH,  # Full year 2025
    "h2_2024":   184 / _DAYS_PER_MONTH,  # Jul 1 – Dec 31, 2024
}


def _aggregate(rows: list) -> dict:
    """Aggregate rows by normalised name → {key: {canonical_name, category, revenue, profit, txns, cases}}"""
    agg = {}
    for r in rows:
        if is_placeholder(r["name"]):
            continue
        if is_keg(r["name"], r.get("category", "")):
            continue
        key = normalise_name(r["name"])
        if key not in agg:
            agg[key] = {
                "canonical_name": r["name"],
                "category": r.get("category", ""),
                "revenue": 0.0, "profit": 0.0, "txns": 0, "cases": 0.0,
            }
        agg[key]["revenue"] += r.get("revenue", 0.0)
        agg[key]["profit"]  += r.get("profit", 0.0)
        agg[key]["txns"]    += r.get("txn_count", 0)
        agg[key]["cases"]   += r.get("cases_sold", 0.0)
    return agg


def build_yoy_historical(data_root: str) -> dict:
    monthly_rows = load_monthly(data_root)   # 2026 YTD
    yearly_rows  = load_yearly(data_root)    # 2025 + H2 2024

    rows_2025  = [r for r in yearly_rows if "1.1.25" in r.get("period", "")]
    rows_h2    = [r for r in yearly_rows if "1.7.24" in r.get("period", "")]

    agg_2026 = _aggregate(monthly_rows)
    agg_2025 = _aggregate(rows_2025)
    agg_h2   = _aggregate(rows_h2)

    mo_2026 = PERIOD_MONTHS["2026_ytd"]
    mo_2025 = PERIOD_MONTHS["2025_full"]
    mo_h2   = PERIOD_MONTHS["h2_2024"]

    all_keys = set(agg_2026) | set(agg_2025) | set(agg_h2)
    items = []

    for key in all_keys:
        d26 = agg_2026.get(key, {})
        d25 = agg_2025.get(key, {})
        dh2 = agg_h2.get(key, {})

        rev_26 = d26.get("revenue", 0.0)
        rev_25 = d25.get("revenue", 0.0)
        rev_h2 = dh2.get("revenue", 0.0)

        if rev_26 + rev_25 + rev_h2 < 50:
            continue

        avg_26 = rev_26 / mo_2026
        avg_25 = rev_25 / mo_2025
        avg_h2 = rev_h2 / mo_h2

        yoy_26_25 = None
        if avg_25 > 0:
            yoy_26_25 = round((avg_26 - avg_25) / avg_25 * 100, 1)
        elif avg_26 > 0:
            yoy_26_25 = 100.0

        yoy_25_h2 = None
        if avg_h2 > 0:
            yoy_25_h2 = round((avg_25 - avg_h2) / avg_h2 * 100, 1)
        elif avg_25 > 0:
            yoy_25_h2 = 100.0

        canonical = (d26.get("canonical_name") or d25.get("canonical_name")
                     or dh2.get("canonical_name") or key)
        category  = (d26.get("category") or d25.get("category")
                     or dh2.get("category") or "")

        items.append({
            "name":              canonical,
            "display_name":      display_name(canonical),
            "category":          category,
            "rev_2026_ytd":      round(rev_26, 2),
            "rev_2025_full":     round(rev_25, 2),
            "rev_h2_2024":       round(rev_h2, 2),
            "avg_month_2026":    round(avg_26, 2),
            "avg_month_2025":    round(avg_25, 2),
            "avg_month_h2_2024": round(avg_h2, 2),
            "yoy_2026_vs_2025":  yoy_26_25,
            "yoy_2025_vs_h2":    yoy_25_h2,
            "in_2026":           rev_26 > 0,
            "in_2025":           rev_25 > 0,
            "in_h2_2024":        rev_h2 > 0,
        })

    items.sort(key=lambda x: -(x["rev_2025_full"] or x["rev_2026_ytd"] or 0))

    commentary = _build_commentary(items, agg_2026, agg_2025, agg_h2,
                                   mo_2026, mo_2025, mo_h2)

    return {
        "items": items,
        "commentary": commentary,
        "period_months": {
            "2026_ytd":  round(mo_2026, 2),
            "2025_full": round(mo_2025, 2),
            "h2_2024":   round(mo_h2,   2),
        },
        "source_files": {
            "2026_ytd":  "Sales Reports/Months/1.1.26-12.4.26.csv",
            "2025_full": "Sales Reports/Yearly/1.1.25-31.12.25.csv",
            "h2_2024":   "Sales Reports/Yearly/1.7.24-31.12.24.csv",
        },
    }


def _build_commentary(items, agg_2026, agg_2025, agg_h2,
                      mo_2026, mo_2025, mo_h2) -> dict:
    total_26 = sum(d["revenue"] for d in agg_2026.values())
    total_25 = sum(d["revenue"] for d in agg_2025.values())
    total_h2 = sum(d["revenue"] for d in agg_h2.values())

    avg_26 = total_26 / mo_2026
    avg_25 = total_25 / mo_2025
    avg_h2 = total_h2 / mo_h2

    yoy_main  = (avg_26 - avg_25) / avg_25 * 100 if avg_25 else 0
    yoy_prior = (avg_25 - avg_h2) / avg_h2 * 100 if avg_h2 else 0

    # Top gainers/losers (need meaningful 2025 base of ≥$50/mo)
    qualified = [i for i in items if i["yoy_2026_vs_2025"] is not None
                 and i["avg_month_2025"] >= 50]
    gainers = sorted(qualified, key=lambda x: -(x["yoy_2026_vs_2025"] or 0))[:5]
    losers  = sorted(qualified, key=lambda x:  (x["yoy_2026_vs_2025"] or 0))[:5]

    new_items  = [i for i in items if i["in_2026"] and not i["in_2025"]
                  and i["avg_month_2026"] >= 30]
    dropped    = [i for i in items if i["in_2025"] and not i["in_2026"]
                  and i["avg_month_2025"] >= 30]

    # Category YoY
    cats = {}
    for i in items:
        cat = i["category"] or "Unknown"
        cats.setdefault(cat, {"a26": 0, "a25": 0, "ah2": 0})
        cats[cat]["a26"] += i["avg_month_2026"]
        cats[cat]["a25"] += i["avg_month_2025"]
        cats[cat]["ah2"] += i["avg_month_h2_2024"]

    cat_yoy = sorted(
        [{"category": c, "yoy": round((d["a26"]-d["a25"])/d["a25"]*100, 1),
          "avg_25": round(d["a25"], 0)}
         for c, d in cats.items() if d["a25"] >= 100],
        key=lambda x: -x["yoy"]
    )

    sections = []

    # Overall trend
    dir_word  = "up" if yoy_main > 3 else ("down" if yoy_main < -3 else "roughly flat")
    dir_word2 = "growing" if yoy_prior > 3 else ("declining" if yoy_prior < -3 else "holding steady")
    sections.append({
        "heading": "Overall Store Trend",
        "body": (
            f"Average monthly revenue is {dir_word} {abs(yoy_main):.1f}% year-on-year: "
            f"${avg_26:,.0f}/mo in 2026 YTD vs ${avg_25:,.0f}/mo across all of 2025. "
            f"Looking back further, the store was {dir_word2} from H2 2024 into 2025 "
            f"({yoy_prior:+.1f}% monthly average). "
            f"2026 YTD total: ${total_26:,.0f} across {mo_2026:.1f} months | "
            f"2025 full year: ${total_25:,.0f} | H2 2024: ${total_h2:,.0f}."
        )
    })

    # Category shifts
    growing_cats  = [c for c in cat_yoy if c["yoy"] > 10][:3]
    declining_cats = [c for c in reversed(cat_yoy) if c["yoy"] < -10][:3]
    if growing_cats or declining_cats:
        body = ""
        if growing_cats:
            body += "Growing: " + ", ".join(
                f"{c['category']} ({c['yoy']:+.0f}%)" for c in growing_cats) + ". "
        if declining_cats:
            body += "Declining: " + ", ".join(
                f"{c['category']} ({c['yoy']:+.0f}%)" for c in declining_cats) + "."
        sections.append({"heading": "Category Shifts (2026 vs 2025)", "body": body.strip()})

    # Top gainers
    if gainers:
        sections.append({
            "heading": "Top Revenue Gainers (2026 vs 2025)",
            "body": " | ".join(
                f"{i['display_name']} {i['yoy_2026_vs_2025']:+.0f}% "
                f"(${i['avg_month_2025']:.0f} → ${i['avg_month_2026']:.0f}/mo)"
                for i in gainers[:4])
        })

    # Top declines
    if losers:
        sections.append({
            "heading": "Top Revenue Declines (2026 vs 2025)",
            "body": " | ".join(
                f"{i['display_name']} {i['yoy_2026_vs_2025']:+.0f}% "
                f"(${i['avg_month_2025']:.0f} → ${i['avg_month_2026']:.0f}/mo)"
                for i in losers[:4])
        })

    if new_items:
        sections.append({
            "heading": f"New in 2026 ({len(new_items)} items)",
            "body": ", ".join(
                f"{i['display_name']} (${i['avg_month_2026']:.0f}/mo)"
                for i in new_items[:6])
        })

    if dropped:
        sections.append({
            "heading": f"Absent from 2026 So Far ({len(dropped)} items)",
            "body": ", ".join(i["display_name"] for i in dropped[:8])
                   + ". Consider whether seasonal or permanently dropped."
        })

    return {
        "store_avg_mo_2026":       round(avg_26, 2),
        "store_avg_mo_2025":       round(avg_25, 2),
        "store_avg_mo_h2_2024":    round(avg_h2, 2),
        "store_total_2026_ytd":    round(total_26, 2),
        "store_total_2025_full":   round(total_25, 2),
        "store_total_h2_2024":     round(total_h2, 2),
        "overall_yoy_2026_vs_2025": round(yoy_main, 1),
        "overall_yoy_2025_vs_h2":   round(yoy_prior, 1),
        "sections": sections,
    }

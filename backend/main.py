"""
main.py — BMSS Order App Backend
Two modes:
  1. CLI:  python main.py --run [--data /path]
     Processes all CSVs and writes JSON to data/*.json

  2. FastAPI server:  uvicorn main:app --reload
     Live endpoints that run analysis on demand

Usage:
  python main.py --run                  # one-shot analysis, write JSON
  python main.py --run --data /path     # custom data root
"""

import argparse
import json
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import DATA_ROOT, OUTPUT_DIR, OUTPUT_FILES
from data_loader import load_protected_items, load_flags, save_flags, display_name, load_current_cart
from sales_engine import build_sales_analysis
from order_engine import build_order_list
from margin_engine import build_margin_analysis, build_margin_summary
from pricing_engine import build_pricing_recommendations
from revival_engine import build_revival_list
from briefing_engine import build_briefing


# ---------------------------------------------------------------------------
# Apply display names to result lists
# ---------------------------------------------------------------------------

def _apply_display_names(items: list, key="name") -> list:
    """Replace raw POS names with simplified display names."""
    for item in items:
        if key in item:
            item["display_name"] = display_name(item[key])
    return items


# ---------------------------------------------------------------------------
# Core analysis runner
# ---------------------------------------------------------------------------

def run_analysis(data_root: str) -> dict:
    print(f"[BMSS] Running analysis: {data_root}")
    print(f"[BMSS] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    sales = build_sales_analysis(data_root)

    # --- Orders ---
    print("[BMSS] Computing order recommendations...")
    orders = build_order_list(data_root)
    _apply_display_names(orders)

    # --- Momentum ---
    print("[BMSS] Computing momentum...")
    velocity = sales["velocity"]
    momentum = sales["momentum"]
    wow      = sales["wow_deltas"]
    weekly_matrix = sales["weekly_matrix"]
    latest_wi     = sales["latest_week_index"]

    momentum_items = []
    for key, mom in momentum.items():
        if key not in weekly_matrix:
            continue
        latest = weekly_matrix[key].get(latest_wi, {})
        if not latest:
            continue
        revenue = latest.get("revenue", 0.0)
        if revenue < 20:
            continue
        vel   = velocity.get(key, {})
        wow_d = wow.get(key, {})
        item  = {
            "name":                       latest.get("name", key),
            "category":                   latest.get("category", ""),
            "revenue_latest_week":        round(revenue, 2),
            "avg_cases_per_week":         vel.get("avg_cases_per_week", 0),
            "velocities":                 vel.get("velocities", []),
            "case_qty":                   vel.get("case_qty", 24),
            "momentum_score":             mom["score"],
            "momentum_direction":         mom["direction"],
            "momentum_label":             mom["label"],
            "pct_change_oldest_to_latest": mom["pct_change"],
            "wow_revenue_delta":          wow_d.get("revenue_delta", 0.0),
            "wow_margin_delta_pts":       wow_d.get("margin_delta_pts", 0.0),
        }
        item["display_name"] = display_name(item["name"])
        momentum_items.append(item)

    rising  = sorted([x for x in momentum_items if x["momentum_direction"] == "rising"],
                     key=lambda x: -x["momentum_score"])[:30]
    falling = sorted([x for x in momentum_items if x["momentum_direction"] == "falling"],
                     key=lambda x: x["momentum_score"])[:30]
    stable  = sorted([x for x in momentum_items if x["momentum_direction"] == "stable"],
                     key=lambda x: -x["revenue_latest_week"])[:20]

    # --- Historical ---
    print("[BMSS] Computing historical baseline...")
    historical     = sales["historical_baseline"]
    historical_list = []
    for key, data in historical.items():
        monthly_rev = data.get("monthly_revenue", 0.0)
        yearly_rev  = data.get("yearly_revenue", 0.0)
        if monthly_rev + yearly_rev < 100:
            continue
        vel_data   = velocity.get(key, {})
        recent_vel = vel_data.get("avg_cases_per_week", 0.0)
        monthly_wkly = monthly_rev / 14.0 if data["in_monthly"] else 0.0
        yearly_wkly  = yearly_rev / 52.0  if data["in_yearly"]  else 0.0

        # Trend: compare recent cases/week vs monthly cases/week (same unit)
        # historical baseline now includes monthly_cases — use that for an apples-to-apples comparison
        monthly_cases_total = data.get("monthly_cases", 0.0)
        monthly_cases_per_week = (monthly_cases_total / 14.0) if (data["in_monthly"] and monthly_cases_total > 0) else 0.0
        trend = None
        if monthly_cases_per_week > 0:
            trend = round(((recent_vel - monthly_cases_per_week) / monthly_cases_per_week) * 100, 1)
        elif monthly_cases_per_week == 0 and recent_vel > 0 and data["in_monthly"]:
            trend = None  # Monthly had zero cases recorded; skip comparison
        hist_item = {
            "name":                   data["canonical_name"],
            "category":               data["category"],
            "monthly_revenue":        round(monthly_rev, 2),
            "yearly_revenue":         round(yearly_rev, 2),
            "monthly_txns":           data.get("monthly_txns", 0),
            "yearly_txns":            data.get("yearly_txns", 0),
            "est_weekly_from_monthly": round(monthly_wkly, 2),
            "est_weekly_from_yearly":  round(yearly_wkly, 2),
            "recent_weekly_velocity":  round(recent_vel, 3),
            "trend_vs_monthly":        trend,
            "in_monthly":             data["in_monthly"],
            "in_yearly":              data["in_yearly"],
        }
        hist_item["display_name"] = display_name(hist_item["name"])
        historical_list.append(hist_item)
    historical_list.sort(key=lambda x: -(x["monthly_revenue"] or x["yearly_revenue"]))

    # --- Margins ---
    print("[BMSS] Computing margin analysis...")
    margins      = build_margin_analysis(data_root)
    margin_summary = build_margin_summary(data_root)
    _apply_display_names(margins)

    # --- Pricing ---
    print("[BMSS] Computing pricing recommendations...")
    pricing = build_pricing_recommendations(data_root)
    _apply_display_names(pricing)

    # --- Revival ---
    print("[BMSS] Computing revival list...")
    revival = build_revival_list(data_root)
    _apply_display_names(revival)

    # --- Briefing ---
    print("[BMSS] Generating Max's Briefing...")
    try:
        briefing = build_briefing(data_root)
    except Exception as e:
        print(f"[BMSS] Warning: briefing generation failed: {e}")
        briefing = {
            "generated_at": datetime.now().isoformat(),
            "week_ending": datetime.now().strftime("%d %b %Y"),
            "sections": [],
            "full_text": "Briefing unavailable — run analysis again.",
            "stats": {},
        }

    # --- Summary ---
    protected = load_protected_items(data_root)
    summary = {
        "generated_at": datetime.now().isoformat(),
        "data_root": data_root,
        "n_weekly_periods": sales["n_weekly_periods"],
        "latest_week_index": latest_wi,
        "orders_critical": len([o for o in orders if o["urgency"] == "CRITICAL"]),
        "orders_high":     len([o for o in orders if o["urgency"] == "HIGH"]),
        "orders_total":    len(orders),
        "margin_issues_count":     len(margins),
        "margin_total_dollar_drag": round(sum(m["dollar_drag"] for m in margins), 2),
        "pricing_opportunities_count": len(pricing),
        "pricing_annual_upside": round(sum(p["annual_profit_gain"] for p in pricing), 2),
        "revival_candidates_count": len(revival),
        "store_margin": margin_summary,
        "protected_profit_items":   len(protected["profit"]),
        "protected_revenue_items":  len(protected["revenue"]),
    }

    return {
        "orders":   orders,
        "momentum": {"rising": rising, "falling": falling, "stable": stable},
        "historical": historical_list[:100],
        "margins":  margins,
        "pricing":  pricing,
        "revival":  revival,
        "summary":  summary,
        "briefing": briefing,
    }


# ---------------------------------------------------------------------------
# CLI runner
# ---------------------------------------------------------------------------

def run_and_save(data_root: str):
    OUTPUT_DIR.mkdir(exist_ok=True)
    results = run_analysis(data_root)

    # Write standard outputs
    for key, filepath in OUTPUT_FILES.items():
        data = results.get(key)
        if data is not None:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str)
            print(f"[BMSS] Wrote {filepath.name} ({len(json.dumps(data, default=str)) // 1024}KB)")

    # Write briefing separately
    briefing_path = OUTPUT_DIR / "briefing.json"
    with open(briefing_path, "w", encoding="utf-8") as f:
        json.dump(results["briefing"], f, indent=2, default=str)
    print(f"[BMSS] Wrote briefing.json")

    print("[BMSS] ✓ Analysis complete.")
    print(f"[BMSS] Orders: {results['summary']['orders_total']} items "
          f"({results['summary']['orders_critical']} critical)")
    print(f"[BMSS] Margin drag: ${results['summary']['margin_total_dollar_drag']:.2f}/wk")
    print(f"[BMSS] Pricing upside: ${results['summary']['pricing_annual_upside']:.2f}/yr")
    print(f"[BMSS] Revival candidates: {results['summary']['revival_candidates_count']}")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

try:
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware

    app = FastAPI(title="BMSS Order App API", version="2.0")
    app.add_middleware(CORSMiddleware, allow_origins=["*"],
                       allow_methods=["*"], allow_headers=["*"])

    def _load_json(key: str):
        fp = OUTPUT_DIR / f"{key}.json"
        if not fp.exists():
            fp = OUTPUT_FILES.get(key)
        if fp and Path(fp).exists():
            with open(fp) as f:
                return json.load(f)
        results = run_analysis(DATA_ROOT)
        return results.get(key, [])

    @app.get("/api/bmss/summary")   
    def get_summary():   return _load_json("summary")
    @app.get("/api/bmss/orders")    
    def get_orders():    return _load_json("orders")
    @app.get("/api/bmss/momentum")  
    def get_momentum():  return _load_json("momentum")
    @app.get("/api/bmss/historical")
    def get_historical():return _load_json("historical")
    @app.get("/api/bmss/margins")   
    def get_margins():   return _load_json("margins")
    @app.get("/api/bmss/pricing")   
    def get_pricing():   return _load_json("pricing")
    @app.get("/api/bmss/revival")   
    def get_revival():   return _load_json("revival")
    @app.get("/api/bmss/briefing")  
    def get_briefing():  return _load_json("briefing")

    @app.post("/api/bmss/refresh")
    def refresh_analysis():
        run_and_save(DATA_ROOT)
        return {"status": "ok", "generated_at": datetime.now().isoformat()}

    @app.get("/api/bmss/flags")
    def get_flags():
        return load_flags(DATA_ROOT)

    @app.post("/api/bmss/flags")
    def update_flags(body: dict):
        current = load_flags(DATA_ROOT)
        for k in (
            "obsolete_items", "limited_edition_overrides", "seasonal_overrides",
            "skipped_items", "discarded_from_revival", "archived_from_orders",
        ):
            if k in body:
                current[k] = body[k]
        save_flags(DATA_ROOT, current)
        return {"status": "ok", "flags": current}

except ImportError:
    app = None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BMSS Order App — Analysis Runner")
    parser.add_argument("--run",  action="store_true", help="Run analysis and write JSON")
    parser.add_argument("--data", type=str, help="Override data root path")
    args = parser.parse_args()

    data_root = args.data or DATA_ROOT
    if args.run:
        run_and_save(data_root)
    else:
        parser.print_help()

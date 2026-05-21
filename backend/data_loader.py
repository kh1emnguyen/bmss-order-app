"""
data_loader.py — BMSS Order App
Normalises all CSV inputs into clean Python dicts.

Input folders (relative to DATA_ROOT in config.py):
  Sales Reports/Weekly/     *.csv
  Sales Reports/Months/     *.csv
  Sales Reports/Yearly/     *.csv  (handles 9-col 2025 AND 14-col H2-2024 formats)
  Inventory Lists/          *.csv (latest by filename date)
  Current Cart/             *.csv (latest by filename date — items already ordered)
  BMSS Items to Protect - Profit.csv
  BMSS Items to Protect - Revenue.csv
"""

import re
import csv
import glob
import json as _json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Dollar / percentage helpers
# ---------------------------------------------------------------------------

def parse_dollar(value: str) -> float:
    if not value:
        return 0.0
    s = str(value).strip().strip('"').replace(",", "")
    negative = s.startswith("-")
    s = s.lstrip("-").lstrip("$")
    try:
        result = float(s)
        return -result if negative else result
    except ValueError:
        return 0.0


def parse_percent(value: str) -> float:
    if not value:
        return 0.0
    s = str(value).strip().strip('"').replace(",", "").replace("%", "")
    try:
        v = float(s)
        return max(-999.0, min(999.0, v))
    except ValueError:
        return 0.0


def parse_int(value: str) -> int:
    try:
        return int(str(value).strip().strip('"').replace(",", ""))
    except ValueError:
        return 0


def parse_float(value: str) -> float:
    try:
        return float(str(value).strip().strip('"').replace(",", ""))
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# Date parsing — filenames like "4.5.26-10.5.26.csv"
# ---------------------------------------------------------------------------

def parse_date_from_filename(filename: str) -> Optional[datetime]:
    name = Path(filename).stem
    match = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{2,4})", name)
    if match:
        day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
        if year < 100:
            year += 2000
        try:
            return datetime(year, month, day)
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------------------
# Display name simplification
# Strip trailing pack-size qualifiers (24pk, 30pk, 24p, 6pk, 4 Pack, Carton, etc.)
# Result: "Brand Name Volume" e.g. "Carlton Draught Can 375ml"
# ---------------------------------------------------------------------------

_PACK_SUFFIX_RE = re.compile(
    r"""(\s+
        (?:
          \d+\s*(?:pk|p)\b\.?   # 24pk / 24p / 30pk
        | (?:Pack|Carton|Pk\.?) # Pack / Carton / Pk
        | \d+\s*x\s*\d+         # 6x4 / 24x375
        )
    )+\s*$""",
    re.VERBOSE | re.IGNORECASE,
)

def display_name(name: str) -> str:
    """
    Return a simplified display name: brand + product + volume.
    Strips trailing pack-size descriptors but keeps the volume (mL / L / %).
    """
    cleaned = _PACK_SUFFIX_RE.sub("", name).strip()
    return cleaned if cleaned else name


# ---------------------------------------------------------------------------
# Volume / format extraction — used for discriminating name matches
# ---------------------------------------------------------------------------

_VOLUME_RE = re.compile(r'\b(\d+(?:\.\d+)?)\s*(ml|l)\b', re.IGNORECASE)
_FORMAT_RE = re.compile(r'\b(bt|btl|bottle|bottles|can|cans|tin|tins)\b', re.IGNORECASE)

def extract_volume_ml(name: str) -> Optional[float]:
    """Return volume in ml from a product name, or None if absent."""
    m = _VOLUME_RE.search(name.lower())
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2).lower()
    return val * 1000 if unit == 'l' else val

def extract_format(name: str) -> Optional[str]:
    """Return 'bottle' or 'can' if format is discernible, else None."""
    m = _FORMAT_RE.search(name.lower())
    if not m:
        return None
    f = m.group(1).lower()
    if f in ('bt', 'btl', 'bottle', 'bottles'):
        return 'bottle'
    if f in ('can', 'cans', 'tin', 'tins'):
        return 'can'
    return None

def names_are_compatible(name_a: str, name_b: str) -> bool:
    """
    Return False if the two names have explicitly incompatible volume or format.
    Used to prevent cross-matching (e.g. 330ml bottle ↔ 375ml can).
    """
    vol_a = extract_volume_ml(name_a)
    vol_b = extract_volume_ml(name_b)
    if vol_a is not None and vol_b is not None:
        # Allow ±10% tolerance for volume (e.g. 330 vs 355)
        if abs(vol_a - vol_b) / max(vol_a, vol_b) > 0.10:
            return False

    fmt_a = extract_format(name_a)
    fmt_b = extract_format(name_b)
    if fmt_a is not None and fmt_b is not None:
        if fmt_a != fmt_b:
            return False

    return True


# ---------------------------------------------------------------------------
# Sales CSV parser — handles weekly/monthly (12-col) AND both yearly formats
# ---------------------------------------------------------------------------

def _map_columns_from_header(header: list[str]) -> dict:
    h = [c.strip().strip('"').lower() for c in header]
    def find(*candidates):
        for c in candidates:
            for i, col in enumerate(h):
                if c in col:
                    return i
        return None

    return {
        "revenue":       find("revenue"),
        "cogs":          find("cost of goods", "cogs"),
        "txn_count":     find("transaction count", "txn count"),
        "profit":        find("profit\",", '"profit"', "^profit$", "profit,"),
        "profit_pct":    find("profit percentage", "profit %"),
        "cases_sold":    find("calculated cases", "cases sold"),
        "items_sold":    find("calculated items", "items sold"),
        "promo_savings": find("promotional savings", "promo"),
        "revenue_pct":   find("revenue percentage", "revenue %"),
        "txn_pct":       find("transaction percentage", "txn %"),
    }


def _map_columns_from_header_v2(header: list[str]) -> dict:
    h = [c.strip().strip('"').lower() for c in header]
    result = {}

    def _find(col_list):
        for needle in col_list:
            for i, h_col in enumerate(h):
                if needle in h_col:
                    return i
        return None

    result["revenue"]       = _find(["revenue"])
    result["cogs"]          = _find(["cost of goods", "cogs"])
    result["txn_count"]     = _find(["transaction count"])
    result["cases_sold"]    = _find(["calculated cases"])
    result["items_sold"]    = _find(["calculated items"])
    result["revenue_pct"]   = _find(["revenue percentage"])
    result["txn_pct"]       = _find(["transaction percentage"])
    result["promo_savings"] = _find(["promotional savings", "promotional saving"])

    for i, h_col in enumerate(h):
        if h_col == "profit" or h_col == '"profit"':
            result["profit"] = i
            break
    else:
        result["profit"] = None

    for i, h_col in enumerate(h):
        if "profit percentage" in h_col or "profit %" in h_col:
            result["profit_pct"] = i
            break
    else:
        result["profit_pct"] = None

    return result


def _is_subtotal_row(row: dict) -> bool:
    name = row.get("name", "").strip()
    cat = row.get("category", "").strip()
    if not name:
        return True
    rev_pct = row.get("revenue_pct", "")
    if rev_pct == "100.00%" and not cat:
        return True
    return False


# ---------------------------------------------------------------------------
# Placeholder item detection — POS line items that are not real products
# ---------------------------------------------------------------------------

_PLACEHOLDER_PATTERNS = [
    "$5 craft beer",
    "credit card surcharge",
    "ice bag",
    "drink here",
    "happy hour",
    "cafe sales",
    "little fat lamb shot single",
]

def is_placeholder(name: str) -> bool:
    """Return True if this POS item is a placeholder (not a real orderable product)."""
    n = name.lower().strip()
    for p in _PLACEHOLDER_PATTERNS:
        if p in n:
            return True
    # Any "bag" item (carry bags, paper bags, etc.)
    if re.search(r'\bbags?\b', n):
        return True
    return False


_NOISE_PATTERNS = [
    "loyalty offer", "clearance product", "loyalty-",
    "loyalty points", "price override", "administration",
]


def load_sales_csv(filepath: str, period_label: str = "") -> list[dict]:
    """
    Load a Bottlemart sales CSV (weekly, monthly, or yearly).
    Uses header-based column detection — handles all formats.
    """
    rows = []
    filepath = Path(filepath)
    if not filepath.exists():
        return rows

    with open(filepath, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header is None:
            return rows

        col_map = _map_columns_from_header_v2(header)
        current_category = ""

        for raw in reader:
            if not raw:
                continue

            cat_raw = raw[0].strip().strip('"')
            name_raw = raw[1].strip().strip('"') if len(raw) > 1 else ""

            if cat_raw:
                current_category = cat_raw
            if not name_raw:
                continue

            def _get(key, parser=parse_dollar, default=0.0):
                idx = col_map.get(key)
                if idx is None or idx >= len(raw):
                    return default
                return parser(raw[idx])

            revenue     = _get("revenue",       parse_dollar, 0.0)
            cogs        = _get("cogs",           parse_dollar, 0.0)
            txn_count   = _get("txn_count",      parse_int,    0)
            profit      = _get("profit",         parse_dollar, 0.0)
            profit_pct  = _get("profit_pct",     parse_percent, 0.0)
            cases_sold  = _get("cases_sold",     parse_float,  0.0)
            items_sold  = _get("items_sold",     parse_float,  0.0)
            promo       = _get("promo_savings",  parse_dollar, 0.0)
            rev_pct     = _get("revenue_pct",    parse_percent, 0.0)
            txn_pct     = _get("txn_pct",        parse_percent, 0.0)

            if profit == 0.0 and revenue != 0.0 and profit_pct != 0.0:
                profit = revenue * profit_pct / 100.0

            row = {
                "category":     current_category,
                "name":         name_raw,
                "revenue":      revenue,
                "cogs":         cogs,
                "txn_count":    txn_count,
                "profit":       profit,
                "profit_pct":   profit_pct,
                "cases_sold":   cases_sold,
                "items_sold":   items_sold,
                "promo_savings": promo,
                "revenue_pct":  rev_pct,
                "txn_pct":      txn_pct,
                "period":       period_label,
                "source_file":  filepath.name,
            }

            if _is_subtotal_row(row):
                continue

            name_lower = row["name"].lower()
            if any(x in name_lower for x in _NOISE_PATTERNS):
                continue

            # Exclude placeholder POS items
            if is_placeholder(row["name"]):
                continue

            rows.append(row)

    return rows


def load_all_weekly(data_root: str) -> list[dict]:
    folder = Path(data_root) / "Sales Reports" / "Weekly"
    files = sorted(glob.glob(str(folder / "*.csv")))
    all_rows = []
    for idx, fp in enumerate(files):
        dt = parse_date_from_filename(fp)
        label = dt.strftime("Week of %d %b %Y") if dt else f"Week {idx+1}"
        rows = load_sales_csv(fp, period_label=label)
        for r in rows:
            r["week_date"] = dt
            r["week_index"] = idx
        all_rows.extend(rows)
    return all_rows


def load_monthly(data_root: str) -> list[dict]:
    folder = Path(data_root) / "Sales Reports" / "Months"
    files = sorted(glob.glob(str(folder / "*.csv")))
    all_rows = []
    for fp in files:
        dt = parse_date_from_filename(fp)
        label = dt.strftime("%b %Y") if dt else Path(fp).stem
        rows = load_sales_csv(fp, period_label=label)
        for r in rows:
            r["week_date"] = dt
            r["week_index"] = -1
        all_rows.extend(rows)
    return all_rows


def load_yearly(data_root: str) -> list[dict]:
    folder = Path(data_root) / "Sales Reports" / "Yearly"
    files = sorted(glob.glob(str(folder / "*.csv")))
    all_rows = []
    for fp in files:
        dt = parse_date_from_filename(fp)
        label = Path(fp).stem
        rows = load_sales_csv(fp, period_label=label)
        for r in rows:
            r["week_date"] = dt
            r["week_index"] = -2
        all_rows.extend(rows)
    return all_rows


# ---------------------------------------------------------------------------
# Current Cart CSV parser
# ---------------------------------------------------------------------------

def load_current_cart(data_root: str) -> list[dict]:
    """
    Load the most recent CSV from Current Cart/ folder.
    Returns list of {name, carton_size, cartons_ordered, units_ordered, total_units_ordered}.
    """
    folder = Path(data_root) / "Current Cart"
    if not folder.exists():
        return []
    files = glob.glob(str(folder / "*.csv"))
    if not files:
        return []

    def file_date(fp):
        d = parse_date_from_filename(fp)
        return d or datetime.min

    latest = max(files, key=file_date)
    items = []

    with open(latest, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            desc = raw.get("Description", "").strip().strip('"')
            if not desc:
                continue
            carton_size = parse_int(raw.get("Carton Size", "0"))
            cartons = parse_float(raw.get("Cartons", "0"))
            units = parse_float(raw.get("Units", "0"))
            if carton_size < 1:
                carton_size = 1
            total_units = cartons * carton_size + units
            items.append({
                "name": desc,
                "carton_size": carton_size,
                "cartons_ordered": cartons,
                "units_ordered": units,
                "total_units_ordered": max(0.0, total_units),
            })
    return items


# ---------------------------------------------------------------------------
# Inventory CSV parser
# ---------------------------------------------------------------------------

def load_inventory(data_root: str) -> list[dict]:
    folder = Path(data_root) / "Inventory Lists"
    files = glob.glob(str(folder / "*.csv"))
    if not files:
        return []

    def file_date(fp):
        d = parse_date_from_filename(fp)
        return d or datetime.min

    latest = max(files, key=file_date)

    seen_ids = {}
    with open(latest, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            pid = raw.get("ID", "").strip().strip('"')
            name = raw.get("Name", "").strip().strip('"')
            if not name:
                continue
            key = pid if pid else name

            if key not in seen_ids:
                seen_ids[key] = {
                    "id": pid,
                    "name": name,
                    "category": raw.get("Category", "").strip().strip('"'),
                    "case_qty": parse_int(raw.get("Case Quantity", "0")),
                    "cases_on_hand": parse_float(raw.get("Cases on hand", "0")),
                    "items_on_hand": parse_float(raw.get("Items on hand", "0")),
                    "cost": parse_dollar(raw.get("Cost", "0")),
                    "price_single": parse_dollar(raw.get("Price", "0")),
                    "profit_pct_single": parse_percent(raw.get("Profit", "0")),
                    "price_tiers": [],
                    "source_file": Path(latest).name,
                }

            qty = parse_int(raw.get("Quantity", "0"))
            price = parse_dollar(raw.get("Price", "0"))
            profit_pct = parse_percent(raw.get("Profit", "0"))
            if qty and price:
                seen_ids[key]["price_tiers"].append({
                    "qty": qty,
                    "price": price,
                    "profit_pct": profit_pct,
                })

    for item in seen_ids.values():
        case_qty = item["case_qty"] or 1
        raw_units = item["cases_on_hand"] * case_qty + item["items_on_hand"]
        item["total_units"] = max(0.0, raw_units)
        if item["price_tiers"]:
            single_tier = min(item["price_tiers"], key=lambda t: t["qty"])
            item["price_single"] = single_tier["price"]
            item["profit_pct_single"] = single_tier["profit_pct"]

    return list(seen_ids.values())


# ---------------------------------------------------------------------------
# Protected items CSV parser
# ---------------------------------------------------------------------------

def load_protected_items(data_root: str) -> dict:
    root = Path(data_root)
    return {
        "profit":  _parse_profit_csv(root / "BMSS Items to Protect - Profit.csv"),
        "revenue": _parse_revenue_csv(root / "BMSS Items to Protect - Revenue.csv"),
    }


def _parse_profit_csv(filepath: Path) -> list[dict]:
    items = []
    if not filepath.exists():
        return items
    with open(filepath, newline="", encoding="latin-1") as f:
        reader = csv.reader(f)
        in_data = False
        for row in reader:
            if not row:
                continue
            if row[0].strip() == "#" or (len(row) > 1 and "Product" in row[1]):
                in_data = True
                continue
            if not in_data:
                continue
            if not row[0].strip() or not row[0].strip().isdigit():
                continue
            items.append({
                "rank": parse_int(row[0]),
                "name": row[1].strip().strip('"') if len(row) > 1 else "",
                "weekly_profit": parse_dollar(row[2]) if len(row) > 2 else 0.0,
                "profit_14wk": parse_dollar(row[3]) if len(row) > 3 else 0.0,
                "margin": parse_percent(row[4]) if len(row) > 4 else 0.0,
                "txns": parse_int(row[5]) if len(row) > 5 else 0,
            })
    return items


def _parse_revenue_csv(filepath: Path) -> list[dict]:
    items = []
    if not filepath.exists():
        return items
    with open(filepath, newline="", encoding="latin-1") as f:
        reader = csv.reader(f)
        in_data = False
        for row in reader:
            if not row:
                continue
            if row[0].strip() == "#" or (len(row) > 1 and "Product" in row[1]):
                in_data = True
                continue
            if not in_data:
                continue
            if not row[0].strip() or not row[0].strip().isdigit():
                continue
            items.append({
                "rank": parse_int(row[0]),
                "name": row[1].strip().strip('"') if len(row) > 1 else "",
                "revenue_14wk": parse_dollar(row[2]) if len(row) > 2 else 0.0,
                "profit_14wk": parse_dollar(row[3]) if len(row) > 3 else 0.0,
                "margin": parse_percent(row[4]) if len(row) > 4 else 0.0,
                "txns": parse_int(row[5]) if len(row) > 5 else 0,
                "cumulative_rev_pct": parse_percent(row[6]) if len(row) > 6 else 0.0,
            })
    return items


# ---------------------------------------------------------------------------
# Name normalisation
# ---------------------------------------------------------------------------

def normalise_name(name: str) -> str:
    s = name.lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[''`]", "", s)
    s = s.strip()
    return s


def build_name_index(items: list, name_key: str = "name") -> dict:
    return {normalise_name(item[name_key]): item for item in items}


# ---------------------------------------------------------------------------
# Keg detection
# ---------------------------------------------------------------------------

def is_keg(name: str, category: str) -> bool:
    name_l = name.lower()
    cat_l = category.lower().strip()
    if "keg" in cat_l or "keg" in name_l:
        return True
    return False


# ---------------------------------------------------------------------------
# Weekly-sold name set
# ---------------------------------------------------------------------------

def get_weekly_sold_names(data_root: str, min_units: float = 0.5) -> set:
    rows = load_all_weekly(data_root)
    sold = set()
    for r in rows:
        if r.get("items_sold", 0) + r.get("cases_sold", 0) * 24 >= min_units:
            sold.add(normalise_name(r["name"]))
        elif r.get("revenue", 0) > 0:
            sold.add(normalise_name(r["name"]))
    return sold


# ---------------------------------------------------------------------------
# 6-month selling rate check (historical + weekly combined)
# ---------------------------------------------------------------------------

def build_six_month_rate(data_root: str) -> dict:
    """
    Returns {norm_name: total_units_sold_in_6_months} across monthly + weekly data.
    Used to filter out items selling < 1 unit per 6 months from reorder panel.
    The monthly report covers Jan–Apr 2026 (~14 weeks) and weekly covers ~4 weeks —
    we combine both to approximate a 6-month window.
    """
    from collections import defaultdict
    rate = defaultdict(float)
    monthly = load_monthly(data_root)
    weekly = load_all_weekly(data_root)
    for r in monthly + weekly:
        key = normalise_name(r["name"])
        units = r.get("items_sold", 0) + r.get("cases_sold", 0) * 24
        rate[key] += units
    return dict(rate)


# ---------------------------------------------------------------------------
# Flags — persistent obsolete / limited-edition / skip / discard storage
# ---------------------------------------------------------------------------

def _flags_path(data_root: str) -> Path:
    return Path(data_root) / "data" / "flags.json"


def load_flags(data_root: str) -> dict:
    fp = _flags_path(data_root)
    if fp.exists():
        try:
            with open(fp, encoding="utf-8") as f:
                data = _json.load(f)
                return {
                    "obsolete_items":             data.get("obsolete_items", []),
                    "limited_edition_overrides":  data.get("limited_edition_overrides", []),
                    "seasonal_overrides":         data.get("seasonal_overrides", []),
                    # Short-term skip: will not order this week — restorable
                    "skipped_items":              data.get("skipped_items", []),
                    # Items discarded from revival — never resurface
                    "discarded_from_revival":     data.get("discarded_from_revival", []),
                    # Items discarded from orders pane — archived to revival consideration
                    "archived_from_orders":       data.get("archived_from_orders", []),
                }
        except Exception:
            pass
    return {
        "obsolete_items": [],
        "limited_edition_overrides": [],
        "seasonal_overrides": [],
        "skipped_items": [],
        "discarded_from_revival": [],
        "archived_from_orders": [],
    }


def save_flags(data_root: str, flags: dict):
    fp = _flags_path(data_root)
    fp.parent.mkdir(parents=True, exist_ok=True)
    with open(fp, "w", encoding="utf-8") as f:
        _json.dump(flags, f, indent=2)

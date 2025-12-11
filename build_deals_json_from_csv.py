import json
from pathlib import Path

import pandas as pd  # requires: py -m pip install pandas


ROOT = Path(__file__).parent
CSV_PATH = ROOT / "deals_today.csv"
JSON_PATH = ROOT / "deals_today.json"


def build_product_name(row: pd.Series) -> str:
    """
    Build a readable product name like:
    'Clif Builders Protein Bar Chocolate Mint (1ct)'
    But avoid double brand names like 'Clif Clif Builders ...'
    """
    parts = []

    brand = row.get("brand")
    base_name = row.get("product_name")

    brand_str = (brand or "").strip()
    base_str = (base_name or "").strip()

    if brand_str:
        # Only add brand if base_name doesn't already start with it (case-insensitive)
        if not base_str.lower().startswith(brand_str.lower()):
            parts.append(brand_str)

    if base_str:
        parts.append(base_str)

    flavor = row.get("flavor")
    if isinstance(flavor, str) and flavor.strip():
        parts.append(flavor.strip())

    # Optional pack size in name
    pack_size = row.get("pack_size")
    try:
        if pack_size and not pd.isna(pack_size):
            parts.append(f"({int(pack_size)}ct)")
    except Exception:
        pass

    return " ".join(parts) if parts else "Unknown product"

def get_percent_off(row):
    pct_off_raw = row.get("pct_off")
    price = row.get("price")
    baseline = row.get("baseline")

    try:
        if pct_off_raw is not None and not pd.isna(pct_off_raw):
            return float(pct_off_raw) * 100.0
    except Exception:
        pass

    # Fallback: compute from baseline + price if available
    try:
        if baseline is not None and not pd.isna(baseline):
            baseline = float(baseline)
            price = float(price)
            if baseline > 0 and price <= baseline:
                return max(0.0, (baseline - price) / baseline * 100.0)
    except Exception:
        pass

    return 0.0


def get_streak_days(row):
    """Try a few possible column names for 'days this deal has been active'."""
    streak_cols = [
        "deal_streak_days",
        "streak_days",
        "deal_streak",
        "days_on_deal",
    ]
    for col in streak_cols:
        if col in row and col in row.index:
            val = row.get(col)
            try:
                if val is None or pd.isna(val):
                    return None
                iv = int(val)
                if iv >= 1:
                    return iv
            except Exception:
                return None
    return None


def get_unit_label(row):
    """
    Helper for the black quantity pill on cards, e.g. '1 bar', '5 bars'.
    Falls back to 'ct' if we don't know better.
    """
    pack_size = row.get("pack_size")
    try:
        if pack_size is None or pd.isna(pack_size):
            return None, None
        count = int(pack_size)
    except Exception:
        return None, None

    category = (row.get("category") or "").lower()
    section = (row.get("section") or "").lower()

    # Treat bar-like things as 'bar(s)'
    if "bar" in category or "bar" in section:
        unit = "bar" if count == 1 else "bars"
    else:
        unit = "ct"

    return count, unit


def main():
    if not CSV_PATH.exists():
        raise SystemExit(f"CSV file not found: {CSV_PATH}")

    df = pd.read_csv(CSV_PATH)

    deals = []

    for _, row in df.iterrows():
        price = row.get("price")
        try:
            new_price = float(price)
        except Exception:
            # No usable price, skip
            continue

        # Original/baseline price (may be missing)
        old_price = None
        baseline = row.get("baseline")
        try:
            if baseline is not None and not pd.isna(baseline):
                old_price = float(baseline)
        except Exception:
            old_price = None

        percent_off = get_percent_off(row)

        product_name = build_product_name(row)

        availability = row.get("availability_norm", "")
        # Let the page generator normalise this; just pass through raw value

        # Deal strength from enrichment (e.g. '🔥 strong', '🟡 mild')
        deal_strength = row.get("deal_strength", "")

        # Optional streak days
        streak_days = get_streak_days(row)

        # Optional quantity pill
        pack_count, pack_unit = get_unit_label(row)

        deal = {
            "product_name": product_name,
            "retailer": row.get("retailer", ""),
            # Top-level grouping: Food / Drinks
            "section": row.get("section", ""),
            # Sub-category within that section: bars, chips, energy drinks, etc.
            "category": row.get("category", ""),
            "old_price": old_price,
            "new_price": new_price,
            "percent_off": round(float(percent_off), 1),
            "image_url": row.get("image_url", ""),
            "retailer_url": row.get("canonical_url", ""),
            "availability": availability,
            "verified_at": row.get("timestamp", ""),
            "deal_strength": deal_strength,
            "pack_count": pack_count,
            "pack_unit": pack_unit,
            "streak_days": streak_days,
        }

        deals.append(deal)

    JSON_PATH.write_text(json.dumps(deals, indent=2), encoding="utf-8")
    print(f"Wrote {JSON_PATH} with {len(deals)} deals.")


if __name__ == "__main__":
    main()


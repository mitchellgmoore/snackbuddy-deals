import json
from pathlib import Path

import pandas as pd  # if this errors, run:  py -m pip install pandas

ROOT = Path(__file__).parent
CSV_PATH = ROOT / "deals_today.csv"
JSON_PATH = ROOT / "deals_today.json"


def build_product_name(row):
    """
    Build a nice human-readable name like:
    'Legendary Foods Protein Pastry Cherry Crumble (4ct)'
    """
    parts = []

    brand = row.get("brand")
    if isinstance(brand, str) and brand.strip():
        parts.append(brand.strip())

    base_name = row.get("product_name")
    if isinstance(base_name, str) and base_name.strip():
        parts.append(base_name.strip())

    flavor = row.get("flavor")
    if isinstance(flavor, str) and flavor.strip():
        parts.append(flavor.strip())

    pack_size = row.get("pack_size")
    try:
        if pack_size and not pd.isna(pack_size):
            parts.append(f"({int(pack_size)}ct)")
    except Exception:
        # If pack_size is weird, just skip it
        pass

    return " ".join(parts) if parts else "Unknown product"


def main():
    if not CSV_PATH.exists():
        raise SystemExit(f"CSV file not found: {CSV_PATH}")

    df = pd.read_csv(CSV_PATH)

    deals = []

    for _, row in df.iterrows():
        # Skip rows without a current price
        price = row.get("price")
        baseline = row.get("baseline")
        pct_off_raw = row.get("pct_off")

        try:
            price = float(price)
        except Exception:
            continue  # no usable price, skip

        # Handle baseline (original price)
        old_price = None
        try:
            if baseline and not pd.isna(baseline):
                old_price = float(baseline)
        except Exception:
            old_price = None

        # pct_off in your CSV is a fraction (0.37 = 37%)
        percent_off = 0.0
        try:
            if pct_off_raw and not pd.isna(pct_off_raw):
                percent_off = float(pct_off_raw) * 100.0
            elif old_price:
                percent_off = max(0.0, (old_price - price) / old_price * 100.0)
        except Exception:
            percent_off = 0.0

        product_name = build_product_name(row)

        deal = {
            "product_name": product_name,
            "retailer": row.get("retailer", ""),
            "category": row.get("category", ""),
            "old_price": old_price,
            "new_price": price,
            "percent_off": round(percent_off, 1),
            "image_url": row.get("image_url", ""),
            "retailer_url": row.get("canonical_url", ""),
            "availability": row.get("availability_norm", ""),
            "verified_at": row.get("timestamp", ""),
        }

        deals.append(deal)

    # Write out JSON in the shape the site generator expects
    JSON_PATH.write_text(json.dumps(deals, indent=2), encoding="utf-8")
    print(f"Wrote {JSON_PATH} with {len(deals)} deals.")


if __name__ == "__main__":
    main()

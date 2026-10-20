"""
Build a compact last_sale.json for the SnackBuddy site from price history.

Output (docs/last_sale.json):
{
  "generated_at": "YYYY-MM-DD",
  "items": {
    "<item_id>": {
      "brand": "...",
      "product_name": "...",
      "pack_size": 4,
      "retailers": {
        "kroger": {
          "last_sale_date": "YYYY-MM-DD",
          "sale_price": 6.99,
          "lowest_price": 5.99
        }
      }
    }
  }
}

item_id = normalize(brand)|normalize(product_name)|pack_size
Sale day = pct_off >= 5 (history stores percent points, not fraction).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
DOCS_DIR = ROOT / "docs"
OUT_PATH = DOCS_DIR / "last_sale.json"

_SNACK_BUDDY = ROOT.parent / "snack-buddy"
HISTORY_CANDIDATES = [
    _SNACK_BUDDY / "Data" / "historical_master_prices.csv",
    _SNACK_BUDDY / "data" / "historical_master_prices.csv",
]

SALE_PCT_MIN = 5.0


def make_item_id(brand, product_name, pack_size) -> str:
    b = " ".join(str(brand or "").strip().lower().split())
    p = " ".join(str(product_name or "").strip().lower().split())
    try:
        pack = str(int(float(pack_size)))
    except (TypeError, ValueError):
        pack = "0"
    return f"{b}|{p}|{pack}"


def normalize_retailer(value) -> str:
    return " ".join(str(value or "").strip().lower().split())


def find_history() -> Path:
    for path in HISTORY_CANDIDATES:
        if path.is_file():
            return path
    raise SystemExit(
        "historical_master_prices.csv not found under snack-buddy Data/ or data/"
    )


def main() -> None:
    history_path = find_history()
    print(f"Reading {history_path}")

    usecols = [
        "date",
        "retailer",
        "brand",
        "product_name",
        "pack_size",
        "price",
        "pct_off",
        "status",
    ]
    df = pd.read_csv(history_path, usecols=usecols, low_memory=False)

    status = df["status"].astype(str).str.strip().str.lower()
    df = df[status.eq("ok") | status.eq("nan") | status.eq("") | df["status"].isna()].copy()

    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["pct_off"] = pd.to_numeric(df["pct_off"], errors="coerce")
    df = df[df["price"].notna() & (df["price"] > 0)].copy()

    df["date"] = df["date"].astype(str).str.slice(0, 10)
    df["retailer_n"] = df["retailer"].map(normalize_retailer)
    df["brand_s"] = df["brand"].fillna("").astype(str).str.strip()
    df["product_s"] = df["product_name"].fillna("").astype(str).str.strip()
    df["pack_i"] = pd.to_numeric(df["pack_size"], errors="coerce").fillna(0).astype(int)
    df["item_id"] = [
        make_item_id(b, p, pack)
        for b, p, pack in zip(df["brand_s"], df["product_s"], df["pack_i"], strict=False)
    ]
    df = df[(df["item_id"].str.len() > 2) & df["retailer_n"].ne("")].copy()

    meta = (
        df.sort_values("date")
        .groupby("item_id", sort=False)
        .agg(
            brand=("brand_s", "last"),
            product_name=("product_s", "last"),
            pack_size=("pack_i", "last"),
        )
    )

    lowest = (
        df.groupby(["item_id", "retailer_n"], sort=False)["price"]
        .min()
        .rename("lowest_price")
        .reset_index()
    )

    sales = df[df["pct_off"].fillna(-999) >= SALE_PCT_MIN]
    if len(sales):
        day_sale = (
            sales.groupby(["item_id", "retailer_n", "date"], sort=False)["price"]
            .min()
            .reset_index()
            .rename(columns={"price": "sale_price", "date": "last_sale_date"})
        )
        day_sale = day_sale.sort_values("last_sale_date")
        last_sales = day_sale.groupby(["item_id", "retailer_n"], sort=False).tail(1)
    else:
        last_sales = pd.DataFrame(
            columns=["item_id", "retailer_n", "last_sale_date", "sale_price"]
        )

    retailer_rows = lowest.merge(
        last_sales, on=["item_id", "retailer_n"], how="left"
    )

    items: dict[str, dict] = {}
    for item_id, row in meta.iterrows():
        items[item_id] = {
            "brand": row["brand"],
            "product_name": row["product_name"],
            "pack_size": int(row["pack_size"]),
            "retailers": {},
        }

    for _, row in retailer_rows.iterrows():
        item_id = row["item_id"]
        if item_id not in items:
            continue
        rec: dict = {"lowest_price": round(float(row["lowest_price"]), 2)}
        if pd.notna(row.get("last_sale_date")) and str(row["last_sale_date"]).strip():
            rec["last_sale_date"] = str(row["last_sale_date"])
            if pd.notna(row.get("sale_price")):
                rec["sale_price"] = round(float(row["sale_price"]), 2)
        items[item_id]["retailers"][row["retailer_n"]] = rec

    # Drop items with no retailer data (shouldn't happen)
    items = {k: v for k, v in items.items() if v.get("retailers")}

    payload = {
        "generated_at": date.today().isoformat(),
        "items": items,
    }

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    size_kb = OUT_PATH.stat().st_size / 1024
    print(f"Wrote {OUT_PATH} — {len(items)} items, {size_kb:.1f} KB")


if __name__ == "__main__":
    main()

"""
Build docs/savings.json — yearly savings estimates for My Items.

Uses the last 56 days of snack-buddy Data/historical_master_prices.csv (status ok)
plus Item Database baselines.

For each item_id (brand|product|pack) + retailer:
  - Split into 8 fixed week buckets.
  - Sale week: week's lowest price is ≥5% below baseline.
  - weekly_saving = baseline − week low (sale weeks only).
  - Stock-up model: you buy on sale and stock up until the next sale, so
    yearly_saving = mean(weekly_saving on sale weeks) × 52.
    (Not diluted by non-sale weeks.)
  - avg_pct_off = mean % off across sale weeks (0–1 fraction).

Skipped (not written):
  - fewer than 4 weeks with prices ("too_new")
  - sale in 7+ of 8 observed weeks ("baseline_suspect")
  - Walmart page title "(N pack) …" bundles ("walmart_bundle")
  - Kroger/HT chronic card discount vs baseline ("everyday_card")

Items with 4+ weeks but zero sale weeks are kept with yearly_saving=0,
avg_pct_off=null, sale_weeks=0.
"""

from __future__ import annotations

import json
import re
import statistics
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

try:
    import openpyxl
except ImportError:  # pragma: no cover
    openpyxl = None

ROOT = Path(__file__).resolve().parent
DOCS_DIR = ROOT / "docs"
OUT_PATH = DOCS_DIR / "savings.json"

_SNACK_BUDDY = ROOT.parent / "snack-buddy"
HISTORY_CANDIDATES = [
    _SNACK_BUDDY / "Data" / "historical_master_prices.csv",
    _SNACK_BUDDY / "data" / "historical_master_prices.csv",
]
ITEM_DB_CANDIDATES = [
    _SNACK_BUDDY / "Item Database.xlsx",
]

LOOKBACK_DAYS = 56
WEEK_COUNT = 8  # LOOKBACK_DAYS // 7
SALE_PCT_MIN = 0.05
MIN_WEEKS = 4
EVERYDAY_CARD_MIN_DAYS = 7
EVERYDAY_CARD_SHARE = 0.70

_BUNDLE_TITLE_RE = re.compile(r"^\(\s*(\d+)\s*packs?\s*\)", re.I)


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


def find_item_db() -> Path:
    for path in ITEM_DB_CANDIDATES:
        if path.is_file():
            return path
    raise SystemExit("Item Database.xlsx not found under snack-buddy/")


def is_kroger_family(retailer: str) -> bool:
    r = normalize_retailer(retailer)
    if "kroger" in r:
        return True
    return "harris" in r and "teeter" in r


def load_baselines(path: Path) -> dict[tuple[str, str], float]:
    """Map (item_id, retailer_n) → baseline price."""
    if openpyxl is None:
        raise SystemExit("openpyxl is required to read Item Database.xlsx")

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[wb.sheetnames[0]]
        rows = ws.iter_rows(values_only=True)
        header = [str(c or "").strip() for c in next(rows)]
        idx = {name: i for i, name in enumerate(header)}
        need = ("Retailer", "Brand", "Product_name", "Pack_Size", "Baseline_Price")
        for col in need:
            if col not in idx:
                raise SystemExit(f"Item Database missing column {col!r}")

        out: dict[tuple[str, str], float] = {}
        for row in rows:
            if not row:
                continue
            try:
                baseline = float(row[idx["Baseline_Price"]] or 0)
            except (TypeError, ValueError):
                continue
            if baseline <= 0:
                continue
            retailer = normalize_retailer(row[idx["Retailer"]])
            if not retailer:
                continue
            item_id = make_item_id(
                row[idx["Brand"]],
                row[idx["Product_name"]],
                row[idx["Pack_Size"]],
            )
            if len(item_id) <= 2:
                continue
            out[(item_id, retailer)] = baseline
        return out
    finally:
        wb.close()


def week_index(d: date, window_start: date) -> int | None:
    delta = (d - window_start).days
    if delta < 0 or delta >= LOOKBACK_DAYS:
        return None
    return delta // 7  # 0..7


def main() -> None:
    history_path = find_history()
    item_db_path = find_item_db()
    today = date.today()
    window_start = today - timedelta(days=LOOKBACK_DAYS)
    print(f"[Savings] Reading history {history_path}")
    print(f"[Savings] Window {window_start.isoformat()} → {today.isoformat()} ({WEEK_COUNT} weeks)")
    print(f"[Savings] Baselines from {item_db_path.name}")

    baselines = load_baselines(item_db_path)
    print(f"[Savings] Loaded {len(baselines)} baseline rows")

    usecols = [
        "date",
        "retailer",
        "brand",
        "product_name",
        "pack_size",
        "price",
        "status",
        "page_title",
    ]
    df = pd.read_csv(history_path, usecols=usecols, low_memory=False)

    status = df["status"].astype(str).str.strip().str.lower()
    df = df[status.eq("ok")].copy()

    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df = df[df["price"].notna() & (df["price"] > 0)].copy()

    df["date"] = pd.to_datetime(df["date"].astype(str).str.slice(0, 10), errors="coerce")
    df = df[df["date"].notna()].copy()
    df["day"] = df["date"].dt.date
    df = df[(df["day"] >= window_start) & (df["day"] <= today)].copy()

    df["retailer_n"] = df["retailer"].map(normalize_retailer)
    df["brand_s"] = df["brand"].fillna("").astype(str).str.strip()
    df["product_s"] = df["product_name"].fillna("").astype(str).str.strip()
    df["pack_i"] = pd.to_numeric(df["pack_size"], errors="coerce").fillna(0).astype(int)
    df["item_id"] = [
        make_item_id(b, p, pack)
        for b, p, pack in zip(df["brand_s"], df["product_s"], df["pack_i"], strict=False)
    ]
    df = df[(df["item_id"].str.len() > 2) & df["retailer_n"].ne("")].copy()
    df["week"] = df["day"].map(lambda d: week_index(d, window_start))
    df = df[df["week"].notna()].copy()
    df["week"] = df["week"].astype(int)

    print(f"[Savings] History rows in window: {len(df):,}")

    # Walmart title-bundle keys (item_id, retailer)
    walmart = df[df["retailer_n"].str.contains("walmart", na=False)].copy()
    walmart["page_title"] = walmart["page_title"].fillna("").astype(str)
    bundle_keys: set[tuple[str, str]] = set()
    for (item_id, retailer), grp in walmart.groupby(["item_id", "retailer_n"], sort=False):
        titles = grp["page_title"].astype(str)
        if titles.map(lambda t: bool(_BUNDLE_TITLE_RE.match(t.strip()))).any():
            bundle_keys.add((item_id, retailer))

    # Daily min price per item/retailer/day (for everyday-card share)
    daily = (
        df.groupby(["item_id", "retailer_n", "day"], sort=False)["price"]
        .min()
        .reset_index()
    )
    daily_prices: dict[tuple[str, str], list[float]] = {}
    for row in daily.itertuples(index=False):
        key = (row.item_id, row.retailer_n)
        daily_prices.setdefault(key, []).append(float(row.price))

    # Weekly min price
    weekly = (
        df.groupby(["item_id", "retailer_n", "week"], sort=False)["price"]
        .min()
        .reset_index()
        .rename(columns={"price": "week_low"})
    )

    skip_counts = {
        "too_new": 0,
        "baseline_suspect": 0,
        "walmart_bundle": 0,
        "everyday_card": 0,
        "no_baseline": 0,
    }
    out: dict[str, dict[str, dict]] = {}
    yearly_by_item: list[float] = []

    pairs = weekly.groupby(["item_id", "retailer_n"], sort=False)
    for (item_id, retailer), wdf in pairs:
        if (item_id, retailer) in bundle_keys:
            skip_counts["walmart_bundle"] += 1
            continue

        baseline = baselines.get((item_id, retailer))
        if baseline is None or baseline <= 0:
            skip_counts["no_baseline"] += 1
            continue

        week_lows = {
            int(r.week): float(r.week_low)
            for r in wdf.itertuples(index=False)
            if r.week_low and float(r.week_low) > 0
        }
        weeks = len(week_lows)
        if weeks < MIN_WEEKS:
            skip_counts["too_new"] += 1
            continue

        # Everyday card: kroger family, ≥70% of days at ≥5% below baseline
        if is_kroger_family(retailer):
            prices = daily_prices.get((item_id, retailer), [])
            n_days = len(prices)
            if n_days >= EVERYDAY_CARD_MIN_DAYS:
                ceiling = baseline * (1.0 - SALE_PCT_MIN)
                share = sum(1 for p in prices if p <= ceiling) / n_days
                if share >= EVERYDAY_CARD_SHARE:
                    skip_counts["everyday_card"] += 1
                    continue

        # Sale-week savings only (stock-up: don't dilute with non-sale weeks).
        sale_savings: list[float] = []
        pct_offs: list[float] = []
        sale_weeks = 0
        threshold = baseline * (1.0 - SALE_PCT_MIN)
        for _w, low in sorted(week_lows.items()):
            if low <= threshold + 1e-9:
                saving = max(0.0, baseline - low)
                sale_weeks += 1
                sale_savings.append(saving)
                pct_offs.append(saving / baseline)

        if sale_weeks >= 7:
            skip_counts["baseline_suspect"] += 1
            continue

        # 4+ weeks with no sale weeks still count — $0 / year, no % off yet.
        if sale_savings:
            avg_sale_saving = sum(sale_savings) / len(sale_savings)
            yearly = round(avg_sale_saving * 52, 2)
            avg_pct = round(sum(pct_offs) / len(pct_offs), 4)
        else:
            yearly = 0.0
            avg_pct = None

        out.setdefault(item_id, {})[retailer] = {
            "yearly_saving": yearly,
            "avg_pct_off": avg_pct,
            "sale_weeks": sale_weeks,
            "weeks": weeks,
        }

    # Per-item best yearly for median
    for _item_id, retailers in out.items():
        best = max(float(v["yearly_saving"]) for v in retailers.values())
        yearly_by_item.append(best)

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    payload = out  # flat map per user spec
    OUT_PATH.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")

    n_pairs = sum(len(v) for v in out.values())
    median_yearly = (
        round(statistics.median(yearly_by_item), 2) if yearly_by_item else 0.0
    )
    size_kb = OUT_PATH.stat().st_size / 1024

    print(f"[Savings] Wrote {OUT_PATH} — {len(out)} items, {n_pairs} item×retailer pairs, {size_kb:.1f} KB")
    print("[Savings] Skipped:")
    for reason, n in skip_counts.items():
        print(f"  {reason}: {n}")
    print(f"[Savings] Median yearly_saving (best store per item): ${median_yearly:.2f}")


if __name__ == "__main__":
    main()

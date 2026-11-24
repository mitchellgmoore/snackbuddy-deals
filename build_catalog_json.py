"""
Build docs/catalog.json — ACTIVE Item Database snacks grouped by star item_id.

One row per brand|product|pack with flavors, stores, latest prices, sale flags,
and probation/new status. Used by the Catalog view on the deals page.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

try:
    import openpyxl
except ImportError:  # pragma: no cover
    openpyxl = None

ROOT = Path(__file__).resolve().parent
DOCS_DIR = ROOT / "docs"
OUT_PATH = DOCS_DIR / "catalog.json"

_SNACK_BUDDY = ROOT.parent / "snack-buddy"
HISTORY_CANDIDATES = [
    _SNACK_BUDDY / "Data" / "historical_master_prices.csv",
    _SNACK_BUDDY / "data" / "historical_master_prices.csv",
]
ITEM_DB_CANDIDATES = [
    _SNACK_BUDDY / "Item Database.xlsx",
]
DEALS_CANDIDATES = [
    ROOT / "deals_today.json",
    ROOT / "deals_today.JSON",
    ROOT / "deals_today.csv",
]
SAVINGS_PATH = DOCS_DIR / "savings.json"

PROBATION_MIN_DAYS = 7
GAP_NOTE_RE = re.compile(r"added from.*gap", re.I)


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


def title_display(value: str) -> str:
    s = " ".join(str(value or "").strip().split())
    if not s:
        return ""
    # Keep short acronyms / brand-ish tokens readable.
    parts = []
    for tok in s.split(" "):
        if tok.isupper() and len(tok) <= 4:
            parts.append(tok)
        else:
            parts.append(tok[:1].upper() + tok[1:] if tok else tok)
    return " ".join(parts)


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


def find_deals_path() -> Path | None:
    for path in DEALS_CANDIDATES:
        if path.is_file():
            return path
    return None


def pack_int(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def notes_cleared(notes: str) -> bool:
    return "probation cleared" in (notes or "").lower()


def notes_gap_like(notes: str) -> bool:
    return bool(GAP_NOTE_RE.search(notes or ""))


def load_active_rows(path: Path) -> list[dict]:
    if openpyxl is None:
        raise SystemExit("openpyxl is required to read Item Database.xlsx")

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[wb.sheetnames[0]]
        rows = ws.iter_rows(values_only=True)
        header = [str(c or "").strip() for c in next(rows)]
        idx = {name: i for i, name in enumerate(header)}
        need = ("Retailer", "Brand", "Product_name", "Pack_Size", "Status")
        for col in need:
            if col not in idx:
                raise SystemExit(f"Item Database missing column {col!r}")

        out: list[dict] = []
        for row in rows:
            if not row:
                continue
            status = str(row[idx["Status"]] or "").strip().upper()
            if status != "ACTIVE":
                continue
            brand = str(row[idx["Brand"]] or "").strip()
            product = str(row[idx["Product_name"]] or "").strip()
            if not brand and not product:
                continue
            pack = pack_int(row[idx["Pack_Size"]])
            item_id = make_item_id(brand, product, pack)
            if item_id == "||0" or item_id.startswith("|") or item_id.endswith("|0") and not product:
                continue
            flavor = ""
            if "Description/Flavor" in idx:
                flavor = str(row[idx["Description/Flavor"]] or "").strip()
            category = ""
            if "Category" in idx:
                category = str(row[idx["Category"]] or "").strip().lower()
            image = ""
            if "image_url" in idx:
                image = str(row[idx["image_url"]] or "").strip()
            notes = str(row[idx["Notes"]] or "").strip() if "Notes" in idx else ""
            retailer = normalize_retailer(row[idx["Retailer"]])
            if not retailer:
                continue
            out.append(
                {
                    "item_id": item_id,
                    "brand": brand,
                    "product_name": product,
                    "pack_size": pack,
                    "category": category,
                    "flavor": flavor,
                    "image_url": image,
                    "notes": notes,
                    "retailer": retailer,
                }
            )
        return out
    finally:
        wb.close()


def load_history_maps(path: Path) -> tuple[dict[tuple[str, str], float], dict[tuple[str, str], set[date]], date | None]:
    """
    Returns:
      latest_price[(item_id, retailer)] — prefer today's date, else last seen
      observed_dates[(item_id, retailer)] — distinct ok dates
      as_of — max date in file
    """
    usecols = [
        "date",
        "retailer",
        "brand",
        "product_name",
        "pack_size",
        "status",
        "price",
    ]
    df = pd.read_csv(path, usecols=usecols, dtype=str, low_memory=False)
    df["status"] = df["status"].fillna("").str.strip().str.lower()
    df = df[df["status"] == "ok"].copy()
    if df.empty:
        return {}, {}, None

    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df = df[df["price"].notna() & (df["price"] > 0)]
    if df.empty:
        return {}, {}, None

    df["retailer"] = df["retailer"].map(normalize_retailer)
    df["item_id"] = [
        make_item_id(b, p, s)
        for b, p, s in zip(df["brand"], df["product_name"], df["pack_size"])
    ]
    as_of = df["date"].max().date()

    # Last-seen price: sort by date, keep last per key.
    df_sorted = df.sort_values("date")
    last_rows = df_sorted.groupby(["item_id", "retailer"], sort=False).tail(1)
    latest_price: dict[tuple[str, str], float] = {}
    for _, r in last_rows.iterrows():
        latest_price[(str(r["item_id"]), str(r["retailer"]))] = round(float(r["price"]), 2)

    # Prefer same-day (as_of) prices when present.
    today_rows = df[df["date"].dt.date == as_of]
    if not today_rows.empty:
        today_last = today_rows.sort_values("date").groupby(["item_id", "retailer"], sort=False).tail(1)
        for _, r in today_last.iterrows():
            latest_price[(str(r["item_id"]), str(r["retailer"]))] = round(float(r["price"]), 2)

    observed: dict[tuple[str, str], set[date]] = defaultdict(set)
    for item_id, retailer, d in zip(
        df["item_id"], df["retailer"], df["date"].dt.date
    ):
        observed[(str(item_id), str(retailer))].add(d)

    return latest_price, observed, as_of


def load_on_sale_maps(
    path: Path | None,
) -> tuple[dict[str, set[str]], dict[str, dict[str, set[str]]]]:
    """
    Returns:
      on_sale_stores[item_id] -> retailers
      on_sale_flavors[item_id][flavor] -> retailers
    """
    stores_out: dict[str, set[str]] = defaultdict(set)
    flavors_out: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    if path is None:
        return stores_out, flavors_out

    if path.suffix.lower() == ".csv":
        df = pd.read_csv(path, dtype=str, low_memory=False)
        records = df.to_dict(orient="records")
    else:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            records = raw.get("deals") or raw.get("items") or raw.get("data") or []
        else:
            records = raw
        if not isinstance(records, list):
            records = []

    for deal in records:
        if not isinstance(deal, dict):
            continue
        brand = deal.get("brand") or ""
        product = deal.get("product_name") or deal.get("product") or ""
        pack = deal.get("pack_count")
        if pack is None:
            pack = deal.get("pack_size")
        retailer = normalize_retailer(deal.get("retailer") or deal.get("store") or "")
        item_id = make_item_id(brand, product, pack)
        if not retailer or not item_id:
            continue
        stores_out[item_id].add(retailer)
        flavor = " ".join(str(deal.get("flavor") or "").split())
        if flavor:
            flavors_out[item_id][flavor].add(retailer)
    return stores_out, flavors_out


def load_often_sale_ids(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return set()
    often: set[str] = set()
    for item_id, retailers in raw.items():
        if not isinstance(retailers, dict):
            continue
        for rec in retailers.values():
            if not isinstance(rec, dict):
                continue
            try:
                weeks = int(rec.get("sale_weeks_8wk") or 0)
            except (TypeError, ValueError):
                weeks = 0
            if weeks >= 3:
                often.add(str(item_id))
                break
    return often


def row_in_probation(notes: str, observed: set[date], today: date) -> bool:
    if notes_cleared(notes):
        return False
    dates = sorted(observed)
    n_days = len(dates)
    first = dates[0] if dates else None
    first_age = (today - first).days if first is not None else None
    gap_like = notes_gap_like(notes)
    recently_started = first is None or (
        first_age is not None and first_age < PROBATION_MIN_DAYS
    )
    if not gap_like and not recently_started:
        return False
    if n_days >= PROBATION_MIN_DAYS:
        return False
    return True


def build_catalog() -> dict:
    db_path = find_item_db()
    hist_path = find_history()
    deals_path = find_deals_path()

    print(f"[Catalog] Item DB {db_path}")
    active = load_active_rows(db_path)
    print(f"[Catalog] ACTIVE rows: {len(active)}")

    print(f"[Catalog] History {hist_path}")
    latest_price, observed_dates, as_of = load_history_maps(hist_path)
    today = as_of or date.today()
    print(f"[Catalog] History as_of={today.isoformat()} price_keys={len(latest_price)}")

    on_sale, on_sale_flavors = load_on_sale_maps(deals_path)
    print(f"[Catalog] Deals source: {deals_path.name if deals_path else 'none'} — {len(on_sale)} on-sale item_ids")

    often_ids = load_often_sale_ids(SAVINGS_PATH)
    print(f"[Catalog] Goes-on-sale-often from savings.json: {len(often_ids)}")

    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in active:
        grouped[row["item_id"]].append(row)

    items: list[dict] = []
    for item_id, rows in grouped.items():
        brand_raw = rows[0]["brand"]
        product_raw = rows[0]["product_name"]
        pack_size = rows[0]["pack_size"]
        # Prefer most common non-empty category
        cats = [r["category"] for r in rows if r["category"]]
        category = max(set(cats), key=cats.count) if cats else ""

        flavors: list[str] = []
        seen_flav: set[str] = set()
        for r in rows:
            f = " ".join(str(r["flavor"] or "").split())
            if not f:
                continue
            key = f.lower()
            if key in seen_flav:
                continue
            seen_flav.add(key)
            flavors.append(f)
        flavors.sort(key=lambda s: s.lower())

        image_url = ""
        for r in rows:
            if r["image_url"]:
                image_url = r["image_url"]
                break

        stores = sorted({r["retailer"] for r in rows if r["retailer"]})
        prices: dict[str, float] = {}
        for store in stores:
            p = latest_price.get((item_id, store))
            if p is not None:
                prices[store] = p

        store_set = set(stores)
        sale_stores = sorted(s for s in (on_sale.get(item_id) or []) if s in store_set)
        flavor_sale: dict[str, list[str]] = {}
        for flav, rets in (on_sale_flavors.get(item_id) or {}).items():
            kept = sorted(s for s in rets if s in store_set)
            if kept:
                flavor_sale[flav] = kept

        # Probation per store-row: store is in probation only if ALL its rows are
        store_prob: dict[str, bool] = {}
        for r in rows:
            store = r["retailer"]
            obs = observed_dates.get((item_id, store), set())
            in_p = row_in_probation(r["notes"], obs, today)
            if store not in store_prob:
                store_prob[store] = in_p
            else:
                store_prob[store] = store_prob[store] and in_p
        is_new = bool(store_prob) and all(store_prob.values())

        items.append(
            {
                "item_id": item_id,
                "brand": title_display(brand_raw),
                "product_name": title_display(product_raw),
                "pack_size": pack_size,
                "category": category,
                "image_url": image_url,
                "flavors": flavors,
                "stores": stores,
                "prices": prices,
                "on_sale_now": bool(sale_stores),
                "on_sale_stores": sale_stores,
                "on_sale_flavors": flavor_sale,
                "goes_on_sale_often": item_id in often_ids,
                "is_new": is_new,
            }
        )

    items.sort(key=lambda it: (it["brand"].lower(), it["product_name"].lower(), it["pack_size"]))

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of": today.isoformat(),
        "count": len(items),
        "items": items,
    }
    return payload


def main() -> None:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    payload = build_catalog()
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    items = payload["items"]
    n_sale = sum(1 for it in items if it["on_sale_now"])
    n_often = sum(1 for it in items if it["goes_on_sale_often"])
    n_new = sum(1 for it in items if it["is_new"])
    size_kb = OUT_PATH.stat().st_size / 1024
    print(
        f"[Catalog] Wrote {OUT_PATH} — {payload['count']} items, "
        f"on_sale={n_sale}, often={n_often}, new={n_new}, {size_kb:.1f} KB"
    )
    for it in items[:3]:
        print(
            f"  e.g. {it['brand']} {it['product_name']} · pack {it['pack_size']} · "
            f"{len(it['flavors'])} flavors · stores={it['stores']} · "
            f"sale={it['on_sale_now']} often={it['goes_on_sale_often']} new={it['is_new']}"
        )


if __name__ == "__main__":
    main()
